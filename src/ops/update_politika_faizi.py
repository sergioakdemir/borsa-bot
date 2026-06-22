"""PPK (Para Politikasi Kurulu) gunu politika faizi otomasyonu.

Cron: 30 14 * * 1-5 (her gun 14:30). Bugun PPK gunu DEGILSE sessizce cikar.
PPK gunuyse TCMB'den guncel politika faizini cekmeye calisir:
  - Basariliysa: macro_last.json'a kaydeder + Telegram'a "yeni faiz" bildirir.
  - Basarisizsa: Telegram'a "manuel guncelle" uyarisi gonderir (mevcut deger ile).

PPK karari ~14:00'te aciklanir; 14:30 calismasi yeni karari yakalamak icindir.
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


def run() -> int:
    _load_dotenv()
    from src.news import macro
    now = datetime.now(_TZ)
    stamp = f"{now:%Y-%m-%d %H:%M}"

    # 1) Bugun PPK gunu degilse sessizce cik (token/telegram harcanmaz)
    if not macro.bugun_ppk_mi(now.date()):
        print(f"[{stamp}] Bugun PPK gunu degil — atlandi.")
        return 0

    from src.notify import telegram
    # Basarisizlik mesajinda gosterilecek mevcut deger (son bilinen / fallback)
    mevcut = macro._load_son_bilinen().get(
        "politika_faizi", macro._POLITIKA_FAIZI_FALLBACK)

    # 2) TCMB'den guncel politika faizini cekmeye calis
    pf, kaynak = macro.canli_politika_faizi()

    if pf is not None:
        # Basarili: kalici sakla + onbellegi temizle (yeni deger okunsun)
        macro._kaydet_son_bilinen({"politika_faizi": pf})
        macro._CACHE.clear()
        mesaj = f"🏦 <b>TCMB yeni faiz kararı:</b> %{pf:g} — sistem güncellendi"
        print(f"[{stamp}] PPK: yeni politika faizi %{pf:g} ({kaynak}); kaydedildi.")
    else:
        # Basarisiz: manuel guncelleme uyarisi
        mesaj = ("⚠️ <b>ÖNEMLİ:</b> Bugün PPK toplantısı var ama yeni politika faizi "
                 "otomatik çekilemedi. Lütfen TCMB sitesini kontrol edip "
                 "macro.py'deki fallback değerini manuel güncelleyin. "
                 f"Şu an kullanılan değer: %{mevcut:g}")
        print(f"[{stamp}] PPK: faiz çekilemedi; manuel uyarı (mevcut %{mevcut:g}).")

    # 3) Telegram bildirimi (yapilandirilmamissa atla)
    if telegram.is_configured():
        try:
            sonuc = telegram.broadcast(mesaj)
            ok = [c for c, s in sonuc.items() if s == "ok"]
            print(f"[{stamp}] Telegram: {len(ok)}/{len(sonuc)} alici.")
        except Exception as e:
            print(f"[{stamp}] Telegram gonderilemedi: {type(e).__name__}: {str(e)[:80]}")
    else:
        print(f"[{stamp}] Telegram yapilandirilmamis — bildirim atlandi.")

    return 0 if pf is not None else 1


if __name__ == "__main__":
    sys.exit(run())
