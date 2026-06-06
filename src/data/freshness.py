"""Cekilen verinin guncelligini (freshness) degerlendirir.

Borsa verisi her gun uretilmez: hafta sonu ve resmi tatillerde islem olmaz.
Bu yuzden 'son bar bugune ait degil' demek her zaman 'veri eski' demek degildir.
Uc durum ayirt edilir:
  FRESH  -> son bar bugune ait
  RECENT -> son bar, beklenen son islem gunune ait (hafta sonu sonrasi normal)
  STALE  -> beklenenden eski (gercekten guncel degil)
"""
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from enum import Enum
from zoneinfo import ZoneInfo
import pandas as pd


class Freshness(str, Enum):
    FRESH = "FRESH"
    RECENT = "RECENT"
    STALE = "STALE"


@dataclass
class FreshnessReport:
    status: Freshness
    last_bar: date | None      # verideki son bar tarihi
    today: date                # piyasa saat dilimine gore bugun
    expected: date | None      # beklenen son islem gunu
    calendar_age: int | None   # takvim gunu farki (son bar -> bugun)
    trading_age: int | None    # kacirilan is gunu sayisi (yaklasik, tatil haric)
    message: str

    @property
    def is_ok(self) -> bool:
        """STALE degilse veri kullanilabilir kabul edilir."""
        return self.status in (Freshness.FRESH, Freshness.RECENT)


def _last_expected_trading_day(today: date) -> date:
    """Bugun hafta ici ise bugun; degilse en yakin onceki hafta ici gun.
    NOT: resmi tatiller hesaba katilmaz; bu yaklasik bir kontroldur."""
    d = today
    while d.weekday() >= 5:  # 5=Cumartesi, 6=Pazar
        d -= timedelta(days=1)
    return d


def check_freshness(df: pd.DataFrame, tz: str = "Europe/Istanbul") -> FreshnessReport:
    """Bir OHLCV DataFrame'inin guncelligini degerlendirir.

    tz: piyasanin saat dilimi (BIST -> Europe/Istanbul, US -> America/New_York).
    """
    today = datetime.now(ZoneInfo(tz)).date()
    expected = _last_expected_trading_day(today)

    if df is None or len(df) == 0:
        return FreshnessReport(
            Freshness.STALE, None, today, expected, None, None,
            "Veri bos — guncellik degerlendirilemez.",
        )

    last_bar = pd.Timestamp(df.index[-1]).date()
    calendar_age = (today - last_bar).days
    # son bar ile bugun arasindaki is gunu sayisi (son bar haric)
    trading_age = max(len(pd.bdate_range(last_bar, today)) - 1, 0)

    if last_bar >= today:
        status = Freshness.FRESH
        msg = f"GUNCEL: son bar bugune ait ({last_bar})."
    elif last_bar >= expected:
        status = Freshness.RECENT
        msg = (f"TAZE: son bar son islem gunune ait ({last_bar}); "
               f"bugun ({today}) icin kapanis verisi henuz olusmamis olabilir.")
    else:
        missed = max(len(pd.bdate_range(last_bar, expected)) - 1, 0)
        status = Freshness.STALE
        msg = (f"ESKI: son bar {last_bar}, beklenen >= {expected} "
               f"(~{missed} islem gunu geride).")

    return FreshnessReport(status, last_bar, today, expected,
                           calendar_age, trading_age, msg)
