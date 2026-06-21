"""BIST-5 AI (Claude) backtest - POINT-IN-TIME gecerli alt kume.

Her ay 1 tarih (2024-2026) icin, O TARIHE KADARKI veriyle:
  - yfinance fiyat/hacim/MA10/MA50/52h yuksek-dusuk (tarihe gore yeniden kuruldu)
  - hacim anomalisi (5 gun ort.)
  - sektor korelasyonu (statik)
  - Claude (commentary.py tam SYSTEM promptu) -> AL/TUT/SAT + risk veto
KAP/analist/bilanco/makro/jeopolitik DAHIL EDILMEDI: bu kaynaklar gecmise donuk
(as-of-date) veri veremiyor; dahil edilseydi 2026 verisi 2024 kararina sizar
(look-ahead). Sonuc ~1 ay (21 islem gunu) ileri gercek fiyatla karsilastirilir.

Sonuc -> data/backtest_results.json (Karne sekmesi bunu gosterir).
"""
import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.ai.commentary import (MODEL, _ai_verdict, _trend, _volume_signal,
                               _load_dotenv)
from src.news.fundamental_source import get_sector_correlation

_TZ = ZoneInfo("Europe/Istanbul")
TICKERS = ["THYAO", "GARAN", "ASELS", "KCHOL", "TUPRS"]
START = "2022-09-01"          # 52h pencere icin 2024 oncesi tampon
BASLANGIC_YIL = 2024
HORIZON = 21                  # ~1 ay islem gunu ileri
BAND = 5.0
OUT = Path(__file__).resolve().parents[2] / "data" / "backtest_results.json"


def _piyasa_asof(closes, highs, lows, vols, i):
    """market_data ile AYNI alanlar, ama yalniz [:i+1] verisiyle (point-in-time)."""
    c = closes[:i + 1]
    h = highs[:i + 1]
    lo = lows[:i + 1]
    v = vols[:i + 1]
    last, prev = c[-1], c[-2]
    gunluk = round((last - prev) / prev * 100, 2) if prev else None

    def ma(n):
        seg = c[-n:]
        return round(sum(seg) / len(seg), 2) if seg else None

    hwin, lwin = h[-252:], lo[-252:]
    y52, d52 = round(max(hwin), 2), round(min(lwin), 2)
    ref = c[-22] if len(c) >= 22 else c[0]
    donem = round((last - ref) / ref * 100, 2) if ref else None
    vwin = v[-20:]
    avg_vol = sum(vwin) / len(vwin) if vwin else 0
    hacim_vs = round((v[-1] / avg_vol - 1) * 100, 2) if avg_vol else None
    rets = [(c[k] - c[k - 1]) / c[k - 1] * 100
            for k in range(max(1, len(c) - 20), len(c)) if c[k - 1]]
    vol_std = round(statistics.pstdev(rets), 2) if len(rets) >= 2 else 0.0
    rng = y52 - d52
    konum = round((last - d52) / rng * 100, 1) if rng > 0 else None
    return {
        "sembol": None, "son_kapanis": round(last, 2),
        "onceki_kapanis": round(prev, 2), "gunluk_degisim_%": gunluk,
        "donem_degisim_%": donem, "ma10": ma(10), "ma50": ma(50),
        "hafta52_yuksek": y52, "hafta52_dusuk": d52, "fiyat_konumu_%": konum,
        "son_hacim": int(v[-1]), "ortalama_hacim": int(avg_vol),
        "hacim_vs_ort_%": hacim_vs, "hacim_sinyali": _volume_signal(hacim_vs),
        "volatilite_%": vol_std, "trend": _trend(donem), "bar_sayisi": len(c),
    }


def _hacim_anom_asof(vols, i):
    v = vols[:i + 1]
    if len(v) < 6:
        return None
    bugun, onceki5 = v[-1], v[-6:-1]
    ort = sum(onceki5) / len(onceki5)
    kat = round(bugun / ort, 2) if ort else None
    sev = "COK YUKSEK" if kat and kat >= 3 else "YUKSEK" if kat and kat >= 2 else "NORMAL"
    return {"bugun_hacim": int(bugun), "ort_5g_hacim": int(ort), "kat": kat, "seviye": sev}


def _aylik_indeksler(dates):
    out, seen = [], set()
    for i, d in enumerate(dates):
        if d.year < BASLANGIC_YIL:
            continue
        key = (d.year, d.month)
        if key not in seen:
            seen.add(key)
            out.append(i)
    return out


def _bucket(d):
    return "AL" if d == "AL" else "SAT" if d == "SAT" else "TUT"


def _dogru(b, fwd):
    return fwd > 0 if b == "AL" else (fwd < 0 if b == "SAT" else abs(fwd) <= BAND)


def _strateji(b, fwd):
    return fwd if b == "AL" else (-fwd if b == "SAT" else 0.0)


def _ozet(trades):
    n = len(trades)
    if not n:
        return {"islem": 0}
    yonlu = [t for t in trades if t["bucket"] in ("AL", "SAT")]
    return {
        "islem": n,
        "basari_orani_%": round(sum(1 for t in trades if t["dogru"]) / n * 100, 1),
        "yonlu_islem": len(yonlu),
        "yonlu_basari_%": round(sum(1 for t in yonlu if t["dogru"]) / len(yonlu) * 100, 1) if yonlu else None,
        "ort_strateji_getiri_%": round(sum(t["strateji_getiri_%"] for t in trades) / n, 2),
        "ort_ileri_getiri_%": round(sum(t["getiri_%"] for t in trades) / n, 2),
        "toplam_strateji_getiri_%": round(sum(t["strateji_getiri_%"] for t in trades), 2),
        "karar_dagilimi": dict(Counter(t["bucket"] for t in trades)),
    }


def run(verbose=True):
    _load_dotenv()
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY yok")
    import anthropic
    import yfinance as yf
    import pandas as pd
    client = anthropic.Anthropic()

    global TICKER
    per_ticker, tum = {}, []
    cagri = 0
    for tk in TICKERS:
        TICKER = tk
        df = yf.Ticker(f"{tk}.IS").history(start=START)
        df = df[df["Volume"] > 0]
        closes = [float(x) for x in df["Close"].tolist()]
        highs = [float(x) for x in df["High"].tolist()]
        lows = [float(x) for x in df["Low"].tolist()]
        vols = [float(x) for x in df["Volume"].tolist()]
        dates = [pd.Timestamp(ix).date() for ix in df.index]
        sektor = get_sector_correlation(tk)

        trades = []
        for t in _aylik_indeksler(dates):
            if t < 52 or t + HORIZON >= len(closes):
                continue
            piyasa = _piyasa_asof(closes, highs, lows, vols, t)
            piyasa["sembol"] = f"{tk}.IS"
            payload = {
                "ticker": tk, "piyasa": piyasa,
                "hacim_anomalisi": _hacim_anom_asof(vols, t),
                "_not": ("Backtest point-in-time: yalniz fiyat/hacim/sektor verisi "
                         "mevcut; haber/analist/bilanco/makro bu tarih icin yok."),
            }
            if sektor.get("available"):
                payload["sektor_korelasyonu"] = {"ozet": sektor.get("ozet"),
                                                 "korelasyonlar": sektor.get("korelasyonlar")}
            try:
                v = _ai_verdict(tk, payload, client=client)
                cagri += 1
            except Exception as e:
                if verbose:
                    print(f"    {tk} {dates[t]}: AI HATA {type(e).__name__}")
                continue
            decision = "VETO" if (v.karar == "AL" and v.risk >= 9) else v.karar
            b = _bucket(decision)
            fwd = (closes[t + HORIZON] - closes[t]) / closes[t] * 100 if closes[t] else 0.0
            trades.append({
                "tarih": dates[t].isoformat(), "karar": decision, "bucket": b,
                "puan": v.puan, "risk": v.risk, "fiyat": round(closes[t], 2),
                "ileri_fiyat": round(closes[t + HORIZON], 2),
                "getiri_%": round(fwd, 2), "dogru": _dogru(b, fwd),
                "strateji_getiri_%": round(_strateji(b, fwd), 2),
                "neden": (v.neden_simdi or "")[:160],
            })
        per_ticker[tk] = {**_ozet(trades), "islemler": trades}
        tum.extend(trades)
        if verbose:
            o = _ozet(trades)
            print(f"  {tk:6} {o.get('islem')} islem · basari %{o.get('basari_orani_%')} "
                  f"· yonlu %{o.get('yonlu_basari_%')} · ort strateji %{o.get('ort_strateji_getiri_%')}")

    sonuc = {
        "uretim_tarihi": datetime.now(_TZ).isoformat(timespec="seconds"),
        "yontem": "AI (Claude) - point-in-time fiyat+hacim+sektor",
        "model": MODEL,
        "aciklama": ("BIST-5 aylik AI backtest (2024-2026). Her tarih icin O ANA "
                     "kadarki fiyat/hacim/sektor verisi + Claude tam sistem promptu. "
                     "KAP/analist/bilanco/makro look-ahead'i onlemek icin haric. "
                     "~1 ay ileri ufuk."),
        "parametreler": {"tickers": TICKERS, "ileri_gun": HORIZON, "tut_bandi_%": BAND},
        "toplam_ai_cagrisi": cagri,
        "genel": _ozet(tum),
        "hisse_bazli": {tk: {k: val for k, val in d.items() if k != "islemler"}
                        for tk, d in per_ticker.items()},
        "detay": per_ticker,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(sonuc, ensure_ascii=False, indent=2), encoding="utf-8")
    if verbose:
        g = sonuc["genel"]
        print(f"\nGENEL: {g.get('islem')} islem · basari %{g.get('basari_orani_%')} "
              f"· yonlu %{g.get('yonlu_basari_%')} · ort strateji %{g.get('ort_strateji_getiri_%')} "
              f"· {cagri} AI cagrisi")
        print(f"Kaydedildi: {OUT}")
    return sonuc


TICKER = ""

if __name__ == "__main__":
    run()
