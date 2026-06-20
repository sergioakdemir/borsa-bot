"""Makro veri: TR faiz (10 yillik tahvil getirisi) ve USD/TRY.

Birincil kaynak: investing.com (tr.investing.com) - KAP proxy fallback ile.
EVDS (TCMB) su an erisilemez oldugundan beklemede; anahtar + uygun ag gelince
_evds_series ile devreye alinabilir.

Genel piyasa baglami olarak commentary.py payload'ina eklenir (hisseye ozel degil).
"""
import os
import re
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Istanbul")

_INVESTING = {
    "usdtry": "https://tr.investing.com/currencies/usd-try",
    "tr_10y_faiz": "https://tr.investing.com/rates-bonds/turkey-10-year-bond-yield",
}

# kucuk TTL onbellek (sayfalari her cagride tekrar cekme)
_CACHE = {}
_TTL = 300.0  # saniye


def _num(s):
    """'46,4339' / '1.234,56' -> float (TR ondalik: virgul = ondalik nokta)."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")   # 1.234,56 -> 1234.56
    elif "," in s:
        s = s.replace(",", ".")                     # 46,4339 -> 46.4339
    try:
        return round(float(s), 4)
    except ValueError:
        return None


def _proxies():
    url = os.environ.get("KAP_PROXY_URL")
    return {"http": url, "https": url} if url else None


def _fetch(url, timeout=20):
    """Sayfayi getirir: once dogrudan, sonra KAP proxy. Basarisizsa None."""
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


def _investing_last(url):
    """investing.com enstruman sayfasindan 'son fiyat' degerini parse eder."""
    html = _fetch(url)
    if not html:
        return None
    m = re.search(r'data-test="instrument-price-last"[^>]*>([^<]+)<', html)
    if not m:
        m = re.search(r'"last"\s*:\s*"?([\d.,]+)', html)
    return _num(m.group(1)) if m else None


def get_macro() -> dict:
    """Makro gostergeleri dondurur (investing.com). Hicbiri gelmezse available=False."""
    now = time.monotonic()
    hit = _CACHE.get("macro")
    if hit and (now - hit[0]) < _TTL:
        return hit[1]

    out = {"available": False, "kaynak": "investing.com"}
    for ad, url in _INVESTING.items():
        out[ad] = _investing_last(url)
    if any(out.get(a) is not None for a in _INVESTING):
        out["available"] = True
    else:
        out["neden"] = "investing.com makro verisi alinamadi"

    _CACHE["macro"] = (now, out)
    return out


# ---------------------------------------------------------------------------
# EVDS (TCMB) - su an beklemede (yurt disi IP cografi engeli + proxy host kisiti).
# EVDS_API_KEY + EVDS'ye izin veren bir proxy varsa ileride devreye alinabilir.
# ---------------------------------------------------------------------------
_EVDS_BASE = "https://evds2.tcmb.gov.tr/service/evds"
_EVDS_SERIES = {"usdtry": "TP.DK.USD.A.YTL",
                "politika_faizi": "TP.APIFON4",
                "tufe_yillik": "TP.FG.J0"}


def _evds_series(code: str, key: str):
    import requests as rq
    today = datetime.now(_TZ).date()
    start = (today - timedelta(days=45)).strftime("%d-%m-%Y")
    end = today.strftime("%d-%m-%Y")
    url = (f"{_EVDS_BASE}/series={code}&startDate={start}&endDate={end}"
           f"&type=json&key={key}")
    px = os.environ.get("EVDS_PROXY_URL")
    for proxies in (None, ({"http": px, "https": px} if px else None)):
        try:
            r = rq.get(url, headers={"key": key, "Accept": "application/json",
                                     "User-Agent": "borsa-bot/1.0"},
                       proxies=proxies, timeout=15)
            if r.status_code != 200 or "json" not in r.headers.get("content-type", "").lower():
                continue
            items = (r.json() or {}).get("items") or []
            for row in reversed(items):
                for k, v in row.items():
                    if k in ("Tarih", "UNIXTIME") or v in (None, "", "null"):
                        continue
                    try:
                        return round(float(v), 4)
                    except (TypeError, ValueError):
                        return v
        except Exception:
            continue
    return None
