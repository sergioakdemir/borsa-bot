"""On-sinyal motoru: ham OHLCV barlari yerine AI'a gonderilecek KOMPAKT,
onceden hesaplanmis teknik sinyal ozeti uretir. Token tasarrufu saglar.

Tum sayilar gercek veriden deterministik hesaplanir; AI yorumlar, hesaplamaz.
"""
import statistics

from .metrics import compute_metrics


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

    return {
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
