"""Deterministik BIST backtest (AI YOK, maliyet $0).

2024-2025 arasi 30 BIST hissesi icin tamamen kural-tabanli bir giris kalitesi
(Entry Quality) modeli kurar ve 5 islem gunu ileri getiriyle olcer. Hicbir LLM
cagrisi yapilmaz; sadece yfinance OHLCV + deterministik hesaplama kullanilir.

Akis:
  1. Her hisse icin 2024-2025 OHLCV (SMA200 icin ~10 ay tampon ile) cekilir.
  2. Her gun icin: SMA20/50/200 trend, volatilite, hacim orani, 5 gun momentum
     ve bunlardan turetilen Entry Quality Score (0-100) hesaplanir.
  3. Iki strateji ayni skorlardan test edilir:
       NORMAL: skor >= 70 -> AL (yuksek giris kalitesi = momentum/trend takibi)
       TERS  : skor <  40 -> AL (asiri satim = mean reversion)
  4. Sektor tavani: ayni gun ayni sektorden en fazla 2 AL (NORMAL'de en yuksek
     skora, TERS'te en dusuk skora oncelik; tasanlar elenir).
  5. 5 islem gunu sonra sonuc: hisse getirisi + XU100 karsilastirmasi.
  6. Rapor: her strateji icin AL sayisi/basari, ortalama getiri & piyasa farki,
     Profit Factor, Hit Rate, en iyi sektor + NORMAL vs TERS karsilastirmasi.

Cikti: data/backtest_deterministik.json  (+ konsol ozeti)
Calistir: venv/bin/python -m scripts.backtest_deterministik
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

from src.ai.learning import SEKTOR_HISSE, _sektor_of

# --- Parametreler --------------------------------------------------------
KARAR_BASLANGIC = "2024-01-01"
KARAR_BITIS = "2025-12-31"
VERI_BASLANGIC = "2023-03-01"   # SMA200 icin tampon
VERI_BITIS = "2026-01-31"       # son kararlar icin 5 gun ileri pencere
ILERI_GUN = 5                   # forward getiri penceresi (islem gunu)
MAX_ABS_GETIRI = 40             # |5g getiri| bu esigi asarsa split/veri artefakti say, ele
AL_ESIK = 70
UZAK_ESIK = 40
SEKTOR_TAVANI = 2               # ayni gun ayni sektorden max AL
REJIM_PENCERE = 10              # piyasa rejimi icin XU100 geriye donuk pencere (islem gunu)
REJIM_ESIK = 2.0               # |XU100 son 10g %| bu esigin: + -> pozitif, - -> negatif, arasi notr
# SEKTOR BAZLI strateji: donuguel/volatil sektorlerde mean-reversion (TERS),
# savunmaci/istikrarli sektorlerde trend-takibi (NORMAL). Digerleri -> NORMAL.
SEKTOR_TERS = {"Bankacılık", "Gayrimenkul", "Telekom", "Havacılık", "Perakende"}
SEKTOR_NORMAL = {"İçecek", "Dayanıklı Tüketim", "Cam", "Gıda", "Savunma"}
_ROOT = Path(__file__).resolve().parents[1]


def _sym(ticker: str) -> str:
    return ticker.upper().replace(".IS", "") + ".IS"


def _watchlist_bist():
    with open(_ROOT / "config" / "watchlist.json") as f:
        wl = json.load(f)
    ham = wl.get("bist_endeks", []) + wl.get("kisisel", [])
    tickerlar = {(h["ticker"] if isinstance(h, dict) else h).upper() for h in ham}
    # Yalniz BIST (ABD/fon disi): '.F' fonlarini ele
    return sorted(t for t in tickerlar if not t.endswith(".F"))


def _indir(sym: str) -> pd.DataFrame | None:
    df = yf.download(sym, start=VERI_BASLANGIC, end=VERI_BITIS,
                     progress=False, auto_adjust=True)
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df.dropna()


# --- Skor bilesenleri (hepsi deterministik) ------------------------------
def _trend_puan(close, sma20, sma50, sma200):
    """0-40: MA dizilimine gore trend gucu."""
    if close > sma20 > sma50 > sma200:
        return 40, "güçlü yükseliş"
    if close > sma50 > sma200:
        return 30, "yükseliş"
    if close > sma200:
        return 20, "yatay-pozitif"
    if close < sma20 < sma50 < sma200:
        return 0, "güçlü düşüş"
    return 8, "yatay-negatif"


def _momentum_puan(mom5):
    """0-25: 5 gun momentum. Ilimli pozitif en iyi, asiri alim cezali."""
    if mom5 < -5:
        return 0
    if mom5 < 0:
        return 8
    if mom5 <= 5:
        return 25
    if mom5 <= 10:
        return 18
    return 10  # asiri alim


def _hacim_puan(hacim_orani):
    """0-20: bugunku hacmin 20 gun ortalamasina orani (teyit)."""
    if hacim_orani >= 1.2:
        return 20
    if hacim_orani >= 0.9:
        return 14
    if hacim_orani >= 0.7:
        return 8
    return 4


def _volatilite_puan(volat):
    """0-15: gunluk getiri std (20g, %). Ilimli oynaklik tercih edilir."""
    if volat < 1.5:
        return 15
    if volat < 2.5:
        return 12
    if volat < 4.0:
        return 7
    return 3


def _kararlari_uret(sym, ticker, df, xu_fwd, xu_rejim):
    """Bir hisse icin gunluk kayitlari (karar oncesi, sektor tavani harici) uretir."""
    close = df["Close"]
    vol = df["Volume"]
    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean()
    vol20 = vol.rolling(20).mean()
    gun_getiri = close.pct_change() * 100
    volat20 = gun_getiri.rolling(20).std()
    mom5 = (close / close.shift(ILERI_GUN) - 1) * 100
    fwd = (close.shift(-ILERI_GUN) / close - 1) * 100  # ileri getiri (%)

    kayitlar = []
    tarihler = close.index
    for i in range(len(tarihler)):
        t = tarihler[i]
        if not (pd.Timestamp(KARAR_BASLANGIC) <= t <= pd.Timestamp(KARAR_BITIS)):
            continue
        if pd.isna(sma200.iloc[i]) or pd.isna(fwd.iloc[i]) or pd.isna(mom5.iloc[i]):
            continue
        c = float(close.iloc[i])
        hacim_orani = float(vol.iloc[i] / vol20.iloc[i]) if vol20.iloc[i] else 1.0
        volat = float(volat20.iloc[i]) if not pd.isna(volat20.iloc[i]) else 3.0

        tp, trend = _trend_puan(c, float(sma20.iloc[i]), float(sma50.iloc[i]),
                                float(sma200.iloc[i]))
        mp = _momentum_puan(float(mom5.iloc[i]))
        hp = _hacim_puan(hacim_orani)
        vp = _volatilite_puan(volat)
        skor = tp + mp + hp + vp

        fwd_ret = float(fwd.iloc[i])
        if abs(fwd_ret) > MAX_ABS_GETIRI:   # split/bonus artefakti (or. CCOLA 2024-08), ele
            continue
        mkt = xu_fwd.get(t)
        excess = (fwd_ret - mkt) if mkt is not None else None

        kayitlar.append({
            "ticker": ticker, "tarih": t.strftime("%Y-%m-%d"), "_ts": t,
            "sektor": _sektor_of(ticker) or "Bilinmiyor",
            "skor": skor,
            "rejim": xu_rejim.get(t),   # o gunku XU100 son 10g getirisi (%), None=veri yok
            "trend": trend, "trend_puan": tp, "mom_puan": mp,
            "hacim_puan": hp, "volat_puan": vp,
            "mom5": round(float(mom5.iloc[i]), 2),
            "hacim_orani": round(hacim_orani, 2),
            "volatilite": round(volat, 2),
            "fwd_ret": round(fwd_ret, 3),
            "mkt_ret": round(mkt, 3) if mkt is not None else None,
            "excess": round(excess, 3) if excess is not None else None,
        })
    return kayitlar


def _kayit_kurali(mod, k, gk):
    """Bir kayit icin uygulanacak kurali dondurur: 'normal' | 'ters' | None (islem yok).

    normal/ters   : sabit.
    hibrit        : gunun XU100 son 10g rejimine gore (>= +REJIM_ESIK -> normal,
                    <= -REJIM_ESIK -> ters, arasi notr -> None).
    sektor_bazli  : kaydin sektorune gore (SEKTOR_TERS -> ters, aksi -> normal)."""
    if mod in ("normal", "ters"):
        return mod
    if mod == "sektor_bazli":
        return "ters" if k["sektor"] in SEKTOR_TERS else "normal"
    # hibrit
    rejim = gk[0]["rejim"]                      # o gunku tum kayitlar ayni XU100 rejimi
    if rejim is None or abs(rejim) < REJIM_ESIK:
        return None
    return "normal" if rejim >= REJIM_ESIK else "ters"


def _al_kararlari(kayitlar, mod):
    """Bir strateji icin AL kararlarini (sektor tavani uygulanmis) dondurur.

    Her kaydin kurali _kayit_kurali ile belirlenir; kural sektor icinde sabittir
    (normal/ters gun bazinda, hibrit gun bazinda, sektor_bazli sektor bazinda).
    Her gun-sektor icin kurala gore adaylar secilir, oncelige gore siralanir ve
    en fazla SEKTOR_TAVANI tanesi alinir."""
    gune_gore = defaultdict(list)
    for k in kayitlar:
        gune_gore[k["tarih"]].append(k)

    al = []
    for _tarih, gk in gune_gore.items():
        sektor_grup = defaultdict(list)
        for k in gk:
            kural = _kayit_kurali(mod, k, gk)
            if kural is not None:
                sektor_grup[k["sektor"]].append((kural, k))
        for _sek, lst in sektor_grup.items():
            kural = lst[0][0]                  # sektor icinde kural sabit
            if kural == "normal":
                adaylar = sorted((k for _kr, k in lst if k["skor"] >= AL_ESIK),
                                 key=lambda x: x["skor"], reverse=True)
            else:
                adaylar = sorted((k for _kr, k in lst if k["skor"] < UZAK_ESIK),
                                 key=lambda x: x["skor"])
            al.extend(adaylar[:SEKTOR_TAVANI])
    return al


def _metrikler(al):
    """AL kayitlarindan ozet metrikler."""
    getiriler = [k["fwd_ret"] for k in al]
    excesler = [k["excess"] for k in al if k["excess"] is not None]
    basarili = [g for g in getiriler if g > 0]
    piyasa_yenen = [e for e in excesler if e > 0]
    return {
        "al": len(al),
        "basarili": len(basarili),
        "hit_rate": (len(basarili) / len(getiriler) * 100) if getiriler else 0.0,
        "piyasa_yenen": len(piyasa_yenen),
        "piyasa_yenen_oran": (len(piyasa_yenen) / len(excesler) * 100) if excesler else 0.0,
        "ort_getiri": float(np.mean(getiriler)) if getiriler else 0.0,
        "ort_excess": float(np.mean(excesler)) if excesler else 0.0,
        "profit_factor": _profit_factor(getiriler),
        "en_iyi": max(getiriler) if getiriler else 0.0,
        "en_kotu": min(getiriler) if getiriler else 0.0,
    }


def _sektor_ozet(al):
    grup = defaultdict(list)
    for k in al:
        grup[k["sektor"]].append(k["fwd_ret"])
    return sorted(
        ({"sektor": s, "al": len(v), "ort_getiri": round(float(np.mean(v)), 3),
          "hit_rate": round(sum(1 for g in v if g > 0) / len(v) * 100, 1)}
         for s, v in grup.items() if len(v) >= 5),
        key=lambda x: x["ort_getiri"], reverse=True)


def _profit_factor(getiriler):
    kazanc = sum(g for g in getiriler if g > 0)
    kayip = -sum(g for g in getiriler if g < 0)
    if kayip == 0:
        return float("inf") if kazanc > 0 else 0.0
    return kazanc / kayip


def _kural_katkisi(kayitlar):
    """Her kuralin (bileseninin) ortalama 5g getiriye kattigi degeri olcer:
    kural 'olumlu' oldugunda vs olmadiginda ort. getiri farki (lift)."""
    kurallar = {
        "Trend (güçlü yükseliş)": lambda k: k["trend_puan"] >= 40,
        "Momentum (ılımlı pozitif)": lambda k: k["mom_puan"] >= 25,
        "Hacim teyidi (>1.2x)": lambda k: k["hacim_puan"] >= 20,
        "Düşük volatilite (<1.5%)": lambda k: k["volat_puan"] >= 15,
    }
    out = []
    for isim, kosul in kurallar.items():
        firing = [k["fwd_ret"] for k in kayitlar if kosul(k)]
        digger = [k["fwd_ret"] for k in kayitlar if not kosul(k)]
        if not firing or not digger:
            continue
        lift = np.mean(firing) - np.mean(digger)
        out.append({
            "kural": isim, "lift": round(float(lift), 3),
            "ort_getiri_aktif": round(float(np.mean(firing)), 3),
            "ort_getiri_pasif": round(float(np.mean(digger)), 3),
            "gozlem": len(firing),
        })
    return sorted(out, key=lambda x: x["lift"], reverse=True)


def calistir():
    tickerlar = _watchlist_bist()
    print(f"DÖNEM: {KARAR_BASLANGIC} .. {KARAR_BITIS}  (veri: {VERI_BASLANGIC}..{VERI_BITIS})")
    print(f"BIST hisse: {len(tickerlar)}  ({', '.join(tickerlar)})")

    # XU100 ileri getiri haritasi (tarih -> 5g fwd %)
    xu = _indir("XU100.IS")
    if xu is None:
        print("XU100 indirilemedi, cikiliyor."); return
    xu_close = xu["Close"]
    xu_fwd_s = (xu_close.shift(-ILERI_GUN) / xu_close - 1) * 100
    xu_fwd = {t: float(v) for t, v in xu_fwd_s.items() if not pd.isna(v)}
    # Piyasa rejimi: XU100 son REJIM_PENCERE gunluk getirisi (geriye donuk, look-ahead yok)
    xu_rejim_s = (xu_close / xu_close.shift(REJIM_PENCERE) - 1) * 100
    xu_rejim = {t: float(v) for t, v in xu_rejim_s.items() if not pd.isna(v)}

    tum_kayit = []
    for tk in tickerlar:
        sym = _sym(tk)
        df = _indir(sym)
        if df is None or len(df) < 210:
            print(f"  ! {tk}: yetersiz veri, atlandi")
            continue
        tum_kayit.extend(_kararlari_uret(sym, tk, df, xu_fwd, xu_rejim))

    if not tum_kayit:
        print("Hic kayit uretilemedi."); return

    print(f"\nToplam degerlendirme: {len(tum_kayit)} gun-hisse")

    # --- Dort stratejiyi de ayni kayitlardan degerlendir -----------------
    stratejiler = {
        "normal":       {"baslik": "NORMAL  (yüksek EQ >=70 -> AL)"},
        "ters":         {"baslik": "TERS    (düşük EQ <40 -> AL, mean reversion)"},
        "hibrit":       {"baslik": "HİBRİT  (rejim +: NORMAL, rejim -: TERS, nötr: BEKLE)"},
        "sektor_bazli": {"baslik": "SEKTÖR BAZLI (döngüsel sektör: TERS, savunmacı: NORMAL)"},
    }
    for mod, s in stratejiler.items():
        al = _al_kararlari(tum_kayit, mod)
        s["al_kayit"] = al
        s["metrik"] = _metrikler(al)
        s["sektor"] = _sektor_ozet(al)

    def _pf_str(pf):
        return "∞" if pf == float("inf") else f"{pf:.2f}"

    # --- Her strateji raporu --------------------------------------------
    for mod, s in stratejiler.items():
        m = s["metrik"]
        print("\n" + "=" * 62)
        print(f"STRATEJI: {s['baslik']}")
        print("=" * 62)
        print(f"AL karari        : {m['al']}")
        print(f"Basarili (>0)    : {m['basarili']}  ->  Hit Rate %{m['hit_rate']:.1f}")
        print(f"Piyasayi yenen   : {m['piyasa_yenen']}  (%{m['piyasa_yenen_oran']:.1f})")
        print(f"Ort. 5g getiri   : %{m['ort_getiri']:+.2f}")
        print(f"Ort. piyasa farki: %{m['ort_excess']:+.2f} (hisse - XU100)")
        print(f"Profit Factor    : {_pf_str(m['profit_factor'])}")
        print(f"En iyi / en kotu : %{m['en_iyi']:+.2f} / %{m['en_kotu']:+.2f}")
        if s["sektor"]:
            e = s["sektor"][0]
            print(f"En iyi sektor    : {e['sektor']} (%{e['ort_getiri']:+.2f}, "
                  f"AL={e['al']}, hit=%{e['hit_rate']:.0f})")

    # --- 4'lu karsilastirma (modlar uzerinde dinamik) -------------------
    modlar = ["normal", "ters", "hibrit", "sektor_bazli"]
    etiket = {"normal": "NORMAL", "ters": "TERS", "hibrit": "HİBRİT",
              "sektor_bazli": "SEKTÖR"}
    W = 11  # kolon genisligi
    M = {mod: stratejiler[mod]["metrik"] for mod in modlar}

    def _fmt(mod, alan):
        v = M[mod][alan]
        if alan == "profit_factor":
            return _pf_str(v)
        return f"{v:+.2f}" if isinstance(v, float) else str(v)

    cizgi = "=" * (20 + W * len(modlar) + 9)
    print("\n" + cizgi)
    print("KARSILASTIRMA: " + " vs ".join(etiket[m] for m in modlar))
    print(cizgi)
    baslik = f"{'Metrik':<20}" + "".join(f"{etiket[m]:>{W}}" for m in modlar) + f"{'Kazanan':>9}"
    print(baslik)
    metrik_satir = [
        ("AL sayısı", "al", None),
        ("Hit Rate %", "hit_rate", "yuksek"),
        ("Ort. getiri %", "ort_getiri", "yuksek"),
        ("Piyasa farkı %", "ort_excess", "yuksek"),
        ("Profit Factor", "profit_factor", "yuksek"),
    ]
    kazanma_sayaci = defaultdict(int)
    for isim, alan, yon in metrik_satir:
        if yon == "yuksek":
            en_iyi_mod = max(modlar, key=lambda mod: M[mod][alan])
            kazanma_sayaci[en_iyi_mod] += 1
            kazanan = etiket[en_iyi_mod]
        else:
            kazanan = ""
        hucreler = "".join(f"{_fmt(m, alan):>{W}}" for m in modlar)
        print(f"{isim:<20}{hucreler}{kazanan:>9}")

    genel = max(modlar, key=lambda mod: (kazanma_sayaci[mod], M[mod]["ort_excess"]))
    print(f"\n>>> GENEL KAZANAN: {etiket[genel]}  "
          f"(4 puanlı metrikten {kazanma_sayaci[genel]}'ini kazandı)")

    # --- Sektor bazli 4'lu karsilastirma --------------------------------
    print("\n" + cizgi)
    print("SEKTÖRE GÖRE: hangi stratejinin ort. 5g getirisi daha iyi? (>=5 AL)")
    print(cizgi)
    print(f"{'Sektör':<20}" + "".join(f"{etiket[m]:>{W}}" for m in modlar) + f"{'En iyi':>9}")
    sektor_map = {mod: {r["sektor"]: r for r in stratejiler[mod]["sektor"]} for mod in modlar}
    tum_sektorler = sorted({s for mod in modlar for s in sektor_map[mod]},
                           key=lambda s: -max((sektor_map[mod].get(s, {}).get("ort_getiri", -99)
                                               for mod in modlar)))
    sektor_kazanan_sayaci = defaultdict(int)
    for sek in tum_sektorler:
        hucre, adaylar = {}, {}
        for mod in modlar:
            r = sektor_map[mod].get(sek)
            hucre[mod] = f"%{r['ort_getiri']:+.2f}" if r else "—"
            if r:
                adaylar[mod] = r["ort_getiri"]
        if adaylar:
            en_iyi_mod = max(adaylar, key=adaylar.get)
            sektor_kazanan_sayaci[en_iyi_mod] += 1
            en_iyi = etiket[en_iyi_mod]
        else:
            en_iyi = "—"
        hucreler = "".join(f"{hucre[m]:>{W}}" for m in modlar)
        print(f"{sek:<20}{hucreler}{en_iyi:>9}")
    if sektor_kazanan_sayaci:
        ozet_s = ", ".join(f"{etiket[m]}={sektor_kazanan_sayaci[m]}" for m in modlar)
        print(f"\nSektör galibiyet dağılımı: {ozet_s}")

    print("\nMALIYET: $0.00 (4 strateji de AI'sız, deterministik)")

    # --- JSON cikti ------------------------------------------------------
    def _pf_json(pf):
        return None if pf == float("inf") else round(pf, 3)

    ozet = {
        "donem": f"{KARAR_BASLANGIC}..{KARAR_BITIS}",
        "ileri_gun": ILERI_GUN,
        "parametreler": {"al_esik": AL_ESIK, "uzak_esik": UZAK_ESIK,
                         "sektor_tavani": SEKTOR_TAVANI,
                         "rejim_pencere": REJIM_PENCERE, "rejim_esik": REJIM_ESIK},
        "toplam_degerlendirme": len(tum_kayit),
        "kural_katkisi": _kural_katkisi(tum_kayit),
        "kazanan": stratejiler[genel]["baslik"],
        "sektor_galibiyet": {etiket[m]: sektor_kazanan_sayaci[m] for m in modlar},
        "stratejiler": {
            mod: {
                "baslik": s["baslik"],
                "metrik": {k: (_pf_json(v) if k == "profit_factor" else
                               round(v, 3) if isinstance(v, float) else v)
                           for k, v in s["metrik"].items()},
                "sektor": s["sektor"],
            } for mod, s in stratejiler.items()
        },
        "maliyet_usd": 0.0,
    }
    out_dir = _ROOT / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"backtest_deterministik_{KARAR_BASLANGIC[:4]}_{KARAR_BITIS[:4]}.json"
    with open(out_path, "w") as f:
        json.dump(ozet, f, ensure_ascii=False, indent=2)
    print(f"\nJSON: {out_path}")
    return ozet


if __name__ == "__main__":
    # Opsiyonel: KARAR penceresini argv ile ver (out-of-sample icin).
    #   venv/bin/python -m scripts.backtest_deterministik 2022-01-01 2023-12-31
    # Sektor atamalari (SEKTOR_TERS/NORMAL) DEGISMEZ -> durust OOS testi.
    if len(sys.argv) >= 3:
        KARAR_BASLANGIC, KARAR_BITIS = sys.argv[1], sys.argv[2]
        VERI_BASLANGIC = (pd.Timestamp(KARAR_BASLANGIC)
                          - pd.Timedelta(days=320)).strftime("%Y-%m-%d")  # SMA200 tamponu
        VERI_BITIS = (pd.Timestamp(KARAR_BITIS)
                      + pd.Timedelta(days=40)).strftime("%Y-%m-%d")       # 5g ileri pencere
    calistir()
