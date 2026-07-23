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

    # 0) ISITMA MODU (14:25 cron): EVDS'yi onceden isit, cikma. 14:30 sorgusu hizli
    #    donsun diye (soguk EVDS ~50sn -> timeout riski).
    if len(sys.argv) > 1 and sys.argv[1] == "isit":
        if macro.bugun_ppk_mi(now.date()):
            ok, sure = macro.evds_isit()
            print(f"[{stamp}] EVDS isitma: {'OK' if ok else 'veri yok'} ({sure}sn)")
        else:
            print(f"[{stamp}] Bugun PPK gunu degil — isitma atlandi.")
        return 0

    # 1) Bugun PPK gunu degilse sessizce cik (token/telegram harcanmaz)
    if not macro.bugun_ppk_mi(now.date()):
        print(f"[{stamp}] Bugun PPK gunu degil — atlandi.")
        return 0

    from src.notify import telegram
    # Basarisizlik mesajinda gosterilecek mevcut deger (son bilinen / fallback)
    mevcut = macro._load_son_bilinen().get(
        "politika_faizi", macro._POLITIKA_FAIZI_FALLBACK)

    # 2) TCMB'den guncel politika faizini cekmeye calis (PPK yolu: EVDS gecikmesine
    #    karsi retry ile). 14:25 isitmasi + buradaki retry, 14:30 'cekilemedi'yi onler.
    pf, kaynak = macro.canli_politika_faizi(retries=3)

    # GECIKMELI KAYNAK TUZAGI (23 Tem 2026): EVDS SERISI, PPK karari aciklandiktan
    # sonra ayni gun icinde guncellenmeyebilir. O durumda cekilen deger ESKI faizdir.
    # Bunu "yeni karar" diye duyurmak, faiz degistigi gun kullaniciya YANLIS rakam
    # vermek olur. Ayrim: TCMB/EVDS2 SAYFALARI guncel orani gosterir (esitlik =
    # gercekten degismedi); EVDS SERISI gecikebilir (esitlik = DOGRULANAMADI).
    GECIKEBILIR = {"evds_seri"}
    onceki = macro._load_son_bilinen().get("politika_faizi")
    degisti = (pf is not None and onceki is not None and pf != onceki)
    dogrulanamadi = (pf is not None and not degisti and kaynak in GECIKEBILIR)

    # SABIT-KALMA TEYIDI (23 Tem 2026): EVDS 'dogrulanamadi' der ya da hic cekilemezse,
    # KARAR METNINI dogrudan TCMB basin duyurusundan oku. Cekilirse kesin; cekilemezse
    # asagidaki durust 'dogrulanamadi/cekilemedi' mesaji kalir (UYDURMA YOK).
    teyit = None
    if dogrulanamadi or pf is None:
        try:
            teyit = macro.tcmb_duyuru_teyit(beklenen=onceki)
        except Exception as e:
            print(f"[{stamp}] TCMB duyuru teyit hatasi: {type(e).__name__}: {str(e)[:80]}")

    if teyit and teyit[0] == "sabit":
        # Karar metni: faiz SABIT. Kesin mesaj + kalici sakla.
        f = teyit[1]
        macro._kaydet_son_bilinen({"politika_faizi": f})
        macro._CACHE.clear()
        pf = f
        mesaj = (f"🏦 <b>TCMB faiz kararı:</b> %{f:g} — <b>SABİT</b> "
                 "(TCMB basın duyurusundan teyitli)")
        print(f"[{stamp}] PPK: TCMB duyurusu SABIT %{f:g} teyit etti; kaydedildi.")
    elif teyit and teyit[0] == "degisti":
        # Karar metni farkli faiz -> degisim (duyurudan teyitli).
        f = teyit[1]
        macro._kaydet_son_bilinen({"politika_faizi": f})
        macro._CACHE.clear()
        pf = f
        if onceki is not None and f != onceki:
            yon = "artırım" if f > onceki else "indirim"
            fark = abs(round((f - onceki) * 100))
            mesaj = (f"🏦 <b>TCMB yeni faiz kararı:</b> %{onceki:g} → %{f:g} "
                     f"({fark}bp {yon}) — TCMB basın duyurusundan teyitli")
        else:
            mesaj = (f"🏦 <b>TCMB faiz kararı:</b> %{f:g} "
                     "(TCMB basın duyurusundan teyitli)")
        print(f"[{stamp}] PPK: TCMB duyurusu DEGISIM %{f:g} teyit etti; kaydedildi.")
    elif dogrulanamadi:
        # Duyuru da cekilemedi -> durust 'dogrulanamadi'. Deger kaydedilir (zaten ayni).
        mesaj = ("🏦 <b>PPK günü:</b> yeni faiz kararı otomatik <b>doğrulanamadı</b>. "
                 f"Elimizdeki değer hâlâ %{pf:g} ve tek otomatik kaynak (EVDS serisi) "
                 "PPK günü gecikmeli güncellenir — TCMB basın duyurusu da çekilemedi. "
                 "Lütfen TCMB duyurusundan teyit edin.")
        print(f"[{stamp}] PPK: deger %{pf:g} ({kaynak}) dogrulanamadi + duyuru cekilemedi "
              "-> DOGRULANAMADI bildirildi.")
    elif degisti:
        # EVDS gecikmeli seri sonunda YENI degeri gosterdi (duyuru teyidi yoksa da).
        macro._kaydet_son_bilinen({"politika_faizi": pf})
        macro._CACHE.clear()
        yon = "artırım" if pf > onceki else "indirim"
        fark = abs(round((pf - onceki) * 100))
        mesaj = (f"🏦 <b>TCMB yeni faiz kararı:</b> %{onceki:g} → %{pf:g} "
                 f"({fark}bp {yon}) — sistem güncellendi")
        print(f"[{stamp}] PPK: faiz DEGISTI %{onceki:g} -> %{pf:g} "
              f"({fark}bp {yon}, kaynak={kaynak}); kaydedildi.")
    elif pf is not None:
        # TCMB/EVDS2 sayfasindan (gecikmesiz kaynak) ayni deger -> gercekten degismedi.
        macro._kaydet_son_bilinen({"politika_faizi": pf})
        macro._CACHE.clear()
        mesaj = (f"🏦 <b>TCMB faiz kararı:</b> %{pf:g} — <b>değişiklik yok</b> "
                 "(TCMB sayfasından teyit edildi)")
        print(f"[{stamp}] PPK: faiz degismedi (%{pf:g}, kaynak={kaynak}); kaydedildi.")
    else:
        # Hicbir kaynak (EVDS + duyuru) gelmedi -> manuel guncelleme uyarisi.
        mesaj = ("⚠️ <b>ÖNEMLİ:</b> Bugün PPK toplantısı var ama yeni politika faizi "
                 "otomatik çekilemedi (EVDS + TCMB duyurusu). Lütfen TCMB sitesini "
                 "kontrol edip macro.py'deki fallback değerini manuel güncelleyin. "
                 f"Şu an kullanılan değer: %{mevcut:g}")
        print(f"[{stamp}] PPK: faiz çekilemedi; manuel uyarı (mevcut %{mevcut:g}).")

    # 3) Telegram bildirimi — KRITIK sinif: 30sn/2dk/5dk retry + kayip carry-forward.
    if telegram.is_configured():
        try:
            sonuc = telegram.broadcast_critical(mesaj, tur="PPK faiz kararı")
            ok = [c for c, s in sonuc.items() if s == "ok"]
            print(f"[{stamp}] Telegram (kritik): {len(ok)}/{len(sonuc)} alici. {sonuc}")
        except Exception as e:
            print(f"[{stamp}] Telegram gonderilemedi: {type(e).__name__}: {str(e)[:80]}")
    else:
        print(f"[{stamp}] Telegram yapilandirilmamis — bildirim atlandi.")

    return 0 if pf is not None else 1


if __name__ == "__main__":
    sys.exit(run())
