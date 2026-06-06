"""yfinance tabanli veri kaynagi. Ileride baska kaynakla degistirilebilir."""
import yfinance as yf
import pandas as pd
from .base import DataSource


class YFinanceSource(DataSource):
    def get_history(self, symbol, start, end=None, interval="1d") -> pd.DataFrame:
        df = yf.download(
            symbol,
            start=start,
            end=end,
            interval=interval,
            auto_adjust=True,
            progress=False,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        return df
