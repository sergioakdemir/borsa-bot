"""Borsa-bot mobil web arayuzu (Flask).

Yalnizca yerel db ve data klasorlerinden okur; disariya hic baglanmaz.
Port: 8080.

Sekmeler:
- Ana     : Portfoyum + Takip Listesi kartlari (AL/TUT/SAT/VETO, fiyat, % degisim)
- Portfoy : kullanici pozisyonlari, alis fiyati, kar/zarar, hedef/stop
- Karne   : gercek karar gecmisi defteri (simdilik backtest.json'dan)

API:
- /api/stocks     -> takip + sinyal kartlari (zengin: yorum, puan detayi, son haber)
- /api/portfolio  -> pozisyonlar + ozet
- /api/karne      -> defter satirlari
- /api/alerts     -> son uyari/sinyal listesi (bildirim paneli)
- /api/summary    -> firsat / uyari sayilari (ust serit)
"""
import base64
import binascii
import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
CONFIG = ROOT / "config"
DB_PATH = DATA / "borsa.db"
WATCHLIST_PATH = CONFIG / "watchlist.json"

# src paketini import edebilmek icin (app.py dogrudan script olarak calisir)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _load_dotenv():
    """ANTHROPIC_API_KEY gibi degiskenleri .env'den ortama yukler."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

app = Flask(__name__)

# DB semasini/migrasyonlari hazirla (para_birimi, telegram_id kolonlari vb.)
try:
    from src.db import database as _db
    _db.init_db()
except Exception:  # pragma: no cover - import yolu sorunlarinda sessiz gec
    _db = None

# watchlist.json yazimlarini serilestir (eszamanli istek korumasi)
_WL_LOCK = threading.Lock()

# Disari acilan arama sonuclari icin kucuk TTL onbellegi (rate-limit korumasi)
_SEARCH_CACHE: dict[str, tuple[float, list]] = {}
_SEARCH_TTL = 300.0  # saniye (5 dakika)

_OPPORTUNITY_MIN = 7  # firsat bolgesine girmek icin gereken puan
_VISION_MODEL = "claude-opus-4-8"  # portfoy fotografi okuma (Claude vision)

# Portfoy ticker -> bigpara fiyat kaynagi (yfinance'de olmayan/yanlis gelen
# enstrumanlar icin). KAP_PROXY_URL uzerinden cekilir. Deger = bigpara URL slug'i.
_BIGPARA_SOURCES = {
    "GMSTR.F": "gmstr-qnb-portfoy-gumus-katilim-byf-detay",
}


# ----------------------------------------------------------------------------
# BIST sirket adlari (ticker -> tam unvan). Yerel; disariya cikmaz.
# ----------------------------------------------------------------------------
COMPANY_NAMES = {
    "THYAO": "Türk Hava Yolları",
    "GARAN": "Garanti BBVA",
    "ASELS": "Aselsan",
    "KCHOL": "Koç Holding",
    "TUPRS": "Tüpraş",
    "EREGL": "Ereğli Demir Çelik",
    "AKBNK": "Akbank",
    "YKBNK": "Yapı Kredi Bankası",
    "SISE": "Şişecam",
    "TCELL": "Turkcell",
    "BIMAS": "BİM Mağazalar",
    "FROTO": "Ford Otosan",
    "TOASO": "Tofaş",
    "KOZAL": "Koza Altın",
    "EKGYO": "Emlak Konut GYO",
    "PETKM": "Petkim",
    "ARCLK": "Arçelik",
    "SAHOL": "Sabancı Holding",
    "HALKB": "Halkbank",
    "VAKBN": "VakıfBank",
    "ISCTR": "İş Bankası (C)",
    "TAVHL": "TAV Havalimanları",
    "PGSUS": "Pegasus",
    "MGROS": "Migros",
    "ULKER": "Ülker",
    "CCOLA": "Coca-Cola İçecek",
    "DOHOL": "Doğan Holding",
    "ENKAI": "Enka İnşaat",
    "KORDS": "Kordsa",
    "TTKOM": "Türk Telekom",
}


def company_name(ticker: str) -> str:
    t = (ticker or "").upper()
    return COMPANY_NAMES.get(t, t)


def _norm(s: str) -> str:
    """Turkce duyarsiz arama icin normalize (kucuk harf + tr->ascii)."""
    s = s or ""
    for a, b in (("İ", "i"), ("I", "i"), ("Ş", "s"), ("Ğ", "g"),
                 ("Ü", "u"), ("Ö", "o"), ("Ç", "c")):
        s = s.replace(a, b)
    s = s.lower()
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"),
                 ("ü", "u"), ("ö", "o"), ("ç", "c"), ("â", "a")):
        s = s.replace(a, b)
    return s


# ----------------------------------------------------------------------------
# yardimcilar
# ----------------------------------------------------------------------------
def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _classify(decision: str) -> tuple[str, str]:
    """Karar kodunu (AL/TUT/SAT/VETO) sade etiket + renk anahtarina cevirir."""
    d = (decision or "").upper()
    if "VETO" in d:
        return "VETO", "yellow"
    if "SAT" in d or "UZAK" in d or "AZALT" in d:   # SAT, GUCLU_SAT, UZAK_DUR, AZALT
        return "SAT", "red"
    if "AL" in d:                # AL, AL_TEMKINLI
        return "AL", "green"
    if "TUT" in d:
        return "TUT", "yellow"
    return "TUT", "yellow"


def _eminlik_tr(e: str) -> str:
    return {"DUSUK": "Düşük", "ORTA": "Orta", "YUKSEK": "Yüksek"}.get(
        (e or "").upper(), (e or "—").title())


def _clamp10(x) -> int:
    try:
        return max(0, min(10, int(round(x))))
    except (TypeError, ValueError):
        return 0


def _puan_detay(rec: dict, sig: dict) -> dict:
    """Mevcut sinyallerden 3 alt puan turetir (0-10): sirket sagligi / fiyat / piyasa.

    - sirket_sagligi : AI ana skoru (saglam=yuksek) ve riskin tersi
    - fiyat          : donem araligindaki fiyat konumu (ucuz=dusuk konum daha cazip degil;
                       burada 'fiyat gucu' olarak konumu kullaniyoruz)
    - piyasa         : trend + donem degisimi + hacim teyidi (momentum)
    """
    risk = (rec.get("risk") or {}).get("score")
    skor = rec.get("score")
    saglik = _clamp10(((skor or 0) + (10 - (risk or 0))) / 2)

    konum = sig.get("fiyat_konumu_%")
    fiyat = _clamp10((konum or 0) / 10)

    piyasa = 5
    if sig.get("trend") == "yukselen":
        piyasa += 2
    elif sig.get("trend") == "dusen":
        piyasa -= 2
    donem = sig.get("donem_degisim_%") or 0
    piyasa += 1 if donem > 0 else (-1 if donem < 0 else 0)
    if sig.get("hacim_sinyali") == "yuksek":
        piyasa += 1
    elif sig.get("hacim_sinyali") == "dusuk":
        piyasa -= 1
    piyasa = _clamp10(piyasa)

    return {"sirket_sagligi": saglik, "fiyat": fiyat, "piyasa": piyasa}


def _son_haber(rec: dict) -> dict | None:
    haberler = rec.get("haberler") or []
    if not haberler:
        return None
    h = haberler[0]
    return {
        "baslik": h.get("baslik"),
        "tarih": h.get("tarih"),
        "tazelik": h.get("tazelik"),
        "fiyatlanma": h.get("fiyatlanma"),
    }


def _ilk_cumleler(metin: str, n: int = 2) -> str:
    """Bir metnin ilk n cumlesini dondurur (kisa AI ozeti icin).

    Cumle sonu = nokta/unlem/soru + bosluk. Ondalik sayilar (1.89) icinde
    bosluk olmadigi icin yanlislikla bolunmez.
    """
    metin = (metin or "").strip()
    if not metin:
        return ""
    parcalar = re.split(r"(?<=[.!?])\s+", metin)
    return " ".join(parcalar[:n]).strip()


def _ozet(rec: dict) -> str:
    """Detay panelinin ustundeki kisa AI ozeti (2-3 cumle, sade)."""
    return _ilk_cumleler(rec.get("gerekce", ""), 3)


def _firsat_neden(rec: dict) -> str:
    """Firsat serisindeki kart icin tek cumlelik 'neden' metni."""
    gozlemler = rec.get("gozlemler") or []
    if gozlemler:
        return str(gozlemler[0]).strip()
    return _ilk_cumleler(rec.get("gerekce", ""), 1)


_ETIKET_SADE = {
    "AL": "almayı düşünebileceğin, olumlu görünen bir hisse",
    "SAT": "satış baskısı olan, dikkatli olunması gereken bir hisse",
    "TUT": "şu an için beklemenin/elde tutmanın mantıklı göründüğü bir hisse",
    "VETO": "bir risk nedeniyle sistemin şimdilik uzak durmayı önerdiği bir hisse",
}


def _aciklama(card: dict) -> str:
    """Yeni başlayan birine yönelik, sade Türkçe açıklama üretir.

    Yapay zekâ çağrısı yapmaz; karttaki yapısal sinyallerden cümle kurar.
    """
    isim = card.get("isim") or card.get("ticker")
    etiket = card.get("etiket") or "TUT"
    skor = card.get("skor")
    risk = card.get("risk")
    trend = card.get("trend")
    eminlik = (card.get("eminlik") or "").lower()
    hacim = card.get("hacim")

    s = []
    s.append(f"Sistem {isim} için “{etiket}” diyor — yani "
             f"{_ETIKET_SADE.get(etiket, 'kararsız kalınan bir hisse')}.")

    if skor is not None:
        if skor >= 8:
            nitelik = "oldukça güçlü"
        elif skor >= 6:
            nitelik = "iyiye yakın ama temkinli"
        elif skor >= 4:
            nitelik = "ortalama / belirsiz"
        else:
            nitelik = "zayıf"
        s.append(f"Puan {skor}/10: hissenin şu anki teknik görünümü {nitelik}. "
                 f"Puan 10'a yaklaştıkça tablo daha olumlu demektir.")

    if risk is not None:
        if risk >= 7:
            rs = "yüksek risk — fiyat sert oynayabilir, dikkatli ol"
        elif risk >= 4:
            rs = "orta risk — normal dalgalanma beklenir"
        else:
            rs = "düşük risk — fiyat görece sakin"
        s.append(f"Risk {risk}/10: {rs}.")

    if trend == "yukselen":
        s.append("Fiyat son dönemde yukarı yönlü hareket ediyor (yükselen trend).")
    elif trend == "dusen":
        s.append("Fiyat son dönemde aşağı yönlü hareket ediyor (düşen trend).")
    elif trend:
        s.append("Fiyat son dönemde yatay, belirgin bir yön yok.")

    if hacim == "yuksek":
        s.append("İşlem hacmi yüksek; yani harekete katılım güçlü, sinyal daha güvenilir.")
    elif hacim == "dusuk":
        s.append("İşlem hacmi düşük; az kişi alıp sattığı için sinyali temkinli karşıla.")

    if eminlik:
        s.append(f"Sistemin bu yorumdaki güveni: {eminlik}. "
                 "Güven düşükse veriyle birlikte kendi araştırmanı da yap.")

    s.append("Not: Bu bir yatırım tavsiyesi değil, sistemin verilerden çıkardığı bir yorumdur.")
    return "\n".join(s)


def _stock_card(rec: dict) -> dict:
    """ai_commentary kaydini zengin karta cevirir."""
    sig = rec.get("kullanilan_on_sinyal", {}) or {}
    etiket, renk = _classify(rec.get("final_decision"))
    tkr = (rec.get("ticker") or "").upper()
    card = {
        "ticker": tkr,
        "isim": company_name(tkr),
        "market": "bist",
        "para_birimi": "₺",
        "etiket": etiket,
        "renk": renk,
        "label_full": rec.get("final_label", ""),
        "fiyat": sig.get("son_kapanis"),
        "gunluk": sig.get("gunluk_degisim_%"),
        "donem": sig.get("donem_degisim_%"),
        "skor": rec.get("score"),
        "risk": (rec.get("risk") or {}).get("score"),
        "eminlik": _eminlik_tr(rec.get("eminlik")),
        "trend": sig.get("trend"),
        "fiyat_konumu": sig.get("fiyat_konumu_%"),
        "hacim": sig.get("hacim_sinyali"),
        # detay panel
        "yorum": rec.get("gerekce", ""),
        "ozet": _ozet(rec),
        "gozlemler": rec.get("gozlemler", []),
        "puan_detay": _puan_detay(rec, sig),
        "son_haber": _son_haber(rec),
        "firsat_neden": _firsat_neden(rec),
        "analist": rec.get("analist"),
        "has_data": True,
    }
    card["aciklama"] = _aciklama(card)
    return card


def _minimal_card(ticker: str) -> dict:
    """Sinyal verisi olmayan takip hissesi icin bos kart iskeleti."""
    t = (ticker or "").upper()
    return {
        "ticker": t, "isim": company_name(t),
        "market": "bist", "para_birimi": "₺",
        "etiket": None, "renk": "yellow", "label_full": "",
        "fiyat": None, "gunluk": None, "donem": None,
        "skor": None, "risk": None, "eminlik": "—",
        "trend": None, "fiyat_konumu": None, "hacim": None,
        "yorum": "", "ozet": "", "aciklama": "", "gozlemler": [],
        "puan_detay": {}, "son_haber": None, "firsat_neden": "",
        "analist": None, "has_data": False,
    }


def _commentary_by_ticker() -> dict:
    out = {}
    for x in _read_json(DATA / "ai_commentary.json", []):
        out[(x.get("ticker") or "").upper()] = x
    return out


# ----------------------------------------------------------------------------
# takip listesi (watchlist.json) okuma/yazma
# ----------------------------------------------------------------------------
def _load_watchlist() -> dict:
    wl = _read_json(WATCHLIST_PATH, {})
    if not isinstance(wl, dict):
        wl = {}
    wl.setdefault("bist_endeks", [])
    wl.setdefault("kisisel", [])         # BIST kisisel takip (brifing bunu okur)
    wl.setdefault("kisisel_diger", [])   # ABD/Kripto takip (brifing yok sayar)
    return wl


def _save_watchlist(wl: dict) -> None:
    WATCHLIST_PATH.write_text(
        json.dumps(wl, ensure_ascii=False, indent=2), encoding="utf-8")


def watchlist_add(ticker: str, market: str = "bist",
                  isim: str = "", cg_id: str = "") -> dict:
    ticker = (ticker or "").upper().strip().replace(".IS", "")
    market = (market or "bist").lower()
    if not ticker:
        return {"ok": False, "hata": "ticker bos"}
    with _WL_LOCK:
        wl = _load_watchlist()
        if market == "bist":
            if ticker not in [t.upper() for t in wl["kisisel"]]:
                wl["kisisel"].append(ticker)
        else:
            key = (ticker, market)
            if not any((d.get("ticker"), d.get("market")) == key
                       for d in wl["kisisel_diger"]):
                wl["kisisel_diger"].append({
                    "ticker": ticker, "market": market,
                    "isim": isim or ticker, "cg_id": cg_id})
        _save_watchlist(wl)
    return {"ok": True, "ticker": ticker, "market": market}


def watchlist_remove(ticker: str, market: str = "bist") -> dict:
    ticker = (ticker or "").upper().strip().replace(".IS", "")
    market = (market or "bist").lower()
    with _WL_LOCK:
        wl = _load_watchlist()
        if market == "bist":
            wl["kisisel"] = [t for t in wl["kisisel"]
                             if t.upper() != ticker]
        else:
            wl["kisisel_diger"] = [d for d in wl["kisisel_diger"]
                                   if not (d.get("ticker") == ticker
                                           and d.get("market") == market)]
        _save_watchlist(wl)
    return {"ok": True, "ticker": ticker, "market": market}


# ----------------------------------------------------------------------------
# disari acilan piyasa aramasi (ABD: yfinance, Kripto: CoinGecko)
# ----------------------------------------------------------------------------
def _cache_get(key: str):
    hit = _SEARCH_CACHE.get(key)
    if hit and (time.monotonic() - hit[0]) < _SEARCH_TTL:
        return hit[1]
    return None


def _cache_set(key: str, val: list):
    _SEARCH_CACHE[key] = (time.monotonic(), val)


def _us_card(symbol, isim, fiyat=None, gunluk=None, borsa="") -> dict:
    return {
        "ticker": symbol, "isim": isim or symbol,
        "market": "abd", "para_birimi": "$", "borsa": borsa,
        "fiyat": fiyat, "gunluk": gunluk,
        "etiket": None, "renk": "yellow", "label_full": "",
        "skor": None, "risk": None, "eminlik": "—",
        "yorum": "", "ozet": "", "aciklama": "", "gozlemler": [],
        "puan_detay": {}, "son_haber": None, "firsat_neden": "",
        "has_data": fiyat is not None,
    }


def _crypto_card(symbol, isim, fiyat=None, gunluk=None, cg_id="") -> dict:
    return {
        "ticker": symbol, "isim": isim or symbol,
        "market": "kripto", "para_birimi": "$", "cg_id": cg_id,
        "fiyat": fiyat, "gunluk": gunluk,
        "etiket": None, "renk": "yellow", "label_full": "",
        "skor": None, "risk": None, "eminlik": "—",
        "yorum": "", "ozet": "", "aciklama": "", "gozlemler": [],
        "puan_detay": {}, "son_haber": None, "firsat_neden": "",
        "has_data": fiyat is not None,
    }


def search_us(q: str) -> list[dict]:
    """ABD hisseleri: yfinance arama + toplu fiyat/gunluk degisim."""
    q = (q or "").strip()
    if not q:
        return []
    ck = f"us:{q.lower()}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    try:
        import yfinance as yf
        res = yf.Search(q, max_results=12)
        quotes = [x for x in (res.quotes or [])
                  if x.get("quoteType") == "EQUITY" and x.get("symbol")]
    except Exception:
        return []
    quotes = quotes[:8]
    syms = [x["symbol"] for x in quotes]
    prices = _yf_prices(syms)
    out = []
    for x in quotes:
        s = x["symbol"]
        p = prices.get(s, {})
        out.append(_us_card(
            s, x.get("shortname") or x.get("longname") or s,
            fiyat=p.get("fiyat"), gunluk=p.get("gunluk"),
            borsa=x.get("exchange") or ""))
    _cache_set(ck, out)
    return out


def _yf_prices(symbols: list[str]) -> dict:
    """Coklu yfinance sembolu icin {sembol: {fiyat, gunluk}} (tek toplu istek).

    BIST (.IS), ABD ve diger yfinance sembolleri ile calisir. Kisa TTL onbellegi
    ile ayni sembol setini tekrar tekrar cekmekten kacinir.
    """
    symbols = sorted({s for s in symbols if s})
    if not symbols:
        return {}
    ck = "yfpx:" + ",".join(symbols)
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    try:
        import yfinance as yf
        df = yf.download(symbols, period="5d", progress=False,
                         threads=True, auto_adjust=True)
    except Exception:
        return {}
    out = {}
    try:
        closes = df["Close"]
    except Exception:
        return {}
    for s in symbols:
        try:
            col = closes[s].dropna() if len(symbols) > 1 else closes.dropna()
            if len(col) >= 2:
                prev, last = float(col.iloc[-2].iloc[0]) if hasattr(col.iloc[-2], "iloc") else float(col.iloc[-2]), float(col.iloc[-1].iloc[0]) if hasattr(col.iloc[-1], "iloc") else float(col.iloc[-1])
                chg = ((last - prev) / prev * 100) if prev else None
                out[s] = {"fiyat": round(last, 2),
                          "gunluk": round(chg, 2) if chg is not None else None}
            elif len(col) >= 1:
                out[s] = {"fiyat": round(float(col.iloc[-1]), 2), "gunluk": None}
        except Exception:
            continue
    _cache_set(ck, out)
    return out


def _usdtry() -> float | None:
    """Guncel USD/TRY kuru (makro -> yfinance yedek). Portfoy toplamini TL'ye cevirir."""
    ck = "usdtry"
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    rate = None
    try:
        from src.news.macro import get_macro
        r = get_macro().get("usdtry")
        rate = float(r) if r else None
    except Exception:
        rate = None
    if not rate:
        px = _yf_prices(["USDTRY=X"]).get("USDTRY=X", {})
        rate = px.get("fiyat")
    if rate:
        _cache_set(ck, rate)
    return rate


def _yf_symbol(ticker: str, para_birimi: str = "TL") -> str:
    """Portfoy ticker'ini dogru yfinance sembolune cevirir.

    - TL (BIST): taban kod + '.IS' (yanlis ekleri ele; 'GMSTR.F' -> 'GMSTR.IS')
    - USD (ABD): kod oldugu gibi (orn. 'AAPL')
    """
    t = (ticker or "").upper().strip()
    if (para_birimi or "TL").upper() == "USD":
        return t
    base = t.split(".")[0]
    return f"{base}.IS" if base else t


def _bigpara_price(slug: str) -> dict:
    """bigpara.hurriyet.com.tr hisse detay sayfasindan {fiyat, gunluk} ceker.

    KAP_PROXY_URL (TR cikisli proxy) uzerinden curl_cffi ile istenir; kisa TTL
    onbellekli. yfinance'de bulunmayan BYF/fonlar icin yedek fiyat kaynagi.
    """
    ck = "bp:" + slug
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    proxy = os.environ.get("KAP_PROXY_URL")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    url = f"https://bigpara.hurriyet.com.tr/borsa/hisse-fiyatlari/{slug}/"
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", proxies=proxies, timeout=20)
        if r.status_code != 200:
            return {}
        html = r.text
    except Exception:
        return {}
    pairs = dict(re.findall(
        r'<span class="name">([^<]+)</span>\s*<span class="value"[^>]*>([^<]+)</span>',
        html))
    out = {}
    fiyat = _num(pairs.get("Son İşlem Fiyatı") or pairs.get("Satış"))
    if fiyat is not None:
        out["fiyat"] = fiyat
    g = (pairs.get("Günlük Değişim %") or "").replace("%", "").replace("&#x2B;", "+")
    gunluk = _num(g)
    if gunluk is not None:
        out["gunluk"] = gunluk
    if out:
        _cache_set(ck, out)
    return out


def search_crypto(q: str) -> list[dict]:
    """Kripto paralar: CoinGecko arama + toplu fiyat/24s degisim."""
    q = (q or "").strip()
    if not q:
        return []
    ck = f"cg:{q.lower()}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    try:
        r = requests.get("https://api.coingecko.com/api/v3/search",
                         params={"query": q}, timeout=12)
        coins = (r.json() or {}).get("coins", [])[:8]
    except Exception:
        return []
    if not coins:
        _cache_set(ck, [])
        return []
    ids = ",".join(c["id"] for c in coins if c.get("id"))
    prices = _crypto_prices(ids)
    out = []
    for c in coins:
        cid = c.get("id")
        p = prices.get(cid, {})
        out.append(_crypto_card(
            (c.get("symbol") or "").upper(), c.get("name") or cid,
            fiyat=p.get("fiyat"), gunluk=p.get("gunluk"), cg_id=cid))
    _cache_set(ck, out)
    return out


def _crypto_prices(ids: str) -> dict:
    """CoinGecko markets: {coin_id: {fiyat, gunluk(24s %)}}."""
    if not ids:
        return {}
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "ids": ids,
                    "price_change_percentage": "24h"}, timeout=12)
        data = r.json() or []
    except Exception:
        return {}
    out = {}
    for c in data:
        out[c.get("id")] = {
            "fiyat": c.get("current_price"),
            "gunluk": c.get("price_change_percentage_24h"),
        }
    return out


# ----------------------------------------------------------------------------
# veri toplayicilar
# ----------------------------------------------------------------------------
def _owned_tickers() -> set:
    try:
        with sqlite3.connect(DB_PATH) as c:
            return {(r[0] or "").upper()
                    for r in c.execute("SELECT ticker FROM portfoy")}
    except sqlite3.Error:
        return set()


def get_stocks() -> dict:
    """Ana sayfa: firsatlar + portfoyum (sahip olunan) + takip listesi (kisisel)."""
    comm = _commentary_by_ticker()
    owned = _owned_tickers()
    wl = _load_watchlist()
    # Takip Listesi = yalnizca kullanicinin kisisel listesi (BIST).
    # bist_endeks brifingin tarama evrenidir, kullanici takip listesi degil.
    watch = [t.upper() for t in wl.get("kisisel", [])]

    portfoyum = [_stock_card(comm[t]) for t in sorted(owned) if t in comm]

    takip = []
    seen = set(owned)
    for t in watch:
        if t in seen:
            continue
        seen.add(t)
        takip.append(_stock_card(comm[t]) if t in comm else _minimal_card(t))

    # ABD/Kripto kisisel takip (canli fiyat ile)
    diger = wl.get("kisisel_diger", []) or []
    us_syms = [d["ticker"] for d in diger if d.get("market") == "abd"]
    cg_ids = [d.get("cg_id") for d in diger if d.get("market") == "kripto" and d.get("cg_id")]
    us_px = _yf_prices(us_syms) if us_syms else {}
    cg_px = _crypto_prices(",".join(cg_ids)) if cg_ids else {}
    for d in diger:
        if d.get("market") == "abd":
            p = us_px.get(d["ticker"], {})
            takip.append(_us_card(d["ticker"], d.get("isim"),
                                  fiyat=p.get("fiyat"), gunluk=p.get("gunluk")))
        elif d.get("market") == "kripto":
            p = cg_px.get(d.get("cg_id"), {})
            takip.append(_crypto_card(d["ticker"], d.get("isim"),
                                      fiyat=p.get("fiyat"), gunluk=p.get("gunluk"),
                                      cg_id=d.get("cg_id")))

    # Firsat bolgesi: puan >= esik olan AL kararlari
    firsatlar = []
    for rec in comm.values():
        etiket, _ = _classify(rec.get("final_decision"))
        skor = rec.get("score") or 0
        if etiket == "AL" and skor >= _OPPORTUNITY_MIN:
            firsatlar.append(_stock_card(rec))
    firsatlar.sort(key=lambda c: c.get("skor") or 0, reverse=True)

    return {"firsatlar": firsatlar, "portfoyum": portfoyum, "takip": takip}


def _search_bist(q: str) -> list[dict]:
    """BIST: yerel evrende ticker/sirket adina gore (Turkce duyarsiz)."""
    nq = _norm(q).strip()
    if not nq:
        return []
    comm = _commentary_by_ticker()
    out = []
    for t, name in COMPANY_NAMES.items():
        if nq in _norm(t) or nq in _norm(name):
            out.append(_stock_card(comm[t]) if t in comm else _minimal_card(t))
    return out


def get_search(q: str, market: str = "bist", kullanici=None) -> list[dict]:
    """Piyasaya gore arama. market='all' -> BIST + ABD + Kripto birlesik.

    kullanici verilirse sonuclar onceliklendirilir ve 'grup' alani eklenir:
    once portfoydeki hisseler, sonra takip listesi, sonra digerleri."""
    market = (market or "bist").lower()
    if market == "abd":
        out = search_us(q)
    elif market == "kripto":
        out = search_crypto(q)
    elif market == "all":
        # evrensel: uc piyasada birden ara, sonuclari birlestir (BIST once)
        out = _search_bist(q)
        try:
            out += search_us(q)
        except Exception:
            pass
        try:
            out += search_crypto(q)
        except Exception:
            pass
    else:
        out = _search_bist(q)

    # Kullaniciya gore siralama: 1) portfoy, 2) takip listesi, 3) diger
    owned = {(t or "").upper() for t in _owned_by_user(kullanici)} if kullanici else set()
    watched = set()
    try:
        wl = _load_watchlist()
        watched = {(t or "").upper().split(".")[0]
                   for t in wl.get("kisisel", [])}
        watched |= {(d.get("ticker") or "").upper().split(".")[0]
                    for d in wl.get("kisisel_diger", [])}
    except Exception:
        pass

    def _rank(s):
        tk = (s.get("ticker") or "").upper().split(".")[0]
        if tk in owned:
            s["grup"] = "portfoy"
            return 0
        if tk in watched:
            s["grup"] = "takip"
            return 1
        s["grup"] = "bist"
        return 2

    out.sort(key=_rank)        # _rank her ogeye 'grup' alanini da yazar
    return out


def get_portfolio(kullanici: str | None = None) -> dict:
    """Portfoy ozeti. kullanici verilirse (ad, orn. 'serhat') yalniz o kisinin
    pozisyonlari dondurulur; yoksa tum kullanicilar."""
    ck = f"portfolio_{kullanici or 'all'}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    comm = _commentary_by_ticker()
    pozisyonlar = []
    toplam_maliyet = toplam_deger = 0.0
    bist_deger = abd_deger = 0.0    # piyasa bazli deger (TL); snapshot icin

    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        kullanici_map = {r["id"]: r["ad"]
                         for r in c.execute("SELECT id, ad FROM kullanici")}
        if kullanici:
            rows = [dict(r) for r in c.execute(
                "SELECT p.* FROM portfoy p JOIN kullanici k ON k.id = p.kullanici_id "
                "WHERE LOWER(k.ad) = LOWER(?) ORDER BY p.id", (kullanici,))]
        else:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM portfoy ORDER BY kullanici_id, id")]

    # Guncel fiyat = CANLI kaynak. Once ozel kaynak (bigpara), sonra yfinance,
    # son care bayat snapshot. GMSTR.F -> bigpara; TUPRS -> TUPRS.IS; USD kendi koduyla.
    sym_of = {r["id"]: _yf_symbol(r["ticker"], r.get("para_birimi"))
              for r in rows if (r["ticker"] or "").upper() not in _BIGPARA_SOURCES}
    live = _yf_prices(list(sym_of.values())) if sym_of else {}

    # Toplamlar TL bazinda: USD pozisyonlari guncel kurla cevrilir (kart'ta yine $)
    usdtry = _usdtry()

    for r in rows:
        raw = (r["ticker"] or "").upper()
        birim_kod = (r.get("para_birimi") or "TL").upper()
        # BIST kodu gosterimde sade olsun (GMSTR.F -> GMSTR); USD oldugu gibi
        tkr = raw if birim_kod == "USD" else raw.split(".")[0] or raw
        adet = r["adet"] or 0.0
        alis = r["alim_fiyati"] or 0.0
        rec = comm.get(tkr, {}) or {}
        sig = rec.get("kullanilan_on_sinyal", {}) or {}

        # fiyat kaynagi onceligi: bigpara -> yfinance -> snapshot
        guncel = gunluk = None
        if raw in _BIGPARA_SOURCES:
            bp = _bigpara_price(_BIGPARA_SOURCES[raw])
            guncel, gunluk = bp.get("fiyat"), bp.get("gunluk")
        else:
            lp = live.get(sym_of.get(r["id"]), {}) or {}
            guncel, gunluk = lp.get("fiyat"), lp.get("gunluk")
        if guncel is None:
            guncel = sig.get("son_kapanis")
        if gunluk is None:
            gunluk = sig.get("gunluk_degisim_%")
        etiket, renk = _classify(rec.get("final_decision"))
        maliyet = adet * alis
        # TL'ye cevrim katsayisi (USD -> TL); kur yoksa 1 (cevrim atlanir)
        fx = (usdtry or 1.0) if birim_kod == "USD" else 1.0
        toplam_maliyet += maliyet * fx

        kz = kz_yuzde = None
        if guncel is not None:
            deger = adet * guncel
            toplam_deger += deger * fx
            deger_tl = deger * fx
            kz = deger - maliyet            # kart icin native para biriminde
            kz_yuzde = (kz / maliyet * 100) if maliyet else None
        else:
            toplam_deger += maliyet * fx
            deger_tl = maliyet * fx
        if birim_kod == "USD":
            abd_deger += deger_tl
        else:
            bist_deger += deger_tl

        birim = "$" if (r.get("para_birimi") or "TL").upper() == "USD" else "₺"
        market = "abd" if birim == "$" else "bist"
        st = _structured(rec) if rec else {}
        pozisyonlar.append({
            "id": r.get("id"),
            "kullanici": kullanici_map.get(r["kullanici_id"], "-"),
            "ticker": tkr,
            "isim": company_name(tkr),
            "market": market,
            "para_birimi": birim,
            "adet": adet,
            "alis": alis,
            "guncel": guncel,
            "gunluk": gunluk,
            "deger_tl": round(deger_tl, 2),     # TL bazli guncel deger (pasta grafigi icin)
            "kz": kz,
            "kz_yuzde": kz_yuzde,
            # yeni arayuz: sade karar + kisa yorum + aksiyon + yumusak durum
            "karar": st.get("decision"), "karar_renk": st.get("decision_renk", "gray"),
            "cardText": st.get("cardText", ""), "actionText": st.get("actionText", ""),
            "statusPhrase": st.get("statusPhrase", ""), "statusColor": st.get("statusColor", "gray"),
            # geriye donuk
            "summary": st.get("cardText", ""), "action": st.get("actionText", ""),
            "risk": st.get("risk", "—"), "risk_renk": st.get("risk_renk", "gray"),
            "riskReason": st.get("riskReason", ""),
            "tarih": r.get("alim_tarihi"),
        })

    toplam_kz = toplam_deger - toplam_maliyet
    owned_recs = [comm[t] for t in {p["ticker"] for p in pozisyonlar}
                  if t in comm and not comm[t].get("skipped")]
    # Snapshot bazli getiri (gunluk/haftalik/aylik) - yalniz tek kullanici sorusunda
    getiri = _portfoy_getiri(_uid(kullanici), toplam_deger) if kullanici else \
        {"gunluk": None, "haftalik": None, "aylik": None}
    result = {
        "pozisyonlar": pozisyonlar,
        "genel_yorum": _cap(_overview_fallback(owned_recs), 280),   # AI yorumu /api/overview ile asenkron
        "ozet": {
            "maliyet": toplam_maliyet,
            "deger": toplam_deger,
            "bist_degeri": round(bist_deger, 2),
            "abd_degeri": round(abd_deger, 2),
            "kz": toplam_kz,
            "kz_yuzde": (toplam_kz / toplam_maliyet * 100) if toplam_maliyet else None,
        },
        "getiri": getiri,
    }
    _cache_set(ck, result)
    return result


def _portfoy_getiri(uid, guncel_deger) -> dict:
    """Snapshot'lara gore gunluk/haftalik/aylik getiri (TL + %). Snapshot yoksa None.

    Referans: ilgili tarihe (1/7/30 gun once) en yakin ONCEKI gun kapanis snapshot'i.
    """
    bos = {"gunluk": None, "haftalik": None, "aylik": None}
    if uid is None or guncel_deger is None:
        return bos
    from src.db import database as db
    bugun = datetime.now(ZoneInfo("Europe/Istanbul")).date()

    def _delta(gun):
        snap = db.snapshot_on_or_before(uid, (bugun - timedelta(days=gun)).isoformat())
        ref = (snap or {}).get("toplam_deger_tl")
        if not ref:
            return None
        tl = guncel_deger - ref
        return {"tl": round(tl, 2),
                "yuzde": round(tl / ref * 100, 2) if ref else None,
                "ref_tarih": snap.get("tarih")}

    return {"gunluk": _delta(1), "haftalik": _delta(7), "aylik": _delta(30)}


_VISION_PROMPT = (
    "Sen bir hisse senedi portföy ekran görüntüsü okuyucususun. Verilen görsel, "
    "bir aracı kurum (örn. Midas) portföy/varlıklar ekranıdır. Görseldeki HER hisse "
    "satırı için şunları çıkar:\n"
    "- ticker: hisse kodu (BÜYÜK harf, örn. THYAO, AAPL). Yoksa şirket adından makul kod üret.\n"
    "- adet: sahip olunan lot/adet (sayı).\n"
    "- fiyat: ortalama alış/maliyet fiyatı (ondalık nokta ile sayı).\n"
    "- para_birimi: 'TL' veya 'USD' (₺ -> TL, $ -> USD; belirsizse TL).\n"
    "YALNIZCA şu JSON ile yanıt ver, başka hiçbir metin yazma:\n"
    '{"holdings":[{"ticker":"THYAO","adet":100,"fiyat":285.5,"para_birimi":"TL"}]}\n'
    "Okuyamadığın sayısal alan için null koy. Hiç hisse yoksa {\"holdings\":[]} dön."
)


def _extract_json(text: str):
    """Model yanitindan ilk JSON nesnesini ayiklar."""
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except json.JSONDecodeError:
            return None
    return None


def _num(x):
    """'1.234,56' / '1,234.56' / '285,5' gibi degerleri float'a cevirir."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip().replace("₺", "").replace("$", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:           # 1.234,56 -> 1234.56  | 1,234.56 -> 1234.56
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:                       # 285,5 -> 285.5
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _image_block(image: str):
    """Tek base64/data-url gorseli Claude image content blokuna cevirir (yoksa None)."""
    media_type = "image/png"
    b64 = image
    if image.startswith("data:"):
        try:
            head, b64 = image.split(",", 1)
            media_type = head.split(":", 1)[1].split(";", 1)[0] or media_type
        except (ValueError, IndexError):
            return None
    try:
        base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    return {"type": "image", "source": {"type": "base64",
                                        "media_type": media_type, "data": b64}}


def parse_portfolio_image(images) -> dict:
    """Bir veya birden cok base64 portfoy fotografini Claude vision ile okur.

    Tum fotograflar tek istekte degerlendirilir; tum hisseler birlestirilmis
    holdings listesi olarak doner.
    """
    if isinstance(images, str):
        images = [images]
    images = [im for im in (images or []) if im]
    if not images:
        return {"ok": False, "hata": "Görsel boş."}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "hata": "AI anahtarı (ANTHROPIC_API_KEY) ayarlı değil."}

    blocks = []
    for im in images[:8]:                 # makul ust sinir
        blk = _image_block(im)
        if blk:
            blocks.append(blk)
    if not blocks:
        return {"ok": False, "hata": "Geçerli görsel çözülemedi."}
    blocks.append({"type": "text", "text": _VISION_PROMPT})

    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=_VISION_MODEL, max_tokens=2000,
            messages=[{"role": "user", "content": blocks}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", "") == "text")
    except Exception as e:
        return {"ok": False, "hata": f"AI okuma hatası: {type(e).__name__}: {str(e)[:120]}"}

    data = _extract_json(text)
    if not data or "holdings" not in data:
        return {"ok": False, "hata": "Fotoğraf okunamadı (geçerli veri çıkmadı)."}

    holdings, seen = [], set()
    for h in (data.get("holdings") or []):
        tkr = (str(h.get("ticker") or "").upper().replace(".IS", "").strip())
        if not tkr or tkr in seen:
            continue
        seen.add(tkr)
        pb = (str(h.get("para_birimi") or "TL").upper())
        pb = "USD" if pb in ("USD", "$", "DOLAR") else "TL"
        holdings.append({
            "ticker": tkr,
            "adet": _num(h.get("adet")),
            "fiyat": _num(h.get("fiyat")),
            "para_birimi": pb,
        })
    return {"ok": True, "holdings": holdings, "foto_sayisi": len(blocks) - 1}


def portfolio_add(d: dict) -> dict:
    """Tek bir pozisyonu portfoy tablosuna ekler."""
    kullanici = (d.get("kullanici") or "").strip()
    ticker = (str(d.get("ticker") or "").upper().replace(".IS", "").strip())
    adet = _num(d.get("adet"))
    fiyat = _num(d.get("alim_fiyati") if d.get("alim_fiyati") is not None
                 else d.get("fiyat"))
    para_birimi = (str(d.get("para_birimi") or "TL").upper())
    para_birimi = "USD" if para_birimi in ("USD", "$", "DOLAR") else "TL"

    if not kullanici:
        return {"ok": False, "hata": "Kullanıcı seçili değil."}
    if not ticker or adet is None or fiyat is None:
        return {"ok": False, "hata": "Hisse kodu, adet ve fiyat gerekli."}
    if _db is None:
        return {"ok": False, "hata": "Veritabanı erişilemiyor."}

    uid = _db.user_id_by_ad(kullanici)
    if uid is None:
        return {"ok": False, "hata": f"Kullanıcı bulunamadı: {kullanici}"}
    try:
        _db.add_position(uid, ticker, adet, fiyat, para_birimi=para_birimi)
    except Exception as e:
        return {"ok": False, "hata": f"Eklenemedi: {type(e).__name__}"}
    try:
        _db.add_memory(uid, "eylem",
                       {"ozet": f"Portföye eklendi: {ticker} {adet:g} @ {fiyat:g} {para_birimi}",
                        "eylem": "portfoy_ekle", "adet": adet, "fiyat": fiyat},
                       ticker=ticker)
    except Exception:
        pass
    return {"ok": True, "ticker": ticker, "adet": adet,
            "fiyat": fiyat, "para_birimi": para_birimi}


def portfolio_remove(d: dict) -> dict:
    """Bir portfoy pozisyonunu id'ye gore siler."""
    pid = d.get("id")
    if pid is None:
        return {"ok": False, "hata": "Pozisyon id gerekli."}
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT kullanici_id, ticker FROM portfoy WHERE id=?",
                            (int(pid),)).fetchone()
            cur = c.execute("DELETE FROM portfoy WHERE id=?", (int(pid),))
            c.commit()
    except (sqlite3.Error, ValueError, TypeError) as e:
        return {"ok": False, "hata": f"Silinemedi: {type(e).__name__}"}
    if row and cur.rowcount > 0 and _db is not None:
        try:
            _db.add_memory(row["kullanici_id"], "eylem",
                           {"ozet": f"Portföyden çıkarıldı: {row['ticker']}",
                            "eylem": "portfoy_sil"}, ticker=row["ticker"])
        except Exception:
            pass
    return {"ok": cur.rowcount > 0, "silinen": cur.rowcount}


_AY_TR = ["", "Oca", "Şub", "Mar", "Nis", "May", "Haz",
          "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]


def _tarih_kisa(iso: str) -> str:
    """2026-06-15 -> '15 Haz'."""
    try:
        y, m, d = (iso or "").split("-")[:3]
        return f"{int(d)} {_AY_TR[int(m)]}"
    except (ValueError, IndexError):
        return iso or ""


def get_model_portfoy() -> dict:
    """Model portfoy (botun 100K sanal portfoyu) - ozet + pozisyonlar + BIST100 kiyasi."""
    try:
        from src.portfolio import model
        s = model.summary()
    except Exception:
        return {"var": False}

    def _poz(p, kapali=False):
        tkr = (p.get("ticker") or "").upper()
        return {
            "ticker": tkr, "isim": company_name(tkr),
            "adet": round(p.get("adet") or 0, 2),
            "alis_fiyati": p.get("alis_fiyati"),
            "alis_tarihi": p.get("alis_tarihi"),
            "guncel_fiyat": p.get("kapanis_fiyati") if kapali else p.get("guncel_fiyat"),
            "kz_tl": p.get("kz_tl"), "kz_yuzde": p.get("kz_yuzde"),
            "gerekce": p.get("karar_gerekce"),
            "kapanis_tarihi": p.get("kapanis_tarihi"),
        }

    acik = [_poz(p) for p in s.get("acik_pozisyonlar", [])]
    kapali = [_poz(p, kapali=True) for p in s.get("kapali_pozisyonlar", [])]
    n = s.get("kapali_sayisi") or 0
    if (s.get("acik_sayisi") or 0) == 0 and n == 0:
        mesaj = "Model portföy henüz işlem yapmadı (her sabah AL kararıyla başlar)."
    else:
        yon = "kazançta" if (s.get("getiri_tl") or 0) >= 0 else "zararda"
        mesaj = (f"Bot 100.000 TL sanal sermaye ile {s.get('acik_sayisi')} açık, "
                 f"{n} kapanan işlem yaptı; toplam %{s.get('getiri_yuzde')} {yon}.")
        if s.get("bist100_fark_yuzde") is not None:
            ustun = "üstünde" if s["bist100_fark_yuzde"] >= 0 else "altında"
            mesaj += f" BIST-100'ün %{abs(s['bist100_fark_yuzde']):g} {ustun}."
    return {"var": True, "ozet": s, "acik": acik, "kapali": kapali, "mesaj": mesaj}


def get_paper_trading() -> dict:
    """Paper trading (sanal islem) ozeti + islem detaylari.

    Bot gercek piyasada sanal olarak yaptigi AL/SAT islemlerinin kar/zararini gosterir.
    """
    try:
        from src.portfolio import paper
        ozet = paper.summary()
    except Exception:
        ozet = {}
    satirlar = []
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM paper_trades ORDER BY id DESC LIMIT 100")]
    except sqlite3.Error:
        rows = []
    for r in rows:
        tkr = (r.get("ticker") or "").upper()
        durum = r.get("durum")
        kz = r.get("kz_yuzde")
        satirlar.append({
            "id": r.get("id"),
            "ticker": tkr,
            "isim": company_name(tkr),
            "karar": r.get("karar"),
            "fiyat": r.get("fiyat"),
            "adet": r.get("adet_sanal"),
            "tarih": r.get("tarih"),
            "kapanis_fiyati": r.get("kapanis_fiyati"),
            "kz_yuzde": kz,
            "durum": durum,
            "durum_tr": "Açık" if durum == "acik" else "Kapandı",
        })

    # Kullanici dostu ozet cumlesi
    n = ozet.get("kapali_sayisi") or 0
    toplam = ozet.get("toplam_kz_tl")
    if (ozet.get("islem_sayisi") or 0) == 0:
        mesaj = "Bot henüz sanal işlem yapmadı."
    else:
        yon = "kazandı" if (toplam or 0) >= 0 else "kaybetti"
        mesaj = (f"Bot gerçek piyasada sanal olarak {ozet.get('islem_sayisi')} işlem yaptı "
                 f"({ozet.get('acik_sayisi')} açık, {n} kapandı); "
                 f"toplam {abs(toplam or 0):.0f} TL {yon}.")
        if ozet.get("basari_orani_%") is not None:
            mesaj += f" Kapanan işlemlerde başarı oranı %{ozet['basari_orani_%']:g}."
    return {"ozet": ozet, "satirlar": satirlar, "mesaj": mesaj}


def get_haber_etki() -> dict:
    """KAP bildirim tipinin ortalama fiyat etkisi (1 gun) + son kayitlar."""
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM haber_etki ORDER BY id DESC LIMIT 300")]
    except sqlite3.Error:
        rows = []

    grup = {}
    for r in rows:
        kat = r.get("haber_kategori") or "Diğer"
        etki = r.get("etki_yuzde_1gun")
        g = grup.setdefault(kat, {"kategori": kat, "adet": 0, "etkili": 0, "toplam": 0.0})
        g["adet"] += 1
        if isinstance(etki, (int, float)):
            g["etkili"] += 1
            g["toplam"] += etki
    kategoriler = []
    for g in grup.values():
        ort = round(g["toplam"] / g["etkili"], 2) if g["etkili"] else None
        kategoriler.append({"kategori": g["kategori"], "adet": g["adet"],
                            "olculen": g["etkili"], "ort_etki_yuzde": ort})
    kategoriler.sort(key=lambda k: (k["ort_etki_yuzde"] is not None,
                                    abs(k["ort_etki_yuzde"] or 0)), reverse=True)

    en = next((k for k in kategoriler if k["ort_etki_yuzde"] is not None), None)
    if en:
        yon = "yükseliş" if en["ort_etki_yuzde"] >= 0 else "düşüş"
        mesaj = (f"{en['kategori']} bildirimleri 1 günde ortalama "
                 f"%{en['ort_etki_yuzde']:+g} {yon} etkisi yaptı "
                 f"({en['olculen']} ölçüm).")
    else:
        mesaj = "Henüz ölçülmüş haber etkisi yok; KAP bildirimleri biriktikçe dolacak."

    satirlar = [{
        "ticker": (r.get("ticker") or "").upper(),
        "baslik": r.get("baslik"),
        "kategori": r.get("haber_kategori"),
        "tarih": (r.get("haber_tarihi") or "")[:16].replace("T", " "),
        "etki_yuzde_1gun": r.get("etki_yuzde_1gun"),
    } for r in rows[:20]]
    return {"kategoriler": kategoriler, "satirlar": satirlar, "mesaj": mesaj}


def _karne_bucket(karar: str):
    """Karar tipini AL / TUT / SAT kovasina indirger (update_decisions/_classify uyumlu).
    AZALT/UZAK_DUR -> SAT (kacinma); VETO/KILL -> None (tip dagiliminda sayilmaz)."""
    k = (karar or "").upper()
    if "KILL" in k or "VETO" in k:
        return None
    if "AL" in k:
        return "AL"
    if "SAT" in k or "AZALT" in k or "UZAK" in k:
        return "SAT"
    if "TUT" in k or "BEKLE" in k:
        return "TUT"
    return None


def _karne_degisim(sonuc: str):
    """decisions.sonuc ('+3.2% · DOGRU') icinden yuzde degisimi cikarir (yoksa None)."""
    m = re.search(r"([+-]?\d+(?:\.\d+)?)%", sonuc or "")
    return float(m.group(1)) if m else None


def get_karne(kullanici: str | None = None) -> dict:
    """KARNE — botun GERCEK karar takibi (decisions tablosu).

    Basari, decisions.sonuc icindeki DOGRU/YANLIS'a dayanir; bu sonuc
    update_decisions.py tarafindan su kriterlerle hesaplanir (uyumlu):
      AL=fiyat yukseldi, TUT=|deg|<=%5, SAT/AZALT=fiyat dustu,
      UZAK_DUR/VETO=fiyat yukselmedi.
    NOT: decisions tablosu kullanici bazli degildir; karar istatistikleri bot
    geneldir. kullanici parametresi baglam/ileri kullanim icin kabul edilir.
    """
    from src.ai.learning import _outcome_wrong
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute("SELECT * FROM decisions ORDER BY id DESC")]
    except sqlite3.Error:
        rows = []

    toplam = len(rows)
    degerlendirilmis = dogru = 0
    tip = {b: {"toplam": 0, "dogru": 0} for b in ("AL", "TUT", "SAT")}
    en_iyi = en_kotu = None
    for r in rows:
        w = _outcome_wrong(r.get("sonuc"))      # None=bekliyor, False=dogru, True=yanlis
        if w is None:
            continue
        degerlendirilmis += 1
        if not w:
            dogru += 1
        b = _karne_bucket(r.get("karar"))
        if b in tip:
            tip[b]["toplam"] += 1
            if not w:
                tip[b]["dogru"] += 1
        deg = _karne_degisim(r.get("sonuc"))
        if deg is not None:
            tkr = (r.get("ticker") or "").upper()
            if not w and (en_iyi is None or deg > en_iyi["degisim"]):
                en_iyi = {"ticker": tkr, "karar": r.get("karar"),
                          "karar_label": _karar_label(r.get("karar")),
                          "degisim": deg, "tarih": r.get("tarih")}
            if w and (en_kotu is None or deg < en_kotu["degisim"]):
                en_kotu = {"ticker": tkr, "karar": r.get("karar"),
                           "karar_label": _karar_label(r.get("karar")),
                           "degisim": deg, "tarih": r.get("tarih")}

    son_kararlar = []
    for r in rows[:10]:                          # rows DESC -> en yeni 10 karar
        w = _outcome_wrong(r.get("sonuc"))
        durum = "bekliyor" if w is None else ("yanlis" if w else "dogru")
        tkr = (r.get("ticker") or "").upper()
        _, renk = _classify(r.get("karar"))
        son_kararlar.append({
            "id": r.get("id"), "ticker": tkr, "isim": company_name(tkr),
            "karar": r.get("karar"), "karar_label": _karar_label(r.get("karar")),
            "renk": renk, "puan": r.get("puan"), "durum": durum,
            "degisim": _karne_degisim(r.get("sonuc")), "tarih": r.get("tarih"),
        })

    def _oran(d, t):
        return round(d / t * 100) if t else None

    tip_basari = {b: {"toplam": v["toplam"], "dogru": v["dogru"],
                      "oran": _oran(v["dogru"], v["toplam"])}
                  for b, v in tip.items()}

    try:                                          # sektor bazli basari (learning.py)
        from src.ai.learning import sector_success_rates
        sek = sector_success_rates()
    except Exception:
        sek = {}
    sektor = [{"sektor": s, "toplam": a["toplam"], "dogru": a["dogru"],
               "oran": a.get("oran_%")}
              for s, a in sorted(sek.items(),
                                 key=lambda kv: (kv[1].get("oran_%") or 0), reverse=True)]

    mp = get_model_portfoy()                      # model portfoy + BIST-100 kiyasi
    piyasa = None
    if mp.get("var"):
        o = mp.get("ozet") or {}
        piyasa = {"model_getiri_%": o.get("getiri_yuzde"),
                  "bist100_getiri_%": o.get("bist100_getiri_yuzde"),
                  "fark_%": o.get("bist100_fark_yuzde")}

    return {
        "kullanici": kullanici,
        "genel": {"toplam": toplam, "degerlendirilmis": degerlendirilmis,
                  "dogru": dogru, "basari_orani": _oran(dogru, degerlendirilmis)},
        "tip_basari": tip_basari,
        "sektor": sektor,
        "son_kararlar": son_kararlar,
        "en_iyi": en_iyi,
        "en_kotu": en_kotu,
        "model_portfoy": mp,
        "piyasa_karsi": piyasa,
    }


def _karar_label(karar: str) -> str:
    return {"AL": "AL", "AL_TEMKINLI": "AL (temkinli)", "TUT": "TUT",
            "SAT": "SAT", "GUCLU_SAT": "Güçlü SAT", "VETO": "VETO",
            "AZALT": "AZALT", "UZAK_DUR": "UZAK DUR", "BEKLE": "BEKLE"}.get(
        (karar or "").upper(), karar or "—")


def get_decisions() -> dict:
    """Gercek karar gunlugu: bot'un verdigi AL/TUT/SAT kararlari (decisions tablosu).

    sonuc=None iken karar 'bekliyor'; ileride fiyat takibiyle dogru/yanlis isaretlenir.
    """
    rows = []
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM decisions ORDER BY id DESC LIMIT 100")]
    except sqlite3.Error:
        rows = []

    satirlar = []
    for r in rows:
        karar = r.get("karar")
        etiket, renk = _classify(karar)
        tkr = (r.get("ticker") or "").upper()
        sonuc = r.get("sonuc")
        puan = r.get("puan")
        risk = r.get("risk")
        eminlik = _eminlik_tr(r.get("eminlik"))
        gerekce = r.get("gerekce") or "Gerekçe kaydı yok."

        # Neden bu karari verdi (hangi veriye dayanarak)
        dayanak_parcalari = []
        if puan is not None:
            dayanak_parcalari.append(f"puan {puan}/10")
        if risk is not None:
            dayanak_parcalari.append(f"risk {risk}/10")
        if r.get("eminlik"):
            dayanak_parcalari.append(f"eminlik {eminlik.lower()}")
        dayanak = (" · ".join(dayanak_parcalari)) or "—"
        neden = gerekce
        if dayanak_parcalari:
            neden = f"{gerekce}\n\nDayanak: {dayanak}."

        # durum + Neden yanildi (piyasada ne degisti) + Cikarilan ders
        if sonuc is None or str(sonuc).strip() == "":
            durum, dogru = "bekliyor", None
            sonuc_metin = "Sonuç henüz belli değil — fiyat takip ediliyor."
            yanilma = ("Henüz yanılma/başarı belli değil; piyasada ne değiştiği, "
                       "kararın sonucu netleşince burada görünecek.")
            ders = ("Sonuç oluşunca bu kurulumda neyin işe yarayıp yaramadığı "
                    "buraya yazılacak.")
        else:
            s = str(sonuc).strip()
            up = s.upper()
            dogru = (up.startswith("+") or "DOGRU" in up or "DOĞRU" in up
                     or "ISABET" in up or "İSABET" in up)
            durum = "dogru" if dogru else "yanlis"
            sonuc_metin = s
            if dogru:
                yanilma = (f"Yanılma yok — beklenen yön tuttu ({s}). "
                           "Piyasa, karardaki sinyalleri doğruladı.")
                ders = ("Bu sinyal birleşimi işe yaradı; benzer kurulumda "
                        "yaklaşıma güven artırılabilir.")
            else:
                yanilma = (f"Beklenen yön tutmadı ({s}). Karardan sonra piyasa "
                           "ters yönde hareket etti; karar anındaki sinyaller bu "
                           "değişimi öngöremedi.")
                ders = ("Benzer durumda eminlik düşük ya da hacim teyidi zayıfsa "
                        "pozisyon küçültülmeli veya ek teyit beklenmeli.")
        satirlar.append({
            "id": r.get("id"),
            "ticker": tkr,
            "isim": company_name(tkr),
            "karar": karar,
            "karar_label": _karar_label(karar),
            "etiket": etiket,
            "renk": renk,
            "puan": puan,
            "risk": risk,
            "eminlik": eminlik,
            "dayanak": dayanak,
            "tarih": r.get("tarih"),
            "gerekce": gerekce,
            "neden": neden,
            "sonuc": sonuc,
            "sonuc_metin": sonuc_metin,
            "durum": durum,
            "dogru": dogru,
            "yanilma": yanilma,
            "ders": ders,
        })
    return {"satirlar": satirlar}


def get_alerts() -> list[dict]:
    """Bildirim paneli: son uyari/sinyaller (en fazla 10).

    Oncelik db.uyari_kayit; bos ise ai_commentary sinyallerinden turetir.
    """
    out = []
    kap_yorumlar = _read_json(DATA / "kap_yorumlar.json", {})  # run_alerts AI yorumlari
    # 1) Fiyat hareketi uyarilari (uyari_kayit) -> bot ozeti
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            for r in c.execute(
                    "SELECT * FROM uyari_kayit WHERE seviye IN ('ACIL','IZLE') "
                    "ORDER BY id DESC LIMIT 8"):
                tkr = (r["ticker"] or "").upper()
                yon = "yükseliş" if (r["degisim"] or 0) > 0 else "düşüş"
                kritik = (r["seviye"] == "ACIL")
                out.append({
                    "ticker": tkr, "isim": company_name(tkr), "tip": "uyari",
                    "tur": "Kritik Uyarı" if kritik else "Fiyat Seviyesi",
                    "baslik": f"{tkr} sert {yon}" if kritik else f"{tkr} fiyat hareketi",
                    "aciklama": _cap(f"%{r['degisim']:+.2f} {yon}; seviye {r['seviye']}. "
                                     "Pozisyonu gözden geçir.", 140),
                    "ilgili": [tkr], "ham_baslik": None,
                    "kaynak": "Fiyat hareketi", "url": None, "tarih": r["ts"] or r["tarih"],
                })
    except sqlite3.Error:
        pass

    # 2) AI sinyalleri + fiyatlanmamis haberler -> kullanicinin anlayacagi bot ozeti
    for rec in _commentary_by_ticker().values():
        tkr = (rec.get("ticker") or "").upper()
        ad = company_name(tkr)
        etiket, _ = _karar5(rec.get("final_decision"))
        ozet = _clean_summary(rec.get("gerekce") or "", 140, _AKSIYON.get(etiket, ""))
        if etiket == "AL":
            out.append({"ticker": tkr, "isim": ad, "tip": "firsat",
                        "tur": "Karar Güncellendi",
                        "baslik": _cap(f"{tkr} fırsat olabilir", 45),
                        "aciklama": ozet or "Görünüm olumlu; radarda değerlendirilebilir.",
                        "ilgili": [tkr], "ham_baslik": None,
                        "kaynak": "AI Analiz", "url": None, "tarih": None})
        elif etiket in ("SAT", "AZALT"):
            out.append({"ticker": tkr, "isim": ad, "tip": "uyari",
                        "tur": "Kritik Uyarı",
                        "baslik": _cap(f"{tkr} için dikkat", 45),
                        "aciklama": ozet or "Görünüm zayıf; pozisyon gözden geçirilmeli.",
                        "ilgili": [tkr], "ham_baslik": None,
                        "kaynak": "AI Analiz", "url": None, "tarih": None})
        for hb in (rec.get("haberler") or []):
            if hb.get("fiyatlanma") == "FIYATLANMADI":
                # KAP bildirimi icin AI yorumu varsa onu goster (run_alerts uretir),
                # yoksa hissenin kendi durumundan kisa ozel yorum.
                kap = kap_yorumlar.get(tkr) or {}
                kap_yorum = kap.get("yorum") if isinstance(kap, dict) else None
                aciklama = (kap_yorum or ozet
                            or f"{ad} tarafında yeni bir gelişme var; "
                               "etkisi henüz fiyata yansımadı.")
                out.append({"ticker": tkr, "isim": ad, "tip": "haber",
                            "tur": "Haber Etkisi",
                            "baslik": _cap(f"{tkr} için gelişme ihtimali", 45),
                            "aciklama": _cap(aciklama, 180),
                            "yorum": kap_yorum,
                            "ilgili": [tkr], "ham_baslik": hb.get("baslik"),
                            "kaynak": hb.get("kaynak") or "KAP", "url": hb.get("url"),
                            "tarih": hb.get("tarih")})
    return out[:14]


def get_summary() -> dict:
    """Ust serit: firsat (AL) ve uyari (SAT/VETO/fiyatlanmamis haber) sayilari."""
    firsat = uyari = 0
    for rec in _commentary_by_ticker().values():
        etiket, _ = _classify(rec.get("final_decision"))
        if etiket == "AL":
            firsat += 1
        elif etiket in ("SAT", "VETO"):
            uyari += 1
        if any(h.get("fiyatlanma") == "FIYATLANMADI"
               for h in (rec.get("haberler") or [])):
            uyari += 1
    return {"firsat": firsat, "uyari": uyari, "bildirim": len(get_alerts())}


# ============================================================================
# YENI ARAYUZ (5 sekme): Bugun / Portfoyum / Radar / Bota Sor / Bildirimler
# ============================================================================
_AI_OVERVIEW_CACHE = {}          # kullanici -> (ts, metin)
_AI_TTL = 1800.0
_CHAT_MODEL = "claude-sonnet-4-6"
_USER_AD = {"serhat": "Serhat", "yigit": "Yiğit", "ufuk": "Ufuk"}


def _karar5(fd: str):
    """final_decision -> sade 5'li etiket (AL/BEKLE/TUT/AZALT/SAT) + renk."""
    d = (fd or "").upper()
    return {
        "AL": ("AL", "green"), "AL_TEMKINLI": ("BEKLE", "yellow"),
        "TUT": ("TUT", "yellow"), "BEKLE": ("BEKLE", "yellow"),
        "VETO": ("BEKLE", "yellow"),
        "SAT": ("AZALT", "red"), "GUCLU_SAT": ("SAT", "red"),
        "AZALT": ("AZALT", "red"), "UZAK_DUR": ("SAT", "red"),
    }.get(d, ("TUT", "yellow"))


def _risk_etiket(score):
    if score is None:
        return ("—", "gray")
    if score >= 7:
        return ("Yüksek risk", "red")
    if score >= 4:
        return ("Orta risk", "yellow")
    return ("Düşük risk", "green")


def _risk_kisa(score):
    """('Yüksek'/'Orta'/'Düşük'/'—', renk) - kart icin sade risk etiketi."""
    if score is None:
        return ("—", "gray")
    if score >= 7:
        return ("Yüksek", "red")
    if score >= 4:
        return ("Orta", "yellow")
    return ("Düşük", "green")


def _cap(s, n):
    """Metni n karaktere kirpar (kelime sinirinda, sonuna …)."""
    s = " ".join((s or "").split()).strip()
    if len(s) <= n:
        return s
    return s[:n - 1].rsplit(" ", 1)[0].rstrip(" ,.;:") + "…"


_DATA_RE = re.compile(
    r"analist|hedef|ortalama|f/?k|pd/?dd|fav[öo]k|roe|bor[çc]/?[öo]z|"
    r"\bma\s?\d|hacim|volatil|\d+\s*kurum|52\s*hafta|\d+\s*g[üu]nl[üu]k|"
    r"%\s*\d|\(\s*\d|\bpuan\b|\d+[.,]\d+\s*(?:tl|₺)", re.I)


def _clean_summary(gerekce: str, limit: int = 160, action: str = "") -> str:
    """Kart icin sade, niteliksel ozet. CUMLECIK (clause) duzeyinde temizler:
    analist/hedef/F-K/MA/hacim/% iceren parcalari atar (bunlar detayda gosterilir),
    kalan niteliksel cumleciklerden limit dolana kadar birlestirir.
    Hicbir temiz parca yoksa karar aksiyonuna duser (asla ham sayi basmaz)."""
    text = " ".join((gerekce or "").split())
    parts = re.split(r"[;:,.!?]\s+|[;:,.!?](?=[A-ZÇĞİÖŞÜ])", text)
    clean = [p.strip() for p in parts
             if p.strip() and len(p.strip()) > 12 and not _DATA_RE.search(p)]
    out = ""
    for c in clean:
        cand = (out + " " + c).strip() if out else c
        if out and len(cand) > limit:
            break
        out = cand
    out = out or action or ""
    # bagimsiz kalan bas baglaci temizle ("ancak ...", "ama ...")
    out = re.sub(r"^\s*(ancak|ama|fakat|ne var ki|bununla birlikte)[,\s]+", "",
                 out, flags=re.I).strip()
    if out and not out.endswith((".", "!", "?")):
        out += "."
    return _cap(out[:1].upper() + out[1:] if out else out, limit)


_AKSIYON = {
    "AL": "Kademeli alım düşünülebilir.",
    "BEKLE": "Acele etme; teyit için bekle.",
    "TUT": "Pozisyonu koru, yeni alım için acele etme.",
    "AZALT": "Pozisyonu azaltmayı değerlendir.",
    "SAT": "Satışı değerlendir.",
}

# Ana kartlarda "Risk: Yüksek" yerine dogal, yumusak durum ifadesi (renk: green/yellow/red/gray)
_STATUS = {
    "AL": ("Fırsat olabilir", "green"),
    "BEKLE": ("Yeni alım için net değil", "yellow"),
    "TUT": ("Pozisyon korunabilir", "gray"),
    "AZALT": ("Yakından izle", "red"),
    "SAT": ("Riskli, dikkatli ol", "red"),
}


def _status_phrase(decision: str, risk_score=None):
    """Karar + risk -> sade durum ifadesi. Yuksek riskte tonu sertlestirir."""
    metin, renk = _STATUS.get(decision, ("İzlemede", "gray"))
    if decision in ("TUT", "BEKLE") and (risk_score or 0) >= 7:
        return ("Şimdilik sakin, dikkatli ol", "yellow")
    return (metin, renk)


def _structured(rec: dict) -> dict:
    """AI ciktisini her ekran icin AYRI sade alanlara boler (formatter katmani):
    decision / cardText / actionText / radarText / reasonShort / reasonLong /
    statusPhrase (+ teknik: risk / riskReason)."""
    etiket, renk = _karar5(rec.get("final_decision"))
    gerekce = rec.get("gerekce", "") or ""
    rs = (rec.get("risk") or {}).get("score")
    rk, rkr = _risk_kisa(rs)
    action = _AKSIYON.get(etiket, "")
    sp, spc = _status_phrase(etiket, rs)
    card = _clean_summary(gerekce, 160, action)
    return {
        "decision": etiket, "decision_renk": renk,
        "cardText": card, "actionText": _cap(action, 80),
        "radarText": _clean_summary(gerekce, 120, action),
        "reasonShort": _clean_summary(gerekce, 240, action),
        "reasonLong": _cap(gerekce, 500),
        "statusPhrase": sp, "statusColor": spc,
        # teknik (yalniz detay ekraninda gosterilir)
        "risk": rk, "risk_renk": rkr, "riskReason": _cap(_risk_sebep(rec), 80),
        # geriye donuk uyumluluk
        "summary": card, "reason": _cap(gerekce, 500), "action": action,
    }


def _risk_sebep(rec: dict) -> str:
    sig = rec.get("kullanilan_on_sinyal", {}) or {}
    sek = rec.get("sektor_korelasyonu") or {}
    parts = []
    if (sig.get("volatilite_%") or 0) >= 2.5:
        parts.append("fiyat oynak")
    temel = rec.get("temel") or {}
    if (temel.get("borc_ozsermaye") or 0) and temel["borc_ozsermaye"] >= 80:
        parts.append("borç yüksek")
    if sek.get("ozet"):
        parts.append(sek["ozet"].lower())
    return "; ".join(parts) or (rec.get("risk", {}) or {}).get("message", "").rstrip(".") or "—"


# Kategori -> Unsplash temsili gorsel (API key gerekmez; dogrudan URL).
_UNSPLASH = {
    "Jeopolitik": "https://images.unsplash.com/photo-1580060839134-75a5edca2e99?w=600",
    "Makro": "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=600",
    "Enerji/Sektör": "https://images.unsplash.com/photo-1518186285589-2f7649de83e0?w=600",
    "Şirket": "https://images.unsplash.com/photo-1486406146926-c627a92ad1ab?w=600",
    "Piyasa": "https://images.unsplash.com/photo-1611974789855-9c2a0a7236a3?w=600",
}


def _haber_gorsel(baslik: str):
    """Haber kategorisi -> ikon + gradient + temsili gorsel (Unsplash URL)."""
    t = _norm(baslik or "")
    if any(w in t for w in ("savas", "ates", "hurmuz", "iran", "israil", "jeopolit",
                            "saldiri", "ambargo", "gerilim", "catisma")):
        kat, ikon, grad = ("Jeopolitik", "◆",
                           "linear-gradient(140deg,#1a0f0f 0%,#7f1d1d 60%,#b45309 100%)")
    elif any(w in t for w in ("faiz", "enflasyon", "tufe", "dolar", "kur", "merkez",
                              "tcmb", "fed", "makro", "buyume", "resesyon")):
        kat, ikon, grad = ("Makro", "▦",
                           "linear-gradient(140deg,#0b1220 0%,#1e3a8a 60%,#5b21b6 100%)")
    elif any(w in t for w in ("petrol", "enerji", "dogalgaz", "elektrik", "celik",
                              "emtia", "altin", "gumus")):
        kat, ikon, grad = ("Enerji/Sektör", "⬡",
                           "linear-gradient(140deg,#1a1206 0%,#92400e 55%,#d97706 100%)")
    elif any(w in t for w in ("bilanco", "kar", "temettu", "ihrac", "sozlesme", "yatirim",
                              "fabrika", "satin alma", "birlesme", "hat", "siparis")):
        kat, ikon, grad = ("Şirket", "▲",
                           "linear-gradient(140deg,#06120e 0%,#064e3b 55%,#0f766e 100%)")
    else:
        kat, ikon, grad = ("Piyasa", "◉",
                           "linear-gradient(140deg,#0c0f1a 0%,#3730a3 55%,#1e40af 100%)")
    return {"kategori": kat, "ikon": ikon, "gradient": grad,
            "img": _UNSPLASH.get(kat, _UNSPLASH["Şirket"])}


def _mini_view(rec: dict, ozet_limit: int = 160) -> dict:
    """Commentary kaydindan sade kart (Bugun/Radar icin) - yapisal + limitli."""
    tkr = (rec.get("ticker") or "").upper()
    sig = rec.get("kullanilan_on_sinyal", {}) or {}
    st = _structured(rec)
    cardtext = st["radarText"] if ozet_limit <= 120 else st["cardText"]
    return {
        "ticker": tkr, "isim": company_name(tkr),
        "market": rec.get("market", "bist"),
        "etiket": st["decision"], "renk": st["decision_renk"],
        "fiyat": sig.get("son_kapanis"), "gunluk": sig.get("gunluk_degisim_%"),
        "para_birimi": rec.get("para_birimi", "₺"),
        "cardText": cardtext, "actionText": st["actionText"],
        "statusPhrase": st["statusPhrase"], "statusColor": st["statusColor"],
        # geriye donuk + ic mantik (today etiketleri risk_renk kullanir)
        "summary": cardtext, "action": st["actionText"],
        "risk": st["risk"], "risk_renk": st["risk_renk"], "riskReason": st["riskReason"],
        "skor": rec.get("score"),
        # "neden ilginc?" -> AI'nin neden_simdi alani (radar kartinda kisa not)
        "neden": _cap(rec.get("neden_simdi") or "", 80),
    }


def _owned_by_user(kullanici=None) -> list[str]:
    try:
        with sqlite3.connect(DB_PATH) as c:
            if kullanici:
                q = ("SELECT DISTINCT p.ticker FROM portfoy p JOIN kullanici k "
                     "ON k.id = p.kullanici_id WHERE LOWER(k.ad)=LOWER(?)")
                rows = c.execute(q, (kullanici,))
            else:
                rows = c.execute("SELECT DISTINCT ticker FROM portfoy")
            return [(r[0] or "").upper().split(".")[0] for r in rows]
    except sqlite3.Error:
        return []


def _overview_fallback(recs) -> str:
    """AI'siz, aninda gosterilebilen deterministik portfoy ozeti."""
    if not recs:
        return "Portföyünde takip ettiğimiz hisse yok. Radar sekmesinden fırsatlara göz atabilirsin."
    al = sum(1 for r in recs if _karar5(r.get("final_decision"))[0] == "AL")
    sat = sum(1 for r in recs if _karar5(r.get("final_decision"))[0] in ("SAT", "AZALT"))
    riskli = sum(1 for r in recs if ((r.get("risk") or {}).get("score") or 0) >= 7)
    return (f"Portföyünde {len(recs)} hisse var: {al} tanesi olumlu, {sat} tanesi "
            f"satış/azaltma yönünde, {riskli} hissede risk yüksek. "
            "Genelde panik gerektiren bir tablo yok; riskli olanları yakından izle.")


def _portfolio_overview(kullanici, recs) -> str:
    """Portfoy geneli icin 2-3 cumle Claude yorumu (kullanici basina onbellekli).

    YAVAS olabilir (canli Claude); arayuz bunu /api/overview ile ASENKRON ceker.
    """
    if not recs:
        return _overview_fallback(recs)
    ck = (kullanici or "_").lower()
    hit = _AI_OVERVIEW_CACHE.get(ck)
    if hit and (time.monotonic() - hit[0]) < _AI_TTL:
        return hit[1]
    fallback = _overview_fallback(recs)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return fallback
    try:
        import anthropic
        ozet = [{"hisse": r.get("ticker"), "karar": r.get("final_decision"),
                 "puan": r.get("score"), "risk": (r.get("risk") or {}).get("score"),
                 "not": _ilk_cumleler(r.get("gerekce", ""), 1)} for r in recs]
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=_CHAT_MODEL, max_tokens=300,
            system=("Sen Max'sin: 40 yasinda, 25 yillik tecrubeli bir Turk borsa uzmani. "
                    "Direkt ve net, gereksiz yumusatmazsin; kendini tanitma, dogrudan ise gir. "
                    "Sade, sicak Turkce; jargon yok. Portfoyun GENEL durumunu 2-3 cumlede "
                    "ozetle: panik mi var, nelere dikkat etmeli. Sadece verilen veriyi kullan, "
                    "rakam uydurma. Markdown, baslik, yildiz veya madde KULLANMA; sadece duz "
                    "cumleler yaz."),
            messages=[{"role": "user", "content":
                       "Portfoy hisseleri:\n" + json.dumps(ozet, ensure_ascii=False)}],
        )
        txt = "".join(getattr(b, "text", "") for b in resp.content
                      if getattr(b, "type", "") == "text").strip()
        txt = re.sub(r"[#*`>_]+", "", txt).strip() or fallback
    except Exception:
        txt = fallback
    _AI_OVERVIEW_CACHE[ck] = (time.monotonic(), txt)
    return txt


def get_today(kullanici=None) -> dict:
    comm = _commentary_by_ticker()
    owned = _owned_by_user(kullanici)
    recs = [comm[t] for t in owned if t in comm and not comm[t].get("skipped")]

    now = datetime.now(ZoneInfo("Europe/Istanbul"))
    saat = now.hour
    selam = ("Günaydın" if 5 <= saat < 12 else
             "İyi günler" if 12 <= saat < 18 else "İyi akşamlar")
    ad = _USER_AD.get((kullanici or "").lower(), (kullanici or "").title() or "")
    _AYLAR = ["", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
              "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
    tarih_str = f"Bugün · {now.day} {_AYLAR[now.month]}"

    # "Hisselerimde durum": kullanicinin TUM pozisyonlari (AI yorumu olmayanlar dahil).
    # get_portfolio canli fiyat + (varsa) AI analizini birlestirir.
    try:
        pozisyonlar = get_portfolio(kullanici).get("pozisyonlar", [])
    except Exception:
        pozisyonlar = []
    hisseler = [{
        "ticker": p.get("ticker"), "isim": p.get("isim"),
        "market": p.get("market", "bist"), "para_birimi": p.get("para_birimi", "₺"),
        "fiyat": p.get("guncel"), "gunluk": p.get("gunluk"),
        "etiket": p.get("karar"), "renk": p.get("karar_renk", "gray"),
        "cardText": p.get("cardText", ""), "actionText": p.get("actionText", ""),
        "statusPhrase": p.get("statusPhrase", ""), "statusColor": p.get("statusColor", "gray"),
        "risk_renk": p.get("risk_renk", "gray"), "skor": None,
        "kz": p.get("kz"), "kz_yuzde": p.get("kz_yuzde"),
    } for p in pozisyonlar]
    sat_n = sum(1 for h in hisseler if h["etiket"] in ("SAT", "AZALT"))
    risk_n = sum(1 for h in hisseler if h["risk_renk"] == "red")
    koru_n = sum(1 for h in hisseler if h["etiket"] in ("TUT", "BEKLE", "AL"))
    etiketler = [
        {"metin": "Panik yok" if sat_n == 0 else "Dikkatli ol",
         "renk": "green" if sat_n == 0 else "red"},
        {"metin": f"{risk_n} hisse dikkat", "renk": "yellow"},
        {"metin": f"{koru_n} pozisyon koru", "renk": "gray"},
    ]

    # Bugunun onemli haberi: tum yorumlardaki haberlerden en dikkat cekici
    haber = None
    best = None
    for rec in comm.values():
        for hb in (rec.get("haberler") or []):
            puan = (2 if hb.get("fiyatlanma") == "FIYATLANMADI" else 0) + \
                   (1 if hb.get("tazelik") == "YENI" else 0)
            if best is None or puan > best[0]:
                best = (puan, rec, hb)
    if best:
        _, rec, hb = best
        tkr = (rec.get("ticker") or "").upper()
        gor = _haber_gorsel(hb.get("baslik"))
        etiket5, _ = _karar5(rec.get("final_decision"))
        etki = ("Negatif" if etiket5 in ("SAT", "AZALT") else
                "Pozitif" if etiket5 == "AL" else "Nötr")
        haber = {**gor, "baslik": hb.get("baslik"), "tarih": hb.get("tarih"),
                 "kaynak": hb.get("kaynak"), "url": hb.get("url"), "ticker": tkr,
                 "etkilenen": [tkr], "etki": etki,
                 "yorum": _clean_summary(rec.get("gerekce", ""), 140,
                                         _AKSIYON.get(etiket5, ""))}

    firsatlar = [_mini_view(r) for r in comm.values()
                 if _karar5(r.get("final_decision"))[0] == "AL"
                 and (r.get("score") or 0) >= _OPPORTUNITY_MIN]
    firsatlar.sort(key=lambda c: c.get("skor") or 0, reverse=True)

    # AI yorumu /api/overview ile asenkron gelir; bu aninda gosterilen fallback.
    if recs:
        yorum = _overview_fallback(recs)
    elif hisseler:
        yorum = (f"Portföyünde {len(hisseler)} hisse var. Güncel fiyatlar yüklendi; "
                 "detaylı yorum için kartlara dokun.")
    else:
        yorum = _overview_fallback(recs)
    # "Takip Ettiklerim": kullanicinin kisisel BIST takip listesi, portfoyde
    # OLMAYANLAR. Canli fiyat toplu cekilir; bot karari (varsa) eklenir.
    wl = _load_watchlist()
    owned_set = {(t or "").upper() for t in owned}
    seen_t = set(owned_set)
    watch_bist = []
    for t in wl.get("kisisel", []):
        tk = (t or "").upper().split(".")[0]
        if tk and tk not in seen_t:
            seen_t.add(tk)
            watch_bist.append(tk)
    live_w = _yf_prices([f"{t}.IS" for t in watch_bist]) if watch_bist else {}
    takip_listesi = []
    for tk in watch_bist:
        card = _stock_card(comm[tk]) if tk in comm else _minimal_card(tk)
        px = live_w.get(f"{tk}.IS") or {}
        fiyat = px.get("fiyat") if px.get("fiyat") is not None else card.get("fiyat")
        gunluk = px.get("gunluk") if px.get("gunluk") is not None else card.get("gunluk")
        takip_listesi.append({
            "ticker": tk, "isim": card.get("isim"), "market": "bist",
            "para_birimi": "₺", "fiyat": fiyat, "gunluk": gunluk,
            "etiket": card.get("etiket"), "renk": card.get("renk", "gray"),
        })

    return {
        "selamlama": f"{selam}{(' ' + ad) if ad else ''}",
        "tarih": tarih_str,
        "portfoy_yorum": _cap(yorum, 280),
        "etiketler": etiketler,
        "hisseler": hisseler,
        "takip_listesi": takip_listesi,
        "onemli_haber": haber,
        "firsatlar": firsatlar[:5],
    }


def get_chat_suggestions(kullanici=None) -> list[str]:
    """Bota Sor icin kullanicinin portfoyune gore kisisellesmis 5 hazir soru.

    - THYAO portfoydeyse -> 'THYAO'yu sat mi?'
    - Zararda pozisyon varsa -> 'En cok zarardaki hissem ... ne yapmali?'
    - AL sinyali varsa (portfoy/radar) -> 'Bugun alim yapilir mi?'
    Kalanlar sabit sorularla 5'e tamamlanir."""
    comm = _commentary_by_ticker()
    try:
        pozisyonlar = get_portfolio(kullanici).get("pozisyonlar", [])
    except Exception:
        pozisyonlar = []
    tickers = [(p.get("ticker") or "").upper() for p in pozisyonlar]
    sorular: list[str] = []

    # 1) Portfoyde belirli hisse -> sat mi? (THYAO oncelikli, yoksa ilk pozisyon)
    if "THYAO" in tickers:
        sorular.append("THYAO'yu sat mı?")
    elif tickers:
        sorular.append(f"{tickers[0]}'yu sat mı?")

    # 2) En cok zarardaki hisse
    zararli = [p for p in pozisyonlar
               if isinstance(p.get("kz_yuzde"), (int, float)) and p["kz_yuzde"] < 0]
    if zararli:
        en = min(zararli, key=lambda p: p.get("kz_yuzde"))
        t = (en.get("ticker") or "").upper()
        sorular.append(f"En çok zarardaki hissem {t} ne yapmalı?" if t
                       else "En çok zarardaki hissem ne yapmalı?")

    # 3) AL sinyali (once portfoyde, yoksa radarda)
    al_var = any(_karar5((comm.get(t) or {}).get("final_decision"))[0] == "AL"
                 for t in tickers if t in comm)
    if not al_var:
        al_var = any(_karar5(r.get("final_decision"))[0] == "AL"
                     for r in comm.values() if not r.get("skipped"))
    if al_var:
        sorular.append("Bugün alım yapılır mı?")

    # Dinamik SEKTOR sorusu: kullanicinin en cok hissesi bulunan sektore gore
    try:
        kapsam = set(tickers)
        wl = _load_watchlist()
        kapsam |= {(t or "").upper().split(".")[0] for t in wl.get("kisisel", [])}
        best, bestn = None, 0
        for sek in _SEKTORLER.values():
            n = len(kapsam & set(sek["tickers"]))
            if n > bestn:
                bestn, best = n, sek
        if best and bestn >= 1:
            q = f"{best['ad']} sektörü nasıl?"
            if q not in sorular:
                sorular.append(q)
    except Exception:
        pass

    # Kalani sabit/genel sorularla doldur (tekrar etmeden)
    varsayilan = [
        "Bugün portföyümde dikkat etmem gereken ne var?",
        "En riskli hissem hangisi?",
        "Bugün ne yapmalıyım?",
        "Hangi hisseyi azaltmalıyım?",
        "Piyasa bugün nasıl görünüyor?",
    ]
    for s in varsayilan:
        if len(sorular) >= 5:
            break
        if s not in sorular:
            sorular.append(s)

    # Profil tamamlama: eksik alan (kayip toleransi) varsa tamamlama sorusunu en basa al
    try:
        uid = _uid(kullanici)
        if uid:
            from src.db import database as db
            prof = db.get_profile(uid) or {}
            if prof.get("kayip_toleransi_yuzde") is None:
                q = "Kayıp toleransın nedir? (örn: %10)"
                if q in sorular:
                    sorular.remove(q)
                sorular.insert(0, q)
    except Exception:
        pass
    return sorular[:5]


# --- Gunun Hareketlileri: BIST-100 + dosya onbellek (15 dk) + arka plan guncelleme ---
_GH_PATH = DATA / "gunun_hareketlileri.json"
_GH_TTL = 900                      # 15 dakika
_GH_LOCK = threading.Lock()
_GH_REFRESHING = {"v": False}


def _bist100_kodlar() -> list[str]:
    """config/bist100.json'dan benzersiz BIST hisse kodlari (taban, .IS'siz)."""
    raw = _read_json(CONFIG / "bist100.json", [])
    if isinstance(raw, dict):
        raw = raw.get("hisseler") or raw.get("tickers") or []
    out = []
    for t in raw:
        base = (t or "").upper().split(".")[0].strip()
        if base and base not in out:
            out.append(base)
    return out


def _gh_compute() -> dict:
    """BIST-100 listesi icin TEK toplu yfinance batch'iyle gunun hareketlileri.

    AI yok; yalniz fiyat + hacim. ~1 ay pencere: gunluk degisim ve hacim/ortalama
    hacim (anomali) icin gerekli. Hafta sonu/borsa kapaliysa son islem gunu verisi
    kullanilir (son_kapanis=True)."""
    bos = {"yukselen": [], "dusen": [], "hacim": [], "populer": [],
           "son_kapanis": False, "veri_tarihi": None}
    kodlar = _bist100_kodlar()
    if not kodlar:
        return bos
    syms = [f"{k}.IS" for k in kodlar]
    try:
        import yfinance as yf
        df = yf.download(syms, period="1mo", interval="1d",
                         progress=False, threads=True, auto_adjust=True)
    except Exception:
        return bos
    try:
        closes, vols = df["Close"], df["Volume"]
    except Exception:
        return bos
    try:
        import pandas as pd
        son_tarih = pd.Timestamp(df.index[-1]).date()
    except Exception:
        son_tarih = None
    bugun = datetime.now(ZoneInfo("Europe/Istanbul")).date()
    son_kapanis = bool(son_tarih and son_tarih < bugun)
    coklu = len(syms) > 1
    satirlar = []
    for k, sym in zip(kodlar, syms):
        try:
            c = (closes[sym] if coklu else closes).dropna()
            v = (vols[sym] if coklu else vols).dropna()
            if len(c) < 2:
                continue
            last, prev = float(c.iloc[-1]), float(c.iloc[-2])
            chg = (last - prev) / prev * 100 if prev else 0.0
            last_vol = float(v.iloc[-1]) if len(v) else 0.0
            taban = v.iloc[-21:-1] if len(v) >= 7 else v.iloc[:-1]
            avg_vol = float(taban.mean()) if len(taban) else 0.0
            hacim_kat = (last_vol / avg_vol) if avg_vol else 0.0
            satirlar.append({
                "ticker": k, "isim": company_name(k), "market": "bist",
                "para_birimi": "₺", "fiyat": round(last, 2),
                "gunluk": round(chg, 2), "degisim": round(chg, 2),
                "hacim": int(last_vol), "hacim_kat": round(hacim_kat, 2)})
        except Exception:
            continue
    if not satirlar:
        return bos
    return {
        "yukselen": sorted(satirlar, key=lambda r: r["degisim"], reverse=True)[:5],
        "dusen": sorted(satirlar, key=lambda r: r["degisim"])[:5],
        "hacim": sorted(satirlar, key=lambda r: r["hacim_kat"], reverse=True)[:5],
        # populer: kombinasyon skoru = |degisim| + hacim anomalisi agirligi
        "populer": sorted(
            satirlar, key=lambda r: abs(r["degisim"]) + max(0.0, r["hacim_kat"] - 1) * 4,
            reverse=True)[:5],
        "son_kapanis": son_kapanis,
        "veri_tarihi": son_tarih.isoformat() if son_tarih else None,
        "taranan": len(satirlar),
    }


def _gh_save(data: dict) -> None:
    try:
        data = {**data, "guncelleme_ts": time.time(),
                "guncelleme": datetime.now(ZoneInfo("Europe/Istanbul")).isoformat(
                    timespec="seconds")}
        DATA.mkdir(exist_ok=True)
        _GH_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _gh_refresh_bg() -> None:
    """Arka planda yeniden hesapla + cache'e yaz (ayni anda yalniz bir kez)."""
    with _GH_LOCK:
        if _GH_REFRESHING["v"]:
            return
        _GH_REFRESHING["v"] = True

    def _run():
        try:
            data = _gh_compute()
            if data.get("yukselen"):
                _gh_save(data)
        finally:
            _GH_REFRESHING["v"] = False

    threading.Thread(target=_run, daemon=True).start()


def get_gunun_hareketlileri() -> dict:
    """BIST-100 'Gunun Hareketlileri'. Dosya onbellek: data/gunun_hareketlileri.json
    (15 dk). Once cache'den sunar; bayatsa arka planda gunceller (eskisini doner).
    Cache hic yoksa ilk seferde senkron hesaplar."""
    cached = _read_json(_GH_PATH, None)
    if isinstance(cached, dict) and cached.get("yukselen"):
        yas = time.time() - (cached.get("guncelleme_ts") or 0)
        if yas >= _GH_TTL:
            _gh_refresh_bg()              # bayat -> arka planda guncelle, eskisini sun
        return cached
    data = _gh_compute()                  # cache yok -> ilk sefer senkron
    if data.get("yukselen"):
        _gh_save(data)
    return data


def get_radar(market: str = "all") -> dict:
    comm = _commentary_by_ticker()
    wl = _load_watchlist()
    izleme_kodlar = {t.upper() for t in wl.get("kisisel", [])}

    alinabilir, riskli, yakin_takip = [], [], []
    for rec in comm.values():
        etiket, _ = _karar5(rec.get("final_decision"))
        mv = _mini_view(rec, ozet_limit=120)   # radar: kisa
        skor = rec.get("score") or 0
        if etiket == "AL":
            alinabilir.append(mv)
        elif etiket in ("SAT", "AZALT"):
            riskli.append(mv)
        elif 6 <= skor <= 7:                   # AL degil ama ilginc -> Yakin Takip
            yakin_takip.append(mv)
    alinabilir.sort(key=lambda c: c.get("skor") or 0, reverse=True)
    yakin_takip.sort(key=lambda c: c.get("skor") or 0, reverse=True)
    izleme = [_mini_view(comm[t], ozet_limit=120) if t in comm else
              {"ticker": t, "isim": company_name(t), "market": "bist",
               "etiket": None, "renk": "gray", "fiyat": None, "gunluk": None,
               "para_birimi": "₺", "summary": "", "action": "",
               "risk": "—", "risk_renk": "gray", "riskReason": ""}
              for t in izleme_kodlar]
    return {"alinabilir": alinabilir, "izleme": izleme,
            "yakin_takip": yakin_takip, "riskli": riskli}


def _price_series(ticker: str, market: str = "bist", gun: int = 30) -> dict:
    """Son ~gun gunluk kapanis serisi + destek/direnc icin yuksek/dusuk."""
    from src.data.factory import get_data_source
    t = (ticker or "").upper().replace(".IS", "")
    symbol = t if market in ("abd", "kripto") else f"{t}.IS"
    start = (datetime.now(ZoneInfo("Europe/Istanbul")).date()
             - timedelta(days=gun + 20)).isoformat()
    try:
        df = get_data_source().get_history(symbol, start=start)
        df = df[df["Volume"] > 0].tail(gun)
    except Exception:
        return {"seri": [], "dusuk": None, "yuksek": None, "son": None}
    if df is None or df.empty:
        return {"seri": [], "dusuk": None, "yuksek": None, "son": None}
    import pandas as pd
    seri = []
    for ix, cl, vol in zip(df.index, df["Close"], df["Volume"]):
        try:
            cv = float(cl)
        except (TypeError, ValueError):
            continue
        if cv != cv:                       # NaN kapanis (tamamlanmamis/eksik bar)
            continue
        seri.append({"t": pd.Timestamp(ix).date().isoformat(),
                     "c": round(cv, 2),
                     "v": int(vol) if vol == vol else 0})
    if not seri:
        return {"seri": [], "dusuk": None, "yuksek": None, "son": None}
    return {"seri": seri,
            "dusuk": round(float(df["Low"].min()), 2),
            "yuksek": round(float(df["High"].max()), 2),
            "son": seri[-1]["c"]}


def get_stock_detail(ticker: str, market: str = "bist") -> dict:
    tkr = (ticker or "").upper().replace(".IS", "")
    comm = _commentary_by_ticker()
    rec = comm.get(tkr)
    base = _stock_card(rec) if rec else _minimal_card(tkr)
    etiket, renk = _karar5(rec.get("final_decision")) if rec else (None, "gray")

    ps = _price_series(tkr, market)
    son = ps["son"] or base.get("fiyat")
    analist = (rec or {}).get("analist") or {}
    hedef = analist.get("ortalama_hedef") or (round(son * 1.15, 2) if son else None)
    destek = ps["dusuk"]
    direnc = ps["yuksek"]
    stop = round(son * 0.92, 2) if son else None

    haberler = []
    for hb in ((rec or {}).get("haberler") or [])[:3]:
        haberler.append({**_haber_gorsel(hb.get("baslik")),
                         "baslik": hb.get("baslik"), "tarih": hb.get("tarih"),
                         "kaynak": hb.get("kaynak"), "url": hb.get("url"),
                         "etki": ("Negatif" if hb.get("fiyatlanma") == "FIYATLANMADI"
                                  and etiket in ("SAT", "AZALT") else "Nötr")})

    st = _structured(rec) if rec else {}
    rk, rkr = _risk_kisa((rec or {}).get("risk", {}).get("score") if rec else None)
    # bos seviye gosterme: yoksa None birak (frontend atlar)
    seviyeler = {k: v for k, v in
                 {"destek": destek, "direnc": direnc, "hedef": hedef, "stop": stop}.items()
                 if v is not None}
    return {
        "ticker": tkr, "isim": company_name(tkr), "market": market,
        "para_birimi": (rec or {}).get("para_birimi") or base.get("para_birimi", "₺"),
        "fiyat": son, "gunluk": base.get("gunluk"),
        "decision": etiket, "renk": renk,
        "puan": (rec or {}).get("score"),
        "risk_skoru": (rec or {}).get("risk", {}).get("score") if rec else None,
        "guven": base.get("eminlik", "—"),
        "risk": rk, "risk_renk": rkr,
        "summary": st.get("summary", ""),
        "reason": st.get("reason") or "Bu hisse için henüz AI yorumu yok.",
        "action": st.get("action", ""),
        "riskReason": st.get("riskReason", "—"),
        "seviyeler": seviyeler,
        "grafik": ps["seri"],
        "haberler": haberler,
        "analist": analist if analist.get("available") else None,
        "temel": (rec or {}).get("temel"),
        "benzer_donem": None,
    }


_SEKTOR_ETIKET = {
    "havayolu": "Havayolu", "banka": "Bankacılık", "savunma": "Savunma",
    "rafineri": "Rafineri", "petrokimya": "Petrokimya", "celik": "Demir-Çelik",
    "gyo": "GYO", "otomotiv": "Otomotiv", "holding": "Holding",
    "perakende": "Perakende", "telekom": "Telekom", "cam": "Cam",
    "altin": "Altın madenciliği", "beyaz_esya": "Beyaz eşya",
    "taahhut": "Taahhüt", "kiymetli_maden": "Kıymetli maden BYF", "diğer": "Diğer",
}


def portfolio_analysis(kullanici=None) -> dict:
    """Portfoyu bir butun olarak analiz eder: sektor yogunlasmasi, en riskli
    pozisyon, genel skor (1-10). USD pozisyonlar TL'ye cevrilerek oransal hesap."""
    from src.ai.scenarios import _TICKER_GRUP
    comm = _commentary_by_ticker()
    try:
        from src.news.macro import get_macro
        usdtry = (get_macro() or {}).get("usdtry") or 1.0
    except Exception:
        usdtry = 1.0
    try:
        poz = get_portfolio(kullanici).get("pozisyonlar", [])
    except Exception:
        poz = []
    if not poz:
        return {"available": False, "neden": "Portföy boş"}

    toplam = 0.0
    sektor_deger = {}
    skor_ag = 0.0
    skor_w = 0.0
    en_riskli = None
    for p in poz:
        norm = (p.get("ticker") or "").upper().replace(".IS", "")
        pb = str(p.get("para_birimi") or "TL")
        kur = usdtry if ("$" in pb or "USD" in pb.upper()) else 1.0
        deger = float((p.get("guncel") or 0) * (p.get("adet") or 0) * kur)
        toplam += deger
        sek = _TICKER_GRUP.get(norm, "diğer")
        sektor_deger[sek] = sektor_deger.get(sek, 0.0) + deger
        r = comm.get(norm) or comm.get(norm.upper()) or {}
        puan = r.get("score")
        risk = (r.get("risk") or {}).get("score")
        if isinstance(puan, (int, float)) and deger > 0:
            skor_ag += puan * deger
            skor_w += deger
        if isinstance(risk, (int, float)):
            if en_riskli is None or risk > en_riskli["risk"]:
                en_riskli = {"hisse": norm, "risk": risk,
                             "karar": r.get("final_decision")}

    yogun = sorted(
        [{"sektor": _SEKTOR_ETIKET.get(s, s),
          "yuzde": round(v / toplam * 100, 1) if toplam else None}
         for s, v in sektor_deger.items()],
        key=lambda x: x["yuzde"] or 0, reverse=True)
    genel_skor = round(skor_ag / skor_w, 1) if skor_w else None
    return {
        "available": True,
        "pozisyon_sayisi": len(poz),
        "sektor_yogunlasma": yogun,
        "en_yogun_sektor": yogun[0] if yogun else None,
        "cesitlendirme": ("zayıf" if yogun and (yogun[0]["yuzde"] or 0) >= 50
                          else "orta" if yogun and (yogun[0]["yuzde"] or 0) >= 35
                          else "iyi"),
        "en_riskli_pozisyon": en_riskli,
        "genel_skor": genel_skor,
    }


# Soruda hisse sembolu tespiti: buyuk harf 2-5 karakterlik kelimeler.
# Yatirim/jargon kisaltmalarini hisse sanmamak icin blokliste.
_TICKER_RE = re.compile(r"\b[A-Z]{2,5}\b")
_TICKER_STOP = {
    "AL", "SAT", "TUT", "BIST", "ABD", "USD", "EUR", "TRY", "TL", "KAP", "PPK",
    "TCMB", "RSI", "MACD", "ETF", "BYF", "IPO", "ATH", "AI", "OK", "TV", "SPK",
    "KZ", "USA", "FED", "GSYH", "TUFE", "UFE", "BIM",
}


_TICKER_RE_ANY = re.compile(r"\b[A-Za-z]{2,5}\b")   # kucuk/karisik harf dahil


def _known_tickers() -> set[str]:
    """Sistemin tanidigi tum semboller (commentary + portfoy + watchlist + BIST evreni).

    Kucuk/karisik harfle yazilmis sembolleri (or. 'spcx', 'Thyao') yakalamak icin
    kullanilir; boylece Turkce kelimeler yanlislikla sembol sanilmaz."""
    known = set(COMPANY_NAMES.keys())
    try:
        known |= {(t or "").upper() for t in _commentary_by_ticker().keys()}
    except Exception:
        pass
    try:
        with sqlite3.connect(DB_PATH) as c:
            for (tk,) in c.execute("SELECT DISTINCT ticker FROM portfoy"):
                known.add((tk or "").upper().split(".")[0])
    except sqlite3.Error:
        pass
    try:
        wl = _load_watchlist()
        for t in (wl.get("kisisel", []) + wl.get("bist_endeks", [])):
            known.add((t or "").upper().split(".")[0])
        for d in wl.get("kisisel_diger", []):
            known.add((d.get("ticker") or "").upper().split(".")[0])
    except Exception:
        pass
    known.discard("")
    return known


def _detect_tickers(text: str, limit: int = 4) -> list[str]:
    """Metindeki olasi hisse sembollerini dondurur.

    1) BUYUK harf yazilmis 2-5 harfli kelimeler (klasik sembol yazimi).
    2) Kucuk/karisik harfle yazilmis ama SISTEMCE BILINEN semboller (or. 'spcx',
       'Thyao') -- boylece kullanici sembolu kucuk yazinca da fiyat cekilir."""
    txt = text or ""
    out = []
    for m in _TICKER_RE.findall(txt):
        if m in _TICKER_STOP or m in out:
            continue
        out.append(m)
    if len(out) < limit:
        known = _known_tickers()
        for m in _TICKER_RE_ANY.findall(txt):
            u = m.upper()
            if u in _TICKER_STOP or u in out:
                continue
            if u in known:
                out.append(u)
                if len(out) >= limit:
                    break
    return out[:limit]


def _anlik_fiyatlar(tickers: list[str], comm: dict | None = None) -> list[dict]:
    """Verilen TUM sembollerin yfinance'ten ANLIK fiyat + gunluk degisimini dondurur.

    Portfoyde olup olmadigina BAKMAZ; soruda gecen her hisse icin fiyat ceker.
    Commentary + portfoy + watchlist yalniz pazar (BIST/.IS vs ABD) tespitinde
    kullanilir; bilinmeyen sembol icin her iki form denenir (Turk borsasi botu
    oldugundan once .IS / BIST, sonra ABD) ve veri donen kullanilir."""
    if not tickers:
        return []
    us, bist = set(), set()
    for t, r in (comm or {}).items():
        (us if r.get("market") == "abd" else bist).add((t or "").upper())
    try:
        with sqlite3.connect(DB_PATH) as c:
            for tk, pb in c.execute("SELECT DISTINCT ticker, para_birimi FROM portfoy"):
                base = (tk or "").upper().split(".")[0]
                (us if (pb or "TL").upper() == "USD" else bist).add(base)
    except sqlite3.Error:
        pass
    wl = _load_watchlist()
    for t in (wl.get("bist_endeks", []) + wl.get("kisisel", [])):
        bist.add((t or "").upper().split(".")[0])

    plan = {}    # ticker -> [(yf_symbol, market), ...] denenecek formlar
    for t in tickers:
        if t in us:
            plan[t] = [(t, "abd")]
        elif t in bist:
            plan[t] = [(f"{t}.IS", "bist")]
        else:                                   # bilinmiyor -> once BIST sonra ABD
            plan[t] = [(f"{t}.IS", "bist"), (t, "abd")]

    syms = [s for cands in plan.values() for s, _ in cands]
    px = _yf_prices(syms)
    out = []
    for t, cands in plan.items():
        bulundu = False
        for sym, mkt in cands:
            d = px.get(sym)
            if d and d.get("fiyat") is not None:
                out.append({"hisse": t, "fiyat": d["fiyat"], "gunluk": d.get("gunluk"),
                            "para_birimi": "$" if mkt == "abd" else "₺"})
                bulundu = True
                break
        if bulundu:
            continue
        # YEDEK: batch download bos dondu (yfinance ara sira sapitiyor, or. SPCX) ->
        # her aday sembolu tek tek Ticker.history / fast_info ile yeniden dene
        for sym, mkt in cands:
            d = _yf_single_price(sym)
            if d and d.get("fiyat") is not None:
                out.append({"hisse": t, "fiyat": d["fiyat"], "gunluk": d.get("gunluk"),
                            "para_birimi": "$" if mkt == "abd" else "₺"})
                break
    return out


def _yf_single_price(sym: str) -> dict | None:
    """Tek sembol icin yedek fiyat cekme (batch yf.download bos donerse).

    Once Ticker.history (en guvenilir), sonra fast_info. {fiyat, gunluk} veya None."""
    try:
        import yfinance as yf
        t = yf.Ticker(sym)
        h = t.history(period="5d")
        if h is not None and not h.empty:
            c = h["Close"].dropna()
            if len(c) >= 2:
                last, prev = float(c.iloc[-1]), float(c.iloc[-2])
                chg = ((last - prev) / prev * 100) if prev else None
                return {"fiyat": round(last, 2),
                        "gunluk": round(chg, 2) if chg is not None else None}
            if len(c) >= 1:
                return {"fiyat": round(float(c.iloc[-1]), 2), "gunluk": None}
        fi = t.fast_info
        lp = fi.get("last_price") if hasattr(fi, "get") else None
        if lp:
            pc = fi.get("previous_close") if hasattr(fi, "get") else None
            chg = ((lp - pc) / pc * 100) if pc else None
            return {"fiyat": round(float(lp), 2),
                    "gunluk": round(chg, 2) if chg is not None else None}
    except Exception:
        return None
    return None


# Kullanici basina bekleyen profil tamamlama sorusu (or. 'kayip_toleransi_yuzde')
_PROFIL_BEKLEYEN: dict = {}


def _profil_tamamla(kullanici, soru):
    """Profil tamamlama akisi (Bota Sor icinde).

    - Kullanici 'Kayip toleransin nedir?' onerisini gonderirse: oran ister ve bekler.
    - Sonraki mesajda bir yuzde verirse: profili gunceller (upsert_profile).
    Bu akisa girilmezse None doner (normal sohbet devam eder)."""
    if not kullanici:
        return None
    from src.db import database as db
    uid = _uid(kullanici)
    if not uid:
        return None
    s = (soru or "").strip()
    low = s.lower()

    # 1) Bekleyen bir profil sorusu varsa ve cevapta oran varsa -> kaydet.
    #    (Sorunun KENDISI tekrar gonderilirse — icinde 'tolerans'/ornek %10 gecer —
    #    bunu cevap sanma; asagidaki adim 2'ye dusur.)
    if _PROFIL_BEKLEYEN.get(uid) == "kayip_toleransi_yuzde" and "tolerans" not in low:
        m = re.search(r"%?\s*(\d{1,3})(?:[.,]\d+)?\s*%?", s)
        if m:
            try:
                val = int(m.group(1))
            except ValueError:
                val = None
            if val is not None and 1 <= val <= 100:
                db.upsert_profile(uid, kayip_toleransi_yuzde=val)
                _PROFIL_BEKLEYEN.pop(uid, None)
                p = db.get_profile(uid) or {}
                guven = p.get("profil_guven_skoru")
                ek = f" Profil güven skorun %{int(guven)} oldu." if guven else ""
                return {"ok": True, "cevap": (
                    f"Kaydettim ✅ Kayıp toleransın %{val} olarak profiline işlendi."
                    f"{ek} Artık önerilerimi buna göre ayarlayacağım.")}
        # oran bulunamadi -> tek satir tekrar iste (akistan cikma)
        return {"ok": True, "cevap": (
            "Kayıp toleransını bir yüzde olarak yazar mısın? Örnek: %10 ya da %20.")}

    # 2) Kullanici 'kayip toleransi' tamamlama sorusunu gonderdi mi?
    if "kayıp tolerans" in low or "kayip tolerans" in low:
        prof = db.get_profile(uid) or {}
        if prof.get("kayip_toleransi_yuzde") is not None:
            return {"ok": True, "cevap": (
                f"Kayıp toleransın zaten kayıtlı: %{int(prof['kayip_toleransi_yuzde'])}. "
                "Değiştirmek istersen yeni oranı yaz (örn: %15).")}
        _PROFIL_BEKLEYEN[uid] = "kayip_toleransi_yuzde"
        return {"ok": True, "cevap": (
            "Bir hissede en fazla yüzde kaç değer kaybına tahammül edebilirsin? "
            "Örnek: %10 ya da %20 yaz, profiline kaydedip önerilerimi ona göre ayarlayayım.")}
    return None


# --- Fiyat alarmi: dogal dil tespiti ("THYAO 300'e düşerse haber ver") ---
_ALARM_DUSER = ("düşerse", "duserse", "altına", "altina", "inerse", "inince",
                "gerilerse", "düşünce", "dusunce")
_ALARM_CIKAR = ("çıkarsa", "cikarsa", "üstüne", "ustune", "geçerse", "gecerse",
                "yükselirse", "yukselirse", "aşarsa", "asarsa", "çıkınca", "cikinca")
_ALARM_NIYET = ("haber ver", "alarm", "uyar", "bildir", "haberim olsun",
                "haber et", "haber ver")


def _parse_fiyat(txt: str):
    """Turkce/ABD ondalik formatini float'a cevirir ('1.234,56'->1234.56, '5,77'->5.77)."""
    t = (txt or "").strip()
    try:
        if "," in t and "." in t:
            t = t.replace(".", "").replace(",", ".")
        elif "," in t:
            t = t.replace(",", ".")
        return float(t)
    except ValueError:
        return None


def _alarm_yakala(kullanici, soru):
    """'X N'e düşerse/çıkarsa haber ver' kalibini yakalar ve DB'ye alarm kaydeder.

    Yakalamazsa None doner (normal akis devam eder)."""
    if not kullanici:
        return None
    s = soru or ""
    low = s.lower()
    if not any(k in low for k in _ALARM_NIYET):
        return None
    if any(k in low for k in _ALARM_DUSER):
        yon = "asagi"
    elif any(k in low for k in _ALARM_CIKAR):
        yon = "yukari"
    else:
        return None
    tickers = _detect_tickers(s, limit=1)
    if not tickers:
        return None
    tkr = tickers[0]
    m = re.search(r"(\d{1,7}(?:[.,]\d+)?)", s)
    if not m:
        return None
    hedef = _parse_fiyat(m.group(1))
    if hedef is None or hedef <= 0:
        return None

    # para birimi: ABD hissesi mi? (commentary market veya portfoy USD kaydi)
    para = "TL"
    try:
        if (_commentary_by_ticker().get(tkr, {}) or {}).get("market") == "abd":
            para = "USD"
        else:
            with sqlite3.connect(DB_PATH) as c:
                row = c.execute("SELECT para_birimi FROM portfoy "
                                "WHERE UPPER(ticker)=? LIMIT 1", (tkr,)).fetchone()
                if row and (row[0] or "TL").upper() == "USD":
                    para = "USD"
    except Exception:
        pass

    try:
        from src.db import database as db
        uid = _uid(kullanici)
        if not uid:
            return None
        db.add_price_alarm(uid, tkr, hedef, yon, para)
    except Exception:
        return {"ok": False, "cevap": "Alarmı kaydedemedim, tekrar dener misin?"}
    birim = "$" if para == "USD" else "TL"
    yon_tr = "altına düşerse" if yon == "asagi" else "üstüne çıkarsa"
    return {"ok": True, "cevap": (
        f"🔔 Alarm kuruldu: <b>{tkr}</b> fiyatı {hedef:g} {birim} {yon_tr} sana "
        "haber vereceğim. (Ayarlar → Fiyat Alarmlarım'dan yönetebilirsin.)")}


def get_alarms(kullanici) -> list[dict]:
    """Kullanicinin AKTIF fiyat alarmlari (Ayarlar ekrani icin)."""
    from src.db import database as db
    uid = _uid(kullanici)
    if not uid:
        return []
    return [{"id": a["id"], "ticker": a["ticker"], "hedef_fiyat": a["hedef_fiyat"],
             "yon": a["yon"], "para_birimi": a.get("para_birimi", "TL"),
             "olusturma_tarihi": a.get("olusturma_tarihi")}
            for a in db.list_price_alarms(kullanici_id=uid, aktif=True)]


# --- Sektor analizi: watchlist hisselerinin son kararlarindan sektor ozeti ---
_SEKTORLER = {
    "bankacilik": {"ad": "Bankacılık", "kw": ("banka", "bankac", "finans"),
                   "tickers": ["GARAN", "AKBNK", "ISCTR", "YKBNK", "HALKB", "VAKBN"]},
    "havacilik": {"ad": "Havacılık", "kw": ("havac", "havayol", "uçak", "ucak", "havalim"),
                  "tickers": ["THYAO", "PGSUS", "TAVHL"]},
    "enerji": {"ad": "Enerji/Rafineri", "kw": ("enerji", "elektrik", "petrol", "rafineri", "akaryakıt", "akaryakit"),
               "tickers": ["TUPRS", "PETKM", "AYGAZ"]},
    "savunma": {"ad": "Savunma", "kw": ("savunma", "silah", "asker"),
                "tickers": ["ASELS", "AGHOL"]},
    "teknoloji": {"ad": "Teknoloji", "kw": ("teknoloji", "yazılım", "yazilim", "bilişim", "bilisim"),
                  "tickers": ["ASELS", "LOGO", "KAREL"]},
    "celik": {"ad": "Demir-Çelik", "kw": ("çelik", "celik", "demir", "metal"),
              "tickers": ["EREGL", "KRDMD", "KORDS"]},
    "gayrimenkul": {"ad": "Gayrimenkul", "kw": ("gayrimenkul", "gyo", "inşaat", "insaat", "konut", "emlak"),
                    "tickers": ["EKGYO"]},
    "otomotiv": {"ad": "Otomotiv", "kw": ("otomotiv", "otomobil", "araç ", "arac "),
                 "tickers": ["TOASO", "FROTO"]},
    "perakende": {"ad": "Perakende/Gıda", "kw": ("perakende", "market", "gıda", "gida", "tüketim", "tuketim"),
                  "tickers": ["BIMAS", "MGROS", "ULKER", "CCOLA"]},
    "telekom": {"ad": "Telekom", "kw": ("telekom", "iletişim", "iletisim", "gsm"),
                "tickers": ["TCELL", "TTKOM"]},
    "holding": {"ad": "Holding", "kw": ("holding",),
                "tickers": ["KCHOL", "SAHOL", "DOHOL", "ENKAI"]},
}


def _sektor_key_from_text(s: str):
    low = (s or "").lower()
    for key, sek in _SEKTORLER.items():
        if any(kw in low for kw in sek["kw"]):
            return key
    return None


def get_sektor_analiz(sektor_key: str):
    """Bir sektordeki takip edilen hisselerin son AI kararlarini ozetler."""
    sek = _SEKTORLER.get(sektor_key)
    if not sek:
        return None
    comm = _commentary_by_ticker()
    hisseler, al, sat = [], 0, 0
    for t in sek["tickers"]:
        rec = comm.get(t)
        if not rec or rec.get("skipped"):
            continue
        et, renk = _karar5(rec.get("final_decision"))
        hisseler.append({"ticker": t, "karar": et, "renk": renk, "puan": rec.get("score")})
        if et == "AL":
            al += 1
        elif et in ("SAT", "AZALT"):
            sat += 1
    if not hisseler:
        return {"sektor": sek["ad"], "hisseler": [],
                "ozet": f"{sek['ad']} sektöründe şu an takip ettiğim güncel analiz yok."}
    if al >= 2 and al > sat:
        gor = "olumlu, alım sinyalleri öne çıkıyor"
    elif sat > al:
        gor = "zayıf, satış/baskı ağırlıkta"
    elif al > 0 and sat == 0:
        gor = "ılımlı olumlu"
    else:
        gor = "kararsız/nötr, çoğunlukla TUT"
    liste = ", ".join(f"{h['ticker']} {h['karar']}" for h in hisseler)
    return {"sektor": sek["ad"], "hisseler": hisseler,
            "ozet": f"{sek['ad']} sektöründe bu hafta: {liste} — genel görünüm {gor}."}


def _sektor_yakala(soru):
    """Soru bir sektor sorusuysa (sektor adi + 'nasil/durum/sektor' gibi) ozet doner."""
    s = soru or ""
    low = s.lower()
    key = _sektor_key_from_text(s)
    if not key:
        return None
    if not any(w in low for w in ("sektör", "sektor", "nasıl", "nasil", "durum",
                                  "genel", "bu hafta", "görünüm", "gorunum")):
        return None
    res = get_sektor_analiz(key)
    return {"ok": True, "cevap": res["ozet"]} if res else None


# --- Kullanici geri bildirimi: "THYAO karari yanlisti" / "bu karar hataliydi" ---
_GERIBILDIRIM_KW = ("yanlıştı", "yanlistı", "yanlıstı", "hatalıydı", "hataliydi",
                    "yanlış karar", "yanlis karar", "kararı yanlış", "karari yanlis",
                    "karar yanlıştı", "hatalı karar", "hatali karar", "yanlış verdin",
                    "yanlis verdin", "kötü karar", "kotu karar", "yanlış çıktı")


def _geri_bildirim_yakala(soru):
    """Kullanici bir karari yanlis buldu -> ilgili hissenin son kararini YANLIS isaretle."""
    s = soru or ""
    low = s.lower()
    if not any(k in low for k in _GERIBILDIRIM_KW):
        return None
    from src.db import database as db
    ts = _detect_tickers(s, limit=1)
    ticker = ts[0] if ts else None
    if ticker:
        res = db.mark_last_decision_wrong(ticker, "kullanici_bildirimi")
        if not res:
            return {"ok": True, "cevap": (
                f"{ticker} için kayıtlı bir kararım yok ama geri bildirimini not aldım, teşekkürler.")}
        return {"ok": True, "cevap": (
            f"Geri bildirim için teşekkürler 🙏 — {ticker} için verdiğim "
            f"{res['karar']} kararını yanlış olarak işaretledim, bunu öğrendim.")}
    # Hisse belirtilmemis ('bu karar hataliydi') -> en son karar
    last = db.last_decision_any()
    if last:
        db.mark_last_decision_wrong(last["ticker"], "kullanici_bildirimi")
        return {"ok": True, "cevap": (
            f"Geri bildirim için teşekkürler 🙏 — en son verdiğim {last['ticker']} "
            f"{last['karar']} kararını yanlış olarak işaretledim, bunu öğrendim.")}
    return {"ok": True, "cevap": "Geri bildirim için teşekkürler, bunu öğrendim."}


def ask_bot(soru: str, kullanici=None, gecmis=None) -> dict:
    soru = (soru or "").strip()
    if not soru:
        return {"ok": False, "cevap": "Bir soru yaz."}

    # --- PROFIL TAMAMLAMA: kayip toleransi soru/cevap akisi (AI anahtari gerekmez) ---
    try:
        prof_res = _profil_tamamla(kullanici, soru)
        if prof_res is not None:
            return prof_res
    except Exception:
        pass

    # --- FIYAT ALARMI: "X N'e düşerse/çıkarsa haber ver" (AI anahtari gerekmez) ---
    try:
        alarm_res = _alarm_yakala(kullanici, soru)
        if alarm_res is not None:
            return alarm_res
    except Exception:
        pass

    # --- SEKTOR ANALIZI: "Bankacılık sektörü nasıl?" (AI anahtari gerekmez) ---
    try:
        sektor_res = _sektor_yakala(soru)
        if sektor_res is not None:
            return sektor_res
    except Exception:
        pass

    # --- KULLANICI GERI BILDIRIMI: "X kararı yanlıştı" (AI anahtari gerekmez) ---
    try:
        gb_res = _geri_bildirim_yakala(soru)
        if gb_res is not None:
            return gb_res
    except Exception:
        pass

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "cevap": "AI anahtarı ayarlı değil; şu an soru yanıtlayamıyorum."}

    # GUNLUK PLAN niyeti: "bugun ne yapmaliyim / bugunku plan nedir" vb.
    plan_modu = bool(re.search(
        r"bug[uü]n.*(ne yap|yapmal[ıi]|plan)|bug[uü]nk[uü]\s+plan|g[uü]nl[uü]k\s+plan",
        soru, re.I))

    # --- KOD MODU: guvenli arayuz (HTML/CSS/JS) degisikligi ---
    try:
        from src.web import code_mode
        norm = soru.lower()
        if kullanici and code_mode.has_pending(kullanici) and code_mode.is_approval(soru):
            return code_mode.apply_pending(kullanici)          # onayli -> uygula
        if kullanici and ("geri al" in norm or "geri-al" in norm) and \
                soru.lower().strip() in ("geri al", "geri-al", "geri alın", "geri al onayla"):
            return code_mode.revert_last(kullanici)
        # HATA DUZELTME MODU: arayuz hatasi bildirimi -> teshis + otomatik fix + restart
        if kullanici and code_mode.is_error_report(soru) and not code_mode.is_approval(soru):
            import anthropic
            return code_mode.fix_error(kullanici, soru, client=anthropic.Anthropic())
        if kullanici and code_mode.is_ui_request(soru) and not code_mode.is_approval(soru):
            import anthropic
            return code_mode.propose(kullanici, soru, client=anthropic.Anthropic())
    except Exception as e:
        return {"ok": False, "cevap": f"Kod modu hatası: {type(e).__name__}"}

    comm = _commentary_by_ticker()
    # Derin portfoy baglami: alis/adet/guncel/kar-zarar/sure + bot karari+gerekce
    baglam = []
    bugun = datetime.now(ZoneInfo("Europe/Istanbul")).date()
    try:
        port = get_portfolio(kullanici)
        pozisyonlar = port.get("pozisyonlar", [])
    except Exception:
        pozisyonlar = []
    for p in pozisyonlar:
        t = (p.get("ticker") or "").upper()
        r = comm.get(t, {}) or {}
        # ne kadar suredir tutuluyor (gun)
        tutma_gun = None
        try:
            alim = (p.get("tarih") or "").split("T")[0]
            if alim:
                tutma_gun = (bugun - datetime.fromisoformat(alim).date()).days
        except (ValueError, TypeError):
            tutma_gun = None
        kz = p.get("kz")
        kz_y = p.get("kz_yuzde")
        baglam.append({
            "hisse": t,
            "alim_fiyati": p.get("alis"),
            "adet": p.get("adet"),
            "guncel_fiyat": p.get("guncel"),
            "para_birimi": p.get("para_birimi"),
            "kar_zarar_tl": round(kz, 2) if isinstance(kz, (int, float)) else None,
            "kar_zarar_yuzde": round(kz_y, 2) if isinstance(kz_y, (int, float)) else None,
            "durum": ("zararda" if isinstance(kz, (int, float)) and kz < 0
                      else "karda" if isinstance(kz, (int, float)) and kz > 0 else "başabaş"),
            "tutma_gun": tutma_gun,
            "bot_karari": r.get("final_decision"),
            "bot_puan": r.get("score"),
            "bot_risk": (r.get("risk") or {}).get("score"),
            "bot_gerekce": _ilk_cumleler(r.get("gerekce", ""), 2),
        })
    # tum izlenen hisseler (sahip olunmayan hisse sorulari icin kisa baglam)
    piyasa = []
    for t, r in comm.items():
        if r.get("skipped"):
            continue
        piyasa.append({"hisse": t, "karar": r.get("final_decision"),
                       "puan": r.get("score"), "risk": (r.get("risk") or {}).get("score"),
                       "not": _ilk_cumleler(r.get("gerekce", ""), 1)})

    # GUNLUK PLAN: radardaki firsatlar (AL sinyalleri, puana gore en iyi 3)
    radar_firsatlar = []
    if plan_modu:
        for t, r in comm.items():
            if r.get("skipped"):
                continue
            if _karar5(r.get("final_decision"))[0] == "AL":
                radar_firsatlar.append({
                    "hisse": t, "puan": r.get("score"),
                    "neden": _cap(r.get("neden_simdi")
                                  or _ilk_cumleler(r.get("gerekce", ""), 1), 80)})
        radar_firsatlar.sort(key=lambda x: x.get("puan") or 0, reverse=True)
        radar_firsatlar = radar_firsatlar[:3]

    # ANLIK FIYAT: soruda gecen hisse sembolleri icin yfinance'ten guncel veri
    try:
        anlik_fiyatlar = _anlik_fiyatlar(_detect_tickers(soru), comm)
    except Exception:
        anlik_fiyatlar = []

    try:
        from src.news.macro import get_macro
        makro = get_macro()
    except Exception:
        makro = {}
    # Kullanici profili + hafiza gecmisi
    from src.db import database as db
    uid = _uid(kullanici)
    profil = db.get_profile(uid) if uid else None
    profil_ozet = {}
    if profil:
        profil_ozet = {k: profil.get(k) for k in (
            "portfoy_buyuklugu", "aylik_birikim", "ek_sermaye_mumkun", "risk_toleransi",
            "yatirim_vadesi", "nakit_ihtiyaci", "panik_egilimi", "tecrube_seviyesi",
            "ana_hedef", "kayip_toleransi_yuzde", "ogrenme_seviyesi",
            "dusus_tepkisi_10", "dusus_tepkisi_20", "sektor_tercihi",
            "gunluk_takip_saat", "ana_korku", "onceki_basari", "risk_tercihi")
            if profil.get(k) is not None}
    hafiza_ozet = []
    if uid:
        for m in db.list_memory(uid, limit=8):
            ic = m.get("icerik")
            oz = (ic.get("ozet") or ic.get("soru") or ic.get("karar")
                  if isinstance(ic, dict) else str(ic))
            hafiza_ozet.append({"tip": m.get("tip"), "tarih": (m.get("tarih") or "")[:10],
                                "ticker": m.get("ticker"), "ozet": _cap(str(oz or ""), 90)})

    # Davranissal not: dususte tepki + ana korku (varsa) -> AI bunlari kararda kullansin
    davranis_notu = ""
    if profil:
        d10 = _PROFIL_DEGER_ETIKET.get(str(profil.get("dusus_tepkisi_10")),
                                       profil.get("dusus_tepkisi_10"))
        d20 = _PROFIL_DEGER_ETIKET.get(str(profil.get("dusus_tepkisi_20")),
                                       profil.get("dusus_tepkisi_20"))
        korku = _PROFIL_DEGER_ETIKET.get(str(profil.get("ana_korku")),
                                         profil.get("ana_korku"))
        risk_t = _PROFIL_DEGER_ETIKET.get(str(profil.get("risk_tercihi")),
                                          profil.get("risk_tercihi"))
        if any([d10, d20, korku, risk_t]):
            davranis_notu = (
                f"\n\nKULLANICI DAVRANIS PROFILI — kararinda MUTLAKA kullan:\n"
                f"%10 dususte tepkisi: {d10 or 'bilinmiyor'}, %20 dususte: "
                f"{d20 or 'bilinmiyor'}, ana korkusu: {korku or 'bilinmiyor'}, "
                f"risk tercihi: {risk_t or 'bilinmiyor'}.\n"
                "- SAT onerirken kullanici PANIKCIYSE (dususte satar / panik egilimi "
                "yuksek) once 'panik satisi yapma, once plan yap' uyarisi ver.\n"
                "- AL onerirken RISK TOLERANSI DUSUKSE (az_kazanc_az_risk / dusuk) "
                "'kademeli giris oner, tek seferde girme' de.\n"
                "- Kullanicinin portfoyundeki ZARARDAKI pozisyonlar icin maliyeti HER "
                "ZAMAN goz onunde bulundur (realize etmek / ortalama dusurmek / beklemek).")

    KARAR_TIPLERI = ("AL, SAT, TUT, BEKLE, POZİSYON AZALT, POZİSYON ARTIR, KADEMELİ GİR, "
                     "KADEMELİ ÇIK, STOP BELİRLE, NAKİTTE KAL, İZLEMEYE AL, PANİK SATIŞ "
                     "YAPMA, MALİYET DÜŞÜRMEYİ DEĞERLENDİR, BU HİSSE SENİN PROFİLİNE UYGUN DEĞİL")

    # Portfoy genel analizi (cesitlendirme/en riskli/genel skor) - "portfoyum nasil" icin
    try:
        portfoy_analiz = portfolio_analysis(kullanici)
    except Exception:
        portfoy_analiz = {"available": False}

    # Guncel baglam sistem promptuna eklenir (her soruda taze); konusma gecmisi messages'ta
    baglam_metni = (
        "\n\nGÜNCEL BAĞLAM (her soruda yenilenir):\n"
        f"Kullanici profili: {json.dumps(profil_ozet, ensure_ascii=False)}\n"
        f"Portfoyu: {json.dumps(baglam, ensure_ascii=False)}\n"
        f"Portfoy genel analizi: {json.dumps(portfoy_analiz, ensure_ascii=False)}\n"
        f"Son kararlar (izlenen): {json.dumps(piyasa, ensure_ascii=False)}\n"
        f"Gecmis sohbet/hafiza ozeti: {json.dumps(hafiza_ozet, ensure_ascii=False)}\n"
        f"Makro: {json.dumps(makro, ensure_ascii=False)}"
        + ("\nAnlik veri (yfinance, su an): " + "; ".join(
            (f"{a['hisse']} su an {a['para_birimi']}{a['fiyat']}, bugün "
             f"%{a['gunluk']:+g} degisim") if a.get("gunluk") is not None
            else f"{a['hisse']} su an {a['para_birimi']}{a['fiyat']}"
            for a in anlik_fiyatlar) if anlik_fiyatlar else "")
        + (f"\nRadar firsatlari (AL): {json.dumps(radar_firsatlar, ensure_ascii=False)}"
           if plan_modu else ""))

    plan_notu = ""
    if plan_modu:
        plan_notu = (
            "\n\nGUNLUK PLAN MODU — kullanici bugun ne yapmasi gerektigini soruyor. "
            "Su sirayla, KISA ve net ver (baslik yazma, dogal cumlelerle):\n"
            "1) Portfoyundeki ZARARDAKI pozisyonlar (varsa hisse + zarar%); yoksa "
            "'portfoyunde belirgin zararda pozisyon yok' de.\n"
            "2) Radardaki firsatlar (yukaridaki 'Radar firsatlari (AL)' listesinden, "
            "en fazla 3 hisse, neden ilginc kisaca).\n"
            "3) Gunun piyasa durumu (makro + genel havadan 1 cumle).\n"
            "Sonunda MUTLAKA 'Bugün şunu yap:' satiriyla baslayan, NET 3 maddelik "
            "aksiyon listesi ver (her madde tek satir, somut). SADECE verilen baglami "
            "kullan, veri uydurma.")

    sistem = (
        "Sen Max'sin: 40 yasinda, 25 yillik tecrubeli bir Turk borsa uzmani ve "
        "kullanicinin kisisel asistanisin. Karakterin: direkt, net, gereksiz "
        "yumusatmazsin; piyasayi iyi okur, kullaniciyi korur, gerektiginde sert "
        "uyarirsin; 'ben olsam soyle yapardim' tonuyla konusursun. Kendini TANITMA "
        "('merhaba ben Max' deme), dogrudan ise gir; karakterini dayatma, dogal "
        "konus. Sade, net, sicak Turkce. KISA ve ODAKLI ol: cevabin EN FAZLA 3 "
        "paragraf olsun. Gereksiz bilgi ekleme, SADECE sorulan seyi cevapla. "
        "Soru sormak istersen SADECE 1 soru sor, asla birden fazla soru sorma. "
        "Turkce konus; jargon (RSI/MACD) yok. Markdown/tablo/yildiz KULLANMA. "
        "Yanitlarini SADECE verilen baglama dayandir; elinde bu konuda veri yoksa "
        "'bu konuda guncel verim yok' de; asla tahmin yapma, uydurma. "
        "Baglamda 'Anlik veri' varsa bir hissenin guncel fiyat/gunluk degisim "
        "sorusunu DOGRUDAN o veriyle cevapla; bu durumda 'gercek zamanli verim yok' "
        "DEME, gercek fiyati ve degisimi soyle. "
        "KULLANICININ PORTFOYU hakkinda EMIN olmadigin bir sey SOYLEME. Portfoy "
        "verisi ('Portfoyu') sana acikca verilmisse kullan; verilmemisse veya bos "
        "ise 'portfoy bilgine erisimim yok' de. Bir hissenin portfoyde olup "
        "olmadigini ancak verilen portfoy listesinde kesin goruyorsan soyle; "
        "'portfoyunde yok / var' diye TAHMIN etme. "
        "Bu yatirim tavsiyesi degildir.\n\n"
        "Sana sunlar verilir: kullanici profili, portfoyu (alis/adet/guncel/"
        "kar-zarar/tutma_gun/bot_karari), portfoy genel analizi (cesitlendirme/en "
        "riskli/genel skor), izlenen hisselerdeki son AI kararlari, gecmis sohbet "
        "ozeti ve makro. AYRICA bu oturumun onceki mesajlari konusma gecmisi olarak "
        "verilir; 'az once konustugumuz' gibi atiflari hatirla ve baglami surdur.\n"
        "'Portfoyum nasil' benzeri sorularda 'portfoy genel analizi'ni kullan "
        "(sektor yogunlasmasi, en riskli pozisyon, genel skor).\n"
        "Cevabini kullanicinin PROFILINE gore uyarla (risk toleransi, vade, nakit "
        "ihtiyaci, panik egilimi, tecrube). Her cevapta sirayla sun: "
        "1) Genel piyasa gorusu (kisa), 2) Kisiye ozel yorum (profili+portfoyu "
        "kullanarak), 3) Net aksiyon, 4) Risk seviyesi, 5) Kisa ogretici not. "
        "Bu basliklari yazma; akici cumlelerle dogal bir paragraf/birkac cumle "
        "halinde ver. ZARARDA pozisyonlarda maliyeti goz onunde bulundur (realize "
        "etmek / ortalama dusurmek / beklemek).\n"
        f"Net aksiyon icin su karar tiplerinden uygun olani kullan: {KARAR_TIPLERI}. "
        "Profili belli olmayan kullaniciya genel konus ve 'seni daha iyi tanirsam "
        "daha isabetli yorum yaparim' diye nazikce hatirlat.\n\n"
        "ARAYUZ DEGISIKLIGI: Kullanici arayuz degisikligi isterse (renk, buton, "
        "baslik, yazi, sekme vb.): 1) Ne yapacagini acikla, 2) Onay bekle, "
        "3) Onaylaninca degisikligi uygula ve servisi yeniden baslat. Yalniz "
        "HTML/CSS/JS degisikligi yapabilirsin; Python, veritabani, .env veya "
        "baska hicbir dosyaya DOKUNAMAZSIN. (Bu akis sistem tarafindan guvenli "
        "sekilde yonetilir.)" + plan_notu + davranis_notu + baglam_metni)

    # Konusma gecmisi (ayni oturum, frontend'den): user/assistant siralamasi
    mesajlar = []
    for m in (gecmis or [])[-12:]:
        rol = m.get("role") or m.get("rol")
        rol = "assistant" if rol in ("bot", "assistant") else "user"
        icerik = (m.get("content") or m.get("metin") or "").strip()
        if icerik:
            mesajlar.append({"role": rol, "content": icerik[:2000]})
    while mesajlar and mesajlar[0]["role"] != "user":
        mesajlar.pop(0)                     # ilk mesaj user olmali (API kurali)
    mesajlar.append({"role": "user", "content": soru})

    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=_CHAT_MODEL, max_tokens=700 if plan_modu else 400,
            system=sistem,
            messages=mesajlar,
        )
        cevap = "".join(getattr(b, "text", "") for b in resp.content
                        if getattr(b, "type", "") == "text").strip()
        cevap = re.sub(r"^[#>\-\*\s]*\|.*$", "", cevap, flags=re.M)   # tablo satirlari
        cevap = re.sub(r"[#*`_]+", "", cevap).strip()
        cevap = cevap or "Yanıt üretemedim."
        # Sohbeti hafizaya kaydet
        if uid:
            try:
                db.add_memory(uid, "sohbet",
                              {"soru": soru[:300], "cevap": cevap[:600],
                               "ozet": _cap(soru, 90)})
            except Exception:
                pass
        return {"ok": True, "cevap": cevap}
    except Exception as e:
        return {"ok": False, "cevap": f"Hata: {type(e).__name__}"}


def get_news(limit: int = 20) -> list[dict]:
    """Mevcut yorumlardaki haberleri kategori + temsili gorsel ile dondurur."""
    comm = _commentary_by_ticker()
    out, seen = [], set()
    for rec in comm.values():
        tkr = (rec.get("ticker") or "").upper()
        etiket5, _ = _karar5(rec.get("final_decision"))
        etki = ("Negatif" if etiket5 in ("SAT", "AZALT") else
                "Pozitif" if etiket5 == "AL" else "Nötr")
        for hb in (rec.get("haberler") or []):
            k = (hb.get("baslik") or "").strip().lower()
            if not k or k in seen:
                continue
            seen.add(k)
            out.append({**_haber_gorsel(hb.get("baslik")),
                        "baslik": hb.get("baslik"), "tarih": hb.get("tarih"),
                        "kaynak": hb.get("kaynak"), "url": hb.get("url"),
                        "ticker": tkr, "etki": etki,
                        "fiyatlanma": hb.get("fiyatlanma"),
                        "yorum": _ilk_cumleler(rec.get("gerekce", ""), 1)})
    out.sort(key=lambda n: n.get("fiyatlanma") == "FIYATLANMADI", reverse=True)
    return out[:limit]


# ----------------------------------------------------------------------------
# Profil + hafiza + onboarding
# ----------------------------------------------------------------------------
_PROFIL_GORUNUM = {
    "portfoy_buyuklugu": ("Portföy büyüklüğü", "tl"),
    "aylik_birikim": ("Aylık birikim", "tl"),
    "ek_sermaye_mumkun": ("Ek sermaye mümkün", "bool"),
    "tecrube_seviyesi": ("Tecrübe", "enum"),
    "risk_toleransi": ("Risk toleransı", "enum"),
    "panik_egilimi": ("Panik eğilimi", "enum"),
    "yatirim_vadesi": ("Yatırım vadesi", "enum"),
    "nakit_ihtiyaci": ("Nakit ihtiyacı", "enum"),
    "nakit_ihtiyac_tarihi": ("Nakit ihtiyaç tarihi", "text"),
    "ana_hedef": ("Ana hedef", "enum"),
    "kayip_toleransi_yuzde": ("Kayıp toleransı", "pct"),
    "dusus_tepkisi_10": ("%10 düşüşte", "enum"),
    "dusus_tepkisi_20": ("%20 düşüşte", "enum"),
    "sektor_tercihi": ("Takip ettiği sektörler", "text"),
    "gunluk_takip_saat": ("Günlük takip", "saat"),
    "ana_korku": ("En büyük korkusu", "enum"),
    "onceki_basari": ("Geçmiş deneyim", "text"),
    "risk_tercihi": ("Risk/ödül tercihi", "enum"),
    "ogrenme_seviyesi": ("Öğrenme seviyesi", "enum"),
    "aciklama_ister": ("Açıklama ister", "bool"),
}
# enum ham deger -> kullaniciya gosterilecek Turkce etiket
_PROFIL_DEGER_ETIKET = {
    "yeni": "Yeni", "orta": "Orta", "tecrubeli": "Tecrübeli",
    "dusuk": "Düşük", "yuksek": "Yüksek",
    "1ay": "1 ay", "3ay": "3 ay", "6ay": "6 ay", "1yil": "1 yıl",
    "3yil": "3 yıl+", "uzun": "Uzun vade",
    "hizli_kazanc": "Hızlı kazanç", "korunma": "Korunma",
    "uzun_vadeli_buyume": "Uzun vadeli büyüme",
    "bekler": "Bekler", "satar": "Satar", "alir": "Alır (ekler)",
    "kayip": "Kayıp", "firsat_kacirma": "Fırsat kaçırmak", "belirsizlik": "Belirsizlik",
    "az_kazanc_az_risk": "Az kazanç, az risk",
    "cok_kazanc_cok_risk": "Çok kazanç, çok risk", "dengeli": "Dengeli",
    "baslangic": "Başlangıç", "ileri": "İleri",
}
_HAFIZA_KATEGORI = {"oneri": "Öneriler", "karar": "Kararlar",
                    "sohbet": "Sohbetler", "eylem": "Öğrendikleri"}


def _uid(kullanici):
    from src.db import database as db
    return db.user_id_by_ad(kullanici) if kullanici else None


def _profil_deger(anahtar, ham):
    if ham is None or ham == "":
        return None
    _, tip = _PROFIL_GORUNUM.get(anahtar, ("", "text"))
    if tip == "tl":
        try:
            return f"{float(ham):,.0f} TL".replace(",", ".")
        except (ValueError, TypeError):
            return str(ham)
    if tip == "pct":
        try:
            return f"%{float(ham):g}"
        except (ValueError, TypeError):
            return str(ham)
    if tip == "bool":
        return "Evet" if ham in (1, True, "1", "true") else "Hayır"
    if tip == "saat":
        try:
            return f"{float(ham):g} saat/gün"
        except (ValueError, TypeError):
            return str(ham)
    if tip == "enum":
        return _PROFIL_DEGER_ETIKET.get(str(ham), str(ham))
    return str(ham)


def _telegram_of(uid):
    """kullanici tablosundan telegram_id (yoksa None)."""
    from src.db import database as db
    try:
        return next((u.get("telegram_id") for u in db.list_users()
                     if u.get("id") == uid), None)
    except Exception:
        return None


def get_profile_view(kullanici) -> dict:
    from src.db import database as db
    uid = _uid(kullanici)
    if uid is None:
        return {"var": False, "guven_skoru": 0, "alanlar": [], "eksik_alanlar": [],
                "onboarding_done": False, "telegram_id": None, "telegram_bagli": False}
    tg = _telegram_of(uid)
    p = db.get_profile(uid)
    if not p:
        return {"var": False, "kullanici": kullanici, "guven_skoru": 0,
                "alanlar": [], "eksik_alanlar": [], "onboarding_done": False,
                "telegram_id": tg, "telegram_bagli": bool(tg)}
    alanlar = []
    for k, (etiket, _t) in _PROFIL_GORUNUM.items():
        dv = _profil_deger(k, p.get(k))
        if dv is not None:
            alanlar.append({"anahtar": k, "etiket": etiket, "deger": dv})
    skor = p.get("profil_guven_skoru") or 0
    return {
        "var": True, "kullanici": kullanici,
        "guven_skoru": skor,
        "alanlar": alanlar,
        "eksik_alanlar": p.get("eksik_alanlar") or [],
        "onboarding_done": skor >= 85,
        "guncelleme": p.get("guncelleme_tarihi"),
        "telegram_id": tg,
        "telegram_bagli": bool(tg),
    }


def get_memory_view(kullanici, tip=None) -> dict:
    from src.db import database as db
    uid = _uid(kullanici)
    if uid is None:
        return {"kategoriler": {}, "toplam": 0}
    rows = db.list_memory(uid, tip=tip, limit=300)
    kategoriler = {ad: [] for ad in _HAFIZA_KATEGORI.values()}
    for r in rows:
        ic = r.get("icerik")
        if isinstance(ic, dict):
            ozet = (ic.get("ozet") or ic.get("soru") or ic.get("baslik")
                    or ic.get("karar") or ic.get("mesaj") or json.dumps(ic, ensure_ascii=False))
        else:
            ozet = str(ic or "")
        kat = _HAFIZA_KATEGORI.get(r.get("tip"), "Öğrendikleri")
        kategoriler.setdefault(kat, []).append({
            "id": r.get("id"), "tip": r.get("tip"),
            "tarih": (r.get("tarih") or "")[:16].replace("T", " "),
            "ticker": r.get("ticker"), "sonuc": r.get("sonuc"),
            "ozet": _cap(str(ozet), 140), "icerik": ic,
        })
    return {"kategoriler": kategoriler, "toplam": len(rows)}


def onboarding_step(kullanici, messages) -> dict:
    from src.db import database as db
    from src.ai import profiling
    uid = _uid(kullanici)
    if uid is None:
        return {"ok": False, "reply": "Kullanıcı bulunamadı."}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "reply": "AI anahtarı ayarlı değil."}
    try:
        import anthropic
        client = anthropic.Anthropic()
        profile = db.get_profile(uid)
        reply = profiling.onboarding_reply(messages, profile=profile, client=client)
        # Yeni reply'i de gecmise ekleyip profil cikar
        full = list(messages or []) + [{"rol": "bot", "metin": reply}]
        profile = profiling.extract_profile_from_chat(uid, full, client=client)
    except Exception as e:
        return {"ok": False, "reply": f"Hata: {type(e).__name__}"}
    # Onboarding'de Telegram numarasi yakalandiysa kullanici tablosuna yaz
    telegram_bagli = False
    tid = (profile or {}).get("telegram_id")
    if tid:
        try:
            db.update_telegram_id(uid, tid)
            telegram_bagli = True
        except Exception:
            pass
    skor = (profile or {}).get("profil_guven_skoru") or 0
    eksik = (profile or {}).get("eksik_alanlar") or []
    done = skor >= 85
    if done:
        try:
            db.add_memory(uid, "eylem",
                          {"ozet": f"Onboarding tamamlandı (tanıma %{skor})"})
        except Exception:
            pass
    return {"ok": True, "reply": reply, "guven_skoru": skor,
            "eksik_alanlar": eksik, "done": done, "telegram_bagli": telegram_bagli}


# ----------------------------------------------------------------------------
# rotalar
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/manifest.webmanifest")
def manifest():
    return jsonify({
        "name": "Borsa Takip", "short_name": "Borsa Takip",
        "description": "Kişisel borsa asistanı",
        "start_url": "/", "scope": "/", "display": "standalone",
        "orientation": "portrait",
        "background_color": "#06080D", "theme_color": "#06080D",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "any maskable"},
        ],
    })


@app.route("/api/stocks")
def api_stocks():
    return jsonify(get_stocks())


@app.route("/api/portfolio")
def api_portfolio():
    return jsonify(get_portfolio(request.args.get("kullanici")))


@app.route("/api/karne")
def api_karne():
    return jsonify(get_karne(request.args.get("kullanici")))


@app.route("/api/paper-trading")
def api_paper_trading():
    return jsonify(get_paper_trading())


@app.route("/api/haber-etki")
def api_haber_etki():
    return jsonify(get_haber_etki())


@app.route("/api/alerts")
def api_alerts():
    return jsonify(get_alerts())


@app.route("/api/summary")
def api_summary():
    return jsonify(get_summary())


@app.route("/api/gunun-hareketlileri")
def api_gunun_hareketlileri():
    return jsonify(get_gunun_hareketlileri())


@app.route("/api/search")
def api_search():
    return jsonify(get_search(request.args.get("q", ""),
                              request.args.get("market", "bist"),
                              request.args.get("kullanici")))


@app.route("/api/chat-suggestions")
def api_chat_suggestions():
    return jsonify({"sorular": get_chat_suggestions(request.args.get("kullanici"))})


@app.route("/api/alarms")
def api_alarms():
    return jsonify({"alarmlar": get_alarms(request.args.get("kullanici"))})


@app.route("/api/sektor-analiz")
def api_sektor_analiz():
    key = (request.args.get("sektor") or "").strip().lower()
    if key not in _SEKTORLER:
        key = _sektor_key_from_text(key) or key
    res = get_sektor_analiz(key)
    return jsonify(res or {"sektor": key, "hisseler": [],
                           "ozet": "Bu sektör için analiz bulunamadı."})


@app.route("/api/alarms/remove", methods=["POST"])
def api_alarms_remove():
    from src.db import database as db
    d = request.get_json(silent=True) or {}
    uid = _uid(d.get("kullanici"))
    if uid is None or d.get("id") is None:
        return jsonify({"ok": False, "hata": "kullanici/id gerekli"})
    ok = db.delete_price_alarm(d.get("id"), kullanici_id=uid)
    return jsonify({"ok": ok})


@app.route("/api/decisions")
def api_decisions():
    return jsonify(get_decisions())


@app.route("/api/backtest")
def api_backtest():
    """Aylik backtest sonuclari (data/backtest_results.json). Yoksa bos doner."""
    p = DATA / "backtest_results.json"
    if not p.exists():
        return jsonify({"var": False})
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return jsonify({"var": True, "genel": d.get("genel", {}),
                        "hisse_bazli": d.get("hisse_bazli", {}),
                        "aciklama": d.get("aciklama", ""),
                        "uretim_tarihi": d.get("uretim_tarihi", "")})
    except Exception:
        return jsonify({"var": False})


@app.route("/api/profile")
def api_profile():
    return jsonify(get_profile_view(request.args.get("kullanici")))


@app.route("/api/profile/update", methods=["POST"])
def api_profile_update():
    from src.db import database as db
    d = request.get_json(silent=True) or {}
    uid = _uid(d.get("kullanici"))
    if uid is None:
        return jsonify({"ok": False, "hata": "kullanici yok"})
    alanlar = {k: v for k, v in d.items()
               if k in db._PROFIL_KOLONLAR and v is not None}
    p = db.upsert_profile(uid, **alanlar) if alanlar else db.get_profile(uid)
    return jsonify({"ok": True, "guven_skoru": (p or {}).get("profil_guven_skoru", 0)})


@app.route("/api/profile/update-telegram", methods=["POST"])
def api_profile_update_telegram():
    from src.db import database as db
    d = request.get_json(silent=True) or {}
    uid = _uid(d.get("kullanici"))
    if uid is None:
        return jsonify({"ok": False, "hata": "kullanici yok"})
    raw = str(d.get("telegram_id") or "")
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not (7 <= len(digits) <= 15):
        return jsonify({"ok": False, "hata": "Geçersiz Telegram ID (7-15 hane olmalı)"})
    db.update_telegram_id(uid, int(digits))
    return jsonify({"ok": True, "telegram_id": int(digits)})


@app.route("/api/memory")
def api_memory():
    return jsonify(get_memory_view(request.args.get("kullanici"),
                                   request.args.get("tip")))


@app.route("/api/memory/clear", methods=["POST"])
def api_memory_clear():
    from src.db import database as db
    d = request.get_json(silent=True) or {}
    uid = _uid(d.get("kullanici"))
    if uid is None:
        return jsonify({"ok": False})
    n = db.clear_memory(uid)
    return jsonify({"ok": True, "silinen": n})


@app.route("/api/onboarding", methods=["POST"])
def api_onboarding():
    d = request.get_json(silent=True) or {}
    return jsonify(onboarding_step(d.get("kullanici"), d.get("messages") or []))


@app.route("/api/model-portfoy")
def api_model_portfoy():
    return jsonify(get_model_portfoy())


@app.route("/api/backtest-aggressive")
def api_backtest_aggressive():
    """Agresif strateji (500K) portfoy backtesti (data/backtest_aggressive.json)."""
    p = DATA / "backtest_aggressive.json"
    if not p.exists():
        return jsonify({"var": False})
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return jsonify({"var": True, "metrikler": d.get("metrikler", {}),
                        "parametreler": d.get("parametreler", {}),
                        "buy_hold": d.get("buy_hold", {}),
                        "islemler": (d.get("islemler") or [])[-15:],
                        "aciklama": d.get("aciklama", ""),
                        "uretim_tarihi": d.get("uretim_tarihi", "")})
    except Exception:
        return jsonify({"var": False})


@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    d = request.get_json(silent=True) or {}
    return jsonify(watchlist_add(
        d.get("ticker", ""), d.get("market", "bist"),
        d.get("isim", ""), d.get("cg_id", "")))


@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    d = request.get_json(silent=True) or {}
    return jsonify(watchlist_remove(d.get("ticker", ""), d.get("market", "bist")))


@app.route("/api/portfolio/parse-image", methods=["POST"])
def api_portfolio_parse_image():
    d = request.get_json(silent=True) or {}
    images = d.get("images")
    if images is None:
        images = d.get("image", "")          # geri uyumluluk (tek gorsel)
    return jsonify(parse_portfolio_image(images))


@app.route("/api/portfolio/add", methods=["POST"])
def api_portfolio_add():
    d = request.get_json(silent=True) or {}
    return jsonify(portfolio_add(d))


@app.route("/api/portfolio/remove", methods=["POST"])
def api_portfolio_remove():
    d = request.get_json(silent=True) or {}
    return jsonify(portfolio_remove(d))


@app.route("/api/today")
def api_today():
    return jsonify(get_today(request.args.get("kullanici")))


@app.route("/api/overview")
def api_overview():
    kullanici = request.args.get("kullanici")
    comm = _commentary_by_ticker()
    recs = [comm[t] for t in _owned_by_user(kullanici)
            if t in comm and not comm[t].get("skipped")]
    return jsonify({"yorum": _cap(_portfolio_overview(kullanici, recs), 280)})


@app.route("/api/radar")
def api_radar():
    return jsonify(get_radar(request.args.get("market", "all")))


@app.route("/api/stock/<ticker>")
def api_stock(ticker):
    return jsonify(get_stock_detail(ticker, request.args.get("market", "bist")))


# periyot -> (yf periyot/gun, interval). 5m = intraday (1G/1H), 1d = gunluk kapanis.
_PERIODS = {
    "1G": ("1d", "5m"), "1H": ("5d", "5m"),
    "1A": (30, "1d"), "3A": (90, "1d"), "6A": (180, "1d"),
    "1Y": (365, "1d"), "5Y": (1825, "1d"),
}


def _intraday_series(ticker: str, market: str, yf_period: str) -> dict:
    """5 dakikalik intraday seri (1G/1H). {seri:[{t,c}], acilis: ilk barin acilisi}."""
    import yfinance as yf
    t = (ticker or "").upper().replace(".IS", "")
    symbol = t if market in ("abd", "kripto") else f"{t}.IS"
    try:
        df = yf.Ticker(symbol).history(period=yf_period, interval="5m")
    except Exception:
        return {"seri": [], "acilis": None}
    if df is None or df.empty:
        return {"seri": [], "acilis": None}
    out = []
    for ix, c, vol in zip(df.index, df["Close"], df["Volume"]):
        try:
            cv = float(c)
        except (TypeError, ValueError):
            continue
        if cv != cv:                       # NaN
            continue
        try:
            ts = ix.isoformat()
        except Exception:
            ts = str(ix)
        out.append({"t": ts, "c": round(cv, 2),
                    "v": int(vol) if vol == vol else 0})
    acilis = None
    try:
        acilis = round(float(df["Open"].iloc[0]), 2)
    except Exception:
        acilis = out[0]["c"] if out else None
    return {"seri": out, "acilis": acilis}


@app.route("/api/series/<ticker>")
def api_series(ticker):
    """Zaman filtreli fiyat serisi. 1G/1H intraday (5dk), digerleri gunluk.
    SVG icin <=180 noktaya seyreltir (son nokta korunur)."""
    market = request.args.get("market", "bist")
    period = (request.args.get("period") or "1A").upper()
    cfg = _PERIODS.get(period) or _PERIODS["1A"]
    intraday = cfg[1] == "5m"
    acilis = None
    if intraday:
        r = _intraday_series(ticker, market, cfg[0])
        seri, acilis = r["seri"], r["acilis"]
    else:
        seri = _price_series(ticker, market, cfg[0])["seri"]
    if len(seri) > 180:                       # seyrelt (son nokta korunur)
        step = len(seri) // 180 + 1
        seri = seri[::step] + ([seri[-1]] if (len(seri) - 1) % step else [])
    cs = [p["c"] for p in seri]
    return jsonify({"period": period, "intraday": intraday, "grafik": seri,
                    "acilis": acilis,           # sadece intraday'de (1G/1H) dolu
                    "dusuk": round(min(cs), 2) if cs else None,
                    "yuksek": round(max(cs), 2) if cs else None,
                    "son": cs[-1] if cs else None})


@app.route("/api/ask", methods=["POST"])
@app.route("/api/chat", methods=["POST"])
def api_ask():
    d = request.get_json(silent=True) or {}
    return jsonify(ask_bot(d.get("soru") or d.get("mesaj") or d.get("message", ""),
                           d.get("kullanici"),
                           d.get("gecmis") or d.get("messages")))


@app.route("/api/portfolio-analysis")
def api_portfolio_analysis():
    return jsonify(portfolio_analysis(request.args.get("kullanici")))


@app.route("/api/today-summary")
def api_today_summary():
    t = get_today(request.args.get("kullanici"))
    return jsonify({"selamlama": t["selamlama"], "portfoy_yorum": t["portfoy_yorum"],
                    "etiketler": t["etiketler"]})


@app.route("/api/news")
def api_news():
    return jsonify(get_news())




@app.route("/api/chat-stream", methods=["POST"])
def api_ask_stream():
    from flask import stream_with_context
    d = request.get_json(silent=True) or {}
    soru = d.get("soru") or d.get("mesaj") or ""
    kullanici = d.get("kullanici")
    gecmis = d.get("gecmis") or d.get("messages")
    @stream_with_context
    def generate():
        import time as _time
        result = ask_bot(soru, kullanici, gecmis)
        cevap = result.get("cevap", "Yanit uretemedi.")
        paragraflar = [p.strip() for p in cevap.split("\n") if p.strip()]
        if not paragraflar:
            paragraflar = [cevap]
        for i, para in enumerate(paragraflar):
            yield "data: " + json.dumps({"paragraf": para}, ensure_ascii=False) + "\n\n"
            if i < len(paragraflar) - 1:
                _time.sleep(2.0)
        yield "data: " + json.dumps({"done": True}, ensure_ascii=False) + "\n\n"
    return app.response_class(generate(), mimetype="text/event-stream",
                               headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Transfer-Encoding": "chunked"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
