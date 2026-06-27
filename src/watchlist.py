"""Izleme listesi yukleyici. Iki katman: config/watchlist.json.

- bist_endeks : BIST-30 otomatik tarama listesi
- kisisel     : kullanicinin kisisel izleme listesi (simdilik bos)

load_watchlist() ikisinin birlesik, sirali, tekrarsiz halini dondurur.
"""
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "config" / "watchlist.json"
_DEFAULT = ["THYAO", "GARAN", "ASELS", "KCHOL", "TUPRS"]


def _clean(lst):
    return [str(t).upper().replace(".IS", "").strip() for t in (lst or []) if str(t).strip()]


def _data():
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_index() -> list[str]:
    d = _data()
    return _clean(d.get("bist_endeks") or d.get("bist") or d.get("tickers") or [])


def load_personal() -> list[str]:
    return _clean(_data().get("kisisel") or [])


def load_watchlist() -> list[str]:
    seen, out = set(), []
    for t in load_index() + load_personal():
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out or _DEFAULT


_US_MARKET_KODLARI = {"abd", "us", "usd", "amerika", "nasdaq", "nyse", "amex"}


def load_markets() -> dict:
    """kisisel_diger'deki ticker -> market kodu ('abd'/'us'/...) haritasi.
    yfinance yonlendirmesinde kullanilir (US tickerlar '.IS' EKLENMEDEN aranir)."""
    out = {}
    for e in (_data().get("kisisel_diger") or []):
        t = str(e.get("ticker") or "").upper().replace(".IS", "").strip()
        m = str(e.get("market") or "").lower().strip()
        if t and m:
            out[t] = m
    return out


def is_us_ticker(ticker: str) -> bool:
    """Ticker watchlist'te ABD piyasasi olarak mi tanimli? (RXT/NVDA gibi)."""
    norm = str(ticker or "").upper().replace(".IS", "").strip()
    return load_markets().get(norm, "") in _US_MARKET_KODLARI


def load_mover_threshold(default: float = 3.0) -> float:
    """Brifingde -hareketli- sayilmak icin gereken |gunluk degisim| esigi (%)."""
    try:
        return float(_data().get("hareketli_esik", default))
    except Exception:
        return default
