"""Haftalik PIYASA ozeti (cron: Pazar 20:00). Watchlist haftalik fiyat degisimi
+ bu haftaki uyari sayilari -> tek Telegram mesaji.

NOT: src/ops/weekly_report.py'den FARKLI gorevdir. Bu dosya PIYASA odaklidir
(fiyat hareketleri + uyari sayisi). weekly_report ise PERFORMANS odaklidir
(karar isabeti, portfoy K/Z, ogrenme). Ikisi farkli baslik kullanir.
"""
import os
import sys
from collections import Counter
from datetime import datetime, timedelta
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
from src.alerts.engine import weekly_change
from src.notify import telegram
from src.db import database as db


def build_message(rows, alerts, now):
    lines = [f"<b>\U0001F4C5 Haftalik Piyasa Ozeti</b> — {now:%Y-%m-%d}",
             "<i>Watchlist haftalik fiyat hareketleri + uyari sayilari</i>", ""]
    for r in sorted(rows, key=lambda x: x["change"], reverse=True):
        sign = "+" if r["change"] > 0 else ""
        arrow = "\U0001F4C8" if r["change"] > 0 else ("\U0001F4C9" if r["change"] < 0 else "▪")
        lines.append(f"{arrow} <b>{r['ticker']}</b>  {sign}{r['change']}%  ({r['last_close']} TL)")
    c = Counter(a["seviye"] for a in alerts)
    lines.append("")
    lines.append(f"<i>Bu hafta: {c.get('ACIL',0)} acil, {c.get('IZLE',0)} izle uyarisi</i>")
    return "\n".join(lines)


def main():
    now = datetime.now(_TZ)
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yapilandirilmamis - ozet atlandi.")
        return 0

    rows = [r for r in (weekly_change(t) for t in load_watchlist()) if r]
    week_ago = (now.date() - timedelta(days=7)).isoformat()
    alerts = db.alerts_between(week_ago, now.date().isoformat())

    telegram.send_message(build_message(rows, alerts, now))
    print(f"[{now:%Y-%m-%d %H:%M}] Haftalik ozet gonderildi ({len(rows)} hisse).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
