"""v2.1 TEST DONEMI SAYACI — 15 Tem 2026.

NEDEN: kural dondurma belirsiz bir takvime ("Agustos") degil, OLCULEBILIR bir
hedefe baglanir. Test doner:

    15 kapanis  VEYA  10 kesintisiz is gunu   -> hangisi ONCE gelirse

TANIMLAR (yoruma acik birakilmasin):
- "kapanis": strategy_version='v2.1' ile ACILMIS ve kapanmis trade
  (trades.kapanis_tarihi dolu). paper_trades'te surum kolonu YOK, o yuzden
  sayim trades uzerinden yapilir.
- "kesintisiz is gunu": v2.1 basladigindan beri, BIST'te >= BIST_MIN_KARAR (70)
  karar uretilen ust uste is gunu sayisi. Bir gun eksik uretirse sayac SIFIRLANIR
  (morning._uretim_garantisi zaten o gun kirmizi alarm gonderir).

Hedefe ulasinca: "test donemi doldu, degerlendirme zamani" alarmi (gunde 1 kez,
health_monitor uzerinden).

Kullanim:
    python -m src.ops.test_donemi
"""
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Istanbul")

SURUM = "v2.1"
HEDEF_KAPANIS = 15
HEDEF_KESINTISIZ_GUN = 10
BIST_MIN_KARAR = 70          # morning.BIST_MIN_KARAR ile AYNI olmali
_ALARM_ANAHTAR = "test_donemi_doldu_bildirildi"


def _bugun() -> date:
    return datetime.now(_TZ).date()


def kapanis_sayisi() -> int:
    """v2.1 ile acilmis ve kapanmis trade sayisi."""
    from src.db import database as db
    with db.get_conn() as c:
        return c.execute(
            "SELECT COUNT(*) FROM trades WHERE strategy_version=? "
            "AND kapanis_tarihi IS NOT NULL", (SURUM,)).fetchone()[0]


def _bist_karar_sayilari() -> dict:
    """{gun_iso: BIST karar sayisi} — v2.1 kararlari."""
    from src.db import database as db
    from src.ops.gun_kalitesi import _is_us
    with db.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT ticker, substr(tarih,1,10) g FROM decisions "
            "WHERE strategy_version=?", (SURUM,))]
    gunler: dict = {}
    for r in rows:
        if _is_us(r["ticker"]):
            continue
        gunler[r["g"]] = gunler.get(r["g"], 0) + 1
    return gunler


def kesintisiz_gun() -> int:
    """Bugunden GERIYE dogru, ust uste kac is gununde >=70 BIST karari uretildi.
    Bugun henuz brifing kosmadiysa bugun sayilmaz (0 karar -> zincir kirilmasin
    diye bugun ATLANIR, dunden baslanir)."""
    gunler = _bist_karar_sayilari()
    if not gunler:
        return 0
    n = 0
    g = _bugun()
    # Bugun brifing kosmamissa (hic v2.1 karari yok) bugunu sayma.
    if gunler.get(g.isoformat(), 0) == 0:
        g -= timedelta(days=1)
    while True:
        if g.weekday() >= 5:               # hafta sonu zinciri kirmaz, atla
            g -= timedelta(days=1)
            continue
        adet = gunler.get(g.isoformat(), 0)
        if adet == 0:
            break                          # v2.1 oncesi / veri yok -> zincir biter
        if adet < BIST_MIN_KARAR:
            break                          # eksik uretim -> zincir SIFIRLANIR
        n += 1
        g -= timedelta(days=1)
    return n


def durum() -> dict:
    kap = kapanis_sayisi()
    gun = kesintisiz_gun()
    doldu = kap >= HEDEF_KAPANIS or gun >= HEDEF_KESINTISIZ_GUN
    if kap >= HEDEF_KAPANIS:
        sebep = f"{kap} kapanis (hedef {HEDEF_KAPANIS})"
    elif gun >= HEDEF_KESINTISIZ_GUN:
        sebep = f"{gun} kesintisiz is gunu (hedef {HEDEF_KESINTISIZ_GUN})"
    else:
        sebep = ""
    return {"surum": SURUM, "kapanis": kap, "hedef_kapanis": HEDEF_KAPANIS,
            "kesintisiz_gun": gun, "hedef_gun": HEDEF_KESINTISIZ_GUN,
            "doldu": doldu, "sebep": sebep}


def ozet_satir(d: dict = None) -> str:
    """Karne/panel icin tek satir."""
    d = d if d is not None else durum()
    if d["doldu"]:
        return (f"{SURUM} TEST DONEMI DOLDU — {d['sebep']}; degerlendirme zamani")
    return (f"{SURUM} kapanis: {d['kapanis']}/{d['hedef_kapanis']} | "
            f"kesintisiz: {d['kesintisiz_gun']}/{d['hedef_gun']} is gunu")


def alarm_gerekli_mi() -> tuple | None:
    """health_monitor icin: hedefe ulasildiysa (anahtar, mesaj); yoksa None.
    Bir kez bildirilir (ayar bayragi) — her gun tekrar etmez."""
    from src.db import database as db
    d = durum()
    if not d["doldu"]:
        return None
    try:
        if db.get_setting(_ALARM_ANAHTAR):
            return None
    except Exception:
        pass
    return ("test_donemi_doldu",
            f"✅ {SURUM} TEST DÖNEMİ DOLDU — {d['sebep']}. "
            f"Kural dondurma değerlendirmesi zamanı.")


def alarm_bildirildi() -> None:
    from src.db import database as db
    db.set_setting(_ALARM_ANAHTAR, datetime.now(_TZ).isoformat())


def main() -> int:
    d = durum()
    print(f"=== {SURUM} TEST DONEMI ===")
    print(f"  kapanis        : {d['kapanis']}/{d['hedef_kapanis']}")
    print(f"  kesintisiz gun : {d['kesintisiz_gun']}/{d['hedef_gun']}")
    print(f"  durum          : {'DOLDU — ' + d['sebep'] if d['doldu'] else 'devam ediyor'}")
    print()
    print(f"  karne satiri: {ozet_satir(d)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
