"""BIST (Borsa Istanbul). yfinance sembolleri .IS son ekiyle gelir."""
from .base import Market


class BIST(Market):
    name = "BIST"
    currency = "TRY"
    timezone = "Europe/Istanbul"

    def to_symbol(self, ticker: str) -> str:
        ticker = ticker.upper().strip()
        return ticker if ticker.endswith(".IS") else f"{ticker}.IS"
