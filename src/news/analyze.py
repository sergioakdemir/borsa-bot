"""Haber filtresi: her haber icin tazelik + fiyatlanma kontrolunu birlestirir."""
from .freshness import check_news_freshness
from .priced_in import check_priced_in


def analyze_news(items, now=None, source=None):
    out = []
    for item in items:
        out.append({
            "item": item,
            "freshness": check_news_freshness(item.published_at, now=now),
            "priced_in": check_priced_in(item, source=source),
        })
    return out
