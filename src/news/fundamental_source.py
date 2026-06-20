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


_VOL_CACHE = {}
_VOL_TTL = 600.0   # gun ici hacim degisir; 10 dk onbellek


def get_volume_anomaly(ticker: str) -> dict:
    """Bugunku hacmi son 5 gunun ortalamasiyla kiyaslar.

    - ort_5g  : bugun haric onceki 5 islem gununun ortalama hacmi
    - kat     : bugunku hacim / 5g ortalama
    - seviye  : kat>=3 -> 'COK YUKSEK', kat>=2 -> 'YUKSEK', altinda 'NORMAL'
    Veri yetersizse available=False.
    """
    ticker = (ticker or "").upper().replace(".IS", "")
    now = time.monotonic()
    hit = _VOL_CACHE.get(ticker)
    if hit and (now - hit[0]) < _VOL_TTL:
        return hit[1]

    vols = []
    try:
        import yfinance as yf
        df = yf.Ticker(_sym(ticker)).history(period="1mo")
        if df is not None and not df.empty:
            vols = [float(v) for v in df["Volume"].tolist() if v and v > 0]
    except Exception:
        vols = []

    if len(vols) < 6:
        out = {"ticker": ticker, "available": False,
               "neden": "Yeterli hacim verisi yok"}
        _VOL_CACHE[ticker] = (now, out)
        return out

    bugun = vols[-1]
    onceki5 = vols[-6:-1]                 # bugun haric son 5 islem gunu
    ort = sum(onceki5) / len(onceki5)
    kat = round(bugun / ort, 2) if ort else None
    if kat is not None and kat >= 3:
        seviye = "COK YUKSEK"
    elif kat is not None and kat >= 2:
        seviye = "YUKSEK"
    else:
        seviye = "NORMAL"

    out = {
        "ticker": ticker, "available": True,
        "bugun_hacim": int(bugun),
        "ort_5g_hacim": int(ort),
        "kat": kat,
        "seviye": seviye,
        "anomali": seviye != "NORMAL",
    }
    _VOL_CACHE[ticker] = (now, out)
    return out


if __name__ == "__main__":
    import json
    import sys
    for tk in (sys.argv[1:] or ["THYAO"]):
        print(json.dumps(get_fundamentals(tk), ensure_ascii=False, indent=2))
        print(json.dumps(get_volume_anomaly(tk), ensure_ascii=False, indent=2))
