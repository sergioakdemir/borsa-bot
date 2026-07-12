"""KILL_SWITCH karne sizintisi tamiri (idempotent bakim scripti).

Sorun: KILL_SWITCH kararlari veri eksikligiyle (AI cagrilmadan) uretilir ve
DOGRU/YANLIS istatistigine GIRMEMELIDIR (sonuc = 'DEGERLENDIRME DISI · veri
eksikligi'). Nadiren erken kayitlar yanlislikla fiyat degisimiyle degerlendirilip
'-X% · YANLIS' damgasi almis olabilir -> karne butunlugunu bozar.

haftalik_tarama._karne_butunlugu bu sizintiyi tespit eder. Bu script bulunan
sizinti kayitlarini 'DEGERLENDIRME DISI · veri eksikligi' yaparak duzeltir
(kayit SILINMEZ). Tekrar calistirilabilir; sizinti yoksa hicbir sey yapmaz.

12 Tem 2026: 3 kayit duzeltildi (SPCX/RXT/NVDA, 2026-06-21).
Calistirma: python -m scripts.fix_killswitch_sizinti   (veya venv/bin/python scripts/...)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_DEGERLENDIRME_DISI = "DEĞERLENDİRME DIŞI · veri eksikliği"


def run(verbose: bool = True) -> int:
    from src.db import database as db
    db.init_db()
    with db.get_conn() as c:
        rows = c.execute(
            "SELECT id, ticker, tarih, sonuc FROM decisions WHERE karar='KILL_SWITCH' "
            "AND (sonuc LIKE '%DOGRU%' OR sonuc LIKE '%YANLIS%') ORDER BY tarih, ticker"
        ).fetchall()
        if verbose:
            print(f"Sizinti KILL_SWITCH kaydi: {len(rows)}")
            for r in rows:
                print(f"  #{r[0]} {r[1]:6} {r[2]} | eski sonuc: {r[3]}")
        for r in rows:
            c.execute("UPDATE decisions SET sonuc=? WHERE id=?",
                      (_DEGERLENDIRME_DISI, r[0]))
    if verbose:
        print(f"-> {len(rows)} kayit '{_DEGERLENDIRME_DISI}' yapildi "
              f"(karne DOGRU/YANLIS hesabindan cikti).")
    return len(rows)


if __name__ == "__main__":
    run()
