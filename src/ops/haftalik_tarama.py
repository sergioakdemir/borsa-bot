"""Haftalik derin sistem taramasi (cron: Pazar 20:00).

Amac: yavas/sessiz bozulmalari yakala. Gunluk karne 'anlik' durumu; bu tarama
7 gunluk desenleri ve yapisal butunlugu denetler:

  1. Fail-safe kontrolu: cift risk vetosu / EQ filtresi / stop motoru / bilanco freni
     hala bagli mi? (7 gunde kac kez tetiklendi; hic tetiklenmediyse supheli)
  2. Anomali: F/K>200 veya <0, ROE>%100 gibi cop veri AI'a giriyor mu? (freni aktif mi)
  3. Olu sembol: art arda >=3 gun veri getirmeyen sembol sayisi
  4. Pazar simetrisi: BIST'te calisan moduller (priced_in, yukselis hafizasi, haber)
     ABD'de de calisiyor mu?
  5. Karne butunlugu: KILL_SWITCH sizinti, duplicate kayit, kirli gun orani

Sonuc: "Sistem saglikli" veya bulunan sorunlarin listesi -> admin Telegram.
Karar KURALLARINA dokunmaz; yalniz denetim/raporlama.
Calistirma: python -m src.ops.haftalik_tarama [--print]
"""
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
_TZ = ZoneInfo("Europe/Istanbul")

_US = {"NVDA", "QQQ", "VOO", "SPCX", "RXT", "CNCK", "ACHR", "AMD", "ASML",
       "BFLY", "IONQ", "MU", "OSS", "RGTI", "RKLB", "TSM", "GMSTR.F"}


def _failsafe(c) -> list:
    """Fail-safe'lerin 7 gunluk tetiklenme izleri + kod-yolu baglanti kontrolu."""
    notlar, sorunlar = [], []
    veto = c.execute("SELECT COUNT(*) FROM decisions WHERE karar='VETO' "
                     "AND tarih>=date('now','-7 day')").fetchone()[0]
    kill = c.execute("SELECT COUNT(*) FROM decisions WHERE karar='KILL_SWITCH' "
                     "AND tarih>=date('now','-7 day')").fetchone()[0]
    notlar.append(f"cift risk vetosu (VETO): 7g {veto} kez")
    notlar.append(f"KILL_SWITCH (veri freni): 7g {kill} kez")
    # Kod yolu bagli mi? (import edilebiliyor mu)
    for ad, yol in (("EQ filtresi", "src.ai.entry_quality"),
                    ("stop motoru", "src.ai.stop_hedef"),
                    ("risk motoru", "src.ai.risk")):
        try:
            __import__(yol)
            notlar.append(f"{ad}: kod yolu BAGLI")
        except Exception as e:
            sorunlar.append(f"{ad} import edilemiyor ({type(e).__name__}) — KOPUK olabilir!")
    # Bilanco freni: commentary'de F/K>200|<0 filtresi hala var mi?
    try:
        src = (ROOT / "src" / "ai" / "commentary.py").read_text(encoding="utf-8")
        if "fk > 200" in src or "_fk > 200" in src:
            notlar.append("bilanco freni (F/K>200|<0): kodda AKTIF")
        else:
            sorunlar.append("bilanco freni (F/K>200 filtresi) commentary.py'de BULUNAMADI!")
    except Exception:
        pass
    if veto == 0 and kill == 0:
        sorunlar.append("7 gunde hic VETO/KILL yok — fail-safe'ler tetiklenmiyor olabilir (supheli).")
    return notlar, sorunlar


def _anomali(c) -> list:
    """Cop temel veri (F/K>200|<0, ROE>100) canli orneklemle taranir; freni bunlari
    AI'dan ONCE elemeli. Orneklem kucuk tutulur (hiz)."""
    notlar, sorunlar = [], []
    try:
        from src.news import fundamental_source as fs
        from src.watchlist import load_index
        ornek = load_index()[:12]
        anomali = 0
        for t in ornek:
            try:
                d = fs.get_fundamentals(t) if hasattr(fs, "get_fundamentals") else None
            except Exception:
                d = None
            if not d or not d.get("available"):
                continue
            fk = d.get("fk")
            roe = d.get("roe_%")
            if (fk is not None and (fk > 200 or fk < 0)) or (roe is not None and roe > 100):
                anomali += 1
        notlar.append(f"anomali orneklem: {len(ornek)} hissede {anomali} cop-deger "
                      f"(freni bunlari AI'dan eler)")
    except Exception as e:
        notlar.append(f"anomali taramasi atlandi ({type(e).__name__})")
    return notlar, sorunlar


def _olu_sembol(c) -> list:
    notlar, sorunlar = [], []
    son3 = [r[0] for r in c.execute(
        "SELECT DISTINCT tarih FROM decisions ORDER BY tarih DESC LIMIT 3")]
    if len(son3) < 3:
        return ["olu sembol: yeterli gun yok"], []
    from collections import Counter
    rows = c.execute("SELECT ticker FROM decisions WHERE tarih IN (?,?,?) "
                     "AND karar='KILL_SWITCH'", tuple(son3)).fetchall()
    cc = Counter(r[0] for r in rows)
    olu = [t for t, n in cc.items() if n >= 3]
    notlar.append(f"olu sembol (3 gun art arda KILL): {len(olu)} {olu if olu else ''}")
    if len(olu) > 3:
        sorunlar.append(f"{len(olu)} sembol 3 gundur veri getirmiyor: {olu}")
    return notlar, sorunlar


def _pazar_simetrisi(c) -> list:
    notlar, sorunlar = [], []
    yh_us = sum(1 for (t,) in c.execute("SELECT DISTINCT ticker FROM yukselis_hafizasi")
                if t in _US)
    he_us = sum(1 for (t,) in c.execute("SELECT DISTINCT ticker FROM haber_etki")
                if t in _US)
    notlar.append(f"yukselis hafizasi ABD kaydi: {yh_us}")
    notlar.append(f"haber_etki ABD kaydi: {he_us}")
    if he_us == 0:
        sorunlar.append("haber_etki'de hic ABD kaydi yok — ABD haber havuzu calismyor olabilir.")
    # priced_in modulu ABD'yi destekliyor mu (import + fonksiyon var mi)
    try:
        from src.news import priced_in  # noqa
        notlar.append("priced_in modulu: BAGLI")
    except Exception as e:
        sorunlar.append(f"priced_in import edilemiyor ({type(e).__name__})")
    return notlar, sorunlar


def _karne_butunlugu(c) -> list:
    notlar, sorunlar = [], []
    # KILL_SWITCH sizintisi: KILL kararina DOGRU/YANLIS sonuc yazilmis mi (olmamali)
    sizinti = c.execute(
        "SELECT COUNT(*) FROM decisions WHERE karar='KILL_SWITCH' "
        "AND (sonuc LIKE '%DOGRU%' OR sonuc LIKE '%YANLIS%')").fetchone()[0]
    notlar.append(f"KILL_SWITCH karne sizintisi: {sizinti}")
    if sizinti > 0:
        sorunlar.append(f"{sizinti} KILL_SWITCH karari basari hesabina sizmis (DEGERLENDIRME DISI olmali).")
    # Duplicate (ticker,tarih)
    dup = c.execute("SELECT COUNT(*) FROM (SELECT ticker,tarih FROM decisions "
                    "GROUP BY ticker,tarih HAVING COUNT(*)>1)").fetchone()[0]
    notlar.append(f"duplicate (ticker,tarih) kayit: {dup}")
    if dup > 0:
        sorunlar.append(f"{dup} mukerrer (ticker,tarih) karar kaydi var (UNIQUE index bozulmus?).")
    # Kirli gun orani
    tot = c.execute("SELECT COUNT(*) FROM decisions WHERE tarih>='2026-06-20'").fetchone()[0]
    kirli = c.execute("SELECT COUNT(*) FROM decisions WHERE gun_kalitesi='KIRLI' "
                      "AND tarih>='2026-06-20'").fetchone()[0]
    oran = (100 * kirli / tot) if tot else 0
    notlar.append(f"kirli gun karar orani: {kirli}/{tot} (%{oran:.1f})")
    if oran > 20:
        sorunlar.append(f"kirli gun karar orani yuksek (%{oran:.0f}) — veri kaynaklari sik cokuyor.")
    return notlar, sorunlar


def run(gonder: bool = True, verbose: bool = True) -> dict:
    from src.db import database as db
    db.init_db()
    bolumler = [
        ("FAIL-SAFE", _failsafe),
        ("ANOMALI", _anomali),
        ("OLU SEMBOL", _olu_sembol),
        ("PAZAR SIMETRISI", _pazar_simetrisi),
        ("KARNE BUTUNLUGU", _karne_butunlugu),
    ]
    tum_not, tum_sorun = [], []
    with db.get_conn() as c:
        for ad, fn in bolumler:
            try:
                notlar, sorunlar = fn(c)
            except Exception as e:
                notlar, sorunlar = [], [f"{ad} taramasi HATA: {type(e).__name__}: {str(e)[:60]}"]
            tum_not.append((ad, notlar, sorunlar))
            tum_sorun.extend(f"[{ad}] {s}" for s in sorunlar)

    satirlar = [f"🔍 HAFTALIK DERIN TARAMA — {datetime.now(_TZ):%Y-%m-%d %H:%M}"]
    for ad, notlar, sorunlar in tum_not:
        satirlar.append(f"\n▸ {ad}")
        for n in notlar:
            satirlar.append(f"  · {n}")
        for s in sorunlar:
            satirlar.append(f"  🔴 {s}")
    satirlar.append("─────")
    if tum_sorun:
        satirlar.append(f"SONUC: 🔴 {len(tum_sorun)} SORUN BULUNDU")
    else:
        satirlar.append("SONUC: ✅ Sistem saglikli — tum kontroller gecti.")
    mesaj = "\n".join(satirlar)
    if verbose:
        print(mesaj)
    if gonder:
        try:
            from src.notify import telegram
            telegram.notify_admins(mesaj, prefix="")
        except Exception as e:
            if verbose:
                print(f"[haftalik] telegram gonderilemedi: {type(e).__name__}")
    return {"sorun_sayisi": len(tum_sorun), "sorunlar": tum_sorun}


if __name__ == "__main__":
    run(gonder=("--print" not in sys.argv))
