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

    # GECIKMELI KAYNAK TUZAGI (23 Tem 2026): EVDS SERISI, PPK karari aciklandiktan
    # sonra ayni gun icinde guncellenmeyebilir. O durumda cekilen deger ESKI faizdir.
    # Bunu "yeni karar" diye duyurmak, faiz degistigi gun kullaniciya YANLIS rakam
    # vermek olur. Ayrim: TCMB/EVDS2 SAYFALARI guncel orani gosterir (esitlik =
    # gercekten degismedi); EVDS SERISI gecikebilir (esitlik = DOGRULANAMADI).
    GECIKEBILIR = {"evds_seri"}
    onceki = macro._load_son_bilinen().get("politika_faizi")
    degisti = (pf is not None and onceki is not None and pf != onceki)
    dogrulanamadi = (pf is not None and not degisti and kaynak in GECIKEBILIR)

    if dogrulanamadi:
        # Deger kaydedilir (zaten ayni), ama DUYURU iddiali olmaz.
        mesaj = ("🏦 <b>PPK günü:</b> yeni faiz kararı otomatik <b>doğrulanamadı</b>. "
                 f"Elimizdeki değer hâlâ %{pf:g} ve tek kaynak (EVDS serisi) PPK günü "
                 "gecikmeli güncellenir — bu rakam karar öncesine ait olabilir. "
                 "Lütfen TCMB duyurusundan teyit edin.")
        print(f"[{stamp}] PPK: deger %{pf:g} ({kaynak}) ama onceki deger ile ayni "
              f"ve kaynak gecikebilir -> DOGRULANAMADI olarak bildirildi.")
    elif pf is not None:
        # Basarili: kalici sakla + onbellegi temizle (yeni deger okunsun)
        macro._kaydet_son_bilinen({"politika_faizi": pf})
        macro._CACHE.clear()
        if degisti:
            yon = "artırım" if pf > onceki else "indirim"
            fark = abs(round((pf - onceki) * 100))
            mesaj = (f"🏦 <b>TCMB yeni faiz kararı:</b> %{onceki:g} → %{pf:g} "
                     f"({fark}bp {yon}) — sistem güncellendi")
            print(f"[{stamp}] PPK: faiz DEGISTI %{onceki:g} -> %{pf:g} "
                  f"({fark}bp {yon}, kaynak={kaynak}); kaydedildi.")
        else:
            mesaj = (f"🏦 <b>TCMB faiz kararı:</b> %{pf:g} — <b>değişiklik yok</b> "
                     "(TCMB sayfasından teyit edildi)")
            print(f"[{stamp}] PPK: faiz degismedi (%{pf:g}, kaynak={kaynak}); kaydedildi.")
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
