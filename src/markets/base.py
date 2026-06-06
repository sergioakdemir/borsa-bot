"""Piyasa (market) soyutlamasi. Her piyasa kendi sembol bicimini bilir."""
from abc import ABC, abstractmethod


class Market(ABC):
    name: str
    currency: str
    timezone: str

    @abstractmethod
    def to_symbol(self, ticker: str) -> str:
        """Yerel ticker'i veri kaynagi formatina cevirir (or. THYAO -> THYAO.IS)."""
        ...
