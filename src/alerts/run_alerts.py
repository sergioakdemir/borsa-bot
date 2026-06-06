"""Sicak uyari kosusu (cron: hafta ici 10:00-18:00, her 30 dk).

Watchlist'teki her hisseyi kontrol eder; ACIL/IZLE seviyesindeki YENI uyarilari
Telegram'a gonderir. Spam onleme: ayni hisseye gun icinde ayni/daha dusuk
seviyede tekrar gondermez. Bayat/tatil verisinde (bugun bari yoksa) uyarmaz.
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")


def _load_dotenv():
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

from src.watchlist import load_watchlist
from src.alerts.engine import intraday_change, classify, level_rank
from src.notify import telegram
from src.db import database as db

_EMOJI = {"ACIL": "\U0001F6A8", "IZLE": "\U0001F440"}


def build_message(new_alerts, now):
    acil = [a for a in new_alerts if a["seviye"] == "ACIL"]
    izle = [a for a in new_alerts if a["seviye"] == "IZLE"]
    lines = [f"<b>\U0001F525 Sicak Uyari</b> — {now:%Y-%m-%d %H:%M}", ""]

    def fmt(a):
        arrow = "\U0001F4C8" if a["change"] > 0 else "\U0001F4C9"
        sign = "+" if a["change"] > 0 else ""
        return (f"{arrow} <b>{a['ticker']}</b>  {sign}{a['change']}%  "
                f"({a['prev_close']} → {a['last_close']} TL)")

    if acil:
        lines.append(f"{_EMOJI['ACIL']} <b>ACIL</b>")
        lines += ["  " + fmt(a) for a in acil]
        lines.append("")
    if izle:
        lines.append(f"{_EMOJI['IZLE']} <b>IZLE</b>")
        lines += ["  " + fmt(a) for a in izle]
    return "\n".join(lines)


def main():
    now = datetime.now(_TZ)
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yapilandirilmamis - uyari atlandi.")
        return 0

    today = now.date().isoformat()
    new_alerts = []
    checked = 0
    for ticker in load_watchlist():
        info = intraday_change(ticker, today=now.date())
        if not info or not info["is_today"]:
            continue
        checked += 1
        level = classify(info["change"])
        if not level:
            continue
        # spam onleme: bugun gonderilmis en yuksek seviye
        sent = max((level_rank(l) for l in db.alert_levels_today(ticker, today)), default=0)
        if level_rank(level) <= sent:
            continue
        db.record_alert(ticker, today, level, info["change"])
        new_alerts.append({"ticker": ticker, "seviye": level, **info})

    if not new_alerts:
        print(f"[{now:%Y-%m-%d %H:%M}] Yeni uyari yok ({checked} hisse bugun islemde).")
        return 0

    telegram.send_message(build_message(new_alerts, now))
    print(f"[{now:%Y-%m-%d %H:%M}] {len(new_alerts)} yeni uyari gonderildi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
