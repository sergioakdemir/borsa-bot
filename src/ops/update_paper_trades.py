"""Acik sanal (paper trading) pozisyonlarin guncel kar/zararini gunceller.

Her gece cron (23:30 civari). Acik her pozisyon icin guncel fiyati ceker ve
kagit (henuz kapanmamis) kar/zarar yuzdesini paper_trades.kz_yuzde'ye yazar.
Pozisyonlar SAT karari gelene kadar acik kalir (kapatma morning.py'de yapilir).
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
    acik = db.list_paper_trades(durum="acik")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] acik sanal pozisyon: {len(acik)}")
    guncellenen = 0
    for t in acik:
        fiyat = _son_fiyat(t["ticker"])
        if fiyat is None:
            if verbose:
                print(f"  {t['ticker']}: fiyat yok, atlandi")
            continue
        giris = t.get("fiyat") or 0.0
        kz_yuzde = round((fiyat - giris) / giris * 100, 2) if giris else None
        db.update_paper_running(t["id"], kz_yuzde)
        guncellenen += 1
        if verbose:
            print(f"  {t['ticker']:7} giris {giris} -> {fiyat} : %{kz_yuzde}")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] {guncellenen} pozisyon guncellendi.")
    return guncellenen


if __name__ == "__main__":
    run()
