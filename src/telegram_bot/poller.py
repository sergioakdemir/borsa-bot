"""Telegram komut yoklayici (cron: her dakika).

Komutlar:
  /portfoy_ekle [kullanici] TICKER ADET FIYAT [not]   -> pozisyon ekle
  /portfoy [kullanici]                                  -> portfoyu listele
  /yardim                                               -> komut listesi

Kullanici belirtilmezse varsayilan: serhat. Offset DB'de (ayar) saklanir.
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")
_DEFAULT_USER = "serhat"


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

_USAGE = ("Kullanim: /portfoy_ekle [kullanici] TICKER ADET FIYAT\n"
          "Ornek   : /portfoy_ekle THYAO 100 285.50")


def _help():
    return ("<b>Komutlar</b>\n"
            "/portfoy_ekle [kullanici] TICKER ADET FIYAT [not]\n"
            "/portfoy [kullanici]\n"
            "/yardim\n\n"
            "Kullanici verilmezse varsayilan: serhat.\n"
            "Ornek: /portfoy_ekle THYAO 100 285.50")


def _cmd_ekle(args):
    users = {u["ad"].lower(): u["id"] for u in db.list_users()}
    a = list(args)
    user = _DEFAULT_USER
    if a and a[0].lower() in users:
        user = a.pop(0).lower()
    if len(a) < 3:
        return "⚠️ Eksik parametre.\n" + _USAGE
    ticker = a[0].upper().replace(".IS", "")
    try:
        adet = float(a[1].replace(",", "."))
        fiyat = float(a[2].replace(",", "."))
    except ValueError:
        return "⚠️ Adet ve fiyat sayi olmali.\n" + _USAGE
    notlar = " ".join(a[3:]) if len(a) > 3 else ""
    uid = db.add_user(user)
    tarih = datetime.now(_TZ).date().isoformat()
    db.add_position(uid, ticker, adet, fiyat, tarih, notlar)
    return (f"✅ <b>{user}</b> portfoyune eklendi: "
            f"{ticker} {adet:g} adet @ {fiyat:g} TL")


def _cmd_list(args):
    users = {u["ad"].lower(): u["id"] for u in db.list_users()}
    user = args[0].lower() if args and args[0].lower() in users else _DEFAULT_USER
    uid = users.get(user) or db.add_user(user)
    pos = db.list_portfolio(uid)
    if not pos:
        return f"{user} portfoyu bos."
    lines = [f"<b>\U0001F4BC {user} portfoyu</b>"]
    for p in pos:
        lines.append(f"• {p['ticker']} {p['adet']:g} ad @ {p['alim_fiyati']:g} TL")
    return "\n".join(lines)


def handle_command(text):
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    if cmd == "/portfoy_ekle":
        return _cmd_ekle(args)
    if cmd == "/portfoy":
        return _cmd_list(args)
    if cmd in ("/yardim", "/help", "/start"):
        return _help()
    return None


def poll_once():
    if not telegram.is_configured():
        return 0
    stored = int(db.get_setting("telegram_offset", "0") or 0)
    updates = telegram.get_updates(offset=(stored + 1) if stored else None, timeout=0)
    if not updates:
        return 0
    offset = stored
    handled = 0
    for u in updates:
        uid = u.get("update_id", 0)
        offset = max(offset, uid)
        msg = u.get("message") or u.get("edited_message") or {}
        chat = (msg.get("chat") or {}).get("id")
        text = msg.get("text", "")
        # Gelen her mesajin chat_id + gondereni loglanir (kesfedilebilirlik icin).
        frm = msg.get("from") or {}
        ad = f"{frm.get('first_name', '')} {frm.get('last_name', '') or ''}".strip()
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] gelen: chat_id={chat} "
              f"ad={ad!r} user=@{frm.get('username')} text={text!r}")
        reply = handle_command(text)
        if reply and chat:
            telegram.send_message(reply, chat_id=chat)
            handled += 1
    db.set_setting("telegram_offset", offset)
    return handled


def main():
    n = poll_once()
    if n:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] {n} komut islendi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
