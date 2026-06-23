"""ABD (US) hisseleri icin Ingilizce RSS haber kaynaklari.

Iki kullanim:
  1. ticker_news(ticker)  : tek hisseye ait son 7 gunluk haberler (Yahoo Finance
     hisse-bazli RSS + Investing.com EN genel akistan hisse adi gecenler).
  2. market_news()        : genel ABD piyasa gundemi (son 24 saat) - Yahoo Finance
     genel + Reuters (erisilebilirse) + Investing.com EN.

Haberler commentary.py'deki mevcut Haiku etki analizinden gecer (Ingilizce sorun
degil). Erisilemeyen feed sessizce atlanir; agir bagimlilik yok (feedparser +
curl_cffi zaten kurulu).
"""
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Istanbul")

# Hisse-bazli Yahoo Finance RSS (sembol icine gomulur).
_YAHOO_TICKER = ("https://feeds.finance.yahoo.com/rss/2.0/headline"
                 "?s={sym}&region=US&lang=en-US")

# Genel ABD piyasa akislari (24 saatlik gundem icin). feeds.reuters.com Reuters
# tarafindan kapatildi; erisilemezse sessizce atlanir, digerleri devreye girer.
_MARKET_FEEDS = [
    {"ad": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex"},
    {"ad": "Reuters", "url": "https://feeds.reuters.com/reuters/businessNews"},
    {"ad": "Investing", "url": "https://www.investing.com/rss/news.rss"},
]

# Hisse-bazli ek (sembol disinda) arama icin kisa sirket adlari.
_US_KEYWORDS = {
    "NVDA": ["NVDA", "Nvidia"],
    "SPCX": ["SPCX", "SpaceX"],
    "RXT": ["RXT", "Rackspace"],
    "CNCK": ["CNCK", "Cinacor", "Coincheck"],
}


def _fetch(url: str, timeout: int = 15):
    """Feed metnini dondurur (curl_cffi, chrome taklidi). Basarisizsa None."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return None
    try:
        r = creq.get(url, impersonate="chrome", timeout=timeout)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception:
        return None
    return None


def _parse_dt(entry):
    """feedparser entry -> tz-aware Europe/Istanbul datetime (yoksa simdi)."""
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            try:
                return datetime(*st[:6], tzinfo=timezone.utc).astimezone(_TZ)
            except Exception:
                pass
    return datetime.now(_TZ)


def _entries(url: str):
    """Bir RSS feed'inin girdilerini (baslik/ozet/link/tarih) dondurur."""
    text = _fetch(url)
    if not text:
        return []
    try:
        import feedparser
        parsed = feedparser.parse(text)
    except Exception:
        return []
    out = []
    for e in parsed.entries:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "baslik": title,
            "ozet": re.sub(r"<[^>]+>", "", e.get("summary") or "").strip(),
            "link": e.get("link"),
            "tarih": _parse_dt(e),
        })
    return out


def _mentions(text: str, ticker: str) -> bool:
    nt = (text or "").lower()
    for kw in _US_KEYWORDS.get(ticker.upper(), [ticker]):
        if re.search(r"\b" + re.escape(kw.lower()) + r"\b", nt):
            return True
    return False


def ticker_news(ticker: str, within_days: int = 7, limit: int = 12) -> list[dict]:
    """Tek ABD hissesine ait son `within_days` gunluk haberler (commentary kaydi
    formatinda: baslik/tarih/kaynak/url/ozet). Erisim yoksa bos liste."""
    ticker = (ticker or "").upper().replace(".IS", "")
    cutoff = datetime.now(_TZ) - timedelta(days=within_days)
    seen, out = set(), []

    # 1) Yahoo Finance hisse-bazli RSS (dogrudan ilgili)
    for e in _entries(_YAHOO_TICKER.format(sym=ticker)):
        if e["tarih"] < cutoff:
            continue
        key = e["baslik"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(_rec(e, "Yahoo Finance"))

    # 2) Investing.com EN genel akis -> yalniz hisse adi gecenler
    for e in _entries("https://www.investing.com/rss/news.rss"):
        if e["tarih"] < cutoff:
            continue
        if not _mentions(f"{e['baslik']} {e['ozet']}", ticker):
            continue
        key = e["baslik"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(_rec(e, "Investing"))

    return out[:limit]


def _rec(e: dict, kaynak: str) -> dict:
    return {
        "baslik": e["baslik"],
        "tarih": e["tarih"].strftime("%Y-%m-%d %H:%M"),
        "kaynak": kaynak,
        "url": e.get("link"),
        "ozet": e.get("ozet") or None,
        "tazelik": "YENI",
        "fiyatlanma": "VERI_YOK",
    }


def market_news(within_hours: int = 24, limit: int = 8) -> list[dict]:
    """Genel ABD piyasa gundemi (son `within_hours` saat). Dondurur:
    [{baslik, kaynak, tarih, url}]. Erisilemeyen feed atlanir."""
    cutoff = datetime.now(_TZ) - timedelta(hours=within_hours)
    seen, out = set(), []
    for feed in _MARKET_FEEDS:
        for e in _entries(feed["url"]):
            if e["tarih"] < cutoff:
                continue
            key = e["baslik"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({"baslik": e["baslik"], "kaynak": feed["ad"],
                        "tarih": e["tarih"].strftime("%Y-%m-%d %H:%M"),
                        "url": e.get("link")})
    # En yeni once
    out.sort(key=lambda r: r["tarih"], reverse=True)
    return out[:limit]


if __name__ == "__main__":
    import json
    import sys
    if sys.argv[1:] and sys.argv[1] == "market":
        print(json.dumps(market_news(), ensure_ascii=False, indent=2))
    else:
        tk = sys.argv[1] if sys.argv[1:] else "NVDA"
        print(json.dumps(ticker_news(tk), ensure_ascii=False, indent=2))
