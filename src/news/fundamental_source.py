"""Temel (bilanco) veri kaynagi - yfinance .info / .financials.

get_fundamentals(ticker) sirket temel oranlarini dondurur:
  - fk            : F/K orani (trailingPE)
  - pddd          : PD/DD (priceToBook)
  - roe_%         : ozsermaye karliligi (returnOnEquity)
  - kar_marji_%   : net kar marji (profitMargins)
  - borc_ozsermaye: borc / ozsermaye (debtToEquity)
  - gelir_buyume_%: yillik gelir buyumesi (revenueGrowth)
  - favok_marji_% : FAVOK marji (ebitdaMargins)

Oranlar (ROE, kar marji, gelir buyume, FAVOK marji) yfinance'te 0-1 araliginda
gelir; yuzdeye cevrilir. Veri yoksa ilgili alan None, hicbiri yoksa available=False.
"""
import time

_CACHE = {}
_TTL = 3600.0   # bilanco verisi yavas degisir; 1 saat onbellek


def _sym(ticker: str) -> str:
    t = (ticker or "").upper().strip()
    return t if t.endswith(".IS") else f"{t}.IS"


def _f(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _pct(v):
    """0-1 araligindaki orani yuzdeye cevirir (0.1556 -> 15.56)."""
    try:
        return round(float(v) * 100, 2)
    except (TypeError, ValueError):
        return None


def get_fundamentals(ticker: str) -> dict:
    """Hisse temel oranlarini dondurur (yfinance .info). TTL onbellekli."""
    ticker = (ticker or "").upper().replace(".IS", "")
    now = time.monotonic()
    hit = _CACHE.get(ticker)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]

    info = {}
    try:
        import yfinance as yf
        info = yf.Ticker(_sym(ticker)).get_info() or {}
    except Exception:
        info = {}

    out = {
        "ticker": ticker,
        "fk": _f(info.get("trailingPE")),
        "pddd": _f(info.get("priceToBook")),
        "roe_%": _pct(info.get("returnOnEquity")),
        "kar_marji_%": _pct(info.get("profitMargins")),
        "borc_ozsermaye": _f(info.get("debtToEquity")),
        "gelir_buyume_%": _pct(info.get("revenueGrowth")),
        "favok_marji_%": _pct(info.get("ebitdaMargins")),
        "kaynak": "yfinance",
    }
    metrikler = ("fk", "pddd", "roe_%", "kar_marji_%",
                 "borc_ozsermaye", "gelir_buyume_%", "favok_marji_%")
    out["available"] = any(out.get(k) is not None for k in metrikler)
    _CACHE[ticker] = (now, out)
    return out


if __name__ == "__main__":
    import json
    import sys
    for tk in (sys.argv[1:] or ["THYAO"]):
        print(json.dumps(get_fundamentals(tk), ensure_ascii=False, indent=2))
