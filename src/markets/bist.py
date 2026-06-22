"""BIST (Borsa Istanbul). yfinance sembolleri .IS son ekiyle gelir."""
from .base import Market


class BIST(Market):
    name = "BIST"
    currency = "TRY"
    timezone = "Europe/Istanbul"

    def to_symbol(self, ticker: str) -> str:
        ticker = ticker.upper().strip()
        if ticker.endswith(".IS"):
            return ticker
        if ticker.endswith(".F"):          # fon/yanlis eki (GMSTR.F -> GMSTR.IS)
            ticker = ticker[:-2]
        return f"{ticker}.IS"
