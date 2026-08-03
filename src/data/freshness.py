"""Cekilen verinin guncelligini (freshness) degerlendirir.

Borsa verisi her gun uretilmez: hafta sonu ve resmi tatillerde islem olmaz.
Bu yuzden 'son bar bugune ait degil' demek her zaman 'veri eski' demek degildir.
Uc durum ayirt edilir:
  FRESH  -> son bar bugune ait
  RECENT -> son bar, beklenen son islem gunune ait (hafta sonu sonrasi normal)
  STALE  -> beklenenden eski (gercekten guncel degil)
"""
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from enum import Enum
from zoneinfo import ZoneInfo
import pandas as pd


class Freshness(str, Enum):
    FRESH = "FRESH"
    RECENT = "RECENT"
    STALE = "STALE"


@dataclass
class FreshnessReport:
    status: Freshness
    last_bar: date | None      # verideki son bar tarihi
    today: date                # piyasa saat dilimine gore bugun
    expected: date | None      # beklenen son islem gunu
    calendar_age: int | None   # takvim gunu farki (son bar -> bugun)
    trading_age: int | None    # kacirilan is gunu sayisi (yaklasik, tatil haric)
    message: str

    @property
    def is_ok(self) -> bool:
        """STALE degilse veri kullanilabilir kabul edilir."""
        return self.status in (Freshness.FRESH, Freshness.RECENT)


def pazar_kodu(symbol: str) -> str:
    """Ham yfinance sembolunden piyasa kodu ('bist' | 'us')."""
    return "bist" if str(symbol or "").upper().endswith(".IS") else "us"


def canli_bar_at(df, market: str = "bist", symbol: str | None = None):
    """Henuz KAPANMAMIS (canli) son bari atar; kapanmis barlarin HEPSINI korur.

    NEDEN HACIM DEGIL TAKVIM
    Kod tabaninda uzun sure `df[df["Volume"] > 0]` kullanildi. Yahoo gunluk bari
    once Volume=0 ile yayinlar ve hacmi saatler sonra konsolide eder (endekslerde
    ertesi gune kadar 0 kalabilir). Hacim filtresi o bari "hic yokmus" gibi
    eledigi icin son bar bir onceki gune duser. Bu ayni hata uretime IKI kez cikti:

      22 Tem 2026 — update_decisions: hisse bugunku bari sayarken endeks saymiyor
                    (asimetri), 137 kararin piyasa_farki NULL kaldi.
      31 Tem 2026 — commentary.market_data: sabah brifinginde watchlist'in son 39
                    hissesi "fiyat verisi 24 saatten eski" diye KILL_SWITCH yedi,
                    yalniz 54 karar uretildi.

    Dogru olcut hacim DEGIL takvimdir: son bar BUGUNE aitse ve o piyasa HALA
    ACIKSA bar henuz kapanmamistir -> atilir. Diger her durumda korunur.

    NOT: Canli (seans ici) bari BILEREK isteyen cagiranlar (or. sicak uyari
    motoru) bu fonksiyonu KULLANMAMALI; onlarin ihtiyaci son kapanmis bar degil,
    o anki bardir. Orada dogru davranis hacim filtresini bastan kaldirmaktir.
    """
    if df is None or len(df) == 0:
        return df
    if symbol is not None:
        market = pazar_kodu(symbol)
    try:
        from src import piyasa_takvim
        # piyasa_takvim.borsa_acik HER IKI piyasa icin de ISTANBUL saati bekler
        # (ABD seansi 16:30-23:00 IST olarak tanimli) -> New York'a cevirme.
        now = datetime.now(ZoneInfo("Europe/Istanbul"))
        if (pd.Timestamp(df.index[-1]).date() == now.date()
                and piyasa_takvim.borsa_acik(now, "us" if market in ("us", "abd")
                                             else "bist")):
            return df.iloc[:-1]
    except Exception:
        pass
    return df


def eksik_seans_onar(df, symbol: str, market: str = "bist", now=None):
    """GUNLUK seride eksik kalan KAPANMIS seanslari SAATLIK barlardan kurar.

    NEDEN GEREKLI (3 Agu 2026 — 93/93 karar uretilemedi)
    Yahoo 31 Tem 2026 (Cuma) GUNLUK barini BIST hisselerinin tamami icin hic
    yayinlamadi: orneklenen 40 hissenin 40'inda o gun yok, seri 30 Tem'den
    3 Agu'ya atliyor. Endekste (XU100.IS) ayni bar SAGLAM duruyor — yani borsa
    acikti, eksik olan yalnizca Yahoo'nun gunluk backfill'i. Pazartesi 09:00
    brifingi (BIST henuz kapali, bugune ait bar yok) elindeki en yeni bari
    30 Tem gorup "arada 1 tam is gunu var" diyerek 93 hissenin hepsine
    KILL_SWITCH yazdi. Fiyat cache de ayni yfinance'ten beslendigi icin
    kurtaramadi (bkz. update_fiyat_cache geri-gitme kilidi).

    AYNI VERI BASKA ENDPOINT'TE DURUYOR: Yahoo'nun 1h serisinde 31 Tem seansi
    (09:30-17:30) gercek fiyat ve hacimle mevcut. Bu fonksiyon o seansi gunluk
    bara cevirir: Open=ilk, High=en yuksek, Low=en dusuk, Close=son, Volume=toplam.

    VERI UYDURULMAZ: yalnizca GERCEKLESMIS islem barlari birlestirilir. Hicbir
    seans bulunamazsa seri oldugu gibi doner ve fren KALKMAZ. Kurulan kapanis
    KAPANIS SEANSINI (18:00 tek fiyat muzayedesi) icermez; olculen sapma
    ~%0.0-0.6'dir (THYAO -%0.24, GARAN +%0.16, ASELS %0.00). Bu yuzden onarilan
    barlar cagirana AYRICA bildirilir ve 'veri_onarim' iziyle kayda gecer.

    Doner: (df, [onarilan tarihler]) — onarim yoksa (df, []).
    """
    onarilan = []
    if df is None or len(df) == 0:
        return df, onarilan
    if symbol is not None:
        market = pazar_kodu(symbol)
    try:
        now = now or datetime.now(ZoneInfo("Europe/Istanbul"))
        bugun = now.date()
        son_bar = pd.Timestamp(df.index[-1]).date()

        # Eksik olabilecek gunler: son bardan SONRA, bugunden ONCE (bugunun
        # yarim bari burada kurulmaz — o is canli_bar_at'in isi).
        from src import piyasa_takvim
        tatiller = (piyasa_takvim.tr_tatilleri(son_bar, bugun)
                    if market not in ("us", "abd") else set())
        adaylar, d = [], son_bar + timedelta(days=1)
        while d < bugun:
            if d.weekday() < 5 and d not in tatiller:
                adaylar.append(d)
            d += timedelta(days=1)
        mevcut = {pd.Timestamp(i).date() for i in df.index}
        adaylar = [g for g in adaylar if g not in mevcut]
        if not adaylar:
            return df, onarilan

        import yfinance as yf
        saatlik = yf.Ticker(symbol).history(
            start=adaylar[0].isoformat(),
            end=(adaylar[-1] + timedelta(days=1)).isoformat(),
            interval="1h", auto_adjust=True,
        )
        if saatlik is None or len(saatlik) == 0:
            return df, onarilan

        yeni = {}
        for gun in adaylar:
            try:
                sub = saatlik[saatlik.index.strftime("%Y-%m-%d") == gun.isoformat()]
                sub = sub[sub["Close"].notna()]
                # Hacimsiz barlar (09:30 acilis-oncesi gosterge fiyati gibi) OHLC'yi
                # bozar; yalnizca GERCEK islem gormus barlardan kur. En az 2 bar
                # yoksa o gun kurulmaz - yarim veriyle bar uydurmaktansa fren kalir.
                islemli = sub[sub["Volume"] > 0]
                if len(islemli) < 2:
                    continue
                yeni[gun] = {
                    "Open": float(islemli["Open"].iloc[0]),
                    "High": float(islemli["High"].max()),
                    "Low": float(islemli["Low"].min()),
                    "Close": float(islemli["Close"].iloc[-1]),
                    "Volume": float(sub["Volume"].sum()),
                }
            except (KeyError, IndexError, ValueError, TypeError):
                continue
        if not yeni:
            return df, onarilan

        ek = pd.DataFrame(
            [{k: v for k, v in satir.items() if k in df.columns}
             for satir in yeni.values()],
            index=pd.DatetimeIndex([pd.Timestamp(g) for g in yeni]),
        )
        df = pd.concat([df, ek]).sort_index()
        onarilan = sorted(g.isoformat() for g in yeni)
    except Exception:
        return df, onarilan
    return df, onarilan


def kapanmis_seri(df, symbol: str | None = None, market: str = "bist"):
    """Degerleme/karar yollari icin SON KAPANMIS bar serisi (tek cagri).

    Iki ayri kusuru birlikte kapatir:
      1) canli_bar_at  — henuz kapanmamis (yarim) bugunku bari atar,
      2) eksik_seans_onar — kaynagin KAYBETTIGI kapanmis seanslari geri kurar.

    3 Agu 2026'da (2) olmadan pozisyon takibi ve XU100 benchmark'i gun boyu
    31 Tem yerine 30 Tem kapanisini "son kapanis" sanacakti: stop/hedef
    kontrolu ve gunluk getiri bir seans geriden hesaplanirdi.

    Onarim yalnizca gercekten bosluk varsa ag istegi yapar; normal gunde
    (son kapanmis bar = onceki islem gunu) hicbir ek cagri olmaz.
    """
    df = canli_bar_at(df, market=market, symbol=symbol)
    if symbol:
        df, _ = eksik_seans_onar(df, symbol, market)
    return df


def _last_expected_trading_day(today: date) -> date:
    """Bugun hafta ici ise bugun; degilse en yakin onceki hafta ici gun.
    NOT: resmi tatiller hesaba katilmaz; bu yaklasik bir kontroldur."""
    d = today
    while d.weekday() >= 5:  # 5=Cumartesi, 6=Pazar
        d -= timedelta(days=1)
    return d


def check_freshness(df: pd.DataFrame, tz: str = "Europe/Istanbul") -> FreshnessReport:
    """Bir OHLCV DataFrame'inin guncelligini degerlendirir.

    tz: piyasanin saat dilimi (BIST -> Europe/Istanbul, US -> America/New_York).
    """
    today = datetime.now(ZoneInfo(tz)).date()
    expected = _last_expected_trading_day(today)

    if df is None or len(df) == 0:
        return FreshnessReport(
            Freshness.STALE, None, today, expected, None, None,
            "Veri bos — guncellik degerlendirilemez.",
        )

    last_bar = pd.Timestamp(df.index[-1]).date()
    calendar_age = (today - last_bar).days
    # son bar ile bugun arasindaki is gunu sayisi (son bar haric)
    trading_age = max(len(pd.bdate_range(last_bar, today)) - 1, 0)

    if last_bar >= today:
        status = Freshness.FRESH
        msg = f"GUNCEL: son bar bugune ait ({last_bar})."
    elif last_bar >= expected:
        status = Freshness.RECENT
        msg = (f"TAZE: son bar son islem gunune ait ({last_bar}); "
               f"bugun ({today}) icin kapanis verisi henuz olusmamis olabilir.")
    else:
        missed = max(len(pd.bdate_range(last_bar, expected)) - 1, 0)
        status = Freshness.STALE
        msg = (f"ESKI: son bar {last_bar}, beklenen >= {expected} "
               f"(~{missed} islem gunu geride).")

    return FreshnessReport(status, last_bar, today, expected,
                           calendar_age, trading_age, msg)
