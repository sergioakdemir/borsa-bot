"""ABD borsalari (NYSE/NASDAQ). yfinance'te son ek yok."""
from .base import Market


class US(Market):
    name = "US"
    currency = "USD"
    timezone = "America/New_York"

    def to_symbol(self, ticker: str) -> str:
        return ticker.upper().strip()
