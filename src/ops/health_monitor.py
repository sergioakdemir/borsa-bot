"""Sistem saglik monitoru (cron: */30 9-19 * * 1-5).

Kontroller:
  1. Fiyat cache tazeligi: data/fiyat_cache.json BORSA ACIKKEN 20 dk'dan eski ise uyar.
  2. ABD hisseleri: NVDA/SPCX/RXT/CNCK cache'te fiyatli mi?
  3. Web servisi: http://127.0.0.1:8080/api/health yanit veriyor mu?
  4. DB: data/borsa.db erisilebilir mi?

Sorun bulunursa Serhat'a (Telegram chat_id=1192292093) "⚠️ SİSTEM UYARISI: ..."
gonderir. SPAM ONLEME: ayni sorun gunde EN FAZLA 1 kez bildirilir
(data/health_state.json'da {sorun_anahtari: 'YYYY-MM-DD'} tutulur).

Calistirma: python -m src.ops.health_monitor
"""
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _load_dotenv():
    """TELEGRAM_BOT_TOKEN gibi degiskenleri .env'den ortama yukler."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

_TZ = ZoneInfo("Europe/Istanbul")
DATA = ROOT / "data"
CACHE_PATH = DATA / "fiyat_cache.json"
DB_PATH = DATA / "borsa.db"
STATE_PATH = DATA / "health_state.json"

HEALTH_URL = "http://127.0.0.1:8080/api/health"
# Uyari alicilari (Telegram chat_id)
BILDIRIM_ALICILARI = [
    1192292093,   # Serhat
    1347729005,   # Yigit
]
ABD_HISSELERI = ["NVDA", "SPCX", "RXT", "CNCK"]
CACHE_BAYAT_DK = 20                   # cache bu kadar dakikadan eskiyse (borsa acikken) uyar


def _bist_acik(now: datetime) -> bool:
    """O an BIST acik mi? (hafta ici 10:00-18:00, Istanbul saati)."""
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return 10 * 60 <= hm <= 18 * 60


# --- kontroller: her biri sorun varsa (anahtar, mesaj) doner, yoksa None ---

def _kontrol_cache_tazelik(now: datetime):
    """Fiyat cache son guncellemesi (dosya mtime) BORSA ACIKKEN 20 dk'dan eski mi?"""
    if not _bist_acik(now):
        return None                   # borsa kapaliyken cache guncellenmez -> kontrol etme
    if not CACHE_PATH.exists():
        return ("cache_yok", "Fiyat cache dosyası (data/fiyat_cache.json) yok.")
    try:
        mtime = datetime.fromtimestamp(CACHE_PATH.stat().st_mtime, _TZ)
    except OSError as e:
        return ("cache_yok", f"Fiyat cache okunamadı: {type(e).__name__}")
    yas_dk = (now - mtime).total_seconds() / 60
    if yas_dk > CACHE_BAYAT_DK:
        return ("cache_bayat",
                f"Fiyat cache {int(yas_dk)} dakikadır güncellenmedi "
                f"(son: {mtime:%H:%M}). update_fiyat_cache cron'u takılmış olabilir.")
    return None


def _kontrol_abd():
    """NVDA/SPCX/RXT/CNCK cache'te fiyatli mi?"""
    try:
        cache = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return ("cache_okunamadi", f"Fiyat cache JSON okunamadı: {type(e).__name__}")
    eksik = [t for t in ABD_HISSELERI
             if not isinstance(cache.get(t), dict) or cache[t].get("fiyat") is None]
    if eksik:
        return ("abd_eksik",
                f"ABD hisseleri cache'te fiyatsız: {', '.join(eksik)}. "
                f"(Sembol yönlendirme / borsa_mcp sorunu olabilir.)")
    return None


def _kontrol_servis():
    """Web servisi /api/health 200 + ok:true donuyor mu?"""
    try:
        import requests
        r = requests.get(HEALTH_URL, timeout=5)
        if r.status_code != 200 or not (r.json() or {}).get("ok"):
            return ("servis_down",
                    f"Web servisi /api/health beklenmedik yanıt: HTTP {r.status_code}.")
    except Exception as e:
        return ("servis_down",
                f"Web servisi yanıt vermiyor ({HEALTH_URL}): {type(e).__name__}.")
    return None


def _kontrol_db():
    """borsa.db acilip basit bir sorgu calisiyor mu?"""
    if not DB_PATH.exists():
        return ("db_yok", "Veritabanı dosyası (data/borsa.db) yok.")
    try:
        with sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5) as c:
            c.execute("SELECT 1 FROM kullanici LIMIT 1").fetchone()
    except sqlite3.Error as e:
        return ("db_erisilemez", f"Veritabanına erişilemiyor: {type(e).__name__}: {str(e)[:80]}")
    return None


# --- spam onleme: gunde 1 kez ---

def _state_yukle() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _state_kaydet(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    except OSError as e:
        print(f"[uyari] health_state.json yazilamadi: {type(e).__name__}")


def _bildir(mesaj: str) -> bool:
    """Tum alicilara (Serhat + Yigit) Telegram uyarisi gonderir.
    En az biri basariliysa True (spam-state yazilsin diye)."""
    try:
        from src.notify import telegram
    except Exception as e:
        print(f"[uyari] telegram modulu yuklenemedi: {type(e).__name__}")
        return False
    metin = f"⚠️ SİSTEM UYARISI: {mesaj}"
    basari = 0
    for cid in BILDIRIM_ALICILARI:
        try:
            telegram.send_message(metin, chat_id=cid)
            basari += 1
        except Exception as e:
            print(f"[uyari] Telegram gonderilemedi (chat={cid}): "
                  f"{type(e).__name__}: {str(e)[:80]}")
    return basari > 0


def run(verbose: bool = True) -> dict:
    now = datetime.now(_TZ)
    bugun = now.date().isoformat()
    sorunlar = []
    for kontrol in (_kontrol_servis, _kontrol_db,
                    lambda: _kontrol_cache_tazelik(now), _kontrol_abd):
        try:
            r = kontrol()
        except Exception as e:                # bir kontrol patlasa digerleri devam etsin
            r = ("monitor_hata", f"Sağlık kontrolü hata verdi: {type(e).__name__}")
        if r:
            sorunlar.append(r)

    state = _state_yukle()
    gonderilen, atlanan = [], []
    for anahtar, mesaj in sorunlar:
        if state.get(anahtar) == bugun:       # bugun zaten bildirildi -> spam onleme
            atlanan.append(anahtar)
            continue
        if _bildir(mesaj):
            state[anahtar] = bugun
            gonderilen.append(anahtar)
        else:
            atlanan.append(anahtar)           # gonderilemedi -> state'e yazma, tekrar dene

    # Bugun cozulen sorunlarin state'ini temizle (yarinki tekrar icin degil; bugun
    # tekrar olusursa yeniden bildirilmesin diye degil -> sadece eski gunleri at).
    state = {k: v for k, v in state.items() if v == bugun}
    _state_kaydet(state)

    if verbose:
        if sorunlar:
            print(f"[{now:%Y-%m-%d %H:%M}] {len(sorunlar)} sorun "
                  f"(bildirilen={gonderilen or '-'}, atlanan/spam={atlanan or '-'})")
            for a, m in sorunlar:
                print(f"  - [{a}] {m}")
        else:
            print(f"[{now:%Y-%m-%d %H:%M}] tüm kontroller OK ✓")
    return {"sorun_sayisi": len(sorunlar), "bildirilen": gonderilen,
            "atlanan": atlanan}


if __name__ == "__main__":
    run()
