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
# EVDS3 (TCMB) - yeni endpoint: POST https://evds3.tcmb.gov.tr/igmevdsms-dis/fe
# (SPA: getSeriVerileri => Le.post("/fe", body)). EVDS_PROXY_URL (TR cikisli)
# + EVDS_API_KEY ile cekilir. Bright Data sertifika MITM yaptigindan verify=False.
# ---------------------------------------------------------------------------
_EVDS3_FE = "https://evds3.tcmb.gov.tr/igmevdsms-dis/fe"
_EVDS_SERIES = {
    "usdtry": ("TP.DK.USD.A.YTL", "avg"),
    "politika_faizi": ("TP.TF.TG.A1", "avg"),
    "tufe_yillik": ("TP.FE.OKTG01", "avg"),
}


def _evds_proxies():
    """EVDS_PROXY_URL'i TR cikisli olacak sekilde dondurur (Bright Data -country-tr)."""
    raw = os.environ.get("EVDS_PROXY_URL")
    if not raw:
        return None
    try:
        pre, rest = raw.split("://", 1)
        cred, host = rest.split("@", 1)
        usr, pw = cred.split(":", 1)
        if ("superproxy" in host or usr.startswith("brd-")) and "-country-" not in usr:
            usr = usr + "-country-tr"
        raw = f"{pre}://{usr}:{pw}@{host}"
    except Exception:
        pass
    return {"http": raw, "https": raw}


def _evds_series(code: str, agg: str, key: str):
    """EVDS3 /fe POST ile bir serinin son degerini dondurur (yoksa None).

    Govde SPA'daki getSeriVerileri ile ayni alanlari tasir. Bright Data residential
    (no-KYC) hesabi TCMB'ye POST'u engelleyebilir (HTTP 402); o durumda None doner.
    """
    import requests as rq
    import urllib3
    urllib3.disable_warnings()
    today = datetime.now(_TZ).date()
    body = {
        "series": code,
        "aggregationTypes": agg or "avg",
        "formulas": "0",
        "startDate": (today - timedelta(days=60)).strftime("%d-%m-%Y"),
        "endDate": today.strftime("%d-%m-%Y"),
        "frequency": "1",
        "decimalSeperator": ".",
        "decimal": False,
    }
    headers = {"key": key, "Accept": "application/json",
               "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    try:
        r = rq.post(_EVDS3_FE, json=body, headers=headers,
                    proxies=_evds_proxies(), timeout=30, verify=False)
        if r.status_code != 200:
            return None
        if "json" not in r.headers.get("content-type", "").lower():
            return None
        data = r.json()
    except Exception:
        return None
    items = data.get("items") or data.get("data") or (data if isinstance(data, list) else [])
    for row in reversed(items):
        if not isinstance(row, dict):
            continue
        for k, v in row.items():
            if any(t in k.upper() for t in ("TARIH", "DATE", "UNIXTIME")):
                continue
            if v in (None, "", "null", "ND"):
                continue
            try:
                return round(float(str(v).replace(",", ".")), 4)
            except (TypeError, ValueError):
                continue
    return None


def evds_macro() -> dict:
    """EVDS3'ten USD/TRY, politika faizi, TUFE ceker (EVDS_API_KEY gerekli)."""
    key = os.environ.get("EVDS_API_KEY")
    if not key:
        return {"available": False, "neden": "EVDS_API_KEY yok"}
    out = {"available": False, "kaynak": "EVDS3"}
    for ad, (code, agg) in _EVDS_SERIES.items():
        out[ad] = _evds_series(code, agg, key)
    if any(out.get(a) is not None for a in _EVDS_SERIES):
        out["available"] = True
    else:
        out["neden"] = "EVDS3 yanit vermedi (muhtemelen proxy POST kisiti / no-KYC)"
    return out
