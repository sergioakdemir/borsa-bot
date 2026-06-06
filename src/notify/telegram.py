"""Telegram bildirim gondericisi.

TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ortam degiskenlerini (veya .env) kullanir.
"""
import os
import requests

_API = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotConfigured(RuntimeError):
    """Telegram kimlik bilgileri ayarli degil."""


def is_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send_message(text: str, parse_mode: str = "HTML", timeout: int = 20) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise TelegramNotConfigured(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID ayarli degil (.env).")
    r = requests.post(
        _API.format(token=token),
        json={"chat_id": chat, "text": text, "parse_mode": parse_mode,
              "disable_web_page_preview": True},
        timeout=timeout,
    )
    if not r.ok:
        raise RuntimeError(f"Telegram API hata {r.status_code}: {r.text[:200]}")
    return r.json()
