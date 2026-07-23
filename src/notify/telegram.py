"""Telegram bildirim + komut alma.

TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID ortam degiskenlerini (veya .env) kullanir.
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

_SEND = "https://api.telegram.org/bot{token}/sendMessage"
_GETUPD = "https://api.telegram.org/bot{token}/getUpdates"

_MAX_LEN = 4096  # Telegram tek mesaj karakter siniri

_TZ = ZoneInfo("Europe/Istanbul")

# KRITIK mesaj retry araliklari (saniye): ilk deneme beklemesiz, sonra 30sn/2dk/5dk.
_KRITIK_BEKLEME = (30, 120, 300)

# Iletilemeyen KRITIK mesaj notlari burada birikir; bir sonraki BASARILI gonderimin
# basina eklenip teslim onaylaninca temizlenir -> kayip sessiz kalmasin.
_KRITIK_KAYIP = Path(__file__).resolve().parents[2] / "data" / "kritik_mesaj_kayip.json"


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


# ---------------------------------------------------------------------------
# KRITIK mesaj sinifi: retry + kayip carry-forward
# ---------------------------------------------------------------------------
# Gerekce (23 Tem 2026): PPK gunu manuel-uyari mesaji 14:30'da 0/3 aliciya dustu
# (gecici blip) ve retry olmadigi icin gunun en kritik mesaji SESSIZCE kayboldu.
# Kritik mesajlar (faiz karari, yedek alarmi, sistem uyarisi) artik: (1) 30sn/2dk/5dk
# araliklarla YALNIZ basarisiz alicilara tekrar denenir, (2) tum denemeler tumuyle
# basarisizsa KRITIK_MESAJ_ULASMADI loglanir + not carry-forward'a yazilir,
# (3) not, sonraki basarili gonderimin (or. aksam karnesi) basina eklenir.

def _kayip_oku() -> list:
    try:
        return json.loads(_KRITIK_KAYIP.read_text(encoding="utf-8"))
    except Exception:
        return []


def _kayip_yaz(kayitlar: list) -> None:
    try:
        _KRITIK_KAYIP.parent.mkdir(exist_ok=True)
        _KRITIK_KAYIP.write_text(json.dumps(kayitlar, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    except Exception:
        pass


def _kayip_ekle(tur: str, ozet: str) -> None:
    """Iletilemeyen kritik mesaji carry-forward listesine ekler (son 20 tutulur)."""
    kayitlar = _kayip_oku()
    kayitlar.append({"tarih": datetime.now(_TZ).strftime("%Y-%m-%d %H:%M"),
                     "tur": tur, "ozet": (ozet or "")[:200]})
    _kayip_yaz(kayitlar[-20:])


def kayip_not_metni() -> str:
    """Bekleyen 'iletilemeyen kritik mesaj' notlarini tek satirlik uyari metnine
    cevirir (yoksa ''). Bir sonraki BASARILI gonderimin basina eklenip
    kayip_temizle() ile silinir -> kayip sessiz kalmaz."""
    kayitlar = _kayip_oku()
    if not kayitlar:
        return ""
    satir = "; ".join(f"{k.get('tarih', '')} {k.get('tur', '')}" for k in kayitlar)
    return f"⚠️ <b>Not:</b> Bugün iletilemeyen kritik mesaj(lar): {satir}\n\n"


def kayip_temizle() -> None:
    """Carry-forward notlarini temizler (teslim onaylandiktan SONRA cagrilir)."""
    _kayip_yaz([])


def _gonder_retry(recipients, text, parse_mode, bekleme, prefix, _sleep) -> dict:
    """recipients'e RETRY ile gonderir: basarisiz alicilar `bekleme` (saniye)
    araliklariyla tekrar denenir; teslim edilen alici bir daha denenmez.
    Doner: {recipient: 'ok'|'hata:...'}."""
    kalan = list(dict.fromkeys(str(r) for r in recipients if r))
    sonuc = {}
    for i, bekle in enumerate([0] + list(bekleme)):   # ilk deneme beklemesiz
        if not kalan:
            break
        if bekle:
            _sleep(bekle)
        yeni_kalan = []
        for cid in kalan:
            try:
                send_message((f"{prefix} {text}" if prefix else text),
                             parse_mode=parse_mode, chat_id=cid)
                sonuc[cid] = "ok"
            except Exception as e:
                sonuc[cid] = f"hata:{type(e).__name__}"
                yeni_kalan.append(cid)
        kalan = yeni_kalan
    return sonuc


def broadcast_critical(text: str, tur: str = "kritik", parse_mode: str = "HTML",
                       bekleme=None, _sleep=None) -> dict:
    """KRITIK mesaji (faiz karari vb.) TUM alicilara RETRY ile gonderir.

    Bekleyen carry-forward notu varsa bu mesajin basina eklenir; teslim onaylaninca
    (>=1 alici 'ok') temizlenir. TUM denemeler TUM alicilarda basarisizsa
    KRITIK_MESAJ_ULASMADI loglanir + not carry-forward'a yazilir (sessiz kayip yok).
    `bekleme`/`_sleep` test icin enjekte edilebilir. Doner: {recipient: 'ok'|'hata'}."""
    bekleme = _KRITIK_BEKLEME if bekleme is None else bekleme
    _sleep = time.sleep if _sleep is None else _sleep
    on_not = kayip_not_metni()
    sonuc = _gonder_retry(recipient_ids(), on_not + text, parse_mode, bekleme, "", _sleep)
    if any(s == "ok" for s in sonuc.values()):
        if on_not:
            kayip_temizle()                 # teslim onaylandi -> bekleyen notlar silindi
    else:
        print(f"[KRITIK_MESAJ_ULASMADI] tur={tur} sonuc={sonuc} | mesaj={text[:120]!r}")
        _kayip_ekle(tur, text)
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


def notify_admins_critical(mesaj: str, tur: str = "kritik", prefix: str = "🔴",
                           bekleme=None, _sleep=None) -> dict:
    """KRITIK yonetici uyarisi (or. yedek alarmi): notify_admins ile ayni alicilar
    ama broadcast_critical retry + carry-forward semantigi. Doner {chat_id:'ok'|'hata'}."""
    bekleme = _KRITIK_BEKLEME if bekleme is None else bekleme
    _sleep = time.sleep if _sleep is None else _sleep
    on_not = kayip_not_metni()
    sonuc = _gonder_retry(ADMIN_CHAT_IDS, on_not + mesaj, "HTML", bekleme, prefix, _sleep)
    if any(s == "ok" for s in sonuc.values()):
        if on_not:
            kayip_temizle()
    else:
        print(f"[KRITIK_MESAJ_ULASMADI] tur={tur} (admin) sonuc={sonuc} | mesaj={mesaj[:120]!r}")
        _kayip_ekle(tur, mesaj)
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
