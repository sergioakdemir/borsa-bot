"""Gunluk portfoy degeri snapshot'i (cron: her gece 23:30).

Her kullanicinin o gunku KAPANIS portfoy degerini (toplam TL + BIST/ABD ayrimi)
portfoy_snapshot tablosuna yazar. Bu kayitlar /api/portfolio'daki gunluk/haftalik/
aylik getiri hesabinin referansidir. Pozisyonu olmayan kullanici atlanir.
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


def run(verbose: bool = True) -> int:
    _load_dotenv()
    from src.db import database as db
    from src.web import app

    bugun = datetime.now(_TZ).date().isoformat()
    yazilan = 0
    for u in db.list_users():
        uid = u["id"]
        try:
            if not db.list_portfolio(uid):
                continue                       # pozisyonu olmayan kullaniciyi atla
            p = app.get_portfolio(u["ad"])
        except Exception as e:
            if verbose:
                print(f"  [{u['ad']}] atlandi: {type(e).__name__}: {str(e)[:80]}")
            continue
        ozet = p.get("ozet") or {}
        deger = ozet.get("deger")
        if deger is None:
            continue
        db.record_portfoy_snapshot(
            uid, bugun, round(float(deger), 2),
            ozet.get("bist_degeri"), ozet.get("abd_degeri"))
        yazilan += 1
        if verbose:
            print(f"  [{u['ad']}] {bugun}: toplam {deger:.2f} TL "
                  f"(BIST {ozet.get('bist_degeri')}, ABD {ozet.get('abd_degeri')})")

    now = datetime.now(_TZ)
    print(f"[{now:%Y-%m-%d %H:%M}] Portfoy snapshot: {yazilan} kullanici kaydedildi.")
    return yazilan


if __name__ == "__main__":
    run()
    sys.exit(0)
