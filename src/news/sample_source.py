"""Test/ornek haber kaynagi.

KAP canli API'si TR disi sunucudan engelli oldugunda, tazelik + fiyatlanma
mantigini GERCEK fiyat verisiyle test etmek icin temsili bildirimler dondurur.
Basliklar/tarihler ORNEKTIR (gercek KAP akisi degil); fiyatlanma analizi ise
bu tarihlerdeki GERCEK yfinance fiyatiyla yapilir.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from .base import NewsSource, NewsItem
from ..markets.bist import BIST

_TZ = ZoneInfo("Europe/Istanbul")

# Tarihler gercek fiyat gecmisiyle ortusur; her tazelik sinifindan birer ornek.
_SAMPLE = {
    "THYAO": [
        {"title": "Ornek: Aylik yolcu trafigi aciklamasi",   "date": "2026-06-05 18:10"},
        {"title": "Ornek: Pay geri alim programi guncellemesi", "date": "2026-06-04 10:00"},
        {"title": "Ornek: Ucak alim sozlesmesi duyurusu",     "date": "2024-03-15 18:30"},
    ]
}


def _dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=_TZ)


class SampleNewsSource(NewsSource):
    IS_SAMPLE = True

    def __init__(self):
        self.market = BIST()

    def get_news(self, ticker: str, limit: int = 20) -> list[NewsItem]:
        ticker = ticker.upper().replace(".IS", "")
        rows = _SAMPLE.get(ticker, [])
        out = [
            NewsItem(
                ticker=ticker, symbol=self.market.to_symbol(ticker),
                title=n["title"], published_at=_dt(n["date"]),
                source="ORNEK", disclosure_id=f"sample-{ticker}-{i}",
            )
            for i, n in enumerate(rows)
        ]
        return out[:limit]
