"""Sicak uyari motoru: hacimsiz CANLI bar dogru isleniyor mu? (sentetik, ag yok)

31 Tem 2026 taramasinda bulundu. alerts/engine.py'de `df[df["Volume"] > 0]`
vardi; Yahoo seans ici bara hacmi hemen yazmayabiliyor (endekslerde ertesi gune
kadar hep 0). Filtre o bari elediginde:
  - intraday_change 'is_today' False donuyor (bugun veri yokmus gibi),
  - 'gunluk degisim' aslinda DUN ile ONCEKI GUNU karsilastiriyor,
  - weekly_change'in 5 gunluk penceresi bir gun geriden olculuyor.
Yani sicak uyari, uyarmasi gereken gunde YANLIS yuzdeyle konusuyor.

Calistir:  ./venv/bin/python tests/test_alerts_hacimsiz_bar.py
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.alerts.engine import intraday_change, weekly_change

SONUC = []


def kontrol(ad, kosul, detay=""):
    SONUC.append(kosul)
    print(f"[{'GECTI' if kosul else 'KALDI'}] {ad} {detay}")


class SahteKaynak:
    """Son barin hacmi 0 (canli/konsolide olmamis), fiyati dolu."""

    def __init__(self, son_gun, kapanislar, son_hacim=0.0):
        n = len(kapanislar)
        idx = pd.bdate_range(end=pd.Timestamp(son_gun), periods=n)
        self.df = pd.DataFrame({
            "Open": kapanislar, "High": kapanislar, "Low": kapanislar,
            "Close": kapanislar,
            "Volume": [1000.0] * (n - 1) + [son_hacim],
        }, index=idx)

    def get_history(self, symbol, start, end=None, interval="1d"):
        return self.df


def isgunu(g: date, k=1) -> date:
    while k:
        g -= timedelta(days=1)
        if g.weekday() < 5:
            k -= 1
    return g


bugun = datetime.now().date()
if bugun.weekday() >= 5:                 # hafta sonu kosulursa son is gunune cek
    bugun = isgunu(bugun)

# 6 bar: ... 100, 110 -> son gun +%10. Son barin HACMI 0 (canli bar).
kapanislar = [90.0, 95.0, 98.0, 99.0, 100.0, 110.0]
src = SahteKaynak(bugun, kapanislar, son_hacim=0.0)

r = intraday_change("TEST", source=src)
kontrol("intraday: hacimsiz canli bar SAYILIYOR (is_today)",
        r is not None and r["is_today"] is True,
        f"-> is_today={r and r['is_today']} last_date={r and r['last_date']}")
kontrol("intraday: gunluk degisim DOGRU (%10, dun/onceki gun degil)",
        r is not None and r["change"] == 10.0,
        f"-> change=%{r and r['change']} (yanlis olsaydi %1.02 cikardi)")
kontrol("intraday: son kapanis canli barin fiyati",
        r is not None and r["last_close"] == 110.0,
        f"-> last_close={r and r['last_close']}")

w = weekly_change("TEST", source=src)
kontrol("weekly: 5 gunluk pencere canli bari iceriyor",
        w is not None and w["change"] == round((110.0 - 90.0) / 90.0 * 100, 2),
        f"-> change=%{w and w['change']} (beklenen %22.22)")

# Hacimli barla sonuc DEGISMEMELI (regresyon: eski davranis korunuyor)
src2 = SahteKaynak(bugun, kapanislar, son_hacim=5000.0)
r2 = intraday_change("TEST", source=src2)
kontrol("hacim varken sonuc ayni (davranis degismedi)",
        r2 is not None and r2["change"] == r["change"] and r2["is_today"] is True,
        f"-> change=%{r2 and r2['change']}")

# Kapanisi BOS olan bar hala elenmeli (filtre tamamen kalkmadi)
src3 = SahteKaynak(bugun, kapanislar, son_hacim=0.0)
src3.df.loc[src3.df.index[-1], "Close"] = float("nan")
r3 = intraday_change("TEST", source=src3)
kontrol("kapanisi NaN olan bar hala eleniyor",
        r3 is not None and r3["last_close"] == 100.0 and r3["is_today"] is False,
        f"-> last_close={r3 and r3['last_close']} is_today={r3 and r3['is_today']}")

print(f"\n{sum(SONUC)}/{len(SONUC)} test gecti")
sys.exit(0 if all(SONUC) else 1)
