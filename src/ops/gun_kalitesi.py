"""Gun veri kalitesi siniflama + karar damgalama + temiz karne hesabi.

Amac: her karar gununu veri-kaynagi sagligina gore TEMIZ / KISMI / KIRLI olarak
etiketler ve decisions.gun_kalitesi kolonuna yazar. Karne (basari orani) hesabi
KIRLI gunlerin kararlarini HARIC tutar (kayit silinmez, yalniz istatistikten cikar).

SINIFLAMA (12 Tem 2026 denetimi):
  KIRLI : o gun BIST brifingi cokuk -> BIST'ten <20 karar uretildi VEYA watchlist'in
          >%30'u "hazirlik HATA" ile atlandi (7-10 Tem 2026 pstdev-NaN cokusu gibi).
  KISMI : watchlist'in %10-30'u atlandi VEYA KAP o gun kopuktu (ORNEK/erisilemedi)
          VEYA (ABD kararlari) 10 Tem 2026 oncesi -> haber taramasi yalniz 3-5 hisseydi.
  TEMIZ : geri kalani (ana kaynaklar calisiyordu).

Skip orani + KAP durumu loglardan (logs/briefing.log) cikarilir; log yoksa yalniz
DB sinyaliyle (BIST karar sayisi) siniflanir. Bu modul KARAR KURALLARINA DOKUNMAZ;
sadece kayit/etiket tutar.

Kullanim:
  python -m src.ops.gun_kalitesi backfill   # 20 Haz'dan bugune tum gunleri damgala
  python -m src.ops.gun_kalitesi karne       # temiz karne ozetini yazdir
  python -m src.ops.gun_kalitesi damgala 2026-07-10
"""
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")
_LOG = Path(__file__).resolve().parents[2] / "logs" / "briefing.log"

# ABD haber taramasi 10 Tem 2026'da 4->16 hisseye cikti; oncesi dar kapsam.
_ABD_TAM_KAPSAM = "2026-07-10"
_US_SET = {"NVDA", "QQQ", "VOO", "SPCX", "RXT", "CNCK", "ACHR", "AMD", "ASML",
           "BFLY", "IONQ", "MU", "OSS", "RGTI", "RKLB", "TSM", "GMSTR.F"}


def _is_us(ticker: str) -> bool:
    return (ticker or "").upper().replace(".IS", "") in _US_SET


def _log_gun_istatistik() -> dict:
    """briefing.log'u BIST kosusuna gore ayristirir.
    Doner: {tarih: {"taranan": int, "hata": int, "kap": "CANLI"|"ORNEK"|"?"}}."""
    out = {}
    cur = None
    if not _LOG.exists():
        return out
    with open(_LOG, encoding="utf-8", errors="replace") as f:
        for ln in f:
            m = re.search(r"\[(\d{4}-\d{2}-\d{2}) \d{2}:\d{2}\] Sabah brifingi - "
                          r"hedef secimi \(bist\)", ln)
            if m:
                cur = m.group(1)
                out.setdefault(cur, {"taranan": 0, "hata": 0, "kap": "?"})
                continue
            if cur is None:
                continue
            mt = re.search(r"taranan=(\d+)", ln)
            if mt and out[cur]["taranan"] == 0:
                out[cur]["taranan"] = int(mt.group(1))
            if "hazirlik HATA" in ln:
                out[cur]["hata"] += 1
            if "KAP CANLI" in ln:
                out[cur]["kap"] = "CANLI"
            elif "KAP erisilemedi" in ln or "ORNEK kaynak" in ln:
                out[cur]["kap"] = "ORNEK"
    return out


def _gun_sinifi(bist_count: int, taranan: int, hata: int, kap: str):
    """BIST tarafi gun sagligini siniflar (TEMIZ/KISMI/KIRLI) + kisa sebep.
    NOT: "<20 karar" KIRLI kurali yalniz BIR BRIFING KOSTUYSA (taranan>0) uygulanir;
    aksi halde (hafta sonu/tatil, brifing loglanmamis) dusuk karar sayisi 'kirli veri'
    degil 'dusuk aktivite'dir -> TEMIZ (task: 'o gun BRIFINGTE atlandi')."""
    oran = (hata / taranan * 100) if taranan else None
    kap_kopuk = (kap == "ORNEK")
    brifing_kostu = taranan > 0
    if brifing_kostu and (bist_count < 20 or (oran is not None and oran > 30)):
        sebep = (f"BIST'ten {bist_count} karar (<20)" if bist_count < 20
                 else f"watchlist'in %{oran:.0f}'i atlandi (>%30)")
        return "KIRLI", sebep
    if (oran is not None and 10 <= oran <= 30) or kap_kopuk:
        sebep = (f"KAP kopuk (ORNEK)" if kap_kopuk
                 else f"watchlist'in %{oran:.0f}'i atlandi (%10-30)")
        return "KISMI", sebep
    return "TEMIZ", "ana kaynaklar calisiyordu"


def _karar_sinifi(is_us: bool, tarih: str, gun_sinif: str) -> str:
    """Tek karar icin nihai sinif. ABD kararlari 10 Tem oncesi en az KISMI (dar
    haber kapsami); gun zaten KIRLI ise KIRLI kalir (yukseltme yok)."""
    if is_us and tarih < _ABD_TAM_KAPSAM and gun_sinif == "TEMIZ":
        return "KISMI"
    return gun_sinif


def siniflandir(tarih: str, log_ist: dict = None) -> dict:
    """Bir gunun BIST sinifini + sebebini doner (ABD kaydi ayri hesaplanir).
    Doner: {"gun_sinif": ..., "sebep": ..., "bist_count": int}."""
    from src.db import database as db
    if log_ist is None:
        log_ist = _log_gun_istatistik()
    with db.get_conn() as c:
        rows = [r[0] for r in c.execute(
            "SELECT ticker FROM decisions WHERE tarih=?", (tarih,))]
    bist_count = sum(1 for t in rows if not _is_us(t))
    li = log_ist.get(tarih, {})
    gun_sinif, sebep = _gun_sinifi(bist_count, li.get("taranan", 0),
                                   li.get("hata", 0), li.get("kap", "?"))
    return {"gun_sinif": gun_sinif, "sebep": sebep, "bist_count": bist_count}


def damgala(tarih: str, log_ist: dict = None, alert: bool = True,
            verbose: bool = True) -> dict:
    """Bir gunun tum kararlarini gun_kalitesi ile damgalar. Gun KIRLI'ye YENI
    donduyse (onceden KIRLI degilse) admin'e Telegram uyarisi (alert=True).
    Doner: {"tarih", "gun_sinif", "sebep", "damgalanan", "kirli_karar"}."""
    from src.db import database as db
    s = siniflandir(tarih, log_ist=log_ist)
    gun_sinif = s["gun_sinif"]
    with db.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, ticker, gun_kalitesi FROM decisions WHERE tarih=?", (tarih,))]
        onceden_kirli = any(r.get("gun_kalitesi") == "KIRLI" for r in rows)
        damgalanan = kirli = 0
        for r in rows:
            k = _karar_sinifi(_is_us(r["ticker"]), tarih, gun_sinif)
            c.execute("UPDATE decisions SET gun_kalitesi=? WHERE id=?", (k, r["id"]))
            damgalanan += 1
            if k == "KIRLI":
                kirli += 1
    if verbose:
        print(f"  {tarih}: {gun_sinif} ({s['sebep']}) -> {damgalanan} karar damgalandi "
              f"({kirli} KIRLI)")
    # Yeni KIRLI gun -> admin bildir (yalniz gunun kendisi KIRLI ve daha once degilse)
    yeni_kirli = (gun_sinif == "KIRLI" and not onceden_kirli)
    if alert and yeni_kirli and kirli:
        try:
            from src.notify import telegram
            telegram.notify_admins(
                f"{tarih} kirli veri gunu olarak isaretlendi ({s['sebep']}) — "
                f"o gunun {kirli} karari karne hesabindan cikarildi.")
        except Exception as e:
            if verbose:
                print(f"  [alarm] telegram gonderilemedi: {type(e).__name__}")
    return {"tarih": tarih, "gun_sinif": gun_sinif, "sebep": s["sebep"],
            "damgalanan": damgalanan, "kirli_karar": kirli, "yeni_kirli": yeni_kirli}


def backfill_hepsi(alert: bool = False, verbose: bool = True) -> dict:
    """20 Haz 2026'dan bugune tum karar gunlerini damgalar. Backfill'de gun basi
    Telegram atmaz (alert=False); sonda TEK ozet uyarisi gonderir."""
    from src.db import database as db
    db.init_db()
    log_ist = _log_gun_istatistik()
    with db.get_conn() as c:
        gunler = [r[0] for r in c.execute(
            "SELECT DISTINCT tarih FROM decisions WHERE tarih>='2026-06-20' "
            "ORDER BY tarih")]
    if verbose:
        print(f"[gun_kalitesi] {len(gunler)} gun damgalanacak")
    kirli_gunler = []
    for g in gunler:
        r = damgala(g, log_ist=log_ist, alert=False, verbose=verbose)
        if r["gun_sinif"] == "KIRLI":
            kirli_gunler.append((g, r["kirli_karar"], r["sebep"]))
    if alert and kirli_gunler:
        try:
            from src.notify import telegram
            satirlar = "\n".join(f"• {g}: {n} karar ({s})" for g, n, s in kirli_gunler)
            telegram.notify_admins(
                f"Geriye donuk veri kalitesi damgalandi. {len(kirli_gunler)} KIRLI gun "
                f"karne hesabindan cikarildi:\n{satirlar}")
        except Exception as e:
            if verbose:
                print(f"  [alarm] telegram gonderilemedi: {type(e).__name__}")
    if verbose:
        print(f"[gun_kalitesi] KIRLI gun sayisi: {len(kirli_gunler)}")
    return {"gun": len(gunler), "kirli_gun": len(kirli_gunler),
            "kirli_gunler": kirli_gunler}


# --- KARNE (basari orani) - KIRLI HARIC ---
def _grup(karar: str) -> str:
    k = (karar or "").upper()
    if k.startswith("AL"):
        return "AL"
    if "TUT" in k:
        return "TUT"
    if "BEKLE" in k:
        return "BEKLE"
    if "SAT" in k or "UZAK" in k or "AZALT" in k or "VETO" in k:
        return "SAT/UZAK_DUR"
    return "DIGER"


def karne_ozet(verbose: bool = True) -> dict:
    """Degerlendirilmis kararlarin karar-tipi bazli basari orani. KIRLI gunler
    HARIC tutulur (kayit silinmez). Doner: {gruplar, dahil, haric, kismi_dahil}."""
    from src.db import database as db
    db.init_db()
    with db.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT karar, sonuc, gun_kalitesi FROM decisions "
            "WHERE tarih>='2026-06-20'")]

    def _dogru(sonuc):
        s = (sonuc or "").upper()
        if "YANLIS" in s or "YANLIŞ" in s:
            return False
        if "DOGRU" in s or "DOĞRU" in s:
            return True
        return None

    gruplar = {}       # grup -> {"dahil":[d,t], "tum":[d,t]}
    dahil = haric = kismi = 0
    for r in rows:
        d = _dogru(r["sonuc"])
        if d is None:                       # degerlendirilmemis
            continue
        gk = r.get("gun_kalitesi")
        g = _grup(r["karar"])
        ga = gruplar.setdefault(g, {"tum": [0, 0], "dahil": [0, 0]})
        ga["tum"][1] += 1
        if d:
            ga["tum"][0] += 1
        if gk == "KIRLI":                   # karne HARICI
            haric += 1
            continue
        dahil += 1
        if gk == "KISMI":
            kismi += 1
        ga["dahil"][1] += 1
        if d:
            ga["dahil"][0] += 1

    if verbose:
        print(f"\n=== TEMIZ KARNE (KIRLI gunler haric) ===")
        print(f"{'KARAR':13} {'DAHIL n':>7} {'DAHIL %':>8} {'TUM n':>6} {'TUM %':>7} {'FARK':>7}")
        for g in ("AL", "BEKLE", "TUT", "SAT/UZAK_DUR", "DIGER"):
            ga = gruplar.get(g)
            if not ga:
                continue
            dp = (100 * ga["dahil"][0] / ga["dahil"][1]) if ga["dahil"][1] else 0
            tp = (100 * ga["tum"][0] / ga["tum"][1]) if ga["tum"][1] else 0
            print(f"{g:13} {ga['dahil'][1]:>7} {dp:>7.1f}% {ga['tum'][1]:>6} {tp:>6.1f}% "
                  f"{dp - tp:>+6.1f}")
        print(f"\nHesaba katilan: {dahil} karar (temiz+kismi; bunun {kismi}'i kismi) | "
              f"Haric tutulan: {haric} karar (kirli gun)")
    return {"gruplar": gruplar, "dahil": dahil, "haric": haric, "kismi_dahil": kismi}


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "backfill"
    if arg == "backfill":
        backfill_hepsi(alert=("--alert" in sys.argv))
        karne_ozet()
    elif arg == "karne":
        karne_ozet()
    elif arg == "damgala" and len(sys.argv) > 2:
        damgala(sys.argv[2])
    else:
        print("kullanim: backfill [--alert] | karne | damgala <YYYY-MM-DD>")
