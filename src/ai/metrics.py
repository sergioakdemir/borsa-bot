"""Hisse barlarindan deterministik teknik metrikler hesaplar.

Tum sayisal hesap BURADA (Python'da) yapilir; AI modeli yalnizca bu
hazir sayilari YORUMLAR. Boylece model rakam uyduramaz.
"""


def compute_metrics(stock: dict) -> dict:
    # volume==0 olan placeholder/tatil barlarini ele
    bars = [b for b in stock.get("bars", []) if b.get("volume")]
    if len(bars) < 2:
        return {"error": "Yeterli islem bari yok (en az 2 gerekli)."}

    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    first_close, last_close = closes[0], closes[-1]
    avg_vol = sum(vols) / len(vols)

    pct = round((last_close - first_close) / first_close * 100, 2) if first_close else None

    # son bara gore gunluk degisim
    prev_close = closes[-2]
    daily_pct = round((last_close - prev_close) / prev_close * 100, 2) if prev_close else None

    return {
        "bar_sayisi": len(bars),
        "ilk_tarih": bars[0]["date"],
        "son_tarih": bars[-1]["date"],
        "ilk_kapanis": round(first_close, 4),
        "son_kapanis": round(last_close, 4),
        "onceki_kapanis": round(prev_close, 4),
        "gunluk_degisim_yuzde": daily_pct,
        "donem_degisim_yuzde": pct,
        "donem_en_yuksek": round(max(b["high"] for b in bars), 4),
        "donem_en_dusuk": round(min(b["low"] for b in bars), 4),
        "ortalama_hacim": round(avg_vol),
        "son_hacim": vols[-1],
        "son_hacim_vs_ortalama_yuzde": round((vols[-1] / avg_vol - 1) * 100, 2) if avg_vol else None,
    }
