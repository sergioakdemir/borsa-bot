"""Agresif strateji backtesti - PORTFOY SIMULASYONU (point-in-time).

SADECE BACKTEST. Canli sistemin esikleri/parametreleri degismez.

Strateji:
  - Baslangic sermayesi 500.000 TL, her pozisyon 50.000 TL, max 10 acik pozisyon
  - AL esigi: puan >= 6 (canli 8); risk veto esigi: risk >= 10 (canli 9)
  - Stop-loss: -%8 (alis altinda) -> tam kapat
  - Take-profit: +%15'te pozisyonun YARISINI kapat (bir kez)
  - Max bekleme 30 gun -> zorla kapat
  - Karar tarihleri: 2024-2026 her ayin ilk islem gunu (aylik)

Veri (look-ahead'siz, dürüst): yfinance fiyat/hacim (as-of-date) + statik sektor
korelasyonu + statik sektor notu + statik tarihsel senaryo (ozet) + Claude tam
SYSTEM promptu. KAP/analist/bilanco/makro HARIC (gecmise donuk veri yok).

Cikti: data/backtest_aggressive.json
Calistir: python -m src.backtest.aggressive
"""
import json
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.ai.commentary import (MODEL, SEKTOR_NOTLARI, _ai_verdict, _trend,
                               _volume_signal, _load_dotenv)
from src.ai.scenarios import get_scenario_context
from src.news.fundamental_source import get_sector_correlation
from src.backtest.full_chain import _piyasa_asof, _hacim_anom_asof

_TZ = ZoneInfo("Europe/Istanbul")

TICKERS = ["THYAO", "GARAN", "AKBNK", "ASELS", "KCHOL",
           "TUPRS", "EREGL", "TCELL", "EKGYO", "SISE"]
START = "2022-09-01"          # 52h pencere icin 2024 oncesi tampon
BASLANGIC_YIL = 2024

SERMAYE = 500_000.0
POZ_TL = 50_000.0
MAX_OPEN = 10
AL_PUAN = 6                   # backtest esigi (canli 8)
VETO_RISK = 10               # backtest esigi (canli 9)
STOP = -0.08                 # -%8
TAKE = 0.15                  # +%15
MAXHOLD_GUN = 30
WORKERS = 6                  # paralel AI cagrisi


# ---------------------------------------------------------------------------
# Veri yukleme
# ---------------------------------------------------------------------------
def _load_series():
    import yfinance as yf
    import pandas as pd
    seri = {}
    for tk in TICKERS:
        df = yf.Ticker(f"{tk}.IS").history(start=START)
        df = df[df["Volume"] > 0]
        if df.empty:
            continue
        dates = [pd.Timestamp(ix).date() for ix in df.index]
        seri[tk] = {
            "dates": dates,
            "closes": [float(x) for x in df["Close"].tolist()],
            "highs": [float(x) for x in df["High"].tolist()],
            "lows": [float(x) for x in df["Low"].tolist()],
            "vols": [float(x) for x in df["Volume"].tolist()],
            "by_date": {d: float(c) for d, c in zip(dates, df["Close"].tolist())},
        }
    return seri


def _master_dates(seri):
    s = set()
    for d in seri.values():
        s.update(d["dates"])
    return sorted(s)


def _decision_dates(master):
    """Her (yil, ay) icin 2024'ten itibaren ilk islem gunu."""
    out, seen = [], set()
    for d in master:
        if d.year < BASLANGIC_YIL:
            continue
        key = (d.year, d.month)
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def _asof_index(dates, hedef):
    """dates icinde hedef'e esit/oncesi en buyuk indeks (yoksa None)."""
    lo, hi, res = 0, len(dates) - 1, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if dates[mid] <= hedef:
            res = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return res


def _price_on(seri_tk, d):
    """d tarihinde (veya oncesi en yakin) kapanis."""
    i = _asof_index(seri_tk["dates"], d)
    return seri_tk["closes"][i] if i is not None else None


# ---------------------------------------------------------------------------
# AI karari (point-in-time) - paralel
# ---------------------------------------------------------------------------
def _payload_asof(tk, seri_tk, i, sektor):
    piyasa = _piyasa_asof(seri_tk["closes"], seri_tk["highs"], seri_tk["lows"],
                          seri_tk["vols"], i)
    piyasa["sembol"] = f"{tk}.IS"
    payload = {
        "ticker": tk, "piyasa": piyasa,
        "hacim_anomalisi": _hacim_anom_asof(seri_tk["vols"], i),
        "_not": ("Backtest point-in-time: yalniz fiyat/hacim/sektor/tarihsel-senaryo "
                 "verisi mevcut; haber/analist/bilanco/makro bu tarih icin yok."),
    }
    if sektor.get("available"):
        payload["sektor_korelasyonu"] = {"ozet": sektor.get("ozet"),
                                         "korelasyonlar": sektor.get("korelasyonlar")}
    sn = SEKTOR_NOTLARI.get(tk)
    if sn:
        payload["sektor_notu"] = sn
    sen = get_scenario_context(tk)            # statik ozet (makro yok -> look-ahead yok)
    if sen.get("available"):
        payload["tarihsel_senaryo"] = sen.get("metin")
    return payload


def _verdict_one(args):
    tk, diso, payload = args
    import anthropic
    client = anthropic.Anthropic()
    for deneme in range(2):
        try:
            v = _ai_verdict(tk, payload, client=client)
            decision = "VETO" if (v.karar == "AL" and v.risk >= VETO_RISK) else v.karar
            return (tk, diso, {"karar": decision, "puan": v.puan, "risk": v.risk,
                               "neden": (v.neden_simdi or "")[:140]})
        except Exception:
            if deneme == 1:
                return (tk, diso, None)
    return (tk, diso, None)


def _gather_verdicts(seri, dec_dates, verbose=True):
    sektorler = {tk: get_sector_correlation(tk) for tk in TICKERS}
    isler = []
    for tk in TICKERS:
        st = seri.get(tk)
        if not st:
            continue
        for dd in dec_dates:
            i = _asof_index(st["dates"], dd)
            if i is None or i < 52:
                continue
            payload = _payload_asof(tk, st, i, sektorler[tk])
            isler.append((tk, dd.isoformat(), payload))
    if verbose:
        print(f"  AI cagrisi: {len(isler)} ({len(TICKERS)} hisse x ~{len(dec_dates)} ay), "
              f"{WORKERS} paralel...")
    verdicts = {}
    tamam = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(_verdict_one, job) for job in isler]
        for f in as_completed(futs):
            tk, diso, v = f.result()
            if v:
                verdicts[(tk, diso)] = v
            tamam += 1
            if verbose and tamam % 30 == 0:
                print(f"    {tamam}/{len(isler)} tamamlandi")
    if verbose:
        print(f"  {len(verdicts)}/{len(isler)} karar alindi.")
    return verdicts


# ---------------------------------------------------------------------------
# Portfoy simulasyonu
# ---------------------------------------------------------------------------
def _simulate(seri, dec_dates, verdicts, verbose=True):
    master = _master_dates(seri)
    ilk = dec_dates[0]
    sim_dates = [d for d in master if d >= ilk]
    dset = {d.isoformat() for d in dec_dates}

    cash = SERMAYE
    positions = {}          # tk -> {entry_price, entry_date, shares, half_sold, cost, proceeds}
    closed = []
    sl_count = tp_count = 0
    peak = SERMAYE
    maxdd = 0.0
    deger_serisi = []

    def kapat(tk, d, p, reason):
        nonlocal cash
        pos = positions.pop(tk)
        cash += pos["shares"] * p
        pos["proceeds"] += pos["shares"] * p
        pnl = pos["proceeds"] - pos["cost"]
        closed.append({
            "ticker": tk, "giris_tarih": pos["entry_date"].isoformat(),
            "cikis_tarih": d.isoformat(), "giris_fiyat": round(pos["entry_price"], 2),
            "cikis_fiyat": round(p, 2), "neden": reason,
            "sure_gun": (d - pos["entry_date"]).days,
            "kz_tl": round(pnl, 2),
            "kz_yuzde": round(pnl / pos["cost"] * 100, 2),
            "kazanc": pnl > 0,
        })

    for d in sim_dates:
        diso = d.isoformat()
        # 1) Karar gunu: once SAT (slot/nakit ac), sonra AL
        if diso in dset:
            for tk in TICKERS:
                v = verdicts.get((tk, diso))
                if v and v["karar"] == "SAT" and tk in positions:
                    p = _price_on(seri[tk], d)
                    if p is not None:
                        kapat(tk, d, p, "SAT")
            # AL adaylari: puan yuksekten dusuge
            adaylar = sorted(
                [(tk, verdicts[(tk, diso)]) for tk in TICKERS
                 if (tk, diso) in verdicts and verdicts[(tk, diso)]["karar"] == "AL"
                 and verdicts[(tk, diso)]["puan"] >= AL_PUAN],
                key=lambda x: x[1]["puan"], reverse=True)
            for tk, v in adaylar:
                if tk in positions or len(positions) >= MAX_OPEN or cash < POZ_TL:
                    continue
                p = _price_on(seri[tk], d)
                if not p:
                    continue
                shares = POZ_TL / p
                cash -= POZ_TL
                positions[tk] = {"entry_price": p, "entry_date": d, "shares": shares,
                                 "half_sold": False, "cost": POZ_TL, "proceeds": 0.0}

        # 2) Cikis kontrolu (her gun): stop-loss / take-profit / max bekleme
        for tk in list(positions.keys()):
            pos = positions[tk]
            p = _price_on(seri[tk], d)
            if p is None:
                continue
            if p <= pos["entry_price"] * (1 + STOP):
                kapat(tk, d, p, "STOP-LOSS")
                sl_count += 1
                continue
            if not pos["half_sold"] and p >= pos["entry_price"] * (1 + TAKE):
                half = pos["shares"] / 2
                cash += half * p
                pos["proceeds"] += half * p
                pos["shares"] -= half
                pos["half_sold"] = True
                tp_count += 1
            if (d - pos["entry_date"]).days >= MAXHOLD_GUN:
                kapat(tk, d, _price_on(seri[tk], d), "MAX-BEKLEME")
                continue

        # 3) Gunluk deger + drawdown
        holdings = sum((_price_on(seri[tk], d) or 0) * pos["shares"]
                       for tk, pos in positions.items())
        val = cash + holdings
        peak = max(peak, val)
        if peak > 0:
            maxdd = min(maxdd, (val - peak) / peak)
        deger_serisi.append({"tarih": diso, "deger": round(val, 2)})

    # Sonda kalan acik pozisyonlari zorla kapat (deger sabit kalir)
    son = sim_dates[-1]
    for tk in list(positions.keys()):
        p = _price_on(seri[tk], son)
        if p is not None:
            kapat(tk, son, p, "SONDA-KAPAT")
    bitis_deger = cash

    return {
        "bitis_deger": round(bitis_deger, 2),
        "closed": closed,
        "sl_count": sl_count,
        "tp_count": tp_count,
        "maxdd": maxdd,
        "deger_serisi": deger_serisi,
    }


def _buy_and_hold(seri, dec_dates):
    ilk = dec_dates[0]
    master = _master_dates(seri)
    son = master[-1]
    pay = SERMAYE / len(TICKERS)
    bitis = 0.0
    detay = []
    for tk in TICKERS:
        st = seri.get(tk)
        if not st:
            continue
        p0 = _price_on(st, ilk)
        p1 = _price_on(st, son)
        if not p0 or not p1:
            continue
        shares = pay / p0
        deg = shares * p1
        bitis += deg
        detay.append({"ticker": tk, "giris": round(p0, 2), "son": round(p1, 2),
                      "getiri_%": round((p1 - p0) / p0 * 100, 2)})
    return {"bitis_deger": round(bitis, 2),
            "getiri_%": round((bitis - SERMAYE) / SERMAYE * 100, 2),
            "baslangic": ilk.isoformat(), "bitis": son.isoformat(),
            "hisse_detay": detay}


def _metrics(sim, bh, dec_dates):
    closed = sim["closed"]
    n = len(closed)
    kazanan = sum(1 for t in closed if t["kazanc"])
    sure = [t["sure_gun"] for t in closed]
    per_tk = {}
    for t in closed:
        per_tk.setdefault(t["ticker"], 0.0)
        per_tk[t["ticker"]] += t["kz_tl"]
    en_iyi = max(per_tk.items(), key=lambda x: x[1]) if per_tk else (None, None)
    en_kotu = min(per_tk.items(), key=lambda x: x[1]) if per_tk else (None, None)
    getiri = (sim["bitis_deger"] - SERMAYE) / SERMAYE * 100

    return {
        "baslangic_sermaye_tl": SERMAYE,
        "bitis_deger_tl": sim["bitis_deger"],
        "toplam_getiri_%": round(getiri, 2),
        "buy_hold_getiri_%": bh["getiri_%"],
        "buy_hold_bitis_tl": bh["bitis_deger"],
        "fark_vs_buyhold_%": round(getiri - bh["getiri_%"], 2),
        "max_drawdown_%": round(sim["maxdd"] * 100, 2),
        "kapanan_islem": n,
        "kazanan_islem": kazanan,
        "basari_orani_%": round(kazanan / n * 100, 1) if n else None,
        "stop_loss_tetik": sim["sl_count"],
        "take_profit_tetik": sim["tp_count"],
        "ort_pozisyon_suresi_gun": round(sum(sure) / len(sure), 1) if sure else None,
        "en_iyi_hisse": {"ticker": en_iyi[0], "kz_tl": round(en_iyi[1], 2) if en_iyi[1] is not None else None},
        "en_kotu_hisse": {"ticker": en_kotu[0], "kz_tl": round(en_kotu[1], 2) if en_kotu[1] is not None else None},
        "cikis_nedeni_dagilimi": dict(Counter(t["neden"] for t in closed)),
        "karar_tarih_sayisi": len(dec_dates),
    }


def run(verbose=True):
    _load_dotenv()
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY yok")
    t0 = datetime.now(_TZ)
    if verbose:
        print(f"[{t0:%H:%M:%S}] Veri yukleniyor ({len(TICKERS)} hisse)...")
    seri = _load_series()
    master = _master_dates(seri)
    dec_dates = _decision_dates(master)
    if verbose:
        print(f"  veri araligi: {master[0]} .. {master[-1]} | karar tarihi: {len(dec_dates)} ay")

    verdicts = _gather_verdicts(seri, dec_dates, verbose=verbose)
    if verbose:
        print(f"[{datetime.now(_TZ):%H:%M:%S}] Simulasyon...")
    sim = _simulate(seri, dec_dates, verdicts, verbose=verbose)
    bh = _buy_and_hold(seri, dec_dates)
    metrics = _metrics(sim, bh, dec_dates)

    sonuc = {
        "uretim_tarihi": datetime.now(_TZ).isoformat(timespec="seconds"),
        "strateji": "Agresif (500K) - portfoy simulasyonu",
        "model": MODEL,
        "aciklama": ("Agresif strateji backtesti (2024-2026 aylik). 500.000 TL sanal "
                     "sermaye, 50.000 TL/pozisyon, max 10 acik. AL puan>=6, risk veto>=10, "
                     "stop-loss -%8, take-profit +%15'te yari kapanis, max 30 gun. Veri: "
                     "yfinance + statik sektor korelasyonu/sektor notu/tarihsel senaryo + "
                     "Claude tam sistem promptu. KAP/analist/bilanco/makro look-ahead "
                     "onlemek icin haric. SADECE BACKTEST; canli esikler degismedi."),
        "parametreler": {
            "tickers": TICKERS, "sermaye_tl": SERMAYE, "pozisyon_tl": POZ_TL,
            "max_acik": MAX_OPEN, "al_puan_esigi": AL_PUAN, "risk_veto_esigi": VETO_RISK,
            "stop_loss_%": STOP * 100, "take_profit_%": TAKE * 100,
            "take_profit_aksiyon": "pozisyonun yarisi", "max_bekleme_gun": MAXHOLD_GUN,
        },
        "metrikler": metrics,
        "buy_hold": bh,
        "islemler": sim["closed"],
        "deger_serisi": sim["deger_serisi"],
    }
    OUT = Path(__file__).resolve().parents[2] / "data" / "backtest_aggressive.json"
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(sonuc, ensure_ascii=False, indent=2), encoding="utf-8")

    if verbose:
        m = metrics
        dk = (datetime.now(_TZ) - t0).total_seconds() / 60
        print(f"\n=== AGRESIF STRATEJI SONUC ({dk:.1f} dk) ===")
        print(f"  500.000 TL -> {m['bitis_deger_tl']:,.0f} TL  (getiri %{m['toplam_getiri_%']})")
        print(f"  Buy&Hold: %{m['buy_hold_getiri_%']} | fark %{m['fark_vs_buyhold_%']}")
        print(f"  Max drawdown: %{m['max_drawdown_%']}")
        print(f"  Islem: {m['kapanan_islem']} | basari %{m['basari_orani_%']} | "
              f"stop {m['stop_loss_tetik']} | take {m['take_profit_tetik']}")
        print(f"  Ort sure: {m['ort_pozisyon_suresi_gun']} gun")
        print(f"  En iyi: {m['en_iyi_hisse']} | En kotu: {m['en_kotu_hisse']}")
        print(f"  Kaydedildi: {OUT}")
    return sonuc


if __name__ == "__main__":
    run()
