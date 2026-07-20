"""Haber servisi: kaynak secimi (canli KAP -> URETIMDE bos) ve filtreden
gecen (ESKI olmayan) haberleri compact formatta dondurme.

KAP erisilemezse URETIMDE haber listesi BOS doner (sahte/ORNEK icerik akisa
girmez). ORNEK kaynak yalniz test/gelistirme icin, BORSA_ORNEK_HABER env
degiskeni acikken devreye girer.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from .kap_source import KAPSource
from .sample_source import SampleNewsSource
from .base import NewsSource, NewsSourceUnavailable
from .freshness import check_news_freshness
from .priced_in import check_priced_in

_TZ = ZoneInfo("Europe/Istanbul")


def _ornek_izinli() -> bool:
    """ORNEK (sahte/test) haber kaynagi yalniz env ile acilir; uretimde KAPALI."""
    return os.getenv("BORSA_ORNEK_HABER", "").strip().lower() in (
        "1", "true", "yes", "on")


class BosHaberKaynagi(NewsSource):
    """Uretim fallback'i: KAP yoksa haber YOK. Sahte icerik uretmez, [] doner."""
    IS_SAMPLE = False

    def get_news(self, ticker: str, limit: int = 20) -> list:
        return []


def _kap_bayrak_yaz(deger: str):
    """KAP son-deneme bayragini yaz ('1' = son deneme basarisiz, '0' = basarili).

    Eskiden bu bayrak bir kez '1' olunca gun sonuna kadar temizlenmiyordu; tek
    bir gecici hata gun boyu 'KAP erisilemiyor' alarmi uretiyordu (20 Tem 2026:
    kap_ok=2974 / kap_fail=82 iken 6 saat boyunca yanlis alarm). Artik her
    deneme bayragi gunceller. Bayrak yazimi haber akisini asla bozmamali."""
    try:
        from src.db import database as _db
        _db.set_setting(f"kap_ornek:{datetime.now(_TZ).date().isoformat()}", deger)
    except Exception:
        pass


def get_news_source(verbose: bool = True):
    """(source, kap_yok) dondurur. Canli KAP denenir; erisilemezse URETIMDE bos
    kaynak (BosHaberKaynagi), yalniz BORSA_ORNEK_HABER env'i acikken ORNEK kaynak.
    Ikinci deger True = KAP canli degil (tarama/uyari bunu gorup atlar)."""
    try:
        kap = KAPSource(timeout=12)
        kap.get_news("THYAO", limit=1)
        _kap_bayrak_yaz("0")          # basarili -> bayrak TEMIZLENIR (yapiskan degil)
        if verbose:
            print("  [haber] KAP CANLI kaynak kullaniliyor.")
        return kap, False
    except NewsSourceUnavailable as e:
        # Bayrak SON denemenin durumunu tutar (gun boyu yapismaz). Tek bir gecici
        # 429/reset gun boyu alarm uretmemeli; health_monitor bu bayragi bugunku
        # kap_ok/kap_fail oraniyla birlikte degerlendirir.
        _kap_bayrak_yaz("1")
        if _ornek_izinli():                       # yalniz test/gelistirme
            if verbose:
                print(f"  [haber] KAP erisilemedi -> ORNEK kaynak (TEST). ({str(e)[:55]})")
            return SampleNewsSource(), True
        # URETIM: sahte icerik yok -> bos haber kaynagi (KAP canli degil = True)
        if verbose:
            print(f"  [haber] KAP erisilemedi -> haber YOK (uretim). ({str(e)[:55]})")
        return BosHaberKaynagi(), True


def filtered_news(ticker: str, source=None, now=None, exclude_stale: bool = True) -> list[dict]:
    """Filtreden gecen haberleri compact dict listesi olarak dondurur.

    ESKI haberler elenir (yeni sanilmasin). Her haber: baslik, tarih, tazelik,
    fiyatlanma (FIYATLANDI/FIYATLANMADI/VERI_YOK).
    """
    now = now or datetime.now(_TZ)
    if source is None:
        source, _ = get_news_source(verbose=False)

    out = []
    for it in source.get_news(ticker, limit=10):
        fr = check_news_freshness(it.published_at, now=now)
        if exclude_stale and fr.status.value == "ESKI":
            continue
        pi = check_priced_in(it)
        out.append({
            "baslik": it.title,
            "tarih": it.published_at.strftime("%Y-%m-%d %H:%M"),
            "tazelik": fr.status.value,
            "fiyatlanma": pi.status,
            "disclosure_id": it.disclosure_id,   # dedup anahtari icin
            "url": it.url,
        })
    return out
