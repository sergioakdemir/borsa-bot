"""Sistem saglik monitoru.

Kontroller iki gruba ayrilir:
  CORE (7/24, her zaman) -- altyapi her an ayakta olmali:
    3. Web servisi: http://127.0.0.1:8080/api/health yanit veriyor mu?
    4. DB: data/borsa.db erisilebilir mi?
  MARKET (yalniz borsa saatleri, hafta ici 9-19) -- fiyat akisi:
    1. Fiyat cache tazeligi: data/fiyat_cache.json BORSA ACIKKEN 20 dk'dan eski ise uyar.

Cron iki ayri satirla calisir:
  */30 * * * *           -> mod 'core'   (servis + DB; 7/24)
  */30 9-19 * * 1-5      -> mod 'market' (fiyat cache; borsa saatleri)

Sorun bulunursa Serhat + Yigit'e "⚠️ SİSTEM UYARISI: ..." gonderir.
SPAM ONLEME: ayni sorun gunde EN FAZLA 1 kez bildirilir
(data/health_state.json'da {sorun_anahtari: 'YYYY-MM-DD'} tutulur).
ISTISNA — KRITIK_ANAHTARLAR (kredi bitti, AI hata patlamasi): sistem karar
uretemiyor demektir; gunluk filtreye TAKILMAZ, sorun surdukce
KRITIK_TEKRAR_SAAT'te bir tekrar bildirilir (state'te ISO zaman damgasi).

Calistirma: python -m src.ops.health_monitor [core|market|all]  (varsayilan: all)
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
BILDIRIM_LISTESI = [
    1192292093,   # Serhat
    1347729005,   # Yigit
]
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


AI_HATA_ESIGI = 5          # gun icinde bu kadardan FAZLA AI hatasi -> admin uyarisi


def _kontrol_kredi():
    """Anthropic kredisi bitti mi? commentary.kredi_freni_koy 'ai_kredi_bitti:<gun>'
    bayragini koyar; bu KRITIK bir durumdur (karar uretimi tamamen durur) ->
    KRITIK_ANAHTARLAR sayesinde gunde 1 kez degil, cozulene kadar periyodik uyarir."""
    try:
        from src.db import database as db
        bugun = datetime.now(_TZ).date().isoformat()
        if db.get_setting(f"ai_kredi_bitti:{bugun}"):
            return ("ai_kredi_bitti",
                    "🔴 KREDİ BİTTİ: Anthropic API bakiyesi tükendi. AI çağrıları "
                    "durduruldu, karar üretilmiyor. Bakiye yükleyin.")
    except Exception:
        return None
    return None


def _kontrol_ai_hata():
    """Bugunku AI cagri hata sayaci (db.ai_hata_sayisi) esigi asti mi? AI cagri
    exception'larinda artan gunluk sayaci okur; 5'ten fazlaysa veri/kredi sorunu
    isareti -> admin uyarisi (KRITIK: cozulene kadar periyodik tekrar)."""
    try:
        from src.db import database as db
        n = db.ai_hata_sayisi()
    except Exception:
        return None
    if n > AI_HATA_ESIGI:
        return ("ai_hata_cok",
                f"Bugün {n} AI çağrısı başarısız — veri/kredi kontrolü gerek.")
    return None


def _kontrol_kap():
    """KAP bugun erisilemez olup ORNEK (sahte) kaynaga dusuldu mu? service.py
    fallback'te 'kap_ornek:<gun>' bayragini yazar; burada okunur -> gunde 1 uyari."""
    try:
        from src.db import database as db
        bugun = datetime.now(_TZ).date().isoformat()
        if str(db.get_setting(f"kap_ornek:{bugun}", "0")) == "1":
            return ("kap_ornek",
                    "KAP erişilemiyor, sahte kaynak devrede — BIST haber akışı kesik.")
    except Exception:
        return None
    return None


# Gece isleri: (heartbeat adi, izin verilen azami yas saat). Cron gece ~23:30-23:50
# calisir; 30s esik hafta ici gecikme/dst payi birakir.
HEARTBEAT_ISLERI = [
    ("update_trades", 30),
    ("update_decisions", 30),
    ("update_haber_etki", 30),
    ("update_model_portfoy", 30),
    ("update_portfoy_snapshot", 30),
]


def _kontrol_heartbeat():
    """Gece bakim islerinden biri sessizce olduyse (son basarili damga esik saatten
    eskiyse ya da hic yoksa) admin'e uyar. Tum gecikenler tek mesajda toplanir."""
    try:
        from src.db import database as db
    except Exception:
        return None
    gecikenler = []
    for is_adi, esik in HEARTBEAT_ISLERI:
        yas = db.kalp_yasi_saat(is_adi)
        if yas is None:
            gecikenler.append(f"{is_adi} (hiç çalışmadı)")
        elif yas > esik:
            gecikenler.append(f"{is_adi} ({yas:.0f}s önce)")
    if gecikenler:
        return ("heartbeat_gec",
                "Gece işleri gecikti/durdu — " + ", ".join(gecikenler) + ".")
    return None


def _kontrol_risk_det():
    """Bugun deterministik risk kac hissede hesaplanamadi? commentary.py sayaci
    ('risk_det_fail:<gun>') artirir; >0 ise gunluk ozet olarak admin'e bildir."""
    try:
        from src.db import database as db
        n = db.gunluk_sayac("risk_det_fail")
    except Exception:
        return None
    if n > 0:
        return ("risk_det_fail",
                f"Bugün {n} hissede deterministik risk hesaplanamadı (risk_det: HESAPLANAMADI).")
    return None


def _kontrol_mcp():
    """Borsa MCP ayakta mi? GUNDE 1 KEZ basit bir fiyat cagrisi (THYAO) yapar;
    None donerse fiyat/KAP fallback'leri devrede demektir -> admin uyarisi.
    MCP cagrisi pahalidir (~birkac sn) -> 'mcp_check:<gun>' ile gunde bir kez
    calisir (30dk'lik core kosularinda tekrar cagirmaz); uyari da run() spam-state
    ile gunde 1 kez gider. (XU100 yerine THYAO: get_price equity'de guvenilir
    canlilik sinyali; endeks destegi MCP cokukken dogrulanamaz.)"""
    try:
        from src.db import database as db
    except Exception:
        return None
    bugun = datetime.now(_TZ).date().isoformat()
    if db.get_setting(f"mcp_check:{bugun}"):
        return None                          # bugun zaten denendi
    db.set_setting(f"mcp_check:{bugun}", "1")
    try:
        from src.news import borsa_mcp
        px = borsa_mcp.get_price("THYAO")
    except Exception:
        px = None
    if not px:
        return ("mcp_yanit_yok",
                "Borsa MCP yanıt vermiyor — fiyat/KAP fallback'leri devrede.")
    return None


# --- spam onleme: gunde 1 kez (KRITIK olanlar haric) ---

# KRITIK sorunlar: sistem karar uretemiyor demektir. Bunlar gunluk spam
# filtresine TAKILMAZ; sorun devam ettigi surece KRITIK_TEKRAR_SAAT'te bir
# yeniden bildirilir (15 Tem 2026: kredi bitti, ilk uyari 09:30'da gitti ama
# gun boyu suren arizanin tekrari bastirildi -> sorun gozden kacti).
# Not: tamamen filtresiz birakmak 30 dk'lik cron ile gunde 48 mesaj demekti;
# periyodik tekrar hem "yutulmasin" hem "spam olmasin" dengesini kurar.
KRITIK_ANAHTARLAR = {"ai_kredi_bitti", "ai_hata_cok"}
KRITIK_TEKRAR_SAAT = 2


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


def _bastirilsin_mi(kayit, anahtar: str, now: datetime) -> bool:
    """Bu uyari spam filtresine takilsin mi?

    Normal sorunlar : gunde 1 kez (kayit == bugunun tarihi ise bastir).
    KRITIK sorunlar : asla gun boyu bastirilmaz; son bildirimden bu yana
                      KRITIK_TEKRAR_SAAT gectiyse yeniden bildirilir.
    Kayit yoksa (ilk kritik uyari) HER ZAMAN bildirilir -> yutulma olmaz.
    """
    if not kayit:
        return False                          # ilk uyari -> mutlaka git
    if anahtar not in KRITIK_ANAHTARLAR:
        return str(kayit) == now.date().isoformat()
    try:
        son = datetime.fromisoformat(str(kayit))
    except ValueError:
        return False                          # eski/bozuk format -> bildir (guvenli taraf)
    if son.tzinfo is None:
        son = son.replace(tzinfo=_TZ)
    gecen_saat = (now - son).total_seconds() / 3600
    return gecen_saat < KRITIK_TEKRAR_SAAT


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
    for cid in BILDIRIM_LISTESI:
        try:
            telegram.send_message(metin, chat_id=cid)
            basari += 1
        except Exception as e:
            print(f"[uyari] Telegram gonderilemedi (chat={cid}): "
                  f"{type(e).__name__}: {str(e)[:80]}")
    return basari > 0


def run(verbose: bool = True, mode: str = "all") -> dict:
    """mode='core' -> servis + DB (7/24); 'market' -> fiyat cache tazeligi
    (borsa saatleri); 'all' -> hepsi (elle/test calistirma)."""
    now = datetime.now(_TZ)
    bugun = now.date().isoformat()
    core = (_kontrol_servis, _kontrol_db, _kontrol_kredi, _kontrol_ai_hata,
            _kontrol_kap, _kontrol_heartbeat, _kontrol_risk_det, _kontrol_mcp)
    market = (lambda: _kontrol_cache_tazelik(now),)
    if mode == "core":
        kontroller = core
    elif mode == "market":
        kontroller = market
    else:
        kontroller = core + market
    sorunlar = []
    for kontrol in kontroller:
        try:
            r = kontrol()
        except Exception as e:                # bir kontrol patlasa digerleri devam etsin
            r = ("monitor_hata", f"Sağlık kontrolü hata verdi: {type(e).__name__}")
        if r:
            sorunlar.append(r)

    state = _state_yukle()
    gonderilen, atlanan = [], []
    for anahtar, mesaj in sorunlar:
        if _bastirilsin_mi(state.get(anahtar), anahtar, now):
            atlanan.append(anahtar)
            continue
        if _bildir(mesaj):
            # KRITIK -> zaman damgasi (periyodik tekrar icin), digerleri -> tarih.
            state[anahtar] = (now.isoformat(timespec="minutes")
                              if anahtar in KRITIK_ANAHTARLAR else bugun)
            gonderilen.append(anahtar)
        else:
            atlanan.append(anahtar)           # gonderilemedi -> state'e yazma, tekrar dene

    # Bugun cozulen sorunlarin state'ini temizle (sadece eski gunleri at).
    # Kritik anahtarlarda deger ISO zaman damgasi ('2026-07-15T14:00') -> ayni
    # gune aitse basindaki tarih bugunle eslesir.
    state = {k: v for k, v in state.items() if str(v).startswith(bugun)}
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
    mod = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mod not in ("core", "market", "all"):
        print(f"Gecersiz mod: {mod} (core|market|all olmali)")
        sys.exit(2)
    run(mode=mod)
