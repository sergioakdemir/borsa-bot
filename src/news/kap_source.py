"""KAP (kap.org.tr) bildirim kaynagi.

NOT: KAP bildirim API'si (/tr/api/disclosures) datacenter/yurt disi IP'lerine
karsi korumalidir; TR disi sunuculardan baglanti resetlenir. Erisilemezse
NewsSourceUnavailable firlatilir. TR IP / proxy ile erisim saglandiginda calisir.
Parser KAP alan adlarina gore yazildi; gercek yanit gorulunce ince ayar gerekebilir.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from .base import NewsSource, NewsItem, NewsSourceUnavailable
from ..markets.bist import BIST

KAP_DISCLOSURES_URL = "https://www.kap.org.tr/tr/api/disclosures"
_TZ = ZoneInfo("Europe/Istanbul")


def _parse_publish_date(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):           # epoch ms
        return datetime.fromtimestamp(val / 1000, tz=_TZ)
    s = str(val).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_TZ)
        except ValueError:
            continue
    return None


class KAPSource(NewsSource):
    def __init__(self, timeout: int = 20):
        self.timeout = timeout
        self.market = BIST()

    def _fetch(self):
        try:
            from curl_cffi import requests as creq
        except ImportError as e:
            raise NewsSourceUnavailable("curl_cffi kurulu degil") from e
        try:
            r = creq.get(KAP_DISCLOSURES_URL, impersonate="chrome", timeout=self.timeout)
        except Exception as e:
            raise NewsSourceUnavailable(
                "KAP bildirim API'sine ulasilamadi (muhtemel cografi/IP engeli - "
                f"TR disi sunucu). {type(e).__name__}: {str(e)[:120]}"
            ) from e
        if r.status_code != 200:
            raise NewsSourceUnavailable(f"KAP HTTP {r.status_code}")
        try:
            return r.json()
        except Exception as e:
            raise NewsSourceUnavailable("KAP yaniti JSON degil") from e

    def get_news(self, ticker: str, limit: int = 20) -> list[NewsItem]:
        ticker = ticker.upper().replace(".IS", "")
        raw = self._fetch()
        rows = raw if isinstance(raw, list) else raw.get("disclosures", [])
        items = []
        for d in rows:
            codes = str(d.get("stockCodes") or d.get("relatedStocks") or "")
            if ticker not in codes.upper():
                continue
            di = d.get("disclosureIndex")
            items.append(NewsItem(
                ticker=ticker,
                symbol=self.market.to_symbol(ticker),
                title=d.get("title") or d.get("kapTitle") or "(baslik yok)",
                published_at=_parse_publish_date(d.get("publishDate")) or datetime.now(_TZ),
                source="KAP",
                url=f"https://www.kap.org.tr/tr/Bildirim/{di}" if di else None,
                summary=d.get("summary"),
                disclosure_id=str(di) if di else None,
            ))
            if len(items) >= limit:
                break
        return items
