"""Test/ornek haber kaynagi.

KAP canli API'si TR disi sunucudan engelli oldugunda haber kaynagi olarak
devreye girer. Sahte/temsili ("Ornek:") basliklar URETILMEZ; gercek olmayan
haber kullaniciya gosterilmemelidir. Bu nedenle bos haber listesi doner ve
sistem haber olmadan (yalniz fiyat sinyalleriyle) calisir.
"""
from .base import NewsSource, NewsItem
from ..markets.bist import BIST

# Sahte ornek haberler kaldirildi. Gercek KAP akisi gelene kadar haber yok.
_SAMPLE: dict[str, list] = {}


class SampleNewsSource(NewsSource):
    IS_SAMPLE = True

    def __init__(self):
        self.market = BIST()

    def get_news(self, ticker: str, limit: int = 20) -> list[NewsItem]:
        # Sahte haber uretilmez; KAP yoksa haber listesi bostur.
        return []
