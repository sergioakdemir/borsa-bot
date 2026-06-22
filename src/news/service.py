"""Haber servisi: kaynak secimi (canli KAP -> ornek fallback) ve filtreden
gecen (ESKI olmayan) haberleri compact formatta dondurme.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from .kap_source import KAPSource
from .sample_source import SampleNewsSource
from .base import NewsSourceUnavailable
from .freshness import check_news_freshness
from .priced_in import check_priced_in

_TZ = ZoneInfo("Europe/Istanbul")


def get_news_source(verbose: bool = True):
    """(source, is_sample) dondurur. Canli KAP denenir, engelliyse ornek."""
    try:
        kap = KAPSource(timeout=12)
        kap.get_news("THYAO", limit=1)
        if verbose:
            print("  [haber] KAP CANLI kaynak kullaniliyor.")
        return kap, False
    except NewsSourceUnavailable as e:
        if verbose:
            print(f"  [haber] KAP erisilemedi -> ORNEK kaynak. ({str(e)[:55]})")
        return SampleNewsSource(), True


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
