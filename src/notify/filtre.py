"""Bildirim geçit filtresi — should_notify().

KURAL
-----
PORTFÖYDEKİ hisseler (portfoy tablosunda ilgili kullanıcıda kayıtlı):
    her bildirim gönderilir (haber / fiyat / karar / hacim / ...). Değişiklik yok.

PORTFÖYDE OLMAYAN hisseler:
    yalnızca şu 3 durumda bildirim gönderilir, diğer her şey susturulur:
      1) karar = AL ve puan >= 8
      2) günlük fiyat değişimi (mutlak) >= %5   (yükseliş veya düşüş)
      3) KAP zorunlu (özel durum) bildirimi  VEYA  analist hedef fiyat değişimi >= %15

Kullanım:
    from src.notify import filtre
    if filtre.should_notify(ticker, user_id, "fiyat", gunluk_degisim=chg):
        telegram.send_message(...)

`user_id` None verilirse "herhangi bir kullanıcının portföyünde mi" diye bakılır;
böylece market geneli (broadcast) bildirimlerinde de kural uygulanabilir. Sıcak
döngülerde `portfoy=` ile önceden hesaplanmış ticker kümesi geçilebilir (her çağrıda
DB'ye gidilmesin diye).
"""
from __future__ import annotations

# Portföy dışı hisseler için istisna eşikleri
ESIK_PUAN = 8        # AL bildirimi için minimum puan
ESIK_DEGISIM = 5.0   # fiyat/hacim bildirimi için minimum |günlük %|
ESIK_HEDEF = 15.0    # analist hedef fiyat değişimi için minimum |%|


def _norm(ticker) -> str:
    """Sembolü karşılaştırma için normalize eder: 'thyao.is' -> 'THYAO'."""
    return (ticker or "").upper().replace(".IS", "").strip()


def portfoy_seti(user_id=None) -> set:
    """user_id verilirse o kullanıcının, verilmezse TÜM portföylerin normalize ticker
    kümesini döndürür. DB hatasında boş küme döner (güvenli taraf: filtre yine çalışır,
    yalnızca 3 istisna geçerli olur)."""
    try:
        from src.db import database as db
        return {_norm(r.get("ticker")) for r in db.list_portfolio(kullanici_id=user_id)
                if r.get("ticker")}
    except Exception:
        return set()


def in_portfolio(ticker, user_id=None, portfoy=None) -> bool:
    """ticker, ilgili portföyde mi? portfoy kümesi verilirse ondan, yoksa DB'den bakar."""
    uyeler = portfoy if portfoy is not None else portfoy_seti(user_id)
    return _norm(ticker) in uyeler


def should_notify(ticker, user_id, bildirim_tipi, *, portfoy=None,
                  puan=None, karar=None, gunluk_degisim=None,
                  kap_zorunlu=False, hedef_degisim=None) -> bool:
    """Bir bildirimin gönderilip gönderilmeyeceğini döndürür (True = gönder).

    ticker         : hisse sembolü
    user_id        : alıcının kullanici_id'si (None -> herhangi bir portföy)
    bildirim_tipi  : 'haber' | 'fiyat' | 'hacim' | 'karar' | 'kap' | 'hedef' | ...
    portfoy        : (ops.) önceden hesaplanmış normalize ticker kümesi (sıcak döngüler)
    puan           : karar puanı (karar bildirimi için)
    karar          : 'AL'/'SAT'/'BEKLE' (karar bildirimi için)
    gunluk_degisim : günlük yüzde değişim (fiyat/hacim bildirimi için)
    kap_zorunlu    : KAP zorunlu özel durum açıklaması mı (haber/kap bildirimi için)
    hedef_degisim  : analist hedef fiyat değişimi yüzdesi (hedef bildirimi için)
    """
    # Portföydeki hisse -> her bildirim geçer.
    if in_portfolio(ticker, user_id, portfoy):
        return True

    tip = (bildirim_tipi or "").lower()

    # 1) Karar: yalnızca AL ve puan >= 8
    if tip in ("karar", "decision"):
        return (karar or "").upper() == "AL" and (puan or 0) >= ESIK_PUAN

    # 2) Fiyat / hacim: yalnızca |günlük %| >= 5
    if tip in ("fiyat", "price", "hacim", "volume"):
        return gunluk_degisim is not None and abs(gunluk_degisim) >= ESIK_DEGISIM

    # 3a) KAP: yalnızca zorunlu özel durum açıklaması
    if tip in ("kap", "ozel_durum"):
        return bool(kap_zorunlu)

    # 3b) Analist hedef fiyat değişimi: yalnızca |%| >= 15
    if tip in ("hedef", "target", "hedef_degisim"):
        return hedef_degisim is not None and abs(hedef_degisim) >= ESIK_HEDEF

    # Haber (ve KAP haberi): portföy dışı yalnızca KAP zorunlu ya da büyük hedef değişimi.
    if tip in ("haber", "news"):
        return bool(kap_zorunlu) or (
            hedef_degisim is not None and abs(hedef_degisim) >= ESIK_HEDEF)

    # Bilinmeyen/diğer tipler: portföy dışı -> sustur (güvenli varsayılan).
    return False
