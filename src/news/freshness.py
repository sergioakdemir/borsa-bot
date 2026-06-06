"""Haber tazeligi: bir haberin yeni mi eski mi oldugunu siniflandirir.
Amac: ESKI haber YENI sanilmasin."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Istanbul")


class NewsFreshness(str, Enum):
    YENI = "YENI"      # cok taze (varsayilan: son 24 saat)
    GUNCEL = "GUNCEL"  # yakin (varsayilan: son 3 gun)
    ESKI = "ESKI"      # eski - yeni sanilmamali


@dataclass
class NewsFreshnessReport:
    status: NewsFreshness
    age_hours: float
    published_at: datetime
    now: datetime
    message: str


def check_news_freshness(published_at, now=None, fresh_hours=24, recent_days=3):
    now = now or datetime.now(_TZ)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=_TZ)
    age = (now - published_at).total_seconds() / 3600.0

    if age < 0:
        status, msg = NewsFreshness.YENI, f"Gelecek tarihli haber ({published_at.date()})."
    elif age <= fresh_hours:
        status, msg = NewsFreshness.YENI, f"YENI: {age:.1f} saat once yayinlandi."
    elif age <= recent_days * 24:
        status, msg = NewsFreshness.GUNCEL, f"GUNCEL: {age/24:.1f} gun once."
    else:
        status, msg = NewsFreshness.ESKI, f"ESKI: {age/24:.1f} gun once - yeni sanilmamali."

    return NewsFreshnessReport(status, round(age, 2), published_at, now, msg)
