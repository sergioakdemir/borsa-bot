"""KAP (kap.org.tr) bildirim kaynagi.

KAP yeni sitesi (Next.js) datacenter/yurt disi IP'lerine karsi korumalidir;
TR disi sunuculardan baglanti resetlenir. Erisim icin .env'deki KAP_PROXY_URL
(TR cikisli proxy) ve bir "KAP" oturum cerezi gerekir. Akis:

  1) Oturum cerezi al  (anasayfayi GET et)
  2) ticker -> mkkMemberOid coz  (POST api/search/combined)
  3) Sirket bildirimlerini cek  (GET api/company-detail/sgbf-data/<oid>/ALL/<gun>)

Erisilemezse NewsSourceUnavailable firlatilir.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from .base import NewsSource, NewsItem, NewsSourceUnavailable
from ..markets.bist import BIST

_BASE = "https://www.kap.org.tr"
_SEARCH_URL = _BASE + "/tr/api/search/combined"
_SGBF_URL = _BASE + "/tr/api/company-detail/sgbf-data/{oid}/{cls}/{days}"
_PRIME_URL = _BASE + "/tr"
_TZ = ZoneInfo("Europe/Istanbul")

# ticker -> mkkMemberOid surec ici onbellek (her cron kosusunda yeniden dolar)
_OID_CACHE: dict[str, str] = {}


def _parse_publish_date(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):           # epoch ms
        return datetime.fromtimestamp(val / 1000, tz=_TZ)
    s = str(val).strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=_TZ)
        except ValueError:
            continue
    return None


class KAPSource(NewsSource):
    def __init__(self, timeout: int = 20, days: int = 30):
        self.timeout = timeout
        self.days = days
        self.market = BIST()
        self._session = None

    # --- altyapi -----------------------------------------------------------
    def _proxies(self):
        url = os.environ.get("KAP_PROXY_URL")
        return {"http": url, "https": url} if url else None

    def _get_session(self):
        """curl_cffi oturumu: proxy + chrome taklidi + KAP oturum cerezi."""
        if self._session is not None:
            return self._session
        try:
            from curl_cffi import requests as creq
        except ImportError as e:
            raise NewsSourceUnavailable("curl_cffi kurulu degil") from e
        s = creq.Session(impersonate="chrome", proxies=self._proxies(),
                         timeout=self.timeout)
        try:
            s.get(_PRIME_URL)                    # KAP oturum cerezini al
        except Exception as e:
            raise NewsSourceUnavailable(
                "KAP'a ulasilamadi (muhtemel cografi/IP engeli - TR proxy gerekli). "
                f"{type(e).__name__}: {str(e)[:100]}"
            ) from e
        if "KAP" not in s.cookies:
            raise NewsSourceUnavailable("KAP oturum cerezi alinamadi")
        self._session = s
        return s

    def _headers(self):
        return {"Accept": "application/json", "Accept-Language": "tr",
                "Referer": _BASE + "/tr/bildirim-sorgu", "Origin": _BASE}

    # --- ticker -> mkkMemberOid -------------------------------------------
    def _resolve_oid(self, ticker: str) -> str:
        if ticker in _OID_CACHE:
            return _OID_CACHE[ticker]
        s = self._get_session()
        try:
            r = s.post(_SEARCH_URL, headers={**self._headers(),
                       "Content-Type": "application/json"}, json={"keyword": ticker})
            data = r.json()
        except Exception as e:
            raise NewsSourceUnavailable(
                f"KAP arama basarisiz ({ticker}): {type(e).__name__}: {str(e)[:80]}"
            ) from e
        # companyOrFunds icinden kodu birebir eslesen sirketi sec
        for cat in (data if isinstance(data, list) else []):
            for res in cat.get("results", []):
                if res.get("searchType") != "C":
                    continue
                code = str(res.get("cmpOrFundCode") or "").upper()
                oid = res.get("memberOrFundOid")
                if oid and code == ticker:
                    _OID_CACHE[ticker] = oid
                    return oid
        raise NewsSourceUnavailable(f"KAP'ta '{ticker}' kodu bulunamadi")

    # --- bildirim cekme ----------------------------------------------------
    def _fetch(self, oid: str) -> list:
        s = self._get_session()
        url = _SGBF_URL.format(oid=oid, cls="ALL", days=self.days)
        try:
            r = s.get(url, headers=self._headers())
        except Exception as e:
            raise NewsSourceUnavailable(
                f"KAP bildirim cekme basarisiz: {type(e).__name__}: {str(e)[:80]}"
            ) from e
        if r.status_code != 200:
            raise NewsSourceUnavailable(f"KAP HTTP {r.status_code}")
        try:
            data = r.json()
        except Exception as e:
            raise NewsSourceUnavailable("KAP yaniti JSON degil") from e
        return data if isinstance(data, list) else []

    def get_news(self, ticker: str, limit: int = 20) -> list[NewsItem]:
        ticker = ticker.upper().replace(".IS", "")
        oid = self._resolve_oid(ticker)
        rows = self._fetch(oid)
        items = []
        for d in rows:
            b = d.get("disclosureBasic") or {}
            di = b.get("disclosureIndex")
            items.append(NewsItem(
                ticker=ticker,
                symbol=self.market.to_symbol(ticker),
                title=b.get("summary") or b.get("title") or "(baslik yok)",
                published_at=_parse_publish_date(b.get("publishDate")) or datetime.now(_TZ),
                source="KAP",
                url=f"{_BASE}/tr/Bildirim/{di}" if di else None,
                summary=b.get("title"),
                disclosure_id=str(di) if di else None,
            ))
            if len(items) >= limit:
                break
        return items
