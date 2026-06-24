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

# Yahoo Finance genel akisi (yalniz market_news icin; hisse adi tasimaz).
_YAHOO_GENERAL = {"ad": "Yahoo Finance", "url": "https://finance.yahoo.com/news/rssindex"}

# Genel ABD finans akislari. HEM market_news (24s gundem) HEM ticker_news
# (hisse adi gecenler) icin kullanilir. Erisilemeyen feed sessizce atlanir.
# feeds.reuters.com Reuters tarafindan kapatildi -> yerine CNBC/MarketWatch vb.
_GENERAL_FEEDS = [
    {"ad": "Investing", "url": "https://www.investing.com/rss/news.rss"},
    {"ad": "CNBC", "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml"
                          "?partnerId=wrss01&id=10000664"},
    {"ad": "MarketWatch", "url": "https://feeds.marketwatch.com/marketwatch/topstories"},
    {"ad": "Seeking Alpha", "url": "https://seekingalpha.com/market_currents.xml"},
    {"ad": "Benzinga", "url": "https://www.benzinga.com/feed"},
]

# Hisse-bazli ek (sembol disinda) arama icin kisa sirket adlari.
_US_KEYWORDS = {
    "NVDA": ["NVDA", "Nvidia"],
    "SPCX": ["SPCX", "SpaceX"],
    "RXT": ["RXT", "Rackspace"],
    "CNCK": ["CNCK", "Cinacor", "Coincheck"],
    # Ufuk'un teknoloji listesi (AI/cip/kuantum/uzay/robotik)
    "AMD": ["AMD", "Advanced Micro Devices"],
    "TSM": ["TSM", "TSMC", "Taiwan Semiconductor"],
    "ASML": ["ASML"],
    "RKLB": ["RKLB", "Rocket Lab"],
    "OSS": ["OSS", "One Stop Systems"],
    "IONQ": ["IONQ", "IonQ"],
    "RGTI": ["RGTI", "Rigetti"],
    "ACHR": ["ACHR", "Archer Aviation"],
    "BFLY": ["BFLY", "Butterfly Network"],
    "MU": ["MU", "Micron"],
}

# Akademik + kurum RSS kaynaklari (AI/cip/kuantum/robotik/uzay/makro). BIST
# haberlerinden AYRI 'akademik_ve_kurum' kategorisinde tutulur; Ufuk'un teknoloji
# odakli analizine ve ABD brifingine baglam saglar. Erisilemeyen feed atlanir.
_ACADEMIC_FEEDS = [
    {"ad": "MIT AI", "url": "https://news.mit.edu/rss/topic/artificial-intelligence-2"},
    {"ad": "Berkeley BAIR", "url": "https://bair.berkeley.edu/blog/feed.xml"},
    {"ad": "Stanford HAI", "url": "https://hai.stanford.edu/rss.xml"},
    {"ad": "arXiv AI", "url": "https://export.arxiv.org/rss/cs.AI"},
    {"ad": "arXiv Chip", "url": "https://export.arxiv.org/rss/cs.AR"},
    {"ad": "arXiv Quantum", "url": "https://export.arxiv.org/rss/quant-ph"},
    {"ad": "arXiv Robotics", "url": "https://export.arxiv.org/rss/cs.RO"},
    {"ad": "NASA", "url": "https://www.nasa.gov/news-release/feed/"},
    {"ad": "NSF", "url": "https://www.nsf.gov/rss/rss_www_news.xml"},
    {"ad": "DARPA", "url": "https://news.mit.edu/rss/topic/darpa"},
    {"ad": "FED", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"ad": "ECB", "url": "https://www.ecb.europa.eu/rss/press.html"},
    {"ad": "Google News Tech",
     "url": "https://news.google.com/rss/search?q=AI+chip+semiconductor&hl=en"},
    # RSS'i olmayan akademik/kurum kaynaklari Google News aramasi uzerinden takip
    # edilir (konferanslar, varlik fonlari, RSS vermeyen merkez bankalari).
    {"ad": "AI Konferanslari",
     "url": "https://news.google.com/rss/search?q=NeurIPS+OR+ICML+OR+ICLR+CVPR+AI+research&hl=en"},
    {"ad": "Norveç Varlık Fonu (NBIM)",
     "url": "https://news.google.com/rss/search?q=Norges+Bank+Investment+Management+NBIM&hl=en"},
    {"ad": "Suudi PIF",
     "url": "https://news.google.com/rss/search?q=Saudi+PIF+Public+Investment+Fund+tech&hl=en"},
    {"ad": "BOJ (Japonya MB)",
     "url": "https://news.google.com/rss/search?q=Bank+of+Japan+BOJ+interest+rate&hl=en"},
    {"ad": "PBOC (Çin MB)",
     "url": "https://news.google.com/rss/search?q=PBOC+China+monetary+policy&hl=en"},
    {"ad": "BOE (İngiltere MB)",
     "url": "https://news.google.com/rss/search?q=Bank+of+England+BOE+rate&hl=en"},
    {"ad": "SNB (İsviçre MB)",
     "url": "https://news.google.com/rss/search?q=Swiss+National+Bank+SNB&hl=en"},
]

# Semantic Scholar arama API'si (RSS degil; JSON doner).
_SEMANTIC_SCHOLAR = ("https://api.semanticscholar.org/graph/v1/paper/search"
                     "?query=AI+semiconductor&limit=5&fields=title,year,abstract")


def _fetch(url: str, timeout: int = 15):
    """Feed metnini dondurur (curl_cffi, chrome taklidi). Basarisizsa None."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return None
    try:
        r = creq.get(url, impersonate="chrome", timeout=timeout, max_redirects=5)
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

    # 2) Genel ABD finans akislari (Investing/CNBC/MarketWatch/SeekingAlpha/Benzinga)
    #    -> yalniz hisse adi/kodu gecen haberler
    for feed in _GENERAL_FEEDS:
        for e in _entries(feed["url"]):
            if e["tarih"] < cutoff:
                continue
            if not _mentions(f"{e['baslik']} {e['ozet']}", ticker):
                continue
            key = e["baslik"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(_rec(e, feed["ad"]))

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
    for feed in [_YAHOO_GENERAL] + _GENERAL_FEEDS:
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


def _semantic_scholar(limit: int = 5) -> list[dict]:
    """Semantic Scholar arama API'si (JSON) -> akademik makale kayitlari.
    Tarih filtresine tabi degildir (makaleler yil bazli)."""
    text = _fetch(_SEMANTIC_SCHOLAR)
    if not text:
        return []
    try:
        import json
        data = json.loads(text)
    except Exception:
        return []
    out = []
    for p in (data.get("data") or [])[:limit]:
        baslik = (p.get("title") or "").strip()
        if not baslik:
            continue
        yil = p.get("year")
        out.append({
            "baslik": baslik, "kaynak": "Semantic Scholar",
            "kategori": "akademik_ve_kurum",
            "tarih": f"{yil}-01-01 00:00" if yil else "",
            "ozet": (p.get("abstract") or None), "url": None,
        })
    return out


def academic_news(within_hours: int = 24, limit: int = 10) -> list[dict]:
    """Akademik + kurum kaynaklarindan (MIT, Berkeley, Stanford, arXiv, NASA, NSF,
    DARPA, FED, ECB, Google News) son `within_hours` saatteki haberler.

    Dondurur: [{baslik, kaynak, kategori='akademik_ve_kurum', tarih, ozet, url}].
    BIST haberlerinden ayridir; AI baglamina/ABD brifingine eklenir. Semantic
    Scholar makaleleri (tarih filtresiz) sona eklenir. Erisilemeyen feed atlanir.
    """
    cutoff = datetime.now(_TZ) - timedelta(hours=within_hours)
    seen, out = set(), []
    for feed in _ACADEMIC_FEEDS:
        for e in _entries(feed["url"]):
            if e["tarih"] < cutoff:
                continue
            key = e["baslik"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "baslik": e["baslik"], "kaynak": feed["ad"],
                "kategori": "akademik_ve_kurum",
                "tarih": e["tarih"].strftime("%Y-%m-%d %H:%M"),
                "ozet": e.get("ozet") or None, "url": e.get("link"),
            })
    out.sort(key=lambda r: r["tarih"], reverse=True)
    # Semantic Scholar makaleleri (yil bazli; en sona)
    for p in _semantic_scholar():
        key = p["baslik"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out[:limit]


if __name__ == "__main__":
    import json
    import sys
    if sys.argv[1:] and sys.argv[1] == "market":
        print(json.dumps(market_news(), ensure_ascii=False, indent=2))
    elif sys.argv[1:] and sys.argv[1] in ("academic", "akademik"):
        print(json.dumps(academic_news(), ensure_ascii=False, indent=2))
    else:
        tk = sys.argv[1] if sys.argv[1:] else "NVDA"
        print(json.dumps(ticker_news(tk), ensure_ascii=False, indent=2))
