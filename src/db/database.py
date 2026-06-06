"""SQLite veritabani.

Tablolar:
- kaynak_sicil : veri/haber kaynaklarinin durumu
- uyari_kayit  : gonderilen sicak uyarilar (spam onleme + haftalik ozet)
- kullanici    : kullanicilar (serhat, yigit, ufuk)
- portfoy      : kullanici bazli pozisyonlar

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
    tur             TEXT NOT NULL,
    durum           TEXT NOT NULL,
    aciklama        TEXT,
    son_erisim      TEXT,
    son_durum_notu  TEXT,
    eklenme         TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uyari_kayit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker    TEXT NOT NULL,
    tarih     TEXT NOT NULL,
    seviye    TEXT NOT NULL,
    degisim   REAL NOT NULL,
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_uyari_ticker_tarih ON uyari_kayit(ticker, tarih);
CREATE TABLE IF NOT EXISTS kullanici (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ad   TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS portfoy (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kullanici_id  INTEGER NOT NULL REFERENCES kullanici(id),
    ticker        TEXT NOT NULL,
    adet          REAL NOT NULL,
    alim_fiyati   REAL NOT NULL,
    alim_tarihi   TEXT,
    notlar        TEXT
);
CREATE INDEX IF NOT EXISTS ix_portfoy_kullanici ON portfoy(kullanici_id);
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


# ---- kaynak sicil ----
def register_source(ad, tur, durum="AKTIF", aciklama=""):
    init_db()
    with get_conn() as c:
        c.execute(
            """INSERT INTO kaynak_sicil (ad, tur, durum, aciklama, eklenme)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(ad) DO UPDATE SET
                 tur=excluded.tur, durum=excluded.durum, aciklama=excluded.aciklama""",
            (ad, tur, durum, aciklama, _now()))


def update_status(ad, durum, not_=""):
    with get_conn() as c:
        c.execute("UPDATE kaynak_sicil SET durum=?, son_durum_notu=?, son_erisim=? WHERE ad=?",
                  (durum, not_, _now(), ad))


def list_sources():
    init_db()
    with get_conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM kaynak_sicil ORDER BY tur, ad")]


def seed_default_sources():
    register_source("yfinance", "VERI", "AKTIF", "Birincil fiyat verisi (BIST .IS + ABD).")
    register_source("KAP", "HABER", "ERISILEMEZ",
                    "kap.org.tr bildirimleri; TR disi sunucudan API engelli.")


# ---- uyari kayitlari ----
def record_alert(ticker, tarih, seviye, degisim):
    init_db()
    with get_conn() as c:
        c.execute("INSERT INTO uyari_kayit (ticker, tarih, seviye, degisim, ts) VALUES (?,?,?,?,?)",
                  (ticker, tarih, seviye, degisim, _now()))


def alert_levels_today(ticker, tarih) -> list[str]:
    init_db()
    with get_conn() as c:
        return [r["seviye"] for r in c.execute(
            "SELECT seviye FROM uyari_kayit WHERE ticker=? AND tarih=?", (ticker, tarih))]


def alerts_between(start_tarih, end_tarih) -> list[dict]:
    init_db()
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM uyari_kayit WHERE tarih BETWEEN ? AND ? ORDER BY ts",
            (start_tarih, end_tarih))]


# ---- kullanici ----
def add_user(ad) -> int:
    init_db()
    with get_conn() as c:
        c.execute("INSERT OR IGNORE INTO kullanici (ad) VALUES (?)", (ad,))
        r = c.execute("SELECT id FROM kullanici WHERE ad=?", (ad,)).fetchone()
        return r["id"] if r else None


def list_users() -> list[dict]:
    init_db()
    with get_conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM kullanici ORDER BY id")]


def seed_users():
    for ad in ("serhat", "yigit", "ufuk"):
        add_user(ad)


# ---- portfoy ----
def add_position(kullanici_id, ticker, adet, alim_fiyati, alim_tarihi=None, notlar=""):
    init_db()
    with get_conn() as c:
        c.execute(
            """INSERT INTO portfoy (kullanici_id, ticker, adet, alim_fiyati, alim_tarihi, notlar)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (kullanici_id, str(ticker).upper().replace(".IS", ""),
             adet, alim_fiyati, alim_tarihi, notlar))


def list_portfolio(kullanici_id=None) -> list[dict]:
    init_db()
    with get_conn() as c:
        if kullanici_id is not None:
            q = "SELECT * FROM portfoy WHERE kullanici_id=? ORDER BY id"
            return [dict(r) for r in c.execute(q, (kullanici_id,))]
        return [dict(r) for r in c.execute("SELECT * FROM portfoy ORDER BY kullanici_id, id")]


if __name__ == "__main__":
    seed_default_sources()
    seed_users()
    print(f"DB: {DB_PATH}\n")
    print("Kullanicilar:", [u["ad"] for u in list_users()])
    print("Kaynaklar    :", [s["ad"] for s in list_sources()])
