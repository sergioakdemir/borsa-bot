"""Model portfoy acik pozisyonlarinin guncel fiyat + kar/zararini gunceller (gecelik).

Her acik pozisyon icin guncel fiyati ceker, guncel_fiyat/kz_tl/kz_yuzde gunceller.
Kapatma (SAT) morning.py'de yapilir; bu cron yalniz kagit k/z yeniler.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")


def _son_fiyat(ticker: str, para_birimi: str = "TL"):
    """Guncel kapanis (yerel para). ABD'de '.IS' eklenmez."""
    from src.data.factory import get_data_source
    is_us = (para_birimi or "TL").upper() == "USD"
    t = ticker.upper().replace(".IS", "")
    symbol = t if is_us else f"{t}.IS"
    start = (datetime.now(_TZ).date() - timedelta(days=10)).isoformat()
    try:
        df = get_data_source().get_history(symbol, start=start)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if "Volume" in df.columns:
        f = df[df["Volume"] > 0]
        if not f.empty:
            df = f
    if df.empty:
        return None
    return float(df["Close"].iloc[-1])


def _usdtry():
    try:
        from src.news.macro import get_macro
        v = get_macro().get("usdtry")
        return float(v) if v else None
    except Exception:
        return None


def run(verbose: bool = True) -> int:
    from src.db import database as db
    db.init_db()
    acik = db.list_model_positions(durum="acik")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] model portfoy acik pozisyon: {len(acik)}")
    fx = None
    guncellenen = 0
    for p in acik:
        pb = (p.get("para_birimi") or "TL").upper()
        native = _son_fiyat(p["ticker"], pb)
        if native is None:
            continue
        if pb == "USD":
            if fx is None:
                fx = _usdtry()
            if not fx:
                continue
            fiyat_tl = native * fx
        else:
            fiyat_tl = native
        giris = p.get("alis_fiyati") or 0.0          # TL bazli
        adet = p.get("adet") or 0.0
        kz_tl = round((fiyat_tl - giris) * adet, 2)
        kz_y = round((fiyat_tl - giris) / giris * 100, 2) if giris else None
        db.update_model_running(p["id"], fiyat_tl, kz_tl, kz_y)
        guncellenen += 1
        if verbose:
            print(f"  {p['ticker']:7} {giris} -> {fiyat_tl:.2f} TL : {kz_tl} TL (%{kz_y})")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] {guncellenen} pozisyon guncellendi.")
    return guncellenen


if __name__ == "__main__":
    run()
