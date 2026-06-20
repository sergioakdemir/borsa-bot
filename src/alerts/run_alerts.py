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

_EMOJI = {"ACIL": "\U0001F6A8", "IZLE": "\U0001F440", "HABER": "\U0001F4F0"}


def unpriced_fresh_news(ticker, news_src=None):
    """Hisseye ait TAZE ve henuz FIYATLANMAMIS bir KAP haberi varsa dondurur."""
    from src.news.service import filtered_news
    try:
        for h in filtered_news(ticker, source=news_src):   # eski olmayanlar
            if h.get("fiyatlanma") == "FIYATLANMADI":
                return h
    except Exception:
        return None
    return None


def build_message(price_alerts, news_alerts, now):
    acil = [a for a in price_alerts if a["seviye"] == "ACIL"]
    izle = [a for a in price_alerts if a["seviye"] == "IZLE"]
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
        lines.append("")
    if news_alerts:
        # KAP haberi var ama fiyat henuz oynamamis -> firsat penceresi
        lines.append(f"{_EMOJI['HABER']} <b>ACIL · FIYATLANMAMIS HABER</b> "
                     "<i>(fiyat henuz oynamadi)</i>")
        for a in news_alerts:
            h = a["haber"]
            lines.append(f"  ⚡ <b>{a['ticker']}</b> ({a['change']:+}%): "
                         f"{h.get('baslik')} <i>[{h.get('tarih')}]</i>")
    return "\n".join(lines).rstrip()


def main():
    now = datetime.now(_TZ)
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yapilandirilmamis - uyari atlandi.")
        return 0

    from src.news.service import get_news_source
    news_src, _ = get_news_source(verbose=False)

    today = now.date().isoformat()
    price_alerts, news_alerts = [], []
    checked = 0
    for ticker in load_watchlist():
        info = intraday_change(ticker, today=now.date())
        if not info or not info["is_today"]:
            continue
        checked += 1
        level = classify(info["change"])
        if level:
            # FIYAT uyarisi (%2+ IZLE / %5+ ACIL) — spam onleme
            sent = max((level_rank(l) for l in db.alert_levels_today(ticker, today)),
                       default=0)
            if level_rank(level) > sent:
                db.record_alert(ticker, today, level, info["change"])
                price_alerts.append({"ticker": ticker, "seviye": level, **info})
        else:
            # Fiyat oynamamis -> KAP'ta taze fiyatlanmamis haber var mi? -> ACIL
            haber = unpriced_fresh_news(ticker, news_src)
            if haber and "HABER" not in db.alert_levels_today(ticker, today):
                db.record_alert(ticker, today, "HABER", info["change"])
                news_alerts.append({"ticker": ticker, "change": info["change"],
                                    "haber": haber})

    if not price_alerts and not news_alerts:
        print(f"[{now:%Y-%m-%d %H:%M}] Yeni uyari yok ({checked} hisse bugun islemde).")
        return 0

    sonuc = telegram.broadcast(build_message(price_alerts, news_alerts, now))
    ok = [c for c, s in sonuc.items() if s == "ok"]
    print(f"[{now:%Y-%m-%d %H:%M}] {len(price_alerts)} fiyat + "
          f"{len(news_alerts)} haber uyarisi -> {len(ok)}/{len(sonuc)} aliciya gonderildi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
