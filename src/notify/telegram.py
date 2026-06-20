"""Telegram bildirim + komut alma.

TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID ortam degiskenlerini (veya .env) kullanir.
"""
import os
import requests

_SEND = "https://api.telegram.org/bot{token}/sendMessage"
_GETUPD = "https://api.telegram.org/bot{token}/getUpdates"


class TelegramNotConfigured(RuntimeError):
    """Telegram kimlik bilgileri ayarli degil."""


def is_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send_message(text: str, parse_mode: str = "HTML", chat_id=None, timeout: int = 20) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise TelegramNotConfigured("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID ayarli degil.")
    r = requests.post(_SEND.format(token=token),
                      json={"chat_id": chat, "text": text, "parse_mode": parse_mode,
                            "disable_web_page_preview": True}, timeout=timeout)
    if not r.ok:
        raise RuntimeError(f"Telegram API hata {r.status_code}: {r.text[:200]}")
    return r.json()


def recipient_ids() -> list:
    """Tum bildirim alicilari (tekrarsiz): env TELEGRAM_CHAT_ID + TELEGRAM_CHAT_IDS
    (virgullu) + kullanici tablosundaki telegram_id'ler."""
    ids = []
    main = os.environ.get("TELEGRAM_CHAT_ID")
    if main:
        ids.append(str(main).strip())
    for x in (os.environ.get("TELEGRAM_CHAT_IDS", "") or "").split(","):
        if x.strip():
            ids.append(x.strip())
    try:                                   # DB kullanici telegram_id'leri
        from src.db import database as db
        for u in db.list_users():
            t = u.get("telegram_id")
            if t:
                ids.append(str(t))
    except Exception:
        pass
    seen, out = set(), []
    for i in ids:
        if i and i not in seen:
            seen.add(i)
            out.append(i)
    return out


def broadcast(text: str, parse_mode: str = "HTML") -> dict:
    """Mesaji tum alicilara gonderir. {chat_id: 'ok'|'hata:...'} dondurur."""
    sonuc = {}
    for cid in recipient_ids():
        try:
            send_message(text, parse_mode=parse_mode, chat_id=cid)
            sonuc[cid] = "ok"
        except Exception as e:
            sonuc[cid] = f"hata:{type(e).__name__}"
    return sonuc


def get_updates(offset=None, timeout: int = 0) -> list:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise TelegramNotConfigured("TELEGRAM_BOT_TOKEN ayarli degil.")
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    r = requests.get(_GETUPD.format(token=token), params=params, timeout=timeout + 20)
    if not r.ok:
        raise RuntimeError(f"Telegram getUpdates hata {r.status_code}: {r.text[:200]}")
    return r.json().get("result", [])
