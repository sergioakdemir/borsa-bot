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
_SEARCH_TTL = 60.0  # saniye

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
    if "SAT" in d:               # SAT, GUCLU_SAT
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
                prev, last = float(col.iloc[-2]), float(col.iloc[-1])
                chg = ((last - prev) / prev * 100) if prev else None
                out[s] = {"fiyat": round(last, 2),
                          "gunluk": round(chg, 2) if chg is not None else None}
            elif len(col) >= 1:
                out[s] = {"fiyat": round(float(col.iloc[-1]), 2), "gunluk": None}
        except Exception:
            continue
    _cache_set(ck, out)
    return out


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


def get_search(q: str, market: str = "bist") -> list[dict]:
    """Piyasaya gore arama. market='all' -> BIST + ABD + Kripto birlesik."""
    market = (market or "bist").lower()
    if market == "abd":
        return search_us(q)
    if market == "kripto":
        return search_crypto(q)
    if market == "all":
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
        return out
    return _search_bist(q)


def get_portfolio(kullanici: str | None = None) -> dict:
    """Portfoy ozeti. kullanici verilirse (ad, orn. 'serhat') yalniz o kisinin
    pozisyonlari dondurulur; yoksa tum kullanicilar."""
    comm = _commentary_by_ticker()
    pozisyonlar = []
    toplam_maliyet = toplam_deger = 0.0

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
        toplam_maliyet += maliyet

        kz = kz_yuzde = None
        if guncel is not None:
            deger = adet * guncel
            toplam_deger += deger
            kz = deger - maliyet
            kz_yuzde = (kz / maliyet * 100) if maliyet else None
        else:
            toplam_deger += maliyet

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
    return {
        "pozisyonlar": pozisyonlar,
        "genel_yorum": _cap(_overview_fallback(owned_recs), 280),   # AI yorumu /api/overview ile asenkron
        "ozet": {
            "maliyet": toplam_maliyet,
            "deger": toplam_deger,
            "kz": toplam_kz,
            "kz_yuzde": (toplam_kz / toplam_maliyet * 100) if toplam_maliyet else None,
        },
    }


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
    return {"ok": True, "ticker": ticker, "adet": adet,
            "fiyat": fiyat, "para_birimi": para_birimi}


def portfolio_remove(d: dict) -> dict:
    """Bir portfoy pozisyonunu id'ye gore siler."""
    pid = d.get("id")
    if pid is None:
        return {"ok": False, "hata": "Pozisyon id gerekli."}
    try:
        with sqlite3.connect(DB_PATH) as c:
            cur = c.execute("DELETE FROM portfoy WHERE id=?", (int(pid),))
            c.commit()
    except (sqlite3.Error, ValueError, TypeError) as e:
        return {"ok": False, "hata": f"Silinemedi: {type(e).__name__}"}
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


def get_karne() -> dict:
    """Defter mantigi: her hisse icin 'karar -> sonuc' satiri.

    Gercek karar logu gelene kadar backtest.json ozetinden turetilir.
    Bir hisse stratejisi al-tut'u gectiyse ✅, gectiyse degilse ❌.
    """
    bt = _read_json(DATA / "backtest.json", {"hisseler": [], "ozet": {}, "ayar": {}})
    ayar = bt.get("ayar", {})
    son = (ayar.get("end") or "")

    satirlar = []
    for h in bt.get("hisseler", []):
        tkr = (h.get("symbol") or "").replace(".IS", "").upper()
        strat = h.get("strateji_getiri_%")
        altut = h.get("al_tut_getiri_%")
        basari = h.get("basari_orani_%")
        kazandi = (strat is not None and altut is not None and strat >= altut)
        kd = h.get("karar_dagilimi", {}) or {}
        # en cok verilen yonlu karar
        baskin = max(((k, v) for k, v in kd.items() if k != "VETO" and k != "TUT"),
                     key=lambda kv: kv[1], default=("AL", 0))[0]
        etiket, renk = _classify(baskin)
        satirlar.append({
            "ticker": tkr,
            "isim": company_name(tkr),
            "tarih": _tarih_kisa(son),
            "etiket": etiket,
            "renk": renk,
            "kazandi": kazandi,
            "getiri": strat,
            "altut": altut,
            "basari": basari,
            "sebep": (f"strateji {strat:+.1f}% vs al-tut {altut:+.1f}%"
                      if strat is not None and altut is not None else ""),
        })

    return {"satirlar": satirlar, "ozet": bt.get("ozet", {}), "ayar": ayar}


def _karar_label(karar: str) -> str:
    return {"AL": "AL", "AL_TEMKINLI": "AL (temkinli)", "TUT": "TUT",
            "SAT": "SAT", "GUCLU_SAT": "Güçlü SAT", "VETO": "VETO"}.get(
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
    # 1) Fiyat hareketi uyarilari (uyari_kayit) -> bot ozeti
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            for r in c.execute(
                    "SELECT * FROM uyari_kayit ORDER BY id DESC LIMIT 8"):
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
                # generic tekrar yerine hissenin kendi durumundan kisa, ozel yorum
                aciklama = ozet or (f"{ad} tarafında yeni bir gelişme var; "
                                    "etkisi henüz fiyata yansımadı.")
                out.append({"ticker": tkr, "isim": ad, "tip": "haber",
                            "tur": "Haber Etkisi",
                            "baslik": _cap(f"{tkr} için gelişme ihtimali", 45),
                            "aciklama": _cap(aciklama, 140),
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
        "TUT": ("TUT", "yellow"), "VETO": ("BEKLE", "yellow"),
        "SAT": ("AZALT", "red"), "GUCLU_SAT": ("SAT", "red"),
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


def _haber_gorsel(baslik: str):
    """Haber kategorisi -> ikon + gradient (kategori bazli temsili gorsel)."""
    t = _norm(baslik or "")
    if any(w in t for w in ("savas", "ates", "hurmuz", "iran", "israil", "jeopolit",
                            "saldiri", "ambargo", "gerilim", "catisma")):
        return {"kategori": "Jeopolitik", "ikon": "◆",
                "gradient": "linear-gradient(140deg,#1a0f0f 0%,#7f1d1d 60%,#b45309 100%)"}
    if any(w in t for w in ("faiz", "enflasyon", "tufe", "dolar", "kur", "merkez",
                            "tcmb", "fed", "makro", "buyume", "resesyon")):
        return {"kategori": "Makro", "ikon": "▦",
                "gradient": "linear-gradient(140deg,#0b1220 0%,#1e3a8a 60%,#5b21b6 100%)"}
    if any(w in t for w in ("petrol", "enerji", "dogalgaz", "elektrik", "celik",
                            "emtia", "altin", "gumus")):
        return {"kategori": "Enerji/Sektör", "ikon": "⬡",
                "gradient": "linear-gradient(140deg,#1a1206 0%,#92400e 55%,#d97706 100%)"}
    if any(w in t for w in ("bilanco", "kar", "temettu", "ihrac", "sozlesme", "yatirim",
                            "fabrika", "satin alma", "birlesme", "hat", "siparis")):
        return {"kategori": "Şirket", "ikon": "▲",
                "gradient": "linear-gradient(140deg,#06120e 0%,#064e3b 55%,#0f766e 100%)"}
    return {"kategori": "Piyasa", "ikon": "◉",
            "gradient": "linear-gradient(140deg,#0c0f1a 0%,#3730a3 55%,#1e40af 100%)"}


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
            system=("Sen kullanicinin kisisel borsa asistanisin. Sade, sicak Turkce; "
                    "jargon yok. Portfoyun GENEL durumunu 2-3 cumlede ozetle: panik mi "
                    "var, nelere dikkat etmeli. Sadece verilen veriyi kullan, rakam uydurma. "
                    "Markdown, baslik, yildiz veya madde KULLANMA; sadece duz cumleler yaz."),
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

    hisseler = [_mini_view(r) for r in recs]
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

    return {
        "selamlama": f"{selam}{(' ' + ad) if ad else ''}",
        "tarih": tarih_str,
        "portfoy_yorum": _cap(_overview_fallback(recs), 280),   # AI yorumu /api/overview ile asenkron
        "etiketler": etiketler,
        "hisseler": hisseler,
        "onemli_haber": haber,
        "firsatlar": firsatlar[:5],
    }


def get_radar(market: str = "all") -> dict:
    comm = _commentary_by_ticker()
    wl = _load_watchlist()
    izleme_kodlar = {t.upper() for t in wl.get("kisisel", [])}

    alinabilir, riskli = [], []
    for rec in comm.values():
        etiket, _ = _karar5(rec.get("final_decision"))
        mv = _mini_view(rec, ozet_limit=120)   # radar: kisa
        if etiket == "AL":
            alinabilir.append(mv)
        elif etiket in ("SAT", "AZALT"):
            riskli.append(mv)
    alinabilir.sort(key=lambda c: c.get("skor") or 0, reverse=True)
    izleme = [_mini_view(comm[t], ozet_limit=120) if t in comm else
              {"ticker": t, "isim": company_name(t), "market": "bist",
               "etiket": None, "renk": "gray", "fiyat": None, "gunluk": None,
               "para_birimi": "₺", "summary": "", "action": "",
               "risk": "—", "risk_renk": "gray", "riskReason": ""}
              for t in izleme_kodlar]
    return {"alinabilir": alinabilir, "izleme": izleme, "riskli": riskli}


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
    seri = [{"t": pd.Timestamp(ix).date().isoformat(), "c": round(float(c), 2)}
            for ix, c in df["Close"].items()]
    return {"seri": seri,
            "dusuk": round(float(df["Low"].min()), 2),
            "yuksek": round(float(df["High"].max()), 2),
            "son": round(float(df["Close"].iloc[-1]), 2)}


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


def ask_bot(soru: str, kullanici=None) -> dict:
    soru = (soru or "").strip()
    if not soru:
        return {"ok": False, "cevap": "Bir soru yaz."}
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {"ok": False, "cevap": "AI anahtarı ayarlı değil; şu an soru yanıtlayamıyorum."}
    comm = _commentary_by_ticker()
    owned = set(_owned_by_user(kullanici))
    baglam = []
    for t in sorted(owned):
        r = comm.get(t)
        if r and not r.get("skipped"):
            baglam.append({"hisse": t, "karar": r.get("final_decision"),
                           "puan": r.get("score"), "risk": (r.get("risk") or {}).get("score"),
                           "not": _ilk_cumleler(r.get("gerekce", ""), 1)})
    # tum izlenen hisseler (sahip olunmayan hisse sorulari icin kisa baglam)
    piyasa = []
    for t, r in comm.items():
        if r.get("skipped"):
            continue
        piyasa.append({"hisse": t, "karar": r.get("final_decision"),
                       "puan": r.get("score"), "risk": (r.get("risk") or {}).get("score"),
                       "not": _ilk_cumleler(r.get("gerekce", ""), 1)})
    try:
        from src.news.macro import get_macro
        makro = get_macro()
    except Exception:
        makro = {}
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=_CHAT_MODEL, max_tokens=600,
            system=("Sen kullanicinin kisisel borsa asistanisin. Sade, net, sicak Turkce "
                    "konus; jargon (RSI/MACD) yok. Yanitlarini SADECE verilen baglama "
                    "(kullanicinin portfoyu + makro) dayandir; baglamda yoksa 'elimde bu "
                    "konuda veri yok' de, uydurma. Bu yatirim tavsiyesi degildir. "
                    "Markdown, tablo, baslik veya yildiz KULLANMA; kisa, duz cumlelerle "
                    "sohbet eder gibi yanitla (en fazla birkac cumle)."),
            messages=[{"role": "user", "content":
                       f"Portfoyum: {json.dumps(baglam, ensure_ascii=False)}\n"
                       f"Izlenen hisseler: {json.dumps(piyasa, ensure_ascii=False)}\n"
                       f"Makro: {json.dumps(makro, ensure_ascii=False)}\n\nSoru: {soru}"}],
        )
        cevap = "".join(getattr(b, "text", "") for b in resp.content
                        if getattr(b, "type", "") == "text").strip()
        cevap = re.sub(r"^[#>\-\*\s]*\|.*$", "", cevap, flags=re.M)   # tablo satirlari
        cevap = re.sub(r"[#*`_]+", "", cevap).strip()
        return {"ok": True, "cevap": cevap or "Yanıt üretemedim."}
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
    return jsonify(get_karne())


@app.route("/api/alerts")
def api_alerts():
    return jsonify(get_alerts())


@app.route("/api/summary")
def api_summary():
    return jsonify(get_summary())


@app.route("/api/search")
def api_search():
    return jsonify(get_search(request.args.get("q", ""),
                              request.args.get("market", "bist")))


@app.route("/api/decisions")
def api_decisions():
    return jsonify(get_decisions())


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
    for ix, c in df["Close"].items():
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
        out.append({"t": ts, "c": round(cv, 2)})
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
                           d.get("kullanici")))


@app.route("/api/today-summary")
def api_today_summary():
    t = get_today(request.args.get("kullanici"))
    return jsonify({"selamlama": t["selamlama"], "portfoy_yorum": t["portfoy_yorum"],
                    "etiketler": t["etiketler"]})


@app.route("/api/news")
def api_news():
    return jsonify(get_news())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
