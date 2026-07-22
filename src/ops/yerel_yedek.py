"""Yerel (disk-ici) gunluk yedek — DB + yapilandirma + kucuk loglar.

NEDEN VAR (22 Tem 2026 denetimi): sistemde HICBIR calisan yedek yoktu.
  * Hetzner haftalik snapshot 22 Haziran'dan beri her hafta basarisiz
    (HETZNER_API_TOKEN yok) — 5 hafta SESSIZ.
  * Google Drive gece yedegi hic calismadi (GOOGLE_DRIVE_FOLDER_ID bos) ve
    servis hesabinin Drive kotasi 0 oldugu icin zaten yukleyemez
    ("Service Accounts do not have storage quota") — bkz. docs/yedekleme.md.
Bu modul, o iki yol acilana kadar EN AZINDAN disk-ici tarih damgali bir kopya
tutar. Ayni diskte durdugu icin disk arizasina karsi korumaz; amaci yanlis
silme / bozulma / hatali migration gibi MANTIK kazalarindan donebilmektir.

NE YEDEKLENIR
  data/borsa.db      : sqlite3 online backup API ile (cron yazarken de tutarli)
  .env               : sirlar (yedek 0600 izinle yazilir)
  config/*           : watchlist, bist100, drive kimligi, crontab kopyasi
  data/*.json        : uretilen durum dosyalari (fiyat cache, changelog, ...)
  crontab            : `crontab -l` ciktisi (canli hali)
  logs/              : YALNIZ kucuk loglar (VARSAYILAN 1 MB alti) — fiyat_cache.log
                       ve web.log gibi devasa dosyalar disarida birakilir.

SESSIZ BASARISIZLIK YASAK (bu isin varlik sebebi): her kosu sonunda dogrulama
yapilir (tarball acilir + icindeki DB integrity_check'ten gecer). Basarisizlikta
Telegram'a KRITIK uyari gider. Ayrica en son BASARILI yedek YEDEK_BAYAT_GUN
gunden eskiyse yine alarm verilir — boylece cron hic kosmasa bile fark edilir.

KULLANIM
    python -m src.ops.yerel_yedek            # yedek al + dogrula + eskiyi sil
    python -m src.ops.yerel_yedek durum      # yalniz rapor (yedek almaz)
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")
_KOK = Path(__file__).resolve().parents[2]

# Yedeklerin durdugu dizin (.env: YEDEK_DIZIN ile degistirilebilir).
VARSAYILAN_DIZIN = Path("/root/yedek")
# Kac gunluk yedek saklanir (daha eskisi silinir).
SAKLA_GUN = 7
# Bu boyutun ustundeki log dosyalari yedege KONMAZ (fiyat_cache.log ~4 MB).
LOG_MAX_BAYT = 1 * 1024 * 1024
# En son basarili yedek bu kadar gunden eskiyse alarm (cron olu kalmasin).
YEDEK_BAYAT_GUN = 2

_AD_KALIP = "borsa-bot-yedek-{tarih}.tar.gz"
_AD_ONEK = "borsa-bot-yedek-"


def _load_dotenv():
    env_path = _KOK / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _dizin() -> Path:
    return Path(os.environ.get("YEDEK_DIZIN") or VARSAYILAN_DIZIN)


def _stamp() -> str:
    return f"{datetime.now(_TZ):%Y-%m-%d %H:%M}"


# --- yardimcilar ------------------------------------------------------------
def _db_tutarli_kopya(kaynak: Path, hedef: Path) -> None:
    """SQLite online backup API ile tutarli kopya.

    Duz dosya kopyasi (cp) yazma ortasinda yakalarsa BOZUK kopya uretir; gece
    isleri 23:30-23:50 arasi yazdigi icin bu gercek bir risk. backup() API'si
    okuyucu kilidiyle sayfa sayfa kopyalar -> her zaman tutarli.
    """
    src = sqlite3.connect(f"file:{kaynak}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(hedef))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _crontab_dok(hedef: Path) -> bool:
    """Canli crontab'i dosyaya yazar. Basarisizsa False (yedek yine de surer)."""
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=20)
        if r.returncode == 0:
            hedef.write_text(r.stdout, encoding="utf-8")
            return True
    except Exception:
        pass
    return False


def _eklenecekler(sahne: Path) -> list:
    """(disk_yolu, arsiv_ici_ad) ciftleri."""
    ciftler = []

    db = _KOK / "data" / "borsa.db"
    if db.exists():
        kopya = sahne / "borsa.db"
        _db_tutarli_kopya(db, kopya)
        ciftler.append((kopya, "data/borsa.db"))

    env = _KOK / ".env"
    if env.exists():
        ciftler.append((env, ".env"))

    for p in sorted((_KOK / "config").glob("*")):
        if p.is_file():
            ciftler.append((p, f"config/{p.name}"))

    for p in sorted((_KOK / "data").glob("*.json")):
        if p.is_file():
            ciftler.append((p, f"data/{p.name}"))

    ct = sahne / "crontab.txt"
    if _crontab_dok(ct):
        ciftler.append((ct, "crontab.txt"))

    logd = _KOK / "logs"
    if logd.is_dir():
        for p in sorted(logd.glob("*")):
            # Devasa loglar yedegi sisirir ve kurtarma degeri dusuktur -> atla.
            if p.is_file() and p.stat().st_size <= LOG_MAX_BAYT:
                ciftler.append((p, f"logs/{p.name}"))
    return ciftler


def _dogrula(arsiv: Path) -> tuple:
    """Yedek GERCEKTEN kullanilabilir mi? (ok, mesaj)

    Iki asama: (1) tarball acilabiliyor ve icinde DB var mi, (2) icindeki DB
    integrity_check'ten geciyor mu. 'Dosya olustu' yeterli degil — bozuk bir
    tarball da dosyadir; yedegin degeri ancak GERI ACILABILDIGINDE vardir.
    """
    try:
        with tarfile.open(arsiv, "r:gz") as t:
            adlar = t.getnames()
            if "data/borsa.db" not in adlar:
                return False, "arsivde data/borsa.db yok"
            with tempfile.TemporaryDirectory() as td:
                t.extract("data/borsa.db", path=td)
                p = Path(td) / "data" / "borsa.db"
                c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                try:
                    sonuc = c.execute("PRAGMA integrity_check").fetchone()[0]
                finally:
                    c.close()
                if sonuc != "ok":
                    return False, f"arsivdeki DB bozuk: {sonuc[:120]}"
        return True, f"{len(adlar)} dosya, DB integrity_check=ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:150]}"


def _eskileri_sil(dizin: Path, sakla: int = SAKLA_GUN) -> list:
    """SAKLA_GUN gununden eski yedekleri siler. Silinenlerin adini doner."""
    sinir = datetime.now(_TZ) - timedelta(days=sakla)
    silinen = []
    for p in sorted(dizin.glob(f"{_AD_ONEK}*.tar.gz")):
        try:
            gun = datetime.strptime(p.stem.replace(_AD_ONEK, "").replace(".tar", ""),
                                    "%Y-%m-%d").replace(tzinfo=_TZ)
        except ValueError:
            continue
        if gun < sinir:
            p.unlink()
            silinen.append(p.name)
    return silinen


def _alarm(mesaj: str) -> None:
    """Telegram'a KRITIK uyari. Alarm gonderilemezse en azindan loga bas —
    alarmin kendisi sessizce kaybolmasin."""
    try:
        _load_dotenv()
        from src.notify import telegram
        # notify_admins alici basina hatayi YUTAR ({id: 'hata:...'} doner) -> sonucu
        # aciktan logla, yoksa "alarm gitti" sanip yine sessiz kalabiliriz.
        sonuc = telegram.notify_admins(mesaj, prefix="🔴")
        basarili = [k for k, v in sonuc.items() if v == "ok"]
        if basarili:
            print(f"  [alarm] Telegram: {sonuc}")
        else:
            print(f"  [alarm] HICBIR ALICIYA ULASILAMADI: {sonuc} | mesaj: {mesaj}")
    except Exception as e:
        print(f"  [alarm] TELEGRAM'A GONDERILEMEDI ({type(e).__name__}): {mesaj}")


# --- ana akis ---------------------------------------------------------------
def son_yedek(dizin: Path = None) -> Path | None:
    """En yeni yedek dosyasi (yoksa None)."""
    dizin = dizin or _dizin()
    if not dizin.is_dir():
        return None
    ler = sorted(dizin.glob(f"{_AD_ONEK}*.tar.gz"))
    return ler[-1] if ler else None


def durum(dizin: Path = None) -> dict:
    """Yedek sagligi ozeti (panel/karne okuyabilir)."""
    _load_dotenv()
    dizin = dizin or _dizin()
    son = son_yedek(dizin)
    yas_gun = None
    if son:
        yas_gun = (datetime.now(_TZ)
                   - datetime.fromtimestamp(son.stat().st_mtime, tz=_TZ)).days
    return {
        "dizin": str(dizin),
        "adet": len(list(dizin.glob(f"{_AD_ONEK}*.tar.gz"))) if dizin.is_dir() else 0,
        "son": son.name if son else None,
        "son_boyut_mb": round(son.stat().st_size / 1024 / 1024, 2) if son else None,
        "yas_gun": yas_gun,
        "bayat": (yas_gun is None or yas_gun > YEDEK_BAYAT_GUN),
    }


def calistir(verbose: bool = True) -> dict:
    """Yedek al -> dogrula -> eskiyi sil. Basarisizlikta Telegram alarmi."""
    _load_dotenv()
    dizin = _dizin()
    bugun = datetime.now(_TZ).date().isoformat()
    # Dizin hazirligi da ALARMLI olmali: disk dolu / izin hatasi / salt-okunur
    # bagli disk burada patlarsa, alarmsiz cokup yine sessiz basarisizlik olurdu.
    try:
        dizin.mkdir(parents=True, exist_ok=True)
        os.chmod(dizin, 0o700)              # icinde .env var -> sadece root
    except Exception as e:
        mesaj = (f"YEDEK DIZINI HAZIRLANAMADI ({bugun})\n"
                 f"{type(e).__name__}: {str(e)[:200]}\nDizin: {dizin}")
        if verbose:
            print(f"  HATA: {mesaj}")
        _alarm(mesaj)
        return {"ok": False, "hata": f"{type(e).__name__}: {e}"}

    hedef = dizin / _AD_KALIP.format(tarih=bugun)
    gecici = hedef.with_suffix(".tmp")

    if verbose:
        print(f"[{_stamp()}] yerel yedek basliyor -> {hedef}")

    try:
        with tempfile.TemporaryDirectory() as td:
            sahne = Path(td)
            ciftler = _eklenecekler(sahne)
            with tarfile.open(gecici, "w:gz") as t:
                for disk, ad in ciftler:
                    t.add(disk, arcname=ad)
        # Atomik yerine koyma: yarim tarball'in gecerli yedek sanilmasini onler.
        gecici.replace(hedef)
        os.chmod(hedef, 0o600)              # .env iceriyor
    except Exception as e:
        gecici.unlink(missing_ok=True)
        mesaj = (f"YEDEK ALINAMADI ({bugun})\n{type(e).__name__}: {str(e)[:200]}\n"
                 f"Hedef: {hedef}")
        if verbose:
            print(f"  HATA: {mesaj}")
        _alarm(mesaj)
        return {"ok": False, "hata": f"{type(e).__name__}: {e}"}

    ok, not_ = _dogrula(hedef)
    boyut_mb = round(hedef.stat().st_size / 1024 / 1024, 2)
    if not ok:
        mesaj = (f"YEDEK DOGRULAMA BASARISIZ ({bugun})\n{not_}\n"
                 f"Dosya: {hedef} ({boyut_mb} MB)\n"
                 f"Bu dosyaya GUVENME — geri yukleme calismayabilir.")
        if verbose:
            print(f"  DOGRULAMA BASARISIZ: {not_}")
        _alarm(mesaj)
        return {"ok": False, "dosya": str(hedef), "hata": not_}

    silinen = _eskileri_sil(dizin)
    if verbose:
        print(f"  OK {hedef.name} ({boyut_mb} MB) | dogrulama: {not_}")
        print(f"  {len(ciftler)} dosya arsivlendi"
              + (f" | {len(silinen)} eski yedek silindi: {', '.join(silinen)}"
                 if silinen else " | silinen eski yedek yok"))
    return {"ok": True, "dosya": str(hedef), "boyut_mb": boyut_mb,
            "dosya_sayisi": len(ciftler), "silinen": silinen, "dogrulama": not_}


def bayatlik_kontrol(verbose: bool = True) -> dict:
    """Cron hic kosmasa bile fark edilsin: son BASARILI yedek cok eskiyse alarm.

    5 haftalik sessiz Hetzner basarisizligi tam olarak bu kontrol olmadigi icin
    fark edilmemisti.
    """
    d = durum()
    if d["bayat"]:
        mesaj = (f"YEDEK BAYAT — son yedek: {d['son'] or 'HIC YOK'}"
                 + (f" ({d['yas_gun']} gun once)" if d["yas_gun"] is not None else "")
                 + f"\nDizin: {d['dizin']} | esik: {YEDEK_BAYAT_GUN} gun"
                 + "\nGunluk yedek cron'u kosmuyor olabilir.")
        if verbose:
            print(f"  BAYAT: {mesaj}")
        _alarm(mesaj)
    elif verbose:
        print(f"  yedek taze: {d['son']} ({d['yas_gun']} gun) — {d['adet']} yedek duruyor")
    return d


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "durum":
        import json
        print(json.dumps(durum(), ensure_ascii=False, indent=2))
        return
    sonuc = calistir()
    bayatlik_kontrol()
    sys.exit(0 if sonuc.get("ok") else 1)


if __name__ == "__main__":
    main()
