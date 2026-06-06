"""SQLite veritabani ve kaynak sicil tablosu.

kaynak_sicil: kullanilan veri/haber kaynaklarinin sicili (durum, son erisim,
aciklama). Kaynaklarin guvenilirligini ve erisilebilirligini takip eder.

DB dosyasi: data/borsa.db (*.db .gitignore'da).
"""
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "borsa.db"
_TZ = ZoneInfo("Europe/Istanbul")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kaynak_sicil (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ad              TEXT NOT NULL UNIQUE,
    tur             TEXT NOT NULL,            -- VERI | HABER
    durum           TEXT NOT NULL,            -- AKTIF | ERISILEMEZ | DEVRE_DISI
    aciklama        TEXT,
    son_erisim      TEXT,
    son_durum_notu  TEXT,
    eklenme         TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as c:
        c.executescript(SCHEMA)


def register_source(ad: str, tur: str, durum: str = "AKTIF", aciklama: str = "") -> None:
    init_db()
    with get_conn() as c:
        c.execute(
            """INSERT INTO kaynak_sicil (ad, tur, durum, aciklama, eklenme)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(ad) DO UPDATE SET
                 tur=excluded.tur, durum=excluded.durum, aciklama=excluded.aciklama""",
            (ad, tur, durum, aciklama, _now()),
        )


def update_status(ad: str, durum: str, not_: str = "") -> None:
    with get_conn() as c:
        c.execute(
            "UPDATE kaynak_sicil SET durum=?, son_durum_notu=?, son_erisim=? WHERE ad=?",
            (durum, not_, _now(), ad),
        )


def list_sources() -> list[dict]:
    init_db()
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM kaynak_sicil ORDER BY tur, ad")]


def seed_default_sources() -> None:
    """Bilinen kaynaklari sicile isler."""
    register_source("yfinance", "VERI", "AKTIF",
                    "Birincil fiyat verisi (BIST .IS + ABD).")
    register_source("KAP", "HABER", "ERISILEMEZ",
                    "kap.org.tr bildirimleri; TR disi sunucudan API engelli.")
    update_status("yfinance", "AKTIF", "Calisiyor.")
    update_status("KAP", "ERISILEMEZ",
                  "Hetzner DE IP'sinden /tr/api/disclosures baglantisi resetleniyor.")


if __name__ == "__main__":
    seed_default_sources()
    print(f"DB: {DB_PATH}\n")
    print(f"{'AD':12s} {'TUR':6s} {'DURUM':12s} {'SON ERISIM':20s} ACIKLAMA")
    print("-" * 90)
    for s in list_sources():
        print(f"{s['ad']:12s} {s['tur']:6s} {s['durum']:12s} "
              f"{(s['son_erisim'] or '-'):20s} {s['aciklama'] or ''}")
