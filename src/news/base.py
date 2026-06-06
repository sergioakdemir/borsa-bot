"""Haber kaynagi soyutlamasi. Tum filtre bu arayuz uzerinden calisir;
kaynagi degistirmek (KAP -> baska) yeni bir alt sinif yazmaktir."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class NewsItem:
    ticker: str
    symbol: str
    title: str
    published_at: datetime          # tz-aware (Europe/Istanbul)
    source: str = "KAP"
    url: str | None = None
    summary: str | None = None
    disclosure_id: str | None = None


class NewsSourceUnavailable(RuntimeError):
    """Haber kaynagina ulasilamadiginda firlatilir (or. KAP cografi engeli)."""


class NewsSource(ABC):
    @abstractmethod
    def get_news(self, ticker: str, limit: int = 20) -> list[NewsItem]:
        ...
