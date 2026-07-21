"""RANK-IC ERKEN IZLEME — olcum altyapisi (KARAR MOTORUNA DOKUNMAZ).

Amac: dogru veriyi dogru anda, tekrar saymadan kaydetmeye BASLAMAK.
Istatistiksel hukum uretmek DEGIL (v2.1'de ~8 islem gunu var; agir istatistik
Agustos ortasina — bkz. docs/analiz_yol_haritasi.md).

Bu script SALT OKUR: decisions/karar_denetim tablolarini ve fiyat verisini okur,
logs/rank_ic_gunluk.csv'ye gunluk Rank IC satiri ekler. Hicbir karar/esik/prompt
degistirmez. v2.1 DONDURMADA.

--- ZAMAN CIZELGESI (look-ahead onleme) ---
  feature_snapshot_ts : karar gununden ONCEKI kapanis (bot sabah brifinginde
                        piyasa acilmadan onceki veriyle calisir)
  decision_ts         : karar_denetim.olusturma (varsa) yoksa BIST 09:00 / ABD 15:30
  price_entry_ts      : karar gunu ACILISI (BIST 10:00 / ABD 16:30 TR) — karar
                        sonrasi ILK islem yapilabilir fiyat. Ayni gunun KAPANISI
                        KULLANILMAZ (o, karardan sonra olusan gelecek bilgisidir).
  return_end_ts       : giristen h islem gunu sonraki KAPANIS

Calistirma: python -m scripts.rank_ic_izleme   (veya python scripts/rank_ic_izleme.py)
"""
from __future__ import annotations

import collections
import csv
import hashlib
import logging
import subprocess
import sys
import warnings
from datetime import datetime, time, timedelta
from pathlib import Path

warnings.filterwarnings("ignore")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

KOK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KOK))

import sqlite3
import pandas as pd
import yfinance as yf

DB = KOK / "data" / "borsa.db"
CSV_YOL = KOK / "logs" / "rank_ic_gunluk.csv"

BASLANGIC = "2026-06-20"          # analiz penceresi basi
UFUKLAR = (1, 3, 5)               # islem gunu
MALIYETLER_BP = (0, 10, 25)       # tek yon islem maliyeti (baz puan)

# Gunluk IC gecerlilik esikleri — saglanmazsa gun INSUFFICIENT_CROSS_SECTION
MIN_TICKER = 20                   # en az gecerli ticker
MIN_UNIQUE_SCORE = 3              # puanlarda yeterli varyasyon
MAX_EKSIK_ORAN = 0.20             # ileri getiri eksikligi < %20

# Piyasa saatleri (Europe/Istanbul); giris fiyati = o gunun ACILISI
ACILIS = {"BIST": time(10, 0), "US": time(16, 30)}
KARAR_SAATI = {"BIST": time(9, 0), "US": time(15, 30)}   # olusturma yoksa varsayilan
KAPANIS_SAATI = {"BIST": time(18, 0), "US": time(23, 0)}


# --------------------------------------------------------------------------
# Yardimcilar
# --------------------------------------------------------------------------
def _git_sha(dosya: str, tarih: str) -> str:
    """tarih'e kadar `dosya`ya dokunan son commit'in kisa SHA'si (prompt_version)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(KOK), "log", "-1", "--format=%h",
             f"--before={tarih} 23:59:59", "--", dosya],
            capture_output=True, text=True, timeout=15)
        return (r.stdout or "").strip() or "bilinmiyor"
    except Exception:
        return "bilinmiyor"


def _watchlist_sha(tickerlar) -> str:
    """O gunku evrenin kisa ozeti — evren degisimini sonradan ayirt etmek icin."""
    h = hashlib.sha1(",".join(sorted(tickerlar)).encode()).hexdigest()[:8]
    return f"{len(tickerlar)}:{h}"


def _pazar(ticker: str, harita: dict) -> str:
    return "US" if harita.get(ticker) == "US" else "BIST"


def _sembol(ticker: str, harita: dict):
    if ticker.endswith(".F"):
        return None
    return ticker if harita.get(ticker) == "US" else f"{ticker}.IS"


# --------------------------------------------------------------------------
# Veri yukleme
# --------------------------------------------------------------------------
def veri_yukle():
    db = sqlite3.connect(DB)
    pazar_harita = {t: m for t, m in db.execute("select ticker,market from instruments")}

    kararlar = []
    for t, k, p, r, g, v in db.execute(
            "select ticker,karar,puan,risk,date(tarih),strategy_version from decisions "
            "where puan is not null and date(tarih)>=? order by date(tarih)", (BASLANGIC,)):
        kararlar.append({"ticker": t, "karar": k, "puan": p, "risk": r,
                         "gun": g, "surum": v or "?"})

    # gercek karar zaman damgasi (varsa) — karar_denetim 20 Tem'de basladi
    olusturma = {}
    for t, g, o in db.execute(
            "select ticker,date(tarih),olusturma from karar_denetim where date(tarih)>=?",
            (BASLANGIC,)):
        if o:
            olusturma[(g, t)] = o
    db.close()

    df = pd.DataFrame(kararlar)
    df["pazar"] = [_pazar(t, pazar_harita) for t in df["ticker"]]
    return df, pazar_harita, olusturma


def fiyat_yukle(tickerlar, pazar_harita):
    sm = {t: _sembol(t, pazar_harita) for t in tickerlar}
    sm = {t: s for t, s in sm.items() if s}
    hepsi = sorted(set(sm.values()) | {"XU100.IS", "SPY"})
    print(f"  fiyat indiriliyor: {len(hepsi)} sembol...", flush=True)
    data = yf.download(hepsi, start="2026-05-15", end="2026-07-23",
                       progress=False, group_by="ticker",
                       auto_adjust=False, threads=True)
    ac, kap = {}, {}
    for t, s in list(sm.items()) + [("__XU100", "XU100.IS"), ("__SPY", "SPY")]:
        try:
            o = data[s]["Open"].dropna()
            c = data[s]["Close"].dropna()
        except Exception:
            continue
        if len(c) >= 3:
            ac[t], kap[t] = o, c
    return ac, kap


# --------------------------------------------------------------------------
# Zaman damgalari + look-ahead ASSERTION
# --------------------------------------------------------------------------
class LookAheadIhlali(AssertionError):
    pass


def zaman_damgalari(satir, olusturma, kap, ac):
    """(feature_ts, decision_ts, entry_ts, entry_px, ...) uretir; yoksa None."""
    t, g, pz = satir["ticker"], satir["gun"], satir["pazar"]
    o_ = ac.get(t)
    c_ = kap.get(t)
    if o_ is None or c_ is None:
        return None
    gunler = [str(d)[:10] for d in c_.index]
    if g not in gunler:
        return None                       # o gun islem yok (tatil/durdurma)
    i = gunler.index(g)
    if i == 0:
        return None                       # onceki kapanis yok -> feature_ts kurulamaz

    # feature: karar gununden ONCEKI kapanis
    feature_ts = datetime.combine(
        datetime.fromisoformat(gunler[i - 1]).date(), KAPANIS_SAATI[pz])
    # karar: gercek damga varsa onu kullan
    ham = olusturma.get((g, t))
    if ham:
        try:
            decision_ts = datetime.fromisoformat(str(ham)[:19])
        except ValueError:
            decision_ts = datetime.combine(datetime.fromisoformat(g).date(),
                                           KARAR_SAATI[pz])
    else:
        decision_ts = datetime.combine(datetime.fromisoformat(g).date(),
                                       KARAR_SAATI[pz])
    # giris: karardan SONRAKI ilk islem yapilabilir fiyat.
    # Karar o gunun acilisindan ONCE verildiyse -> ayni gunun ACILISI.
    # Karar acilistan SONRA verildiyse (or. 20 Tem 15:10 gun ici yeniden calisma)
    # o gunun acilisi ARTIK GECMISTIR -> bir SONRAKI islem gununun acilisi.
    # (Ayni gunun kapanisi hicbir kosulda kullanilmaz.)
    gun_acilis = datetime.combine(datetime.fromisoformat(g).date(), ACILIS[pz])
    if decision_ts < gun_acilis:
        gi = i
    else:
        gi = i + 1
        if gi >= len(gunler):
            return None                   # sonraki islem gunu henuz yok
    entry_ts = datetime.combine(datetime.fromisoformat(gunler[gi]).date(), ACILIS[pz])
    try:
        entry_px = float(o_.loc[c_.index[gi]])
    except (KeyError, ValueError, TypeError):
        return None
    if not entry_px or entry_px <= 0 or pd.isna(entry_px):
        return None

    return {"feature_ts": feature_ts, "decision_ts": decision_ts,
            "entry_ts": entry_ts, "entry_px": entry_px,
            "i": gi, "karar_idx": i, "gunler": gunler}


def ileri_getiri(satir, z, kap, h):
    """giris ACILISINDAN h islem gunu sonraki KAPANISA brut getiri (%) + bitis ts."""
    t, pz = satir["ticker"], satir["pazar"]
    c_ = kap.get(t)
    i, gunler = z["i"], z["gunler"]
    j = i + h
    if j >= len(c_):
        return None, None
    cikis = float(c_.iloc[j])
    if not cikis or pd.isna(cikis):
        return None, None
    end_ts = datetime.combine(datetime.fromisoformat(gunler[j]).date(),
                              KAPANIS_SAATI[pz])
    return (cikis / z["entry_px"] - 1) * 100, end_ts


def assertionlari_calistir(df, olusturma, ac, kap):
    """Her satir icin zorunlu look-ahead testleri. Ihlal -> LookAheadIhlali."""
    ihlal = []
    kontrol = 0
    for satir in df.to_dict("records"):
        z = zaman_damgalari(satir, olusturma, kap, ac)
        if z is None:
            continue
        for h in UFUKLAR:
            _, end_ts = ileri_getiri(satir, z, kap, h)
            if end_ts is None:
                continue
            kontrol += 1
            if not (z["feature_ts"] <= z["decision_ts"]):
                ihlal.append(f"{satir['ticker']} {satir['gun']}: "
                             f"feature_ts {z['feature_ts']} > decision_ts {z['decision_ts']}")
            if not (z["entry_ts"] > z["decision_ts"]):
                ihlal.append(f"{satir['ticker']} {satir['gun']}: "
                             f"entry_ts {z['entry_ts']} <= decision_ts {z['decision_ts']}")
            if not (end_ts > z["entry_ts"]):
                ihlal.append(f"{satir['ticker']} {satir['gun']} h={h}: "
                             f"end_ts {end_ts} <= entry_ts {z['entry_ts']}")
    return kontrol, ihlal


# --------------------------------------------------------------------------
# Tekil islem defteri: FLAT -> OPEN -> CLOSED
# --------------------------------------------------------------------------
def islem_defteri(df, olusturma, ac, kap, tutma_gun=5):
    """AL kararlarindan TEKIL islem uretir.

    Kurallar:
      * yalniz FLAT durumdayken AL -> yeni islem (BEKLE->AL gecisi gercek giris)
      * pozisyon aciksa gelen AL'lar YOK SAYILIR (tekrar sayim yok)
      * cikis: giristen `tutma_gun` islem gunu sonraki kapanis -> CLOSED, sonra FLAT
    """
    islemler = []
    durum = collections.defaultdict(lambda: {"acik": False, "kapanis_idx": None})
    al = df[df["karar"] == "AL"].sort_values("gun")
    for satir in al.to_dict("records"):
        t = satir["ticker"]
        z = zaman_damgalari(satir, olusturma, kap, ac)
        if z is None:
            continue
        d = durum[t]
        if d["acik"]:
            if z["i"] < (d["kapanis_idx"] or -1):
                continue                      # hala acik -> yeni giris YOK
            d["acik"] = False                 # suresi doldu -> FLAT
        brut, end_ts = ileri_getiri(satir, z, kap, tutma_gun)
        if brut is None:
            continue                          # ufuk dolmadi -> islem OLGUNLASMADI
        d["acik"] = True
        d["kapanis_idx"] = z["i"] + tutma_gun
        islemler.append({"ticker": t, "gun": satir["gun"], "surum": satir["surum"],
                         "puan": satir["puan"], "pazar": satir["pazar"],
                         "brut_%": brut, "durum": "CLOSED"})
    return pd.DataFrame(islemler)


def maliyet_uygula(brut_yuzde, bp_tek_yon):
    """gidis+donus maliyeti (2 x tek yon)."""
    return brut_yuzde - 2 * bp_tek_yon / 100.0


# --------------------------------------------------------------------------
# Gunluk Rank IC
# --------------------------------------------------------------------------
def gunluk_ic(df, olusturma, ac, kap, pazar_harita):
    """Gun x ufuk bazinda Spearman Rank IC. Tie handling: ORTALAMA SIRA (average rank).

    Gun gecerli degilse ic=None + sebep=INSUFFICIENT_CROSS_SECTION.
    """
    satirlar = []
    for gun, grup in df.groupby("gun"):
        surumler = grup["surum"].value_counts()
        surum = surumler.index[0] if len(surumler) else "?"
        prompt_v = _git_sha("src/ai/commentary.py", gun)
        wl_v = _watchlist_sha(grup["ticker"].tolist())
        # piyasa getirisi + rejim (BIST endeksi referans)
        piyasa_get, rejim = piyasa_durumu(gun, kap)
        for h in UFUKLAR:
            puanlar, getiriler = [], []
            toplam = 0
            for satir in grup.to_dict("records"):
                toplam += 1
                z = zaman_damgalari(satir, olusturma, kap, ac)
                if z is None:
                    continue
                g_, _ = ileri_getiri(satir, z, kap, h)
                if g_ is None:
                    continue
                puanlar.append(satir["puan"])
                getiriler.append(g_)
            gecerli = len(puanlar)
            uniq = len(set(puanlar))
            eksik_oran = 1 - (gecerli / toplam) if toplam else 1.0
            yeterli = (gecerli >= MIN_TICKER and uniq >= MIN_UNIQUE_SCORE
                       and eksik_oran < MAX_EKSIK_ORAN)
            ic = None
            if yeterli:
                s = pd.Series(puanlar).rank(method="average")
                r = pd.Series(getiriler).rank(method="average")
                ic = float(s.corr(r, method="pearson"))   # sira uzerinde pearson = spearman
            satirlar.append({
                "date": gun, "strategy_version": surum, "prompt_version": prompt_v,
                "watchlist_version": wl_v, "horizon": h,
                "ticker_count": toplam, "valid_ticker_count": gecerli,
                "unique_score_count": uniq,
                "ic": round(ic, 4) if ic is not None else "",
                "market_return": ("" if piyasa_get is None else round(piyasa_get, 4)),
                "market_regime": rejim,
                "durum": "OK" if yeterli else "INSUFFICIENT_CROSS_SECTION",
            })
    return pd.DataFrame(satirlar)


def piyasa_durumu(gun, kap):
    """(endeks gunluk getirisi %, rejim). Rejim = endeksin SMA20'ye gore konumu."""
    c = kap.get("__XU100")
    if c is None:
        return None, "bilinmiyor"
    gunler = [str(d)[:10] for d in c.index]
    if gun not in gunler:
        return None, "bilinmiyor"
    i = gunler.index(gun)
    if i == 0:
        return None, "bilinmiyor"
    getiri = (float(c.iloc[i]) / float(c.iloc[i - 1]) - 1) * 100
    sma = c.rolling(20).mean()
    if pd.isna(sma.iloc[i]):
        return getiri, "bilinmiyor"
    return getiri, ("endeks_sma20_ustu" if c.iloc[i] > sma.iloc[i] else "endeks_sma20_alti")


def csv_yaz(ic_df):
    """logs/rank_ic_gunluk.csv'ye ekler; ayni (date,horizon) varsa GUNCELLER."""
    kolonlar = ["date", "strategy_version", "prompt_version", "watchlist_version",
                "horizon", "ticker_count", "valid_ticker_count", "unique_score_count",
                "ic", "market_return", "market_regime", "durum"]
    mevcut = {}
    if CSV_YOL.exists():
        with CSV_YOL.open(newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                mevcut[(r.get("date"), r.get("horizon"))] = r
    for r in ic_df.to_dict("records"):
        mevcut[(r["date"], str(r["horizon"]))] = r
    CSV_YOL.parent.mkdir(exist_ok=True)
    with CSV_YOL.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=kolonlar)
        w.writeheader()
        for k in sorted(mevcut, key=lambda x: (x[0], int(x[1]))):
            sat = mevcut[k]
            w.writerow({c: sat.get(c, "") for c in kolonlar})
    return len(mevcut)


# --------------------------------------------------------------------------
def n_etiket(n):
    return f"n={n}" + ("  [GUVENILMEZ n<10]" if n < 10 else "")


def main():
    print("veri yukleniyor...", flush=True)
    df, pazar_harita, olusturma = veri_yukle()
    ac, kap = fiyat_yukle(sorted(df["ticker"].unique()), pazar_harita)

    # ---- 1) ZORUNLU LOOK-AHEAD TESTI ----
    kontrol, ihlal = assertionlari_calistir(df, olusturma, ac, kap)
    if ihlal:
        print("\n" + "!" * 70)
        print("TEST FAILED - RAPOR GECERSIZ")
        print("!" * 70)
        print(f"look-ahead ihlali: {len(ihlal)} adet. Ilk 10:")
        for x in ihlal[:10]:
            print("   -", x)
        # Sizinti SESSIZCE GECMESIN: bugunu CSV'ye GECERSIZ olarak isaretle.
        # (Satir atlanirsa gun "veri yoktu" gibi gorunur; ihlal kaybolur.)
        bugun = datetime.now().date().isoformat()
        isaret = pd.DataFrame([{
            "date": bugun, "strategy_version": "?", "prompt_version": "?",
            "watchlist_version": "?", "horizon": h, "ticker_count": len(df),
            "valid_ticker_count": 0, "unique_score_count": 0, "ic": "",
            "market_return": "", "market_regime": "bilinmiyor",
            "durum": f"GECERSIZ_LOOKAHEAD({len(ihlal)})",
        } for h in UFUKLAR])
        try:
            csv_yaz(isaret)
            print(f"\n{bugun} -> CSV'ye GECERSIZ_LOOKAHEAD olarak isaretlendi.")
        except Exception as e:
            print(f"\n[uyari] GECERSIZ isareti yazilamadi: {type(e).__name__}")
        print("DIGER SONUCLAR GOSTERILMIYOR.")
        return 1

    print("\n" + "=" * 74)
    print("RANK-IC ERKEN IZLEME RAPORU")
    print("=" * 74)
    print("Bu rapor karar vermek icin degil, olcum altyapisinin dogru calistigini")
    print("dogrulamak icindir.")
    print("=" * 74)
    print(f"\n[1] LOOK-AHEAD TESTI: GECTI ({kontrol} satir x 3 assertion "
          f"= {kontrol*3} kontrol, 0 ihlal)")
    print("    feature_snapshot_ts <= decision_ts  : OK")
    print("    price_entry_ts      >  decision_ts  : OK")
    print("    return_end_ts       >  price_entry_ts: OK")
    print("    Giris kurali: karardan SONRAKI ilk ACILIS. Karar o gunun acilisindan")
    print("    once ise ayni gun, sonra ise BIR SONRAKI islem gunu acilisi kullanilir.")
    print("    Ayni gunun KAPANISI hicbir kosulda giris fiyati olarak KULLANILMAZ.")

    # ---- 2) TEKIL ISLEM DEFTERI ----
    print("\n[2] TEKIL ISLEM DEFTERI (FLAT->OPEN->CLOSED, tutma 5 islem gunu)")
    isl = islem_defteri(df, olusturma, ac, kap, tutma_gun=5)
    ham_al = int((df["karar"] == "AL").sum())
    print(f"    ham AL karari         : {ham_al}")
    print(f"    OLGUNLASMIS tekil islem: {len(isl)}  {n_etiket(len(isl))}")
    if len(isl):
        for v, g in isl.groupby("surum"):
            print(f"      {v:5} {n_etiket(len(g))}  ort brut {g['brut_%'].mean():+.2f}%")

    # ---- 3) ISLEM MALIYETI ----
    print("\n[3] ISLEM MALIYETI (tek yon bp; gidis+donus uygulanir)")
    print(f"    {'grup':22}{'n':>5}{'brut':>9}{'0bp':>9}{'10bp':>9}{'25bp':>9}")
    yeni = df[df["surum"].isin(["v2", "v2.1"])]
    bandlar = [("puan 8+", yeni[yeni["puan"] >= 8]),
               ("puan 7", yeni[yeni["puan"] == 7]),
               ("puan 6", yeni[yeni["puan"] == 6]),
               ("gercek AL (tekil)", None)]
    for ad, grup in bandlar:
        if ad.startswith("gercek AL"):
            if not len(isl):
                print(f"    {ad:22}{0:>5}   (olgunlasmis islem yok)")
                continue
            getiriler = isl["brut_%"].tolist()
        else:
            getiriler = []
            for satir in grup.to_dict("records"):
                z = zaman_damgalari(satir, olusturma, kap, ac)
                if z is None:
                    continue
                g_, _ = ileri_getiri(satir, z, kap, 5)
                if g_ is not None:
                    getiriler.append(g_)
        n = len(getiriler)
        if n == 0:
            print(f"    {ad:22}{n:>5}   (5g verisi yok)")
            continue
        brut = sum(getiriler) / n
        sat = f"    {ad:22}{n:>5}{brut:>+8.2f}%"
        for bp in MALIYETLER_BP:
            sat += f"{maliyet_uygula(brut, bp):>+8.2f}%"
        sat += "  [GUVENILMEZ n<10]" if n < 10 else ""
        print(sat)

    # ---- 4) GUNLUK RANK IC ----
    print("\n[4] GUNLUK RANK IC (Spearman; tie handling = ORTALAMA SIRA)")
    print(f"    gecerlilik: >={MIN_TICKER} ticker, >={MIN_UNIQUE_SCORE} farkli puan, "
          f"eksik <%{int(MAX_EKSIK_ORAN*100)}")
    ic_df = gunluk_ic(df, olusturma, ac, kap, pazar_harita)
    n_satir = csv_yaz(ic_df)
    print(f"    -> logs/rank_ic_gunluk.csv yazildi ({n_satir} satir toplam)")
    for h in UFUKLAR:
        alt = ic_df[ic_df["horizon"] == h]
        ok = alt[alt["durum"] == "OK"]
        yetersiz = len(alt) - len(ok)
        print(f"\n    --- ufuk {h} gun --- gecerli gun: {len(ok)}, "
              f"INSUFFICIENT_CROSS_SECTION: {yetersiz}")
        if len(ok) == 0:
            print("        (gecerli gun yok — ortalama HESAPLANMADI)")
            continue
        print(f"        {'date':12}{'surum':7}{'n':>5}{'uniq':>6}{'IC':>8}  rejim")
        for r in ok.to_dict("records"):
            print(f"        {r['date']:12}{r['strategy_version']:7}"
                  f"{r['valid_ticker_count']:>5}{r['unique_score_count']:>6}"
                  f"{float(r['ic']):>+8.3f}  {r['market_regime']}")
        icler = [float(x) for x in ok["ic"]]
        etiket = "  [GUVENILMEZ n<10]" if len(icler) < 10 else ""
        print(f"        ORT IC = {sum(icler)/len(icler):+.3f}  "
              f"(gun sayisi {len(icler)}){etiket}")

    # ---- 5) ORNEKLEM DURUSTLUGU ----
    print("\n[5] ORNEKLEM DURUSTLUGU")
    for v in ("v1", "v2", "v2.1"):
        g = df[df["surum"] == v]
        if not len(g):
            continue
        sayac = {h: 0 for h in UFUKLAR}
        for satir in g.to_dict("records"):
            z = zaman_damgalari(satir, olusturma, kap, ac)
            if z is None:
                continue
            for h in UFUKLAR:
                if ileri_getiri(satir, z, kap, h)[0] is not None:
                    sayac[h] += 1
        print(f"    {v:5} karar={len(g):4}  " +
              "  ".join(f"{h}g verisi={sayac[h]:4}" for h in UFUKLAR))
    v21_5g = 0
    for satir in df[df["surum"] == "v2.1"].to_dict("records"):
        z = zaman_damgalari(satir, olusturma, kap, ac)
        if z and ileri_getiri(satir, z, kap, 5)[0] is not None:
            v21_5g += 1
    if v21_5g == 0:
        print("\n    >>> v2.1 5g YOK — yukaridaki 5 gunluk sayilar v2 donemine aittir.")
    print("\n[6] AGIR ISTATISTIK: Agustos ortasina ertelendi "
          "(bkz. docs/analiz_yol_haritasi.md)")
    print("    Kosul: v2.1 ~30 islem gunu VE dondurma bitmis olmali.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
