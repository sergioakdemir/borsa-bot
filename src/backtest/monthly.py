"""BIST-5 aylik backtest (2024-2026) -> data/backtest_results.json.

Her ayin ilk islem gununu secer; O TARIHE KADARKI fiyat verisiyle sistemin GERCEK
karar zincirini (on-sinyal -> deterministik puan -> karar esigi -> risk veto)
calistirip AL/TUT/SAT uretir; ~1 ay (21 islem gunu) sonraki gercek fiyatla
karsilastirir. Basari orani + ortalama getiri hesaplar.

AI (Claude) cagrilmaz: gecmise donuk haber/KAP olmadigindan yalniz fiyat-turevli
sinyal kullanilir (canli sistemle ayni deterministik skorlayici).
"""
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.factory import get_data_source
from src.ai.presignal import build_presignal
from src.ai.decision import decision_from_score
from src.ai.risk import assess_risk
from src.backtest.scorer import deterministic_score

_TZ = ZoneInfo("Europe/Istanbul")
TICKERS = ["THYAO.IS", "GARAN.IS", "ASELS.IS", "KCHOL.IS", "TUPRS.IS"]
START = "2023-09-01"          # MA/pencere icin 2024 oncesi tampon
BASLANGIC_YIL = 2024
WINDOW = 10                   # on-sinyal penceresi (canli sistemle ayni)
HORIZON = 21                  # ileri ufuk (~1 ay islem gunu)
BAND = 5.0                    # TUT icin yatay band (+/- %)
AL_K = ("AL", "AL_TEMKINLI")
SAT_K = ("SAT", "GUCLU_SAT", "AZALT", "UZAK_DUR")
OUT = Path(__file__).resolve().parents[2] / "data" / "backtest_results.json"


def _bucket(d):
    if d in AL_K:
        return "AL"
    if d in SAT_K:
        return "SAT"
    return "TUT"            # TUT + VETO (islemden kacin) -> yatay beklenti


def _dogru(bucket, fwd):
    if bucket == "AL":
        return fwd > 0
    if bucket == "SAT":
        return fwd < 0
    return abs(fwd) <= BAND


def _strateji(bucket, fwd):
    return fwd if bucket == "AL" else (-fwd if bucket == "SAT" else 0.0)


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


def _ozet(trades):
    n = len(trades)
    if not n:
        return {"islem": 0}
    yonlu = [t for t in trades if t["bucket"] in ("AL", "SAT")]
    dogru = sum(1 for t in trades if t["dogru"])
    yonlu_dogru = sum(1 for t in yonlu if t["dogru"])
    return {
        "islem": n,
        "basari_orani_%": round(dogru / n * 100, 1),
        "yonlu_islem": len(yonlu),
        "yonlu_basari_%": round(yonlu_dogru / len(yonlu) * 100, 1) if yonlu else None,
        "ort_strateji_getiri_%": round(sum(t["strateji_getiri_%"] for t in trades) / n, 2),
        "ort_ileri_getiri_%": round(sum(t["getiri_%"] for t in trades) / n, 2),
        "toplam_strateji_getiri_%": round(sum(t["strateji_getiri_%"] for t in trades), 2),
        "karar_dagilimi": dict(Counter(t["bucket"] for t in trades)),
    }


def run(verbose=True):
    src = get_data_source()
    per_ticker, tum = {}, []
    for sym in TICKERS:
        try:
            df = src.get_history(sym, start=START)
            df = df[df["Volume"] > 0]
            bars = [{"date": ix.date().isoformat(),
                     "open": float(r["Open"]), "high": float(r["High"]),
                     "low": float(r["Low"]), "close": float(r["Close"]),
                     "volume": int(r["Volume"]) if r["Volume"] == r["Volume"] else 0}
                    for ix, r in df.iterrows()]
            closes = [b["close"] for b in bars]
            dates = [df.index[i].date() for i in range(len(bars))]
        except Exception as e:
            per_ticker[sym] = {"islem": 0, "hata": type(e).__name__}
            if verbose:
                print(f"  {sym}: VERI HATASI {type(e).__name__}")
            continue

        trades = []
        for t in _aylik_indeksler(dates):
            if t < WINDOW - 1 or t + HORIZON >= len(closes):
                continue
            win = bars[t - WINDOW + 1:t + 1]
            stock = {"symbol": sym, "freshness": {"status": "RECENT"}, "bars": win}
            ps = build_presignal(stock)
            score = deterministic_score(ps)
            dcode, _ = decision_from_score(score)
            decision = "VETO" if assess_risk(stock).veto else dcode
            bucket = _bucket(decision)
            fwd = (closes[t + HORIZON] - closes[t]) / closes[t] * 100 if closes[t] else 0.0
            trades.append({
                "tarih": dates[t].isoformat(), "karar": decision, "bucket": bucket,
                "puan": score, "fiyat": round(closes[t], 2),
                "ileri_fiyat": round(closes[t + HORIZON], 2),
                "getiri_%": round(fwd, 2), "dogru": _dogru(bucket, fwd),
                "strateji_getiri_%": round(_strateji(bucket, fwd), 2),
            })
        per_ticker[sym] = {**_ozet(trades), "islemler": trades}
        tum.extend(trades)
        if verbose and trades:
            o = _ozet(trades)
            print(f"  {sym:9} {o['islem']:2} islem · basari %{o['basari_orani_%']:.0f} "
                  f"· yonlu %{o['yonlu_basari_%']} · ort strateji %{o['ort_strateji_getiri_%']:+.2f}")

    sonuc = {
        "uretim_tarihi": datetime.now(_TZ).isoformat(timespec="seconds"),
        "aciklama": "BIST-5 aylik backtest (2024-2026); sistemin deterministik fiyat "
                    "karar zinciri, ~1 ay ileri ufuk. Canli Claude kullanilmadi.",
        "parametreler": {"tickers": TICKERS, "baslangic_yil": BASLANGIC_YIL,
                         "pencere": WINDOW, "ileri_gun": HORIZON, "tut_bandi_%": BAND},
        "genel": _ozet(tum),
        "hisse_bazli": {s: {k: v for k, v in d.items() if k != "islemler"}
                        for s, d in per_ticker.items()},
        "detay": per_ticker,
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(sonuc, ensure_ascii=False, indent=2), encoding="utf-8")
    if verbose:
        g = sonuc["genel"]
        print(f"\nGENEL: {g.get('islem')} islem · basari %{g.get('basari_orani_%')} "
              f"· yonlu %{g.get('yonlu_basari_%')} · ort strateji getiri %{g.get('ort_strateji_getiri_%')}")
        print(f"Kaydedildi: {OUT}")
    return sonuc


if __name__ == "__main__":
    run()
