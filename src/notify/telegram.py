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
