"""Telegram bildirim + komut alma.

TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID ortam degiskenlerini (veya .env) kullanir.
"""
import os
import requests

_SEND = "https://api.telegram.org/bot{token}/sendMessage"
_GETUPD = "https://api.telegram.org/bot{token}/getUpdates"

_MAX_LEN = 4096  # Telegram tek mesaj karakter siniri


def _split_message(text: str, limit: int = _MAX_LEN) -> list:
    """Metni Telegram limitine gore parcalara boler. Once satir sonunda,
    olmazsa kelime arasinda, o da olmazsa sert keser. Kelime ortasinda
    bolme yapmamaya calisir."""
    if len(text) <= limit:
        return [text]
    parcalar = []
    kalan = text
    while len(kalan) > limit:
        dilim = kalan[:limit]
        kes = dilim.rfind("\n")          # tercih: satir sonu
        if kes <= 0:
            kes = dilim.rfind(" ")       # alternatif: kelime arasi
        if kes <= 0:
            kes = limit                  # son care: sert kesim
        parcalar.append(kalan[:kes])
        kalan = kalan[kes:].lstrip("\n") if kes < limit else kalan[kes:]
    if kalan:
        parcalar.append(kalan)
    return parcalar


class TelegramNotConfigured(RuntimeError):
    """Telegram kimlik bilgileri ayarli degil."""


def is_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def send_message(text: str, parse_mode: str = "HTML", chat_id=None, timeout: int = 20) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise TelegramNotConfigured("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID ayarli degil.")
    son = None
    for parca in _split_message(text or ""):
        r = requests.post(_SEND.format(token=token),
                          json={"chat_id": chat, "text": parca, "parse_mode": parse_mode,
                                "disable_web_page_preview": True}, timeout=timeout)
        if not r.ok:
            raise RuntimeError(f"Telegram API hata {r.status_code}: {r.text[:200]}")
        son = r.json()
    return son or {}


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


# Sistem/operasyon uyarilari icin sabit yonetici alicilari (Serhat + Yigit).
ADMIN_CHAT_IDS = [1192292093, 1347729005]


def notify_admins(mesaj: str, prefix: str = "⚠️") -> dict:
    """Operasyonel uyariyi yoneticilere (Serhat + Yigit) gonderir. Telegram ayarli
    degilse/erisilmezse sessizce atlar. {chat_id: 'ok'|'hata:...'} dondurur."""
    sonuc = {}
    for cid in ADMIN_CHAT_IDS:
        try:
            send_message(f"{prefix} {mesaj}" if prefix else mesaj, chat_id=cid)
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
