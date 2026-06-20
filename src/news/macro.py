"""TCMB EVDS makro verisi: USD/TRY, politika faizi, enflasyon (TUFE).

EVDS API'si erisim icin bir anahtar ister (https://evds2.tcmb.gov.tr -> uyelik).
Anahtar .env'de EVDS_API_KEY olarak verilirse cekilir; yoksa sessizce
'available: False' doner ve analiz zinciri makro veri olmadan calisir.

Genel piyasa baglami olarak commentary.py payload'ina eklenir (hisseye ozel degil).
"""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Istanbul")
_BASE = "https://evds2.tcmb.gov.tr/service/evds"

# Seri kodlari: USD/TRY alis, 1 haftalik repo (politika faizi), TUFE yillik %
_SERIES = {
    "usdtry": "TP.DK.USD.A.YTL",
    "politika_faizi": "TP.APIFON4",
    "tufe_yillik": "TP.FG.J0",
}


def _transports():
    """EVDS icin denenecek baglanti yollari (sirayla): dogrudan + proxy.

    EVDS_PROXY_URL verilirse o, yoksa KAP_PROXY_URL denenir. Boylece EVDS host'una
    izin veren bir TR proxy varsa otomatik kullanilir; yoksa dogrudan denenir.
    """
    yield None                                       # 1) dogrudan
    px = os.environ.get("EVDS_PROXY_URL") or os.environ.get("KAP_PROXY_URL")
    if px:
        yield {"http": px, "https": px}              # 2) proxy (KAP ile ayni)


def _fetch_series(code: str, key: str):
    """Bir seri icin son degeri dondurur (yoksa None).

    Once dogrudan, sonra proxy uzerinden denenir; JSON donen ilk yol kullanilir.
    EVDS yurt disi/datacenter IP'den JSON yerine web SPA (HTML) dondururse veya
    proxy host'u engellerse nazikce None doner.
    """
    import requests as rq
    today = datetime.now(_TZ).date()
    start = (today - timedelta(days=45)).strftime("%d-%m-%Y")
    end = today.strftime("%d-%m-%Y")
    url = (f"{_BASE}/series={code}&startDate={start}&endDate={end}"
           f"&type=json&key={key}")
    data = None
    for proxies in _transports():
        try:
            r = rq.get(url, headers={"key": key, "Accept": "application/json",
                                     "User-Agent": "borsa-bot/1.0"},
                       proxies=proxies, timeout=15)
            if r.status_code != 200:
                continue
            if "json" not in r.headers.get("content-type", "").lower():
                continue                              # HTML SPA -> bu yol calismadi
            data = r.json()
            break
        except Exception:
            continue
    if data is None:
        return None
    items = (data or {}).get("items") or []
    # son dolu degeri bul
    for row in reversed(items):
        for k, v in row.items():
            if k in ("Tarih", "UNIXTIME"):
                continue
            if v not in (None, "", "null"):
                try:
                    return round(float(v), 4)
                except (TypeError, ValueError):
                    return v
    return None


def get_macro() -> dict:
    """Makro gostergeleri dondurur. Anahtar yoksa available=False."""
    key = os.environ.get("EVDS_API_KEY")
    if not key:
        return {"available": False, "neden": "EVDS_API_KEY tanimli degil"}
    out = {"available": True}
    for ad, code in _SERIES.items():
        out[ad] = _fetch_series(code, key)
    # hicbiri gelmediyse erisim sorunu say
    if all(out.get(a) is None for a in _SERIES):
        return {"available": False, "neden": "EVDS yanit vermedi"}
    return out
