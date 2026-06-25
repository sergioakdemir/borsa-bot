"""Açık trade'lerin (gerçek işlem defteri) günlük güncellemesi.

Her gece cron. Her açık trade için:
  - Güncel fiyattan anlık getiri yüzdesi hesaplanır.
  - max_profit / max_drawdown (en iyi / en kötü görülen yüzde) güncellenir.
  - Fiyat STOP'a değer/altına inerse veya HEDEF'e değer/üstüne çıkarsa pozisyon
    kapatılır (kapanis_sebep='stop' / 'hedef', pnl_yuzde + holding_days ile).

Karar bazlı kapanış (AZALT/UZAK_DUR/SAT) commentary.py'de yapılır; bu cron yalnız
stop/hedef tetiğiyle kapatır ve uç değerleri (max_drawdown/max_profit) günceller.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")


def _son_fiyat(ticker: str, para_birimi: str = "TL"):
    """Güncel kapanış (yerel para). ABD'de '.IS' eklenmez."""
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


def _gun_farki(baslangic, bitis) -> int | None:
    try:
        a = datetime.fromisoformat(str(baslangic)[:10]).date()
        b = datetime.fromisoformat(str(bitis)[:10]).date()
        return (b - a).days
    except Exception:
        return None


def run(verbose: bool = True) -> dict:
    from src.db import database as db
    db.init_db()
    bugun = datetime.now(_TZ).date().isoformat()
    acik = db.list_trades(durum="acik")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] açık trade: {len(acik)}")

    guncellenen = kapanan = 0
    for t in acik:
        native = _son_fiyat(t["ticker"], t.get("para_birimi"))
        if native is None:
            if verbose:
                print(f"  {t['ticker']}: fiyat yok, atlandı")
            continue
        entry = t.get("entry_fiyat") or 0.0
        if not entry:
            continue
        pct = (native - entry) / entry * 100

        # max_profit / max_drawdown (yüzde) güncelle
        eski_mp = t.get("max_profit")
        eski_md = t.get("max_drawdown")
        yeni_mp = round(pct if eski_mp is None else max(eski_mp, pct), 2)
        yeni_md = round(pct if eski_md is None else min(eski_md, pct), 2)
        db.update_trade_extremes(t["id"], yeni_md, yeni_mp)
        guncellenen += 1

        # Stop / hedef tetiği -> kapat
        stop = t.get("stop_fiyat")
        hedef = t.get("hedef_fiyat")
        sebep = None
        if stop is not None and native <= stop:
            sebep = "stop"
        elif hedef is not None and native >= hedef:
            sebep = "hedef"
        if sebep:
            pnl_y = round(pct, 2)
            hold = _gun_farki(t.get("acilis_tarihi"), bugun)
            db.close_trade(t["id"], native, kapanis_sebep=sebep, pnl_yuzde=pnl_y,
                           holding_days=hold, tarih=bugun)
            kapanan += 1
            if verbose:
                print(f"  {t['ticker']:7} {sebep.upper()} @ {native:.2f} -> %{pnl_y} "
                      f"(giriş {entry})")
        elif verbose:
            print(f"  {t['ticker']:7} giriş {entry} -> {native:.2f} : %{pct:.2f} "
                  f"(maxP %{yeni_mp} / maxD %{yeni_md})")

    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] {guncellenen} güncellendi, "
              f"{kapanan} kapandı (stop/hedef).")
    return {"guncellenen": guncellenen, "kapanan": kapanan}


if __name__ == "__main__":
    run()
