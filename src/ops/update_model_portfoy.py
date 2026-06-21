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


def _son_fiyat(ticker: str):
    from src.data.factory import get_data_source
    from src.markets.bist import BIST
    symbol = BIST().to_symbol(ticker)
    start = (datetime.now(_TZ).date() - timedelta(days=10)).isoformat()
    try:
        df = get_data_source().get_history(symbol, start=start)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if "Volume" in df.columns:
        df = df[df["Volume"] > 0]
    if df.empty:
        return None
    return float(df["Close"].iloc[-1])


def run(verbose: bool = True) -> int:
    from src.db import database as db
    db.init_db()
    acik = db.list_model_positions(durum="acik")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] model portfoy acik pozisyon: {len(acik)}")
    guncellenen = 0
    for p in acik:
        fiyat = _son_fiyat(p["ticker"])
        if fiyat is None:
            continue
        giris = p.get("alis_fiyati") or 0.0
        adet = p.get("adet") or 0.0
        kz_tl = round((fiyat - giris) * adet, 2)
        kz_y = round((fiyat - giris) / giris * 100, 2) if giris else None
        db.update_model_running(p["id"], fiyat, kz_tl, kz_y)
        guncellenen += 1
        if verbose:
            print(f"  {p['ticker']:7} {giris} -> {fiyat} : {kz_tl} TL (%{kz_y})")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] {guncellenen} pozisyon guncellendi.")
    return guncellenen


if __name__ == "__main__":
    run()
