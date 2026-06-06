"""Veri kaynagini tek noktadan secer. Kaynak degisecekse sadece burasi degisir."""
from .base import DataSource
from .yfinance_source import YFinanceSource

_SOURCES = {
    "yfinance": YFinanceSource,
}


def get_data_source(name: str = "yfinance") -> DataSource:
    try:
        return _SOURCES[name]()
    except KeyError:
        raise ValueError(f"Bilinmeyen veri kaynagi: {name}. Mevcut: {list(_SOURCES)}")
