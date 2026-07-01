"""On-sinyal motoru: ham OHLCV barlari yerine AI'a gonderilecek KOMPAKT,
onceden hesaplanmis teknik sinyal ozeti uretir. Token tasarrufu saglar.

Tum sayilar gercek veriden deterministik hesaplanir; AI yorumlar, hesaplamaz.
"""
import statistics
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .metrics import compute_metrics

_TZ = ZoneInfo("Europe/Istanbul")

# Trend etiketi -> AI baglamina girecek sade aciklama. SMA sayilari kullaniciya
# ASLA gosterilmez; sadece "yukari/asagi/yatay" trend olarak aktarilir.
_TREND_CONTEXT = {
    "güçlü yükseliş": "güçlü yükseliş (fiyat tüm ortalamaların üzerinde)",
    "güçlü düşüş": "güçlü düşüş (fiyat tüm ortalamaların altında)",
    "yatay/belirsiz": "yatay/belirsiz trend",
}


def _sma(closes, n):
    """Son n kapanisin basit hareketli ortalamasi. Yeterli veri yoksa None."""
    if len(closes) < n:
        return None
    return round(sum(closes[-n:]) / n, 4)


def _sma_trend_label(last, s20, s50, s200):
    """SMA dizilimine gore trend. Fiyat > SMA20 > SMA50 > SMA200 -> guclu yukselis,
    tersi -> guclu dusus, karisik -> yatay/belirsiz. Eksik SMA'da None."""
    if None in (last, s20, s50, s200):
        return None
    if last > s20 > s50 > s200:
        return "güçlü yükseliş"
    if last < s20 < s50 < s200:
        return "güçlü düşüş"
    return "yatay/belirsiz"


def compute_sma_trend(symbol, src=None) -> str | None:
    """yfinance'den ~200 islem gunluk kapanisla SMA20/50/200 hesaplayip trend
    etiketi dondurur (guclu yukselis / guclu dusus / yatay). Yalnizca AI baglamina
    girer; SMA rakamlari kullaniciya ASLA gosterilmez. Veri yoksa None."""
    if not symbol:
        return None
    try:
        from ..data.factory import get_data_source
        src = src or get_data_source()
        start = (datetime.now(_TZ).date() - timedelta(days=400)).isoformat()
        df = src.get_history(symbol, start=start)
    except Exception:
        return None
    if df is None or getattr(df, "empty", True):
        return None
    try:
        closes = [float(x) for x in df["Close"].tolist() if x == x and x]  # NaN/0 ele
    except Exception:
        return None
    if len(closes) < 20:
        return None
    last = closes[-1]
    label = _sma_trend_label(last, _sma(closes, 20), _sma(closes, 50), _sma(closes, 200))
    return _TREND_CONTEXT.get(label) if label else None


def _trend(pct):
    if pct is None:
        return "belirsiz"
    if pct > 1:
        return "yukselen"
    if pct < -1:
        return "dusen"
    return "yatay"


def _volume_signal(pct):
    if pct is None:
        return "belirsiz"
    if pct > 25:
        return "yuksek"
    if pct < -25:
        return "dusuk"
    return "normal"


def build_presignal(stock: dict) -> dict:
    m = compute_metrics(stock)
    status = stock.get("freshness", {}).get("status")
    if "error" in m:
        return {"sembol": stock.get("symbol"), "tazelik": status, "hata": m["error"]}

    bars = [b for b in stock.get("bars", []) if b.get("volume")]
    closes = [b["close"] for b in bars]
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] * 100
            for i in range(1, len(closes)) if closes[i - 1]]
    vol = round(statistics.pstdev(rets), 2) if len(rets) >= 2 else 0.0

    lo, hi, last = m["donem_en_dusuk"], m["donem_en_yuksek"], m["son_kapanis"]
    pos = round((last - lo) / (hi - lo) * 100, 1) if hi > lo else None

    teknik_trend = compute_sma_trend(stock.get("symbol"))

    out = {
        "sembol": stock.get("symbol"),
        "tazelik": status,
        "trend": _trend(m.get("donem_degisim_yuzde")),
        "donem_degisim_%": m.get("donem_degisim_yuzde"),
        "gunluk_degisim_%": m.get("gunluk_degisim_yuzde"),
        "son_kapanis": last,
        "donem_yuksek": hi,
        "donem_dusuk": lo,
        "fiyat_konumu_%": pos,          # [dusuk,yuksek] araliginda konum
        "hacim_sinyali": _volume_signal(m.get("son_hacim_vs_ortalama_yuzde")),
        "hacim_vs_ort_%": m.get("son_hacim_vs_ortalama_yuzde"),
        "volatilite_%": vol,
        "bar_sayisi": m.get("bar_sayisi"),
    }
    if teknik_trend:
        out["teknik_trend"] = teknik_trend
    return out
