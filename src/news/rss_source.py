"""RSS tabanli haber kaynaklari (Bloomberg HT, Investing.com TR, Mynet Finans).

Her kaynak icin:
  - Son 24 saatin haberlerini ceker (tek seferde tum feed, surec ici onbellek).
  - Hisse adi/kodu gecen haberleri filtreler (Turkce duyarsiz, kelime sinirli).
  - NewsItem'e cevirir; commentary.py mevcut filtrelerle (tazelik 0-1-2 +
    fiyatlanma) isler.

TR siteleri datacenter IP'sinden engelleyebilir; bu yuzden once dogrudan,
sonra KAP_PROXY_URL uzerinden denenir. Ulasilamayan feed sessizce atlanir.
"""
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .base import NewsSource, NewsItem
from ..markets.bist import BIST

_TZ = ZoneInfo("Europe/Istanbul")

# Varsayilan RSS kaynaklari (ad, url). Mynet RSS su an 403/404 donebilir;
# erisilemezse sessizce bos doner (URL/erisim duzeldiginde otomatik calisir).
DEFAULT_FEEDS = [
    {"ad": "BloombergHT", "url": "https://www.bloomberght.com/rss"},
    {"ad": "Investing", "url": "https://tr.investing.com/rss/news.rss"},
    {"ad": "Mynet", "url": "https://finans.mynet.com/borsa/rss/"},
]

# ticker -> haber metninde aranacak ayirt edici anahtar kelimeler (kod + kisa ad).
# Kisa/jenerik adlar (Koc, Is) yerine ayirt edici biçim kullanilir.
COMPANY_KEYWORDS = {
    "THYAO": ["THYAO", "Türk Hava Yolları", "THY"],
    "GARAN": ["GARAN", "Garanti BBVA", "Garanti Bankası"],
    "ASELS": ["ASELS", "Aselsan"],
    "KCHOL": ["KCHOL", "Koç Holding"],
    "TUPRS": ["TUPRS", "Tüpraş"],
    "EREGL": ["EREGL", "Ereğli Demir", "Erdemir"],
    "AKBNK": ["AKBNK", "Akbank"],
    "YKBNK": ["YKBNK", "Yapı Kredi"],
    "SISE": ["SISE", "Şişecam"],
    "TCELL": ["TCELL", "Turkcell"],
    "BIMAS": ["BIMAS", "BİM Mağazalar", "BİM"],
    "FROTO": ["FROTO", "Ford Otosan"],
    "TOASO": ["TOASO", "Tofaş"],
    "KOZAL": ["KOZAL", "Koza Altın"],
    "EKGYO": ["EKGYO", "Emlak Konut"],
    "PETKM": ["PETKM", "Petkim"],
    "ARCLK": ["ARCLK", "Arçelik"],
    "SAHOL": ["SAHOL", "Sabancı Holding"],
    "HALKB": ["HALKB", "Halkbank"],
    "VAKBN": ["VAKBN", "VakıfBank"],
    "ISCTR": ["ISCTR", "İş Bankası"],
    "TAVHL": ["TAVHL", "TAV Havalimanları", "TAV"],
    "PGSUS": ["PGSUS", "Pegasus"],
    "MGROS": ["MGROS", "Migros"],
    "ULKER": ["ULKER", "Ülker"],
    "CCOLA": ["CCOLA", "Coca-Cola İçecek"],
    "DOHOL": ["DOHOL", "Doğan Holding"],
    "ENKAI": ["ENKAI", "Enka İnşaat", "Enka"],
    "KORDS": ["KORDS", "Kordsa"],
    "TTKOM": ["TTKOM", "Türk Telekom"],
}


def _norm(s: str) -> str:
    s = s or ""
    for a, b in (("İ", "i"), ("I", "ı")):
        s = s.replace(a, b)
    s = s.lower()
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"),
                 ("ü", "u"), ("ö", "o"), ("ç", "c"), ("â", "a")):
        s = s.replace(a, b)
    return s


def keywords_for(ticker: str) -> list[str]:
    t = (ticker or "").upper().replace(".IS", "")
    return COMPANY_KEYWORDS.get(t, [t])


def mentions(text: str, ticker: str) -> bool:
    """Metin (baslik+ozet) ilgili hisseyi aniyor mu? Kelime sinirli, TR duyarsiz."""
    nt = _norm(text)
    for kw in keywords_for(ticker):
        if re.search(r"\b" + re.escape(_norm(kw)) + r"\b", nt):
            return True
    return False


def _proxies():
    url = os.environ.get("KAP_PROXY_URL")
    return {"http": url, "https": url} if url else None


def _fetch(url: str, timeout: int = 18):
    """Feed metnini dondurur (once dogrudan, sonra proxy). Basarisizsa None."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return None
    for proxies in (None, _proxies()):
        try:
            r = creq.get(url, impersonate="chrome", proxies=proxies, timeout=timeout)
            if r.status_code == 200 and r.text:
                return r.text
        except Exception:
            continue
    return None


def _parse_dt(entry):
    """feedparser entry -> tz-aware Europe/Istanbul datetime (yoksa simdi)."""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                dt = datetime(*st[:6], tzinfo=timezone.utc)
                return dt.astimezone(_TZ)
            except Exception:
                pass
    return datetime.now(_TZ)


class RSSNewsSource(NewsSource):
    """Birden cok RSS feed'inden 24 saatlik haberleri toplar ve hisseye gore filtreler."""

    IS_SAMPLE = False

    def __init__(self, feeds=None, within_hours: int = 24):
        self.feeds = feeds or DEFAULT_FEEDS
        self.within_hours = within_hours
        self.market = BIST()
        self._entries = None      # surec ici onbellek: tum feed girdileri

    def _all_entries(self) -> list[dict]:
        if self._entries is not None:
            return self._entries
        import feedparser
        cutoff = datetime.now(_TZ) - timedelta(hours=self.within_hours)
        out = []
        for feed in self.feeds:
            text = _fetch(feed["url"])
            if not text:
                continue
            try:
                parsed = feedparser.parse(text)
            except Exception:
                continue
            for e in parsed.entries:
                dt = _parse_dt(e)
                if dt < cutoff:
                    continue
                title = (e.get("title") or "").strip()
                summary = re.sub(r"<[^>]+>", "", e.get("summary") or "").strip()
                if not title:
                    continue
                out.append({
                    "kaynak": feed["ad"],
                    "baslik": title,
                    "ozet": summary,
                    "link": e.get("link"),
                    "tarih": dt,
                })
        self._entries = out
        return out

    def get_news(self, ticker: str, limit: int = 20) -> list[NewsItem]:
        ticker = ticker.upper().replace(".IS", "")
        symbol = self.market.to_symbol(ticker)
        items = []
        for e in self._all_entries():
            text = f"{e['baslik']} {e['ozet']}"
            if not mentions(text, ticker):
                continue
            items.append(NewsItem(
                ticker=ticker, symbol=symbol,
                title=e["baslik"], published_at=e["tarih"],
                source=e["kaynak"], url=e.get("link"),
                summary=e.get("ozet") or None,
                disclosure_id=f"{e['kaynak']}:{abs(hash(e['baslik'])) % 10**10}",
            ))
            if len(items) >= limit:
                break
        return items

    def recent_count(self) -> int:
        """Toplam cekilen (24s) haber sayisi - teshis icin."""
        return len(self._all_entries())
