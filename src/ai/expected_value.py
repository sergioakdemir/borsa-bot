"""Beklenen Değer (Expected Value) motoru — AL kararlarını EV'ye göre sıralar.

    EV = hit_rate * ort_kazanc - (1 - hit_rate) * abs(ort_kayip)

hit_rate: kararın isabet olasılığı (kazanma oranı).
ort_kazanc / ort_kayip: yüzde cinsinden ortalama kazanç / kayıp büyüklüğü.

Karne (decisions) dolana kadar VARSAYILAN değerler kullanılır:
  hit_rate=0.55, ort_kazanc=%3, ort_kayip=%2  ->  EV = +0.75
Yeterli değerlendirilmiş AL kararı biriktikçe SEKTÖR BAZLI gerçek istatistiğe
(o yoksa tüm AL kararlarından hesaplanan genel istatistiğe) güncellenir.

Kaynak öncelik sırası: sektör(gerçek) -> genel(gerçek) -> varsayılan.

Kullanım (morning.py):
    ist = ev.sektor_istatistikleri()
    genel = ev.genel_istatistik()
    p = ev.karar_ev("ASELS", guncel=120, hedef=132, stop=114,
                    sektor_ist=ist, genel_ist=genel)
    #  -> {"ev": 1.2, "hit_rate": .58, "ort_kazanc": 10.0, "ort_kayip": 5.0, ...}
"""
from src.ai.learning import _sektor_of

VARSAYILAN_HIT = 0.55
VARSAYILAN_KAZANC = 3.0        # %
VARSAYILAN_KAYIP = 2.0         # %
MIN_ORNEK = 8                  # sektör/genel gerçek istatistik için gereken min değerlendirilmiş AL
_LIMIT = 5000                  # decisions taramasında üst sınır


def ev(hit_rate: float, ort_kazanc: float, ort_kayip: float) -> float:
    """Temel EV formülü. ort_kayip mutlak (pozitif) beklenir."""
    return round(hit_rate * ort_kazanc - (1 - hit_rate) * abs(ort_kayip), 3)


def _istatistik(degisimler) -> dict | None:
    """AL kararlarının ilk-gün % değişim listesinden hit_rate + ortalama kazanç/kayıp."""
    xs = [d for d in degisimler if isinstance(d, (int, float))]
    if not xs:
        return None
    kaz = [d for d in xs if d > 0]
    kay = [d for d in xs if d <= 0]
    hit = len(kaz) / len(xs)
    ort_kaz = (sum(kaz) / len(kaz)) if kaz else VARSAYILAN_KAZANC
    ort_kay = abs(sum(kay) / len(kay)) if kay else VARSAYILAN_KAYIP
    return {
        "hit_rate": round(hit, 3),
        "ort_kazanc": round(ort_kaz, 2),
        "ort_kayip": round(ort_kay, 2),
        "ev": ev(hit, ort_kaz, ort_kay),
        "n": len(xs),
    }


def _al_degisimleri():
    """decisions tablosundan AL kararlarını (ilk_gun_degisim ile) sektöre göre gruplar.
    Döner: (sektor -> [degisim...], tum_degisimler)."""
    from src.db import database as db
    try:
        rows = db.list_decisions(limit=_LIMIT)
    except Exception:
        return {}, []
    grup, tum = {}, []
    for r in rows:
        if "AL" not in (r.get("karar") or "").upper():
            continue
        d = r.get("ilk_gun_degisim")
        if not isinstance(d, (int, float)):
            continue
        sek = _sektor_of(r.get("ticker")) or "Diğer"
        grup.setdefault(sek, []).append(d)
        tum.append(d)
    return grup, tum


def sektor_istatistikleri(min_ornek: int = MIN_ORNEK) -> dict:
    """Yeterli veri olan her sektör için gerçek EV istatistiği (yoksa boş dict)."""
    grup, _ = _al_degisimleri()
    out = {}
    for sek, ds in grup.items():
        if len(ds) < min_ornek:
            continue
        ist = _istatistik(ds)
        if ist:
            ist["kaynak"] = "gerçek-sektör"
            out[sek] = ist
    return out


def genel_istatistik(min_ornek: int = MIN_ORNEK) -> dict | None:
    """Tüm AL kararlarından tek bir gerçek istatistik (sektör verisi yetersizse yedek)."""
    _, tum = _al_degisimleri()
    if len(tum) < min_ornek:
        return None
    ist = _istatistik(tum)
    if ist:
        ist["kaynak"] = "gerçek-genel"
    return ist


def _varsayilan() -> dict:
    return {
        "hit_rate": VARSAYILAN_HIT, "ort_kazanc": VARSAYILAN_KAZANC,
        "ort_kayip": VARSAYILAN_KAYIP, "n": 0, "kaynak": "varsayılan",
        "ev": ev(VARSAYILAN_HIT, VARSAYILAN_KAZANC, VARSAYILAN_KAYIP),
    }


def ev_profili(ticker: str, sektor_ist: dict | None = None,
               genel_ist: dict | None = None) -> dict:
    """Bir hisse için hit_rate + ortalama kazanç/kayıp profili.
    Öncelik: sektör(gerçek) -> genel(gerçek) -> varsayılan."""
    sek = _sektor_of(ticker) or "Diğer"
    if sektor_ist and sek in sektor_ist:
        p = dict(sektor_ist[sek])
    elif genel_ist:
        p = dict(genel_ist)
    else:
        p = _varsayilan()
    p["sektor"] = sek
    return p


def karar_ev(ticker: str, guncel=None, hedef=None, stop=None,
             sektor_ist: dict | None = None, genel_ist: dict | None = None) -> dict:
    """Tek bir AL kararı için EV.

    hit_rate: sektör/genel/varsayılan istatistikten (yukarıdaki formül).
    ort_kazanc/ort_kayip: karara ÖZEL hedef/stop numerikse ondan (gerçek R/R),
      değilse sektör/varsayılan ortalamalar. Böylece EV hem istatistiksel hem
      hisseye özel olur.

    Döner: {ev, hit_rate, ort_kazanc, ort_kayip, sektor, kaynak}.
    """
    p = ev_profili(ticker, sektor_ist, genel_ist)
    hit = p["hit_rate"]
    kaz, kay = p["ort_kazanc"], p["ort_kayip"]
    kaynak = p["kaynak"]

    # Karara özel hedef/stop varsa gerçek yüzde büyüklüklerini kullan.
    if guncel and hedef and hedef > guncel:
        kaz = round((hedef - guncel) / guncel * 100, 2)
    if guncel and stop and stop < guncel:
        kay = round((guncel - stop) / guncel * 100, 2)
        kaynak = kaynak + "+hedef/stop"

    return {
        "ev": ev(hit, kaz, kay), "hit_rate": hit,
        "ort_kazanc": kaz, "ort_kayip": kay,
        "sektor": p["sektor"], "kaynak": kaynak, "n": p.get("n", 0),
    }


if __name__ == "__main__":
    import json
    ist = sektor_istatistikleri()
    genel = genel_istatistik()
    print("Sektör istatistikleri:", json.dumps(ist, ensure_ascii=False, indent=2))
    print("Genel istatistik:", json.dumps(genel, ensure_ascii=False))
    for tk in ("ASELS", "GARAN", "BIMAS"):
        print(tk, "->", json.dumps(
            karar_ev(tk, guncel=100, hedef=110, stop=95,
                     sektor_ist=ist, genel_ist=genel), ensure_ascii=False))
