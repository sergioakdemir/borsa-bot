"""BORSA ACIK/KAPALI TAKVIMI — tek kaynak (20 Tem 2026).

Onceden ayni mantik UC yerde kopyalanmisti (update_fiyat_cache._piyasa_acik,
run_alerts._borsa_acik, health_monitor._bist_acik) ve UCU DE resmi tatilleri
gormuyordu: hafta ici bir tatil gunu (29 Ekim, 30 Agustos, dini bayramlar)
borsa ACIK saniliyordu -> sahte "cache bayat" alarmi + kullaniciya "guncel
fiyat" diye kapanis fiyati.

Tatil listesi zaten commentary._TR_SABIT_TATIL/_TR_BAYRAM'da vardi ama yalniz
veri-bayatlik kill-switch'inde kullaniliyordu; acik/kapali kontrolune hic
baglanmamisti. Artik tablolar BURADA yasar, commentary de buradan okur.

BAGIMLILIK: bilerek sifir proje-ici import (sadece stdlib). Boylece hem
`src.ops.*` hem `src.alerts.*` hem `src.ai.commentary` dongusel import riski
olmadan cagirabilir.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Istanbul")

# Turkiye sabit tarihli resmi tatilleri (ay, gun) — borsa kapali.
TR_SABIT_TATIL = ((1, 1), (4, 23), (5, 1), (5, 19), (7, 15), (8, 30), (10, 29))

# Degisken tarihli dini bayramlar: her yil resmi ilan sonrasi MANUEL eklenir.
# (Yeni yil eklenmezse o yilin bayramlari tatil sayilmaz — bakim notu.)
TR_BAYRAM = {
    2026: ((3, 20), (3, 21), (3, 22),                 # Ramazan Bayrami
           (6, 5), (6, 6), (6, 7), (6, 8), (6, 9)),   # Kurban Bayrami
}

# Seans saatleri (Istanbul saati, dakika cinsinden).
_BIST_ACILIS = 10 * 60          # 10:00
_BIST_KAPANIS = 18 * 60         # 18:00
_ABD_ACILIS = 16 * 60 + 30      # NYSE ~16:30 IST
_ABD_KAPANIS = 23 * 60          # NYSE ~23:00 IST


def tr_tatilleri(start, end) -> set:
    """[start, end] yillarini kapsayan BIST tatil gunleri (hafta sonu haric)."""
    hols = set()
    for yil in range(start.year, end.year + 1):
        for ay, gun in TR_SABIT_TATIL:
            hols.add(date(yil, ay, gun))
        for ay, gun in TR_BAYRAM.get(yil, ()):
            hols.add(date(yil, ay, gun))
    return hols


def tatil_mi(d) -> bool:
    """Verilen gun BIST resmi tatili mi? (hafta sonu BURADA sayilmaz)"""
    if isinstance(d, datetime):
        d = d.date()
    return d in tr_tatilleri(d, d)


def borsa_acik(now=None, market: str = "bist") -> bool:
    """O an ilgili borsa acik mi?

    Kapali sayilan haller: hafta sonu, TR resmi tatili, seans disi saat.
    market: "bist" (10:00-18:00) | "abd"/"us" (16:30-23:00 IST).

    NOT: TR tatil takvimi ABD icin de uygulanmaz — ABD tarafinda yalniz hafta
    sonu + seans saati bakilir (NYSE tatilleri ayri bir liste, bkz.
    commentary._piyasa_tatilleri; buradaki kontrol cache tazeligi icin yeterli).
    """
    now = now or datetime.now(TZ)
    if now.weekday() >= 5:                 # Cumartesi/Pazar
        return False
    hm = now.hour * 60 + now.minute
    if market in ("abd", "us"):
        return _ABD_ACILIS <= hm <= _ABD_KAPANIS
    if tatil_mi(now):                      # TR resmi tatili -> BIST kapali
        return False
    return _BIST_ACILIS <= hm <= _BIST_KAPANIS
