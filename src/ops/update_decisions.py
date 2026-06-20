"""Karar sonuclarini otomatik doldurur (hafiza/ogrenme).

Her gece calisir (cron 23:30). 5 gunden eski ve sonucu HENUZ BOS olan kararlar
icin, karar gunundeki kapanis ile bugunku kapanis arasindaki yuzde degisimi
hesaplar ve karara gore 'DOGRU/YANLIS' verir; decisions.sonuc kolonunu gunceller.

Kazanma kurali (karar yonune gore):
  AL / AL_TEMKINLI : fiyat yukseldiyse DOGRU
  SAT / GUCLU_SAT  : fiyat dustuyse DOGRU
  TUT              : fiyat ~yatay kaldiysa (|degisim| <= %5) DOGRU
  VETO             : islemden kacinildi; fiyat yukselmediyse (<= 0) DOGRU
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")
KAPANIS_GUN = 5          # bu kadar gun gecmis kararlar degerlendirilir
TUT_BANT = 5.0           # TUT icin yatay sayilan +/- yuzde bandi


def _verdict(karar: str, degisim: float) -> bool:
    k = (karar or "").upper()
    if "VETO" in k:
        return degisim <= 0
    if "SAT" in k:          # SAT, GUCLU_SAT
        return degisim < 0
    if "AL" in k:           # AL, AL_TEMKINLI
        return degisim > 0
    return abs(degisim) <= TUT_BANT   # TUT


def _price_change(ticker: str, karar_tarihi: str):
    """Karar gunundeki kapanis -> bugunku kapanis yuzde degisimi (yoksa None)."""
    from src.data.factory import get_data_source
    from src.markets.bist import BIST
    import pandas as pd

    symbol = BIST().to_symbol(ticker)
    start = (datetime.fromisoformat(karar_tarihi).date() - timedelta(days=4)).isoformat()
    try:
        df = get_data_source().get_history(symbol, start=start)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df[df["Volume"] > 0]
    if df.empty:
        return None

    kdate = datetime.fromisoformat(karar_tarihi).date()
    dates = [pd.Timestamp(ix).date() for ix in df.index]
    # karar gunu veya sonraki ilk islem gunu
    idx = next((i for i, d in enumerate(dates) if d >= kdate), None)
    if idx is None:
        return None
    karar_fiyat = float(df["Close"].iloc[idx])
    son_fiyat = float(df["Close"].iloc[-1])
    if not karar_fiyat:
        return None
    return round((son_fiyat - karar_fiyat) / karar_fiyat * 100, 2)


def run(verbose: bool = True) -> int:
    from src.db import database as db
    db.init_db()
    today = datetime.now(_TZ).date()
    cutoff = (today - timedelta(days=KAPANIS_GUN)).isoformat()

    with db.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM decisions WHERE (sonuc IS NULL OR sonuc='') "
            "AND tarih <= ? ORDER BY id", (cutoff,))]

    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] degerlendirilecek karar: {len(rows)}")
    guncellenen = 0
    for r in rows:
        deg = _price_change(r["ticker"], r["tarih"])
        if deg is None:
            if verbose:
                print(f"  {r['ticker']} ({r['tarih']}): fiyat verisi yok, atlandi")
            continue
        dogru = _verdict(r["karar"], deg)
        sonuc = f"{deg:+.1f}% · {'DOGRU' if dogru else 'YANLIS'}"
        db.set_decision_outcome(r["id"], sonuc)
        guncellenen += 1
        if verbose:
            print(f"  {r['ticker']:7} {r['karar']:11} {r['tarih']} -> {sonuc}")

    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] {guncellenen} karar sonucu guncellendi.")
    return guncellenen


if __name__ == "__main__":
    run()
