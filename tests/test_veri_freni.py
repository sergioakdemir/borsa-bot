"""Veri freni (sahte KILL_SWITCH) regresyon testleri — sentetik veri, ag ISTEGI YOK.

31 Tem 2026 arizasi: sabah brifinginde watchlist'in son 39 hissesi
"fiyat verisi 24 saatten eski" diye atlandi; 54 karar uretildi. Sebep, gunluk
barin Yahoo'da once Volume=0 ile yayinlanmasi ve market_data'daki
df[df["Volume"] > 0] filtresinin o bari "hic yokmus" gibi elemesiydi.

Bu testler o deseni sabitler:
  T1/T1b  hacimsiz son bar korunuyor (eski filtre ayni veride bari duşuruyordu)
  T2      seri bayat + fiyat cache taze -> cache fallback devrede
  T3      cache DE bayatsa fren KALKMIYOR (uydurma veriyle karar uretilmez)
  T4/T5   cekim retry'i (2 hata sonrasi basari / 3 hatada None)

Calistir:  ./venv/bin/python tests/test_veri_freni.py
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ai import commentary as C
from src.ai import presignal


def seri(son_gun: date, n=260, son_hacim=1000.0):
    """n is gunluk sentetik OHLCV; son bar tarihi = son_gun."""
    idx = pd.bdate_range(end=pd.Timestamp(son_gun), periods=n)
    df = pd.DataFrame({
        "Open": [100.0] * n, "High": [101.0] * n, "Low": [99.0] * n,
        "Close": [100.0 + i * 0.01 for i in range(n)],
        "Volume": [1000.0] * (n - 1) + [son_hacim],
    }, index=idx)
    return df


class SahteKaynak:
    def __init__(self, df, hata_sayisi=0):
        self.df, self.hata_sayisi, self.cagri = df, hata_sayisi, 0

    def get_history(self, symbol, start, end=None, interval="1d"):
        self.cagri += 1
        if self.cagri <= self.hata_sayisi:
            raise ConnectionError("sentetik ag hatasi")
        return self.df


def kur(df, hata_sayisi=0):
    kaynak = SahteKaynak(df, hata_sayisi)

    import src.data.factory as factory
    factory.get_data_source = lambda name="yfinance": kaynak
    return kaynak


def onceki_isgunu(g: date, k=1) -> date:
    while k:
        g -= timedelta(days=1)
        if g.weekday() < 5:
            k -= 1
    return g


SONUC = []


def kontrol(ad, kosul, detay=""):
    SONUC.append(kosul)
    print(f"[{'GECTI' if kosul else 'KALDI'}] {ad} {detay}")


bugun = datetime.now(C._TZ).date()
dun_is = onceki_isgunu(bugun)            # en son kapanmis islem gunu
onceki = onceki_isgunu(bugun, 2)

# --- TEST 1: son bar Volume=0 (31 Tem arizasinin ta kendisi) ---------------
# Yahoo son gunluk bari once hacimsiz yayinlar. ESKI kod bu bari eleyip son_bar'i
# bir onceki gune dusuruyor ve bayat=True -> KILL_SWITCH. YENI kod barı korumali.
kur(seri(dun_is, son_hacim=0.0))
d = C.market_data("TEST1", "bist")
kontrol("T1 Volume=0 son bar korunuyor",
        d is not None and d["son_bar_tarihi"] == dun_is.isoformat() and not d["bayat"],
        f"-> son_bar={d and d['son_bar_tarihi']} bayat={d and d['bayat']}")

# ESKI davranisin ayni veride ne yaptigini goster (regresyon kanidi)
_df = seri(dun_is, son_hacim=0.0)
_eski = _df[_df["Volume"] > 0]
kontrol("T1b eski hacim filtresi ayni barda son_bar'i geri atiyordu",
        pd.Timestamp(_eski.index[-1]).date() == onceki,
        f"-> eski son_bar={pd.Timestamp(_eski.index[-1]).date()} (yeni: {dun_is})")

# --- TEST 2: seri bayat + fiyat cache taze -> fallback devrede -------------
kur(seri(onceki, son_hacim=1000.0))      # yfinance 1 islem gunu geride
# NOT: _FIYAT_CACHE sozlugunu doldurmak YETMEZ — _load_fiyat_cache gercek dosyanin
# mtime'ini gorup diskten yeniden okur ve sahte veriyi ezer. Fonksiyonu degistir.
presignal._load_fiyat_cache = lambda: {
    "TEST2": {"fiyat": 42.5, "gunluk": 1.0,
              "guncelleme": f"{dun_is.isoformat()} 18:55",
              "bar_tarihi": dun_is.isoformat(), "kaynak": "yfinance"}}
d = C.market_data("TEST2", "bist")
kontrol("T2 bayat seri + taze cache -> fallback",
        d is not None and not d["bayat"] and d["veri_fallback"] is not None
        and d["son_bar_tarihi"] == dun_is.isoformat() and d["son_kapanis"] == 42.5,
        f"-> son_bar={d and d['son_bar_tarihi']} son_kapanis={d and d['son_kapanis']} "
        f"fallback={d and d['veri_fallback']}")

# --- TEST 3: cache DA bayatsa fallback yok -> KILL_SWITCH korunmali --------
kur(seri(onceki, son_hacim=1000.0))
presignal._load_fiyat_cache = lambda: {
    "TEST3": {"fiyat": 42.5, "gunluk": 1.0,
              "guncelleme": f"{onceki_isgunu(bugun, 5).isoformat()} 18:55",
              "bar_tarihi": onceki_isgunu(bugun, 5).isoformat(),
              "kaynak": "yfinance"}}
d = C.market_data("TEST3", "bist")
kontrol("T3 cache de bayat -> fren KALKMIYOR (kill korunuyor)",
        d is not None and d["bayat"] is True and d["veri_fallback"] is None,
        f"-> bayat={d and d['bayat']}")

# --- TEST 4: retry - ilk 2 deneme patliyor, 3.'su donuyor ------------------
C._CEKIM_BEKLEME = (0, 0)                # testte beklemeyelim
k = kur(seri(dun_is), hata_sayisi=2)
d = C.market_data("TEST4", "bist")
kontrol("T4 2 hatadan sonra 3. deneme basarili",
        d is not None and k.cagri == 3,
        f"-> cagri={k.cagri} son_bar={d and d['son_bar_tarihi']}")

# --- TEST 5: 3 deneme de patliyor -> None (kill) ---------------------------
k = kur(seri(dun_is), hata_sayisi=99)
d = C.market_data("TEST5", "bist")
kontrol("T5 3 deneme de patlarsa None", d is None and k.cagri == 3,
        f"-> cagri={k.cagri} sonuc={d}")

# --- TEST 6: cache'te bar_tarihi YOKSA fallback YAPILMAZ -------------------
# (eski format cache / investing-bigpara gibi bar tasimayan kaynak)
kur(seri(onceki, son_hacim=1000.0))
presignal._load_fiyat_cache = lambda: {
    "TEST6": {"fiyat": 42.5, "gunluk": 1.0,
              "guncelleme": f"{dun_is.isoformat()} 18:55", "kaynak": "investing"}}
d = C.market_data("TEST6", "bist")
kontrol("T6 bar_tarihi yoksa fallback YOK (kill korunuyor)",
        d is not None and d["bayat"] is True and d["veri_fallback"] is None,
        f"-> bayat={d and d['bayat']} fallback={d and d['veri_fallback']}")

# --- TEST 7: PAZARTESI SENARYOSU ------------------------------------------
# Cache Pazartesi 09:00'da yenilenir ama BIST henuz kapalidir: 'guncelleme'
# Pazartesi, tasidigi fiyat ise CUMA kapanisidir. Fallback'in yazdigi tarih
# CUMA olmali (cekim gunu degil) — aksi halde "Pazartesi etiketli Cuma fiyati"
# uydurma bir tazelik iddiasi olurdu.
kur(seri(onceki, son_hacim=1000.0))
presignal._load_fiyat_cache = lambda: {
    "TEST7": {"fiyat": 42.5, "gunluk": 1.0,
              "guncelleme": f"{bugun.isoformat()} 09:00",   # CEKIM: bugun
              "bar_tarihi": dun_is.isoformat(),             # BAR: onceki islem gunu
              "kaynak": "yfinance"}}
d = C.market_data("TEST7", "bist")
kontrol("T7 fallback tarihi BAR tarihi (cekim zamani degil)",
        d is not None and d["son_bar_tarihi"] == dun_is.isoformat()
        and d["veri_fallback"] and d["veri_fallback"]["tarih"] == dun_is.isoformat(),
        f"-> son_bar={d and d['son_bar_tarihi']} (cekim {bugun.isoformat()}, "
        f"bar {dun_is.isoformat()})")

# --- TEST 8-10: EKSIK SEANS ONARIMI (3 Agu 2026 arizasi) ------------------
# Yahoo 31 Tem 2026 GUNLUK barini BIST hisselerinin TAMAMINDA yayinlamadi
# (orneklenen 40/40'inda yok; endekste XU100.IS bar SAGLAM -> borsa acikti).
# Pazartesi 09:00'da bugune ait bar da olmadigi icin en yeni bar 2 islem gunu
# geride kaldi ve 93/93 KILL_SWITCH cikti. Ayni seans Yahoo'nun SAATLIK
# serisinde duruyor; fren inmeden once oradan kurulur.
from src.data import freshness as F                                # noqa: E402


class SahteTicker:
    """yf.Ticker taklidi: istenen gunler icin sentetik 1h seansi doner."""

    def __init__(self, gunler, hacimli=True):
        self.gunler, self.hacimli = gunler, hacimli

    def history(self, start=None, end=None, interval="1h", **kw):
        satir, idx = [], []
        for g in self.gunler:
            for saat, fiyat in ((10, 50.0), (12, 52.0), (17, 51.0)):
                idx.append(pd.Timestamp(f"{g.isoformat()} {saat:02d}:30:00"))
                satir.append({"Open": fiyat, "High": fiyat + 1, "Low": fiyat - 1,
                              "Close": fiyat,
                              "Volume": 5000.0 if self.hacimli else 0.0})
        if not satir:
            return pd.DataFrame()
        return pd.DataFrame(satir, index=pd.DatetimeIndex(idx))


def kur_saatlik(gunler, hacimli=True):
    import yfinance as yf
    yf.Ticker = lambda sym: SahteTicker(gunler, hacimli)


# T8: gunluk seride eksik olan kapanmis seans saatlikten kuruluyor
df8 = seri(onceki)                       # son bar 2 islem gunu geride
kur_saatlik([dun_is])                    # eksik gun saatlikte VAR
df8r, onarilan = F.eksik_seans_onar(df8, "TEST8.IS", "bist")
kontrol("T8 eksik kapanmis seans saatlik barlardan kuruluyor",
        onarilan == [dun_is.isoformat()]
        and pd.Timestamp(df8r.index[-1]).date() == dun_is
        and float(df8r["Close"].iloc[-1]) == 51.0,
        f"-> onarilan={onarilan} son_bar={pd.Timestamp(df8r.index[-1]).date()} "
        f"kapanis={float(df8r['Close'].iloc[-1])}")

# T9: saatlik veri de yoksa seri DEGISMEZ -> fren KALKMAZ (uydurma yok)
kur_saatlik([])
df9 = seri(onceki)
df9r, onarilan9 = F.eksik_seans_onar(df9, "TEST9.IS", "bist")
kontrol("T9 saatlik veri yoksa onarim YOK (fren korunuyor)",
        onarilan9 == [] and pd.Timestamp(df9r.index[-1]).date() == onceki,
        f"-> onarilan={onarilan9} son_bar={pd.Timestamp(df9r.index[-1]).date()}")

# T10: yalnizca HACIMSIZ bar varsa (acilis-oncesi gosterge fiyati) bar KURULMAZ
kur_saatlik([dun_is], hacimli=False)
df10 = seri(onceki)
df10r, onarilan10 = F.eksik_seans_onar(df10, "TEST10.IS", "bist")
kontrol("T10 hacimsiz saatlik barlardan bar UYDURULMUYOR",
        onarilan10 == [] and pd.Timestamp(df10r.index[-1]).date() == onceki,
        f"-> onarilan={onarilan10} son_bar={pd.Timestamp(df10r.index[-1]).date()}")

# --- TEST 11-12: FIYAT CACHE GERI-GITME KILIDI ----------------------------
# Cache her calismada sifirdan kurulup dosyayi eziyordu. Yahoo bir seansi
# geriye donuk kaybedince cache ZAMANDA GERI gidiyor (Cuma 18:00'de 31 Tem
# kapanisi vardi, Pazartesi 09:00 tazelemesi 30 Tem'e dusurdu) ve karar
# motorunun bayat-veri yedegi de bosa cikiyordu.
import json as _json                                              # noqa: E402
import tempfile                                                   # noqa: E402
from src.ops import update_fiyat_cache as UFC                     # noqa: E402


def cache_yaz_oku(eski: dict, yeni_bar: str):
    """Diskteki 'eski' cache uzerine 'yeni_bar' tarihli cekim yazilinca ne kalir?"""
    tmp = Path(tempfile.mkdtemp()) / "fiyat_cache.json"
    tmp.write_text(_json.dumps(eski), encoding="utf-8")
    UFC.CACHE_PATH = tmp
    UFC._semboller = lambda: {"TESTC": "bist"}
    UFC._mcp_batch = lambda d: {}
    UFC._batch_cek = lambda syms: {"TESTC.IS": {"fiyat": 10.0, "gunluk": 0.5,
                                                "bar_tarihi": yeni_bar}}
    UFC._sma_batch = lambda m: {}
    UFC.BIGPARA_ONLY = {}
    ozet = UFC.guncelle()
    return _json.loads(tmp.read_text(encoding="utf-8"))["TESTC"], ozet


# T11: kaynak GERI giderse (yeni bar daha ESKI) eski kayit korunur
eski_taze = {"TESTC": {"fiyat": 99.0, "gunluk": 2.0,
                       "guncelleme": f"{dun_is.isoformat()} 18:00",
                       "bar_tarihi": dun_is.isoformat(), "kaynak": "yfinance"}}
kayit, ozet = cache_yaz_oku(eski_taze, onceki.isoformat())
kontrol("T11 kaynak bar tarihi GERI giderse taze kayit korunuyor",
        kayit["bar_tarihi"] == dun_is.isoformat() and kayit["fiyat"] == 99.0
        and ozet.get("korunan") == 1,
        f"-> bar_tarihi={kayit['bar_tarihi']} fiyat={kayit['fiyat']} "
        f"korunan={ozet.get('korunan')}")

# T12: normal ILERI tazeleme etkilenmez (kilit yalniz geri gidiste devrede)
eski_bayat = {"TESTC": {"fiyat": 99.0, "gunluk": 2.0,
                        "guncelleme": f"{onceki.isoformat()} 18:00",
                        "bar_tarihi": onceki.isoformat(), "kaynak": "yfinance"}}
kayit2, ozet2 = cache_yaz_oku(eski_bayat, dun_is.isoformat())
kontrol("T12 ileri giden normal tazeleme etkilenmiyor",
        kayit2["bar_tarihi"] == dun_is.isoformat() and kayit2["fiyat"] == 10.0
        and ozet2.get("korunan") == 0,
        f"-> bar_tarihi={kayit2['bar_tarihi']} fiyat={kayit2['fiyat']} "
        f"korunan={ozet2.get('korunan')}")

print(f"\n{sum(SONUC)}/{len(SONUC)} test gecti")
sys.exit(0 if all(SONUC) else 1)
