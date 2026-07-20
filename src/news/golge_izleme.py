"""GÖLGE izleme + otomatik canliya-alma esigi (21 Tem 2026).

CANLI KARARA ETKI ETMEZ. golge_kurallar.golge_karar_v2'nin (Is 2/3 yeni-katalizor
ayrimi) biriken GERCEK isabetini olcer ve veri esigi dolunca admin'e Telegram atar.

Onemli tasarim: AYRI TABLO / PERSIST YOK. haber_sinyal tablosu her sinyali zaten
fiyat_sinyal + sonuc + getiri_yuzde ile sakliyor; v2 karari da sakl
kolonlardan (yon/guc/fiyatlanmislik_sayisal/fiyat_hareket_yuzde/baslik) TURETILIR.
Yani v2 performansi saf bir OKUMA — her gun sonuclandir() olgunlastikca guncel.

Esik: >=15 olgun v2 sinyali VE isabet >=%70 VE ort getiri >0 -> "hazir" (Telegram:
onay iste). Olgun>=15 ama isabet dusuk -> "dusuk" (gozden gecir). Aksi -> sessiz.
HICBIR golge kural burada canliya ALINMAZ — yalniz izleme + haber verme.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_STATE = ROOT / "data" / "golge_v2_state.json"

MIN_OLGUN = 15          # otomatik esik: en az bu kadar OLGUN (getirisi olculmus) v2 sinyali
MIN_ISABET = 70.0       # isabet orani esigi (%)
ISABET_GETIRI = 1.5     # ertesi gun +%1.5 ustu = isabet (haber_sinyal._ISABET_ESIK ile ayni)


def ozet_v2() -> dict:
    """golge_karar_v2 AL/AL_KISMI sinyallerinin biriken isabeti (CANLI DEGIL).
    Doner: olgun, isabet, iskalama, isabet_oran(%), ort_getiri, kalan, bugun."""
    from src.db import database as db
    from src.news import golge_kurallar as gk
    out = {"olgun": 0, "isabet": 0, "iskalama": 0, "isabet_oran": None,
           "ort_getiri": None, "kalan": MIN_OLGUN, "bugun": 0, "durum": "birikmemis"}
    try:
        with db.get_conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT tarih,ticker,konu,baslik,yon,guc,fiyatlanmislik_sayisal,"
                "fiyat_hareket_yuzde,sonuc,getiri_yuzde FROM haber_sinyal")]
    except Exception:
        return out
    try:
        from src.news.haber_sinyal import _bugun
        bugun = _bugun()
    except Exception:
        bugun = None
    getiler = []
    for d in rows:
        # TEMIZ v2: yalniz DETERMINISTIK fiyatlanmislik olcumu CALISMIS sinyaller
        # sayilir. Eski donem (fiyatlanmislik_sayisal=None) sinyallerinde v2
        # mantigi eksik calisir (varsayilan AL) -> gercek v2 performansini
        # kirletir; ELENIR. Boylece esik, v2'nin GERCEK isabetini olcer.
        if d.get("fiyatlanmislik_sayisal") is None:
            continue
        v2, _ = gk.golge_karar_v2(d["yon"], d["guc"], d["fiyatlanmislik_sayisal"],
                                  d["fiyat_hareket_yuzde"], d["baslik"])
        if v2 not in ("AL", "AL_KISMI"):
            continue
        if bugun and d["tarih"] == bugun:
            out["bugun"] += 1
        g = d.get("getiri_yuzde")
        if g is None:
            continue                          # henuz olgunlasmadi
        out["olgun"] += 1
        getiler.append(g)
        if g >= ISABET_GETIRI:
            out["isabet"] += 1
        elif g <= -ISABET_GETIRI:
            out["iskalama"] += 1
    if getiler:
        out["ort_getiri"] = round(sum(getiler) / len(getiler), 2)
        out["isabet_oran"] = round(out["isabet"] / out["olgun"] * 100, 1)
    out["kalan"] = max(0, MIN_OLGUN - out["olgun"])
    out["durum"] = _durum(out)
    return out


def _durum(o: dict) -> str:
    if o["olgun"] < MIN_OLGUN:
        return "birikmemis"
    if (o["isabet_oran"] or 0) >= MIN_ISABET and (o["ort_getiri"] or 0) > 0:
        return "hazir"
    return "dusuk"


def kart_satiri(o: dict = None) -> str:
    """Is 4: aksam saglik karnesi tek satir ozet."""
    o = o or ozet_v2()
    if o["olgun"] == 0:
        return (f"Gölge haber kuralı v2: bugün {o['bugun']} sinyal, "
                f"henüz olgunlaşan yok, canlıya {MIN_OLGUN} kaldı.")
    return (f"Gölge haber kuralı v2: bugün {o['bugun']} sinyal, biriken isabet "
            f"{o['isabet']}/{o['olgun']} (%{o['isabet_oran'] or 0:.0f}), "
            f"ort {'+' if (o['ort_getiri'] or 0) >= 0 else ''}%{o['ort_getiri'] or 0}, "
            f"canlıya {o['kalan']} sinyal kaldı.")


def esik_bildir(force: bool = False, verbose: bool = False) -> dict:
    """Is 2: otomatik canliya-alma esigi. Durum DEGISTIYSE admin'e Telegram atar
    (ayni durumda tekrar atmaz — state dosyasi). Canliya ALMAZ, yalniz haber verir."""
    o = ozet_v2()
    d = o["durum"]
    prev = {}
    try:
        prev = json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    if not force and prev.get("son_durum") == d:
        return {"durum": d, "bildirim": False, "ozet": o}

    mesaj = prefix = None
    if d == "hazir":
        prefix = "✅"
        mesaj = (f"✅ Gölge haber kuralı v2 olgunlaştı: {o['olgun']} sinyal, "
                 f"%{o['isabet_oran']:.0f} isabet, ort +%{o['ort_getiri']}. "
                 f"Canlıya almaya hazır — onayın?")
    elif d == "dusuk":
        prefix = "⚠️"
        mesaj = (f"⚠️ Gölge haber kuralı v2 olgunlaştı ({o['olgun']} sinyal) ama "
                 f"isabet düşük (%{o['isabet_oran']:.0f}, ort %{o['ort_getiri']}). "
                 f"Canlıya ALINMADI — gözden geçir.")
    if mesaj is None:
        return {"durum": d, "bildirim": False, "ozet": o}   # birikmemis -> sessiz

    gonderildi = False
    try:
        from src.notify import telegram
        telegram.notify_admins(mesaj, prefix=prefix)
        gonderildi = True
    except Exception as e:
        if verbose:
            print(f"[golge_izleme] telegram gonderilemedi: {type(e).__name__}")
    try:
        _STATE.write_text(json.dumps({"son_durum": d}), encoding="utf-8")
    except Exception:
        pass
    return {"durum": d, "bildirim": gonderildi, "ozet": o}


if __name__ == "__main__":
    import sys
    o = ozet_v2()
    print(kart_satiri(o))
    print(f"durum={o['durum']} | olgun={o['olgun']} isabet_oran={o['isabet_oran']} "
          f"ort_getiri={o['ort_getiri']} kalan={o['kalan']}")
    if "--bildir" in sys.argv:
        print(esik_bildir(force=("--force" in sys.argv), verbose=True))
