"""Midas Atlas canli tick alicisi + 'once Midas, yoksa yfinance' fiyat katmani.

Mimari: MacBook Chrome, Midas WS'ini residential IP'sinden dinler ve her tick'i
POST /api/midas/tick ile buraya gonderir (bkz. src/web/app.py). Tick'ler hem
bellek-ici cache'e hem data/midas_ticks.json'a yazilir. Web app yazar; fiyat
okuyan CRON surecleri (ayri process) ayni veriyi DOSYADAN okur -> cache
surecler-arasi paylasimli olmali (bu yuzden dosya-destekli).

Tazelik SUNUCU alis zamanina gore olculur (received_at), tick'in kendi damgasina
degil (istemci saat kaymasina karsi). fresh_price() son <=60 sn icinde tick gelen
sembol icin fiyati doner; yoksa None -> cagiran yfinance'e duser.
"""
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
TICKS_PATH = ROOT / "data" / "midas_ticks.json"
LOG_PATH = ROOT / "logs" / "midas_ticks.log"
_TZ = ZoneInfo("Europe/Istanbul")

MAX_AGE_SEC = 60          # bir tick bu kadar sn sonra "bayat" sayilir
_MAX_SYMBOLS = 500        # kotuye kullanima karsi ust sinir
_RING = 100              # /status ve loglama icin bellekte tutulan son tick sayisi

_LOCK = threading.RLock()
_CACHE: dict[str, dict] = {}          # symbol -> {price, volume, ts, received_at, raw?}
_RECENT: deque = deque(maxlen=_RING)  # son tick'ler (debug/log)
_file_cache = {"mtime": None, "data": {}}

# esnek alan adlari (Midas ham formatina toleransli)
_SYM_KEYS = ("symbol", "sembol", "s", "ticker", "code", "channel", "instrument")
_PRICE_KEYS = ("price", "fiyat", "p", "last", "l", "lastprice", "c", "close", "value")
_VOL_KEYS = ("volume", "vol", "v", "hacim", "q", "size", "qty")
_TS_KEYS = ("timestamp", "ts", "time", "t", "date", "datetime")


def _first(d: dict, keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
        # buyuk/kucuk harf toleransi
        for dk in d:
            if dk.lower() == k and d[dk] not in (None, ""):
                return d[dk]
    return None


def normalize_symbol(val) -> str | None:
    """'trade-tr-THYAO-mi-instant' / 'THYAO.IS' / 'thyao' -> 'THYAO'."""
    if val is None:
        return None
    s = str(val).strip().upper()
    if not s:
        return None
    # centrifugo kanal formati: TRADE-TR-<TICKER>-MI-INSTANT
    if "-" in s and "TR" in s:
        parts = [p for p in s.split("-") if p]
        # en olası ticker: TR'den sonraki, salt harf/rakam parcasi
        for i, p in enumerate(parts):
            if p in ("TR", "US") and i + 1 < len(parts):
                return parts[i + 1].replace(".IS", "")
    return s.replace(".IS", "")


def _to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _persist_locked():
    """_CACHE'i atomik olarak diske yaz (kilit altinda cagrilir).
    Surecler-arasi okuyan (cron) suffer etmesin diye kayip yazma OLMAMALI."""
    try:
        TICKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = TICKS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_CACHE, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, TICKS_PATH)          # atomik
    except Exception:
        pass                                  # persist hatasi tick akisini bozmaz


def flush():
    """Bekleyen cache'i diske yaz (batch sonrasi tek cagri)."""
    with _LOCK:
        _persist_locked()


def record_tick(payload: dict, persist: bool = True) -> dict:
    """Ham tick sozlugunu normalize edip cache'e yazar. Doner: {ok, symbol, price}.
    persist=False -> diske yazma ertelenir (batch icin); caller flush() cagirmali."""
    if not isinstance(payload, dict):
        return {"ok": False, "hata": "gecersiz govde"}
    symbol = normalize_symbol(_first(payload, _SYM_KEYS))
    price = _to_float(_first(payload, _PRICE_KEYS))
    if not symbol or price is None:
        return {"ok": False, "hata": "symbol/price cozulemedi"}
    volume = _to_float(_first(payload, _VOL_KEYS))
    ts = _first(payload, _TS_KEYS)
    now = time.time()
    entry = {"price": price, "volume": volume, "ts": ts, "received_at": now}
    with _LOCK:
        if symbol not in _CACHE and len(_CACHE) >= _MAX_SYMBOLS:
            return {"ok": False, "hata": "sembol limiti"}
        _CACHE[symbol] = entry
        _RECENT.append({"symbol": symbol, "price": price, "volume": volume,
                        "received_at": now})
        if persist:
            _persist_locked()
    _log_tick(symbol, price, volume)
    return {"ok": True, "symbol": symbol, "price": price}


def _log_tick(symbol, price, volume):
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {symbol} price={price} vol={volume}\n")
    except Exception:
        pass


def _current_map() -> dict:
    """Surecler-arasi birlesik gorunum: dosya (mtime-cache'li) + bellek (yeni kazanir)."""
    try:
        mtime = TICKS_PATH.stat().st_mtime
    except OSError:
        mtime = None
    if mtime is not None and _file_cache["mtime"] != mtime:
        try:
            _file_cache["data"] = json.loads(TICKS_PATH.read_text(encoding="utf-8"))
        except Exception:
            _file_cache["data"] = {}
        _file_cache["mtime"] = mtime
    merged = dict(_file_cache["data"] or {})
    with _LOCK:
        for sym, e in _CACHE.items():
            cur = merged.get(sym)
            if not cur or e.get("received_at", 0) >= cur.get("received_at", 0):
                merged[sym] = e
    return merged


def get_tick(ticker, max_age_sec: float = MAX_AGE_SEC) -> dict | None:
    """Sembol icin TAZE tick ({price, volume, ts, yas_sn}) veya None."""
    sym = normalize_symbol(ticker)
    if not sym:
        return None
    e = _current_map().get(sym)
    if not e:
        return None
    yas = time.time() - e.get("received_at", 0)
    if yas > max_age_sec:
        return None
    return {"price": e.get("price"), "volume": e.get("volume"),
            "ts": e.get("ts"), "yas_sn": round(yas, 1)}


def fresh_price(ticker, max_age_sec: float = MAX_AGE_SEC) -> float | None:
    """'Once Midas' katmani: son <=max_age_sec icinde tick geldiyse fiyat, yoksa None."""
    t = get_tick(ticker, max_age_sec)
    return t["price"] if t else None


def status(max_age_sec: float = MAX_AGE_SEC) -> dict:
    """Saglik ozeti: kac sembol taze, son tick ne zaman geldi."""
    m = _current_map()
    now = time.time()
    taze, semboller = [], {}
    son = None
    for sym, e in m.items():
        ra = e.get("received_at", 0)
        yas = now - ra
        if son is None or ra > son:
            son = ra
        if yas <= max_age_sec:
            taze.append(sym)
            semboller[sym] = {"price": e.get("price"), "yas_sn": round(yas, 1)}
    return {
        "toplam_sembol": len(m),
        "taze_sembol": len(taze),
        "taze_semboller": semboller,
        "son_tick_yas_sn": round(now - son, 1) if son else None,
        "son_tick_zaman": (datetime.fromtimestamp(son, _TZ).strftime("%Y-%m-%d %H:%M:%S")
                           if son else None),
        "max_age_sn": max_age_sec,
    }
