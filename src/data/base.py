"""Veri kaynagi soyutlamasi. Tum botlar bu arayuz uzerinden veri ceker;
kaynagi degistirmek = yeni bir alt sinif yazmak."""
from abc import ABC, abstractmethod
import pandas as pd


class DataSource(ABC):
    @abstractmethod
    def get_history(
        self,
        symbol: str,
        start: str,
        end: str | None = None,
        interval: str = "1d",
    ) -> pd.DataFrame:
        """OHLCV gecmis verisi dondurur (DatetimeIndex'li DataFrame)."""
        ...
