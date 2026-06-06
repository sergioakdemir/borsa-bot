"""Gunluk portfoy takibi: her kullanicinin pozisyonlari icin TUT/SAT sinyali +
kar/zarar -> Telegram. (cron: hafta ici kapanis sonrasi)
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

from src.notify import telegram
from src.db import database as db
from src.portfolio.engine import portfolio_report

_EMOJI = {"TUT": "⚪", "SAT": "\U0001F534", "VERI_YOK": "❔"}


def _section(ad, rep):
    lines = [f"<b>{ad}</b>"]
    for r in rep["rows"]:
        em = _EMOJI.get(r["signal"], "⚪")
        if r["last_close"] is None:
            lines.append(f"{em} {r['ticker']} {r['adet']:g} ad — veri yok")
            continue
        pnl = r["kar_zarar"]; pct = r["kar_zarar_yuzde"]
        sign = "+" if (pnl or 0) >= 0 else ""
        lines.append(
            f"{em} <b>{r['ticker']}</b> {r['adet']:g} ad · "
            f"{r['alim_fiyati']:g}→{r['last_close']:g} TL · "
            f"{sign}{pct}% ({sign}{pnl:g} TL) · {r['signal']} ({r['score']}/10, risk {r['risk']})")
    t_sign = "+" if rep["toplam_kar_zarar"] >= 0 else ""
    lines.append(f"<i>Toplam: {rep['toplam_maliyet']:g} → {rep['toplam_deger']:g} TL "
                 f"({t_sign}{rep['toplam_kar_zarar_yuzde']}% / {t_sign}{rep['toplam_kar_zarar']:g} TL)</i>")
    return "\n".join(lines)


def main():
    now = datetime.now(_TZ)
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yok - portfoy takibi atlandi.")
        return 0

    sections = []
    for u in db.list_users():
        rep = portfolio_report(u["id"])
        if rep["rows"]:
            sections.append(_section(u["ad"], rep))

    if not sections:
        print(f"[{now:%Y-%m-%d %H:%M}] Hicbir portfoyde pozisyon yok - gonderim yok.")
        return 0

    msg = f"<b>\U0001F4BC Portfoy Takibi</b> — {now:%Y-%m-%d}\n\n" + "\n\n".join(sections)
    telegram.send_message(msg)
    print(f"[{now:%Y-%m-%d %H:%M}] Portfoy takibi gonderildi ({len(sections)} kullanici).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
