"""Makro veri: TR faiz (10 yillik tahvil getirisi) ve USD/TRY.

Birincil kaynak: investing.com (tr.investing.com) - KAP proxy fallback ile.
EVDS (TCMB) su an erisilemez oldugundan beklemede; anahtar + uygun ag gelince
_evds_series ile devreye alinabilir.

Genel piyasa baglami olarak commentary.py payload'ina eklenir (hisseye ozel degil).
"""
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Istanbul")

_INVESTING = {
    "usdtry": "https://tr.investing.com/currencies/usd-try",
    "tr_10y_faiz": "https://tr.investing.com/rates-bonds/turkey-10-year-bond-yield",
}

# investing.com cekilemezse Yahoo Finance alternatifi (yfinance sembolleri)
_YAHOO = {
    "usdtry": "TRY=X",
    "tr_10y_faiz": "^TYF",
}

# Hicbir kaynak gelmezse kullanilacak son bilinen degerlerin kalici deposu
_SON_BILINEN = Path(__file__).resolve().parents[2] / "data" / "macro_last.json"

# TCMB politika faizi (1 hafta repo): once resmi sayfa, sonra EVDS2
_TCMB_FAIZ_URL = ("https://www.tcmb.gov.tr/wps/wcm/connect/TR/tcmb+tr/main+menu/"
                  "para+politikasi/merkez+bankasi+faiz+oranlari")
_EVDS2_URL = "https://evds2.tcmb.gov.tr/index.php?lang=tr"
# Hicbir kaynak ve onceki deger yoksa kullanilacak guvenli sabit (en son bilinen)
_POLITIKA_FAIZI_FALLBACK = 37.0

# TCMB PPK (Para Politikasi Kurulu) toplanti tarihleri (ay, gun) - HER YIL MANUEL GUNCELLE
_PPK_TARIHLERI = {
    2026: ((1, 23), (3, 6), (4, 17), (6, 11), (7, 24), (9, 18), (10, 23), (12, 19)),
    # 2027: TCMB yalniz yilin ILK YARISINI resmen acikladi (21 Oca, 18 Mar, 22 Nis,
    # 10 Haz). Ikinci yari (Tem-Ara) HENUZ ACIKLANMADI; asagidaki 4 tarih 2026
    # temposuna gore TAHMINIDIR — TCMB takvimi yayinlayinca guncellenmeli.
    # Kaynak: tcmb.gov.tr/takvim (2026 tam + 2027 ilk yari).
    2027: ((1, 21), (3, 18), (4, 22), (6, 10),          # resmi (2027 ilk yari)
           (7, 22), (9, 16), (10, 21), (12, 16)),       # TAHMINI (2027 ikinci yari)
}


def ppk_tarihleri(yil=None) -> list:
    """PPK toplanti tarihleri (date listesi, sirali). yil verilirse o yila filtreler."""
    out = []
    for y, gunler in _PPK_TARIHLERI.items():
        if yil is None or y == yil:
            out.extend(date(y, ay, gun) for ay, gun in gunler)
    return sorted(out)


def bugun_ppk_mi(gun=None) -> bool:
    """Verilen gun (vars. bugun) bir PPK toplanti gunu mu?"""
    gun = gun or datetime.now(_TZ).date()
    return gun in set(ppk_tarihleri())


def sonraki_ppk(gun=None, dahil=True):
    """Verilen gunden (vars. bugun) sonraki ilk PPK tarihi. dahil=False ise bugunu
    haric tutar (PPK gununde 'bir sonraki'yi gostermek icin). Yoksa None."""
    gun = gun or datetime.now(_TZ).date()
    for d in ppk_tarihleri():
        if (d >= gun) if dahil else (d > gun):
            return d
    return None


def canli_politika_faizi():
    """TCMB resmi sayfa -> EVDS2 ile guncel politika faizini ceker.
    (deger, kaynak) veya (None, None). PPK gunu otomasyonu kullanir."""
    return _politika_faizi()

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


_HATA_LOG = Path(__file__).resolve().parents[2] / "logs" / "macro_hata.log"

# Sirayla denenecek fiyat selector'lari (ilki kirilirsa digerleri devreye girer)
_FIYAT_PATTERNS = (
    r'data-test="instrument-price-last"[^>]*>([^<]+)<',          # birincil (DOM)
    r'<meta[^>]+itemprop="price"[^>]+content="([\d.,]+)"',        # microdata meta
    r'"price"\s*:\s*"?([\d.,]+)"?',                               # JSON-LD / state
    r'"last"\s*:\s*"?([\d.,]+)',                                  # eski state alani
    r'(?:og:price:amount|twitter:data1)"[^>]+content="([\d.,]+)"',  # meta og/twitter
)


def _log_macro_hata(url, neden):
    """Sessiz kaybi gorunur kilmak icin macro_hata.log'a yaz."""
    try:
        _HATA_LOG.parent.mkdir(exist_ok=True)
        ts = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
        with _HATA_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {neden} :: {url}\n")
    except Exception:
        pass


def _investing_last(url):
    """investing.com enstruman sayfasindan 'son fiyat'i parse eder.

    Birincil selector kirilirsa sirayla alternatifleri (meta/JSON-LD/og) dener.
    Sayfa cekilemezse veya hicbir selector tutmazsa logs/macro_hata.log'a yazar."""
    html = _fetch(url)
    if not html:
        _log_macro_hata(url, "FETCH_BASARISIZ (sayfa cekilemedi)")
        return None
    for i, pat in enumerate(_FIYAT_PATTERNS):
        m = re.search(pat, html)
        if m:
            v = _num(m.group(1))
            if v is not None:
                if i > 0:                        # birincil selector kirildi, alternatif tuttu
                    _log_macro_hata(url, f"BIRINCIL_SELECTOR_KIRIK (alternatif #{i} kullanildi)")
                return v
    _log_macro_hata(url, "TUM_SELECTORLAR_BASARISIZ (HTML geldi ama fiyat bulunamadi)")
    return None


def _yahoo_last(symbol):
    """Yahoo Finance (yfinance) son kapanis degeri; cekilemezse None."""
    if not symbol:
        return None
    try:
        import logging
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # gurultu kapali
        from src.data.factory import get_data_source
        start = (datetime.now(_TZ).date() - timedelta(days=10)).isoformat()
        df = get_data_source().get_history(symbol, start=start)
        if df is None or df.empty:
            return None
        return round(float(df["Close"].iloc[-1]), 4)
    except Exception:
        return None


def _load_son_bilinen() -> dict:
    """data/macro_last.json'dan son bilinen makro degerleri okur."""
    try:
        import json
        return json.loads(_SON_BILINEN.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _kaydet_son_bilinen(degerler: dict):
    """Taze cekilen makro degerleri son bilinen depoya yazar (birlestirerek)."""
    if not degerler:
        return
    try:
        import json
        _SON_BILINEN.parent.mkdir(exist_ok=True)
        mevcut = _load_son_bilinen()
        mevcut.update(degerler)
        _SON_BILINEN.write_text(
            json.dumps(mevcut, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _parse_politika_faizi(html):
    """HTML'den 1 hafta repo (politika faizi) yuzdesini cikarir; bulunamazsa None."""
    if not html:
        return None
    t = re.sub(r"<[^>]+>", " ", html)
    t = re.sub(r"\s+", " ", t)
    # "1 Hafta Repo" / "Politika Faizi" / "Repo" anahtari yakininda makul bir yuzde ara
    for anahtar in ("1 Hafta Repo", "Hafta Repo", "Politika Faizi", "Repo Faiz", "Repo"):
        m = re.search(re.escape(anahtar) + r"[^\d%]{0,40}%?\s*([0-9]{1,2}(?:[.,][0-9]{1,2})?)",
                      t, re.IGNORECASE)
        if m:
            v = _num(m.group(1))
            if v is not None and 5 <= v <= 100:   # makul politika faizi araligi
                return v
    return None


def _politika_faizi():
    """TCMB resmi sayfasi -> EVDS2; bulunamazsa (None, None). Deger + kaynak doner."""
    for url, ad in ((_TCMB_FAIZ_URL, "tcmb"), (_EVDS2_URL, "evds2")):
        v = _parse_politika_faizi(_fetch(url))
        if v is not None:
            return v, ad
    return None, None


def _borsapy_macro() -> dict:
    """borsapy (opsiyonel) ile TUFE (yillik) + politika faizi. Kutuphane yok/hata
    olursa {} doner -> mevcut fallback korunur. NOT: borsapy.policy_rate su an
    hatali (7.0 gibi) deger donebiliyor; makul aralik disi degerler REDDEDILIR."""
    out = {}
    try:
        import borsapy as bp
    except Exception:
        return out
    # TUFE (yillik) - calisiyor (bp.Inflation().latest())
    try:
        enf = bp.Inflation().latest()
        v = enf.get("yearly_inflation") if isinstance(enf, dict) else None
        if v is not None:
            v = round(float(v), 2)
            if 0 < v < 300:                 # makul yillik TUFE araligi
                out["tufe_yillik"] = v
    except Exception:
        pass
    # Politika faizi - makullik kontrolu (borsapy bazen 7.0 gibi hatali doner)
    try:
        pf = bp.policy_rate()
        if pf is not None:
            pf = round(float(pf), 2)
            if 20 <= pf <= 80:              # guncel TR politika faizi makul araligi
                out["politika_faizi"] = pf
    except Exception:
        pass
    return out


def _investing_cpi_yoy(url=None):
    """investing.com ekonomik-takvim event sayfasindan TUFE (yillik) degerini ceker.

    Turkiye CPI (YoY) event URL'i ortamda erisilemez (JS/anti-scraping); bu yuzden
    URL yapilandirilabilir (TUFE_INVESTING_URL). Verilirse event sayfasindaki en
    guncel 'Gerceklesen' (yoksa 'Onceki') yuzde degeri parse edilir.
    """
    url = url or os.environ.get("TUFE_INVESTING_URL")
    if not url:
        return None
    html = _fetch(url)
    if not html:
        return None
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    for m in re.finditer(r"(?:Gerçekleşen|Önceki)\s*:?\s*%?\s*([\d][\d.,]*)", t):
        v = _num(m.group(1))
        if v is not None:
            return v
    return None


def get_macro() -> dict:
    """Makro gostergeleri dondurur (iki kaynak birlesik).

    - USD/TRY ve TR 10 yillik faiz: investing.com (su an calisiyor)
    - Politika faizi ve TUFE: EVDS3 (EVDS_API_KEY + KYC'li proxy gelince otomatik)

    Hangi kaynak veri verirse o alan dolar; hicbiri gelmezse available=False.
    """
    now = time.monotonic()
    hit = _CACHE.get("macro")
    if hit and (now - hit[0]) < _TTL:
        return hit[1]

    out = {"available": False, "kaynaklar": []}

    # 1) usdtry + tr_10y_faiz: investing.com -> Yahoo Finance -> son bilinen deger
    son_bilinen = _load_son_bilinen()
    taze = {}
    for ad, url in _INVESTING.items():
        v = _investing_last(url)
        if v is not None:
            out[ad] = v
            taze[ad] = v
            if "investing.com" not in out["kaynaklar"]:
                out["kaynaklar"].append("investing.com")
            continue
        # investing.com basarisiz -> Yahoo Finance alternatifi
        yv = _yahoo_last(_YAHOO.get(ad))
        if yv is not None:
            out[ad] = yv
            taze[ad] = yv
            if "yahoo" not in out["kaynaklar"]:
                out["kaynaklar"].append("yahoo")
            continue
        # her iki kaynak da basarisiz -> son bilinen degere dus + logla
        sv = son_bilinen.get(ad)
        out[ad] = sv
        _log_macro_hata(_YAHOO.get(ad) or ad,
                        f"YEDEK_KULLANILDI (investing+yahoo basarisiz, "
                        f"son bilinen deger={sv})")
        if sv is not None and "son_bilinen" not in out["kaynaklar"]:
            out["kaynaklar"].append("son_bilinen")

    # 2) EVDS3 -> politika_faizi, tufe_yillik (KYC sonrasi otomatik devreye girer)
    out["politika_faizi"] = None
    out["tufe_yillik"] = None

    # 2a) borsapy (opsiyonel): TUFE + politika faizi. Basarisiz/yok ise sessizce atla.
    bpy = _borsapy_macro()
    for ad in ("tufe_yillik", "politika_faizi"):
        if bpy.get(ad) is not None:
            out[ad] = bpy[ad]
            taze[ad] = bpy[ad]
            if "borsapy" not in out["kaynaklar"]:
                out["kaynaklar"].append("borsapy")

    # 2b) EVDS3 (KYC sonrasi) - yalniz borsapy'den gelmeyen alanlari doldurur
    key = os.environ.get("EVDS_API_KEY")
    if key:
        evds_katki = False
        for ad in ("politika_faizi", "tufe_yillik"):
            if out.get(ad) is not None:
                continue
            code, agg = _EVDS_SERIES[ad]
            v = _evds_series(code, agg, key)
            if v is not None:
                out[ad] = v
                taze[ad] = v
                evds_katki = True
        if evds_katki and "EVDS3" not in out["kaynaklar"]:
            out["kaynaklar"].append("EVDS3")

    # 2c) Politika faizi hala yoksa: TCMB sayfasi -> EVDS2 -> son bilinen -> 46.0
    if out.get("politika_faizi") is None:
        pf, pk = _politika_faizi()
        if pf is not None:
            out["politika_faizi"] = pf
            taze["politika_faizi"] = pf
            if pk not in out["kaynaklar"]:
                out["kaynaklar"].append(pk)
        else:
            sv = son_bilinen.get("politika_faizi", _POLITIKA_FAIZI_FALLBACK)
            out["politika_faizi"] = sv
            _log_macro_hata(_TCMB_FAIZ_URL,
                            f"YEDEK_KULLANILDI (TCMB+EVDS2 basarisiz, "
                            f"politika faizi={sv})")
            if "son_bilinen" not in out["kaynaklar"]:
                out["kaynaklar"].append("son_bilinen")

    # 3) TUFE EVDS'ten gelmediyse investing.com event sayfasindan (TUFE_INVESTING_URL)
    if out.get("tufe_yillik") is None:
        tv = _investing_cpi_yoy()
        if tv is not None:
            out["tufe_yillik"] = tv
            out["kaynaklar"].append("investing.com(TUFE)")

    # taze cekilen degerleri kalici sakla (sonraki yedek icin)
    _kaydet_son_bilinen(taze)

    out["available"] = bool(out["kaynaklar"])
    if not out["available"]:
        out["neden"] = "makro veri alinamadi (investing.com + EVDS3 bos)"

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
