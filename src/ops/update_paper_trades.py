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


def _son_fiyat(ticker: str, para_birimi: str = "TL"):
    """Guncel kapanis (yerel para). ABD'de '.IS' eklenmez."""
    from src.data.factory import get_data_source
    is_us = (para_birimi or "TL").upper() == "USD"
    symbol = ticker.upper().replace(".IS", "") if is_us else f"{ticker.upper().replace('.IS','')}.IS"
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
    acik = db.list_paper_trades(durum="acik")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] acik sanal pozisyon: {len(acik)}")
    fx = None
    guncellenen = 0
    for t in acik:
        pb = (t.get("para_birimi") or "TL").upper()
        native = _son_fiyat(t["ticker"], pb)
        if native is None:
            if verbose:
                print(f"  {t['ticker']}: fiyat yok, atlandi")
            continue
        if pb == "USD":
            if fx is None:
                fx = _usdtry()
            if not fx:
                if verbose:
                    print(f"  {t['ticker']}: USD/TRY kuru yok, atlandi")
                continue
            fiyat_tl = native * fx
        else:
            fiyat_tl = native
        giris = t.get("fiyat") or 0.0              # TL bazli
        kz_yuzde = round((fiyat_tl - giris) / giris * 100, 2) if giris else None
        db.update_paper_running(t["id"], kz_yuzde)
        guncellenen += 1
        if verbose:
            print(f"  {t['ticker']:7} giris {giris} -> {fiyat_tl:.2f} TL : %{kz_yuzde}")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] {guncellenen} pozisyon guncellendi.")
    return guncellenen


if __name__ == "__main__":
    run()
