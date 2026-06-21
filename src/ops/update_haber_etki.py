"""Haber-fiyat korelasyonu: KAP bildirimi sonrasi fiyat tepkisini olcer.

run_alerts.py'deki KAP taramasi yeni bildirim gorunce o anki fiyati (fiyat_haber_ani)
kaydeder. Bu cron (her gece) eksik kalan 30dk / 2saat / 1gun fiyatlarini doldurur ve
1 gunluk yuzde etkiyi hesaplar.

30dk ve 2saat fiyatlari yfinance gun-ici (15dk) barlarindan, 1gun fiyati gunluk
kapanistan alinir. Gun-ici veri yfinance'te yalniz son ~60 gun icin mevcuttur;
ulasilamayan alanlar bos birakilir (sonraki gece tekrar denenmez cunku artik cok eski).
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")


# ---------------------------------------------------------------------------
# KAP bildirim kategorisi (baslik anahtar kelimelerinden)
# ---------------------------------------------------------------------------
_KATEGORILER = [
    ("Temettü", ("temett", "kar payı", "kâr payı", "kar dagit", "kâr dağıt")),
    ("Sermaye Artırımı", ("sermaye artır", "bedelli", "bedelsiz")),
    ("Pay Geri Alım", ("geri alım", "geri alim", "pay geri")),
    ("Birleşme/Devralma", ("birleşme", "birlesme", "devralma", "satın alma",
                            "satin alma", "devir")),
    ("Bilanço/Finansal", ("bilanço", "bilanco", "finansal rapor", "finansal tablo",
                           "faaliyet raporu", "finansal durum")),
    ("Sözleşme/İhale", ("sözleşme", "sozlesme", "ihale", "sipariş", "siparis",
                        "anlaşma", "anlasma", "kontrat")),
    ("Yatırım", ("yatırım", "yatirim", "tesis", "kapasite", "fabrika", "üretim")),
    ("Ortaklık Değişimi", ("pay alım", "pay satım", "ortaklık yapı", "hakim ortak",
                           "pay devri")),
    ("Yönetim/İdari", ("yönetim kurulu", "genel kurul", "istifa", "atama")),
]


def kategori_of(baslik: str) -> str:
    s = (baslik or "").lower()
    for ad, anahtarlar in _KATEGORILER:
        if any(k in s for k in anahtarlar):
            return ad
    if "özel durum" in s or "ozel durum" in s:
        return "Özel Durum Açıklaması"
    return "Diğer"


# ---------------------------------------------------------------------------
# Fiyat doldurma
# ---------------------------------------------------------------------------
def _intraday_price_at(ticker: str, hedef: datetime):
    """Hedef zamana en yakin (>=) 15dk barinin kapanisini dondurur (yoksa None)."""
    from src.data.factory import get_data_source
    from src.markets.bist import BIST
    import pandas as pd
    symbol = BIST().to_symbol(ticker)
    start = (hedef.date() - timedelta(days=1)).isoformat()
    end = (hedef.date() + timedelta(days=2)).isoformat()
    try:
        df = get_data_source().get_history(symbol, start=start, end=end, interval="15m")
    except Exception:
        return None
    if df is None or df.empty:
        return None
    for ix in df.index:
        ts = pd.Timestamp(ix)
        if ts.tzinfo is None:
            ts = ts.tz_localize(_TZ)
        else:
            ts = ts.tz_convert(_TZ)
        if ts.to_pydatetime() >= hedef:
            return float(df.loc[ix, "Close"])
    return None


def _daily_close_after(ticker: str, gun, ofset_gun: int = 1):
    """Haber gununden ofset_gun sonraki ilk islem gununun kapanisi."""
    from src.data.factory import get_data_source
    from src.markets.bist import BIST
    import pandas as pd
    symbol = BIST().to_symbol(ticker)
    start = (gun - timedelta(days=2)).isoformat()
    try:
        df = get_data_source().get_history(symbol, start=start)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if "Volume" in df.columns:
        df = df[df["Volume"] > 0]
    hedef = gun + timedelta(days=ofset_gun)
    for ix in df.index:
        d = pd.Timestamp(ix).date()
        if d >= hedef:
            return float(df.loc[ix, "Close"])
    return None


def run(verbose: bool = True) -> int:
    from src.db import database as db
    db.init_db()
    rows = db.haber_etki_eksikler()
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] eksik haber_etki kaydi: {len(rows)}")
    now = datetime.now(_TZ)
    guncellenen = 0
    for r in rows:
        try:
            ht = datetime.fromisoformat(r["haber_tarihi"])
            if ht.tzinfo is None:
                ht = ht.replace(tzinfo=_TZ)
        except (ValueError, TypeError):
            continue
        ticker = r["ticker"]
        alanlar = {}
        if not r.get("haber_kategori"):
            alanlar["haber_kategori"] = kategori_of(r.get("baslik"))

        # 30 dk / 2 saat: yalniz haber 60 gunden yeniyse (gun-ici veri var)
        gun_yas = (now - ht).days
        if gun_yas <= 58:
            if r.get("fiyat_30dk") is None and (now - ht) >= timedelta(minutes=30):
                p = _intraday_price_at(ticker, ht + timedelta(minutes=30))
                if p is not None:
                    alanlar["fiyat_30dk"] = round(p, 2)
            if r.get("fiyat_2saat") is None and (now - ht) >= timedelta(hours=2):
                p = _intraday_price_at(ticker, ht + timedelta(hours=2))
                if p is not None:
                    alanlar["fiyat_2saat"] = round(p, 2)

        # 1 gun: haber gununden sonraki ilk islem gunu kapanisi
        if r.get("fiyat_1gun") is None and (now - ht) >= timedelta(days=1):
            p = _daily_close_after(ticker, ht.date(), ofset_gun=1)
            if p is not None:
                alanlar["fiyat_1gun"] = round(p, 2)
                ani = r.get("fiyat_haber_ani")
                if ani:
                    alanlar["etki_yuzde_1gun"] = round((p - ani) / ani * 100, 2)

        if alanlar:
            db.update_haber_etki(r["id"], **alanlar)
            guncellenen += 1
            if verbose:
                print(f"  {ticker:7} {r['haber_tarihi']} -> {alanlar}")

    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] {guncellenen} kayit guncellendi.")
    return guncellenen


if __name__ == "__main__":
    run()
