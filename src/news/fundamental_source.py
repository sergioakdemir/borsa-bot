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


def _sym(ticker: str, market: str = "bist") -> str:
    t = (ticker or "").upper().strip().replace(".IS", "")
    if market in ("us", "abd"):
        return t                      # ABD: yfinance'te son ek yok
    return f"{t}.IS"


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


def get_fundamentals(ticker: str, market: str = "bist") -> dict:
    """Hisse temel oranlarini dondurur (yfinance .info). TTL onbellekli."""
    ticker = (ticker or "").upper().replace(".IS", "")
    now = time.monotonic()
    ck = f"{ticker}:{market}"
    hit = _CACHE.get(ck)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]

    info = {}
    try:
        import yfinance as yf
        info = yf.Ticker(_sym(ticker, market)).get_info() or {}
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
    _CACHE[ck] = (now, out)
    return out


_VOL_CACHE = {}
_VOL_TTL = 600.0   # gun ici hacim degisir; 10 dk onbellek


def get_volume_anomaly(ticker: str, market: str = "bist") -> dict:
    """Bugunku hacmi son 5 gunun ortalamasiyla kiyaslar.

    - ort_5g  : bugun haric onceki 5 islem gununun ortalama hacmi
    - kat     : bugunku hacim / 5g ortalama
    - seviye  : kat>=3 -> 'COK YUKSEK', kat>=2 -> 'YUKSEK', altinda 'NORMAL'
    Veri yetersizse available=False.
    """
    ticker = (ticker or "").upper().replace(".IS", "")
    now = time.monotonic()
    ck = f"{ticker}:{market}"
    hit = _VOL_CACHE.get(ck)
    if hit and (now - hit[0]) < _VOL_TTL:
        return hit[1]

    vols = []
    try:
        import yfinance as yf
        df = yf.Ticker(_sym(ticker, market)).history(period="1mo")
        if df is not None and not df.empty:
            vols = [float(v) for v in df["Volume"].tolist() if v and v > 0]
    except Exception:
        vols = []

    if len(vols) < 6:
        out = {"ticker": ticker, "available": False,
               "neden": "Yeterli hacim verisi yok"}
        _VOL_CACHE[ck] = (now, out)
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
    _VOL_CACHE[ck] = (now, out)
    return out


# ----------------------------------------------------------------------------
# Sektor korelasyonu (statik): hisse -> hangi makro gostergeyle iliskili + yon
# ----------------------------------------------------------------------------
_GOSTERGE_AD = {
    "petrol": "Petrol fiyatı",
    "dolar": "Dolar/TL",
    "faiz": "Faiz",
    "celik_demir": "Çelik/Demir fiyatı",
}

# her hisse: [(gosterge, yon)] - yon: 'pozitif' veya 'ters'
_SECTOR_CORR = {
    # Havayolu: yakit (petrol) maliyeti -> ters; dovizli gelir -> dolar pozitif
    "THYAO": [("petrol", "ters"), ("dolar", "pozitif")],
    "PGSUS": [("petrol", "ters"), ("dolar", "ters")],
    # Bankalar: faiz artisi -> ters
    "GARAN": [("faiz", "ters")],
    "AKBNK": [("faiz", "ters")],
    "ISCTR": [("faiz", "ters")],
    "YKBNK": [("faiz", "ters")],
    "HALKB": [("faiz", "ters")],
    "VAKBN": [("faiz", "ters")],
    # Savunma/teknoloji: dovizli gelir -> dolar pozitif
    "ASELS": [("dolar", "pozitif")],
    # Demir-celik: emtia fiyati -> pozitif
    "EREGL": [("celik_demir", "pozitif")],
    "KRDMD": [("celik_demir", "pozitif")],
    "KORDS": [("celik_demir", "pozitif")],
    # Rafineri/gaz: petrol -> pozitif
    "TUPRS": [("petrol", "pozitif")],
    "AYGAZ": [("petrol", "pozitif")],
}


def get_sector_correlation(ticker: str) -> dict:
    """Hissenin hangi makro gostergeyle (ve hangi yonde) iliskili oldugunu dondurur."""
    ticker = (ticker or "").upper().replace(".IS", "")
    pairs = _SECTOR_CORR.get(ticker)
    if not pairs:
        return {"ticker": ticker, "available": False}
    korelasyonlar = [{"gosterge": _GOSTERGE_AD.get(g, g), "yon": y} for g, y in pairs]
    ozet = ", ".join(
        f"{_GOSTERGE_AD.get(g, g)} ile {'pozitif' if y == 'pozitif' else 'ters'}"
        for g, y in pairs)
    return {"ticker": ticker, "available": True,
            "korelasyonlar": korelasyonlar, "ozet": ozet}


if __name__ == "__main__":
    import json
    import sys
    for tk in (sys.argv[1:] or ["THYAO"]):
        print(json.dumps(get_fundamentals(tk), ensure_ascii=False, indent=2))
        print(json.dumps(get_volume_anomaly(tk), ensure_ascii=False, indent=2))
        print(json.dumps(get_sector_correlation(tk), ensure_ascii=False, indent=2))
