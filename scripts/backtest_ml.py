"""Hisse-bazli 'ogrenen' strateji secimi — DURUST out-of-sample (AI YOK, $0).

Fikir: sabit sektor haritasi 2024-25'e asiri uyum sagladi (bkz. overfitting
bulgusu). Onun yerine her hisse icin strateji tercihini SISTEMATIK bir kuralla
egitim doneminden ogren, sonra tamamen ayri bir donemde test et.

AŞAMA 1 — EGIT (2022-2023):
  Her hisse icin Entry Quality skoru ile 5g ileri getiri arasindaki korelasyon:
    corr < 0  -> yuksek skorlu gunler kotu biter -> MEAN REVERSION (TERS tercih)
    corr > 0  -> yuksek skorlu gunler iyi biter  -> TREND TAKIP (NORMAL tercih)
  mean_reversion_skoru = (1 - corr) / 2   (0=saf trend, 1=saf mean-reversion)
  Ayrica: hangi rejimde daha iyi, ortalama momentum/volatilite/hacim profili,
  NORMAL vs TERS alpha (capraz kontrol).

AŞAMA 2 — TEST (2024-2025):
  2022-23'te ogrenilen hisse-bazli tercihleri 2024-25 verisine uygula.
  Sektor tavani + metrikler (Hit Rate, Profit Factor, Alpha).

AŞAMA 3 — KARSILASTIR:
  ÖĞRENEN vs sabit NORMAL vs sabit TERS vs SEKTÖR BAZLI (hepsi ayni test verisi).
  Hangi hissede ogrenme ise yaradi (test-en-iyi ile tercih ortusuyor mu)?

Cikti: data/backtest_ml.json  (+ konsol raporu)
Calistir: venv/bin/python -m scripts.backtest_ml
"""
import json
import sys
import warnings
from collections import defaultdict
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
import yfinance as yf

from scripts.backtest_deterministik import (
    _trend_puan, _momentum_puan, _hacim_puan, _volatilite_puan,
    _profit_factor, _sym, _watchlist_bist,
    _al_kararlari, _metrikler,
    ILERI_GUN, MAX_ABS_GETIRI, AL_ESIK, UZAK_ESIK, SEKTOR_TAVANI,
    REJIM_PENCERE, REJIM_ESIK,
)
from src.ai.learning import _sektor_of

EGIT = ("2022-01-01", "2023-12-31")
TEST = ("2024-01-01", "2025-12-31")
MIN_KAYIT = 60          # bir hisseyi egitmek icin gereken min gun-kayit sayisi
_ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ veri
def _indir(sym, start, end):
    buf_bas = (pd.Timestamp(start) - pd.Timedelta(days=320)).strftime("%Y-%m-%d")
    buf_bit = (pd.Timestamp(end) + pd.Timedelta(days=40)).strftime("%Y-%m-%d")
    df = yf.download(sym, start=buf_bas, end=buf_bit, progress=False, auto_adjust=True)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


def _kayitlar(ticker, df, karar_bas, karar_bit, xu_fwd, xu_rejim):
    """Bir hisse icin karar penceresindeki gunluk kayitlar (deterministik script
    ile ayni skor semasi)."""
    close, vol = df["Close"], df["Volume"]
    sma20, sma50, sma200 = (close.rolling(w).mean() for w in (20, 50, 200))
    vol20 = vol.rolling(20).mean()
    volat20 = (close.pct_change() * 100).rolling(20).std()
    mom5 = (close / close.shift(ILERI_GUN) - 1) * 100
    fwd = (close.shift(-ILERI_GUN) / close - 1) * 100

    out = []
    idx = close.index
    for i in range(len(idx)):
        t = idx[i]
        if not (pd.Timestamp(karar_bas) <= t <= pd.Timestamp(karar_bit)):
            continue
        if pd.isna(sma200.iloc[i]) or pd.isna(fwd.iloc[i]) or pd.isna(mom5.iloc[i]):
            continue
        fwd_ret = float(fwd.iloc[i])
        if abs(fwd_ret) > MAX_ABS_GETIRI:      # split/bonus artefakti
            continue
        c = float(close.iloc[i])
        hacim_orani = float(vol.iloc[i] / vol20.iloc[i]) if vol20.iloc[i] else 1.0
        volat = float(volat20.iloc[i]) if not pd.isna(volat20.iloc[i]) else 3.0
        tp, _tr = _trend_puan(c, float(sma20.iloc[i]), float(sma50.iloc[i]),
                              float(sma200.iloc[i]))
        skor = tp + _momentum_puan(float(mom5.iloc[i])) + \
            _hacim_puan(hacim_orani) + _volatilite_puan(volat)
        mkt = xu_fwd.get(t)
        out.append({
            "ticker": ticker, "tarih": t.strftime("%Y-%m-%d"),
            "sektor": _sektor_of(ticker) or "Bilinmiyor",
            "skor": skor, "rejim": xu_rejim.get(t),
            "mom5": round(float(mom5.iloc[i]), 2),
            "hacim_orani": round(hacim_orani, 2), "volatilite": round(volat, 2),
            "fwd_ret": round(fwd_ret, 3),
            "mkt_ret": round(mkt, 3) if mkt is not None else None,
            "excess": round(fwd_ret - mkt, 3) if mkt is not None else None,
        })
    return out


def _donem_yukle(karar_bas, karar_bit, tickerlar):
    """Bir donem icin {ticker: [kayit...]} dondurur."""
    xu = _indir("XU100.IS", karar_bas, karar_bit)
    if xu is None:
        raise RuntimeError("XU100 indirilemedi")
    xc = xu["Close"]
    xu_fwd = {t: float(v) for t, v in ((xc.shift(-ILERI_GUN) / xc - 1) * 100).items()
              if not pd.isna(v)}
    xu_rejim = {t: float(v) for t, v in ((xc / xc.shift(REJIM_PENCERE) - 1) * 100).items()
                if not pd.isna(v)}
    veri = {}
    for tk in tickerlar:
        df = _indir(_sym(tk), karar_bas, karar_bit)
        if df is None or len(df) < 210:
            print(f"  ! {tk}: yetersiz veri, atlandi")
            continue
        veri[tk] = _kayitlar(tk, df, karar_bas, karar_bit, xu_fwd, xu_rejim)
    return veri


# ------------------------------------------------------------------ istatistik
def _pearson(x, y):
    if len(x) < 3:
        return 0.0
    x, y = np.asarray(x, float), np.asarray(y, float)
    if x.std() == 0 or y.std() == 0:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def _alpha(kayitlar, kural):
    """Bir hissenin kayitlarindan, verilen kural altindaki AL kararlarinin ort.
    alpha'si (piyasa farki). kural: 'normal' (skor>=70) | 'ters' (skor<40)."""
    if kural == "normal":
        sec = [k for k in kayitlar if k["skor"] >= AL_ESIK and k["excess"] is not None]
    else:
        sec = [k for k in kayitlar if k["skor"] < UZAK_ESIK and k["excess"] is not None]
    if not sec:
        return None
    return float(np.mean([k["excess"] for k in sec]))


# ------------------------------------------------------------------ AŞAMA 1
def egit(egit_veri):
    """Her hisse icin strateji tercihini ogren."""
    ogrenilen = {}
    for tk, kayitlar in egit_veri.items():
        if len(kayitlar) < MIN_KAYIT:
            ogrenilen[tk] = {"tercih": "NORMAL", "yeterli": False,
                             "mean_reversion_skoru": 0.5, "corr": 0.0, "n": len(kayitlar)}
            continue
        skors = [k["skor"] for k in kayitlar]
        fwds = [k["fwd_ret"] for k in kayitlar]
        corr = _pearson(skors, fwds)
        mr = (1 - corr) / 2
        tercih = "TERS" if corr < 0 else "NORMAL"
        # rejim ayristirmasi
        yuk = [k["fwd_ret"] for k in kayitlar if (k["rejim"] or 0) > 0]
        dus = [k["fwd_ret"] for k in kayitlar if (k["rejim"] or 0) < 0]
        ogrenilen[tk] = {
            "sektor": kayitlar[0]["sektor"], "n": len(kayitlar),
            "corr": round(corr, 3),
            "mean_reversion_skoru": round(mr, 3),
            "trend_skoru": round(1 - mr, 3),
            "tercih": tercih, "yeterli": True,
            "egit_normal_alpha": round(_alpha(kayitlar, "normal") or 0, 3),
            "egit_ters_alpha": round(_alpha(kayitlar, "ters") or 0, 3),
            "iyi_rejim": ("yükselen" if (np.mean(yuk) if yuk else -9) >
                          (np.mean(dus) if dus else -9) else "düşen"),
            "ort_momentum": round(float(np.mean([k["mom5"] for k in kayitlar])), 2),
            "ort_volatilite": round(float(np.mean([k["volatilite"] for k in kayitlar])), 2),
            "ort_hacim_orani": round(float(np.mean([k["hacim_orani"] for k in kayitlar])), 2),
        }
    return ogrenilen


# ------------------------------------------------------------------ AŞAMA 2
def _al_ogrenen(test_kayit_flat, tercih_map):
    """Her hisseye kendi ogrenilen kuralini uygula, gun-sektor tavani ile AL sec.
    Sektor icinde oncelik: esik otesindeki 'konviksiyon' (esikten uzaklik)."""
    gune = defaultdict(list)
    for k in test_kayit_flat:
        gune[k["tarih"]].append(k)
    al = []
    for _t, gk in gune.items():
        sektor_aday = defaultdict(list)
        for k in gk:
            kural = tercih_map.get(k["ticker"], "NORMAL")
            if kural == "NORMAL" and k["skor"] >= AL_ESIK:
                sektor_aday[k["sektor"]].append((k["skor"] - AL_ESIK, k))
            elif kural == "TERS" and k["skor"] < UZAK_ESIK:
                sektor_aday[k["sektor"]].append((UZAK_ESIK - k["skor"], k))
        for _sek, lst in sektor_aday.items():
            lst.sort(key=lambda x: x[0], reverse=True)
            al.extend(k for _c, k in lst[:SEKTOR_TAVANI])
    return al


# ------------------------------------------------------------------ AŞAMA 3
def _hisse_isabet(test_veri, tercih_map):
    """Her hisse icin: test doneminde gercekte hangi kural daha iyi (alpha)?
    Ogrenilen tercih onunla ortusuyor mu?"""
    satir = []
    for tk, kayitlar in test_veri.items():
        na = _alpha(kayitlar, "normal")
        ta = _alpha(kayitlar, "ters")
        if na is None or ta is None:
            continue
        test_best = "TERS" if ta > na else "NORMAL"
        learned = tercih_map.get(tk, "NORMAL")
        satir.append({
            "ticker": tk, "sektor": kayitlar[0]["sektor"],
            "ogrenilen": learned, "test_en_iyi": test_best,
            "test_normal_alpha": round(na, 3), "test_ters_alpha": round(ta, 3),
            "secilen_alpha": round(ta if learned == "TERS" else na, 3),
            "isabet": learned == test_best,
        })
    return sorted(satir, key=lambda x: x["secilen_alpha"], reverse=True)


def _pf_str(pf):
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def calistir():
    tickerlar = _watchlist_bist()
    print(f"BIST hisse: {len(tickerlar)}")
    print(f"EGIT dönemi: {EGIT[0]}..{EGIT[1]}   TEST dönemi: {TEST[0]}..{TEST[1]}")

    print("\n[1/2] Egit verisi indiriliyor (2022-2023)...")
    egit_veri = _donem_yukle(*EGIT, tickerlar)
    print("[2/2] Test verisi indiriliyor (2024-2025)...")
    test_veri = _donem_yukle(*TEST, tickerlar)

    # === AŞAMA 1: ogren =================================================
    ogrenilen = egit(egit_veri)
    print("\n" + "=" * 78)
    print("AŞAMA 1 — ÖĞRENİLEN HİSSE TERCİHLERİ (2022-2023)")
    print("=" * 78)
    print(f"{'Hisse':<8}{'Sektör':<18}{'corr':>7}{'MR skoru':>10}"
          f"{'Tercih':>9}{'İyi rejim':>11}{'mom':>7}{'vol':>7}")
    for tk in sorted(ogrenilen, key=lambda x: -ogrenilen[x]["mean_reversion_skoru"]):
        o = ogrenilen[tk]
        if not o.get("yeterli"):
            print(f"{tk:<8}{'(yetersiz veri)':<18}{'—':>7}{'—':>10}{o['tercih']:>9}")
            continue
        print(f"{tk:<8}{o['sektor']:<18}{o['corr']:>7.2f}{o['mean_reversion_skoru']:>10.2f}"
              f"{o['tercih']:>9}{o['iyi_rejim']:>11}{o['ort_momentum']:>7.1f}"
              f"{o['ort_volatilite']:>7.1f}")
    ters_n = sum(1 for o in ogrenilen.values() if o["tercih"] == "TERS")
    print(f"\nÖğrenilen dağılım: TERS={ters_n}, NORMAL={len(ogrenilen)-ters_n}")

    # === AŞAMA 2: test uygula ===========================================
    tercih_map = {tk: o["tercih"] for tk, o in ogrenilen.items()}
    test_flat = [k for ks in test_veri.values() for k in ks]

    strat = {
        "ogrenen": _al_ogrenen(test_flat, tercih_map),
        "normal":  _al_kararlari(test_flat, "normal"),
        "ters":    _al_kararlari(test_flat, "ters"),
        "sektor":  _al_kararlari(test_flat, "sektor_bazli"),
    }
    metrik = {k: _metrikler(v) for k, v in strat.items()}

    # === AŞAMA 3: karsilastir ===========================================
    modlar = ["ogrenen", "normal", "ters", "sektor"]
    etiket = {"ogrenen": "ÖĞRENEN", "normal": "NORMAL", "ters": "TERS", "sektor": "SEKTÖR"}
    W = 11
    print("\n" + "=" * 78)
    print("AŞAMA 3 — TEST (2024-2025): ÖĞRENEN vs NORMAL vs TERS vs SEKTÖR")
    print("=" * 78)
    print(f"{'Metrik':<18}" + "".join(f"{etiket[m]:>{W}}" for m in modlar) + f"{'Kazanan':>9}")
    satirlar = [("AL sayısı", "al", None), ("Hit Rate %", "hit_rate", "y"),
                ("Ort. getiri %", "ort_getiri", "y"), ("Piyasa farkı %", "ort_excess", "y"),
                ("Profit Factor", "profit_factor", "y")]
    kazanc = defaultdict(int)
    for isim, alan, yon in satirlar:
        if yon:
            best = max(modlar, key=lambda m: metrik[m][alan]); kazanc[best] += 1
            kaz = etiket[best]
        else:
            kaz = ""
        hup = "".join(
            f"{(_pf_str(metrik[m][alan]) if alan=='profit_factor' else (f'{metrik[m][alan]:+.2f}' if isinstance(metrik[m][alan], float) else str(metrik[m][alan]))):>{W}}"
            for m in modlar)
        print(f"{isim:<18}{hup}{kaz:>9}")
    genel = max(modlar, key=lambda m: (kazanc[m], metrik[m]["ort_excess"]))
    print(f"\n>>> GENEL KAZANAN: {etiket[genel]}  ({kazanc[genel]}/4 metrik)")

    # Hisse bazli isabet
    isabet = _hisse_isabet(test_veri, tercih_map)
    dogru = sum(1 for r in isabet if r["isabet"])
    print("\n" + "=" * 78)
    print(f"HİSSE BAZLI ÖĞRENME İSABETİ: {dogru}/{len(isabet)} hissede "
          f"öğrenilen tercih = test'te gerçekten en iyi olan")
    print("=" * 78)
    print(f"{'Hisse':<8}{'Sektör':<18}{'Öğren':>8}{'Test-iyi':>10}"
          f"{'N-alpha':>9}{'T-alpha':>9}{'Sonuç':>9}")
    for r in isabet:
        print(f"{r['ticker']:<8}{r['sektor']:<18}{r['ogrenilen']:>8}{r['test_en_iyi']:>10}"
              f"{r['test_normal_alpha']:>9.2f}{r['test_ters_alpha']:>9.2f}"
              f"{('✓' if r['isabet'] else '✗'):>9}")

    calisan = [r["ticker"] for r in isabet if r["isabet"]]
    calismayan = [r["ticker"] for r in isabet if not r["isabet"]]
    print(f"\n✓ Öğrenme İŞE YARADI ({len(calisan)}): {', '.join(calisan)}")
    print(f"✗ Öğrenme İŞE YARAMADI ({len(calismayan)}): {', '.join(calismayan)}")
    print("\nMALIYET: $0.00 (AI çağrısı yok)")

    # === JSON ===========================================================
    def _pf_json(pf):
        return None if pf == float("inf") else round(pf, 3)
    ozet = {
        "egit_donem": f"{EGIT[0]}..{EGIT[1]}", "test_donem": f"{TEST[0]}..{TEST[1]}",
        "genel_kazanan": etiket[genel],
        "hisse_isabet": f"{dogru}/{len(isabet)}",
        "ogrenilen": ogrenilen,
        "test_metrikleri": {
            m: {k: (_pf_json(v) if k == "profit_factor" else
                    round(v, 3) if isinstance(v, float) else v)
                for k, v in metrik[m].items()} for m in modlar},
        "hisse_detay": isabet,
        "maliyet_usd": 0.0,
    }
    out = _ROOT / "data" / "backtest_ml.json"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w") as f:
        json.dump(ozet, f, ensure_ascii=False, indent=2)
    print(f"JSON: {out}")
    return ozet


if __name__ == "__main__":
    calistir()
