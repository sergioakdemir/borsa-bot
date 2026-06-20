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
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ad          TEXT NOT NULL UNIQUE,
    telegram_id INTEGER
);
CREATE TABLE IF NOT EXISTS portfoy (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kullanici_id  INTEGER NOT NULL REFERENCES kullanici(id),
    ticker        TEXT NOT NULL,
    adet          REAL NOT NULL,
    alim_fiyati   REAL NOT NULL,
    alim_tarihi   TEXT,
    notlar        TEXT,
    para_birimi   TEXT DEFAULT 'TL'
);
CREATE INDEX IF NOT EXISTS ix_portfoy_kullanici ON portfoy(kullanici_id);
CREATE TABLE IF NOT EXISTS decisions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker    TEXT NOT NULL,
    karar     TEXT NOT NULL,
    puan      INTEGER,
    risk      INTEGER,
    eminlik   TEXT,
    gerekce   TEXT,
    tarih     TEXT NOT NULL,
    sonuc     TEXT
);
CREATE INDEX IF NOT EXISTS ix_decisions_ticker_tarih ON decisions(ticker, tarih);
"""


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(c) -> None:
    """Eski DB'lere eksik kolonlari ekler (idempotent)."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(kullanici)")}
    if "telegram_id" not in cols:
        c.execute("ALTER TABLE kullanici ADD COLUMN telegram_id INTEGER")
    cols_p = {r["name"] for r in c.execute("PRAGMA table_info(portfoy)")}
    if "para_birimi" not in cols_p:
        c.execute("ALTER TABLE portfoy ADD COLUMN para_birimi TEXT DEFAULT 'TL'")


def init_db() -> None:
    with get_conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)


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


def set_telegram_id(ad, telegram_id) -> None:
    init_db()
    with get_conn() as c:
        c.execute("UPDATE kullanici SET telegram_id=? WHERE ad=?",
                  (telegram_id, ad))


def get_user_by_telegram_id(telegram_id):
    init_db()
    with get_conn() as c:
        r = c.execute("SELECT * FROM kullanici WHERE telegram_id=?",
                      (telegram_id,)).fetchone()
        return dict(r) if r else None


# ---- portfoy ----
def add_position(kullanici_id, ticker, adet, alim_fiyati, alim_tarihi=None,
                 notlar="", para_birimi="TL"):
    init_db()
    with get_conn() as c:
        c.execute(
            """INSERT INTO portfoy
                 (kullanici_id, ticker, adet, alim_fiyati, alim_tarihi, notlar, para_birimi)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (kullanici_id, str(ticker).upper().replace(".IS", ""),
             adet, alim_fiyati, alim_tarihi, notlar, (para_birimi or "TL").upper()))


def user_id_by_ad(ad):
    init_db()
    with get_conn() as c:
        r = c.execute("SELECT id FROM kullanici WHERE LOWER(ad)=LOWER(?)",
                      (str(ad),)).fetchone()
        return r["id"] if r else None


def list_portfolio(kullanici_id=None) -> list[dict]:
    init_db()
    with get_conn() as c:
        if kullanici_id is not None:
            q = "SELECT * FROM portfoy WHERE kullanici_id=? ORDER BY id"
            return [dict(r) for r in c.execute(q, (kullanici_id,))]
        return [dict(r) for r in c.execute("SELECT * FROM portfoy ORDER BY kullanici_id, id")]


# ---- karar gunlugu (decisions) ----
def record_decision(ticker, karar, puan=None, risk=None, eminlik=None,
                    gerekce=None, tarih=None, sonuc=None) -> int:
    """Bir AL/TUT/SAT kararini gunluge yazar. sonuc ileride doldurulur (None)."""
    init_db()
    tarih = tarih or datetime.now(_TZ).date().isoformat()
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO decisions (ticker, karar, puan, risk, eminlik, gerekce, tarih, sonuc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(ticker).upper().replace(".IS", ""), karar, puan, risk,
             eminlik, gerekce, tarih, sonuc))
        return cur.lastrowid


def list_decisions(limit: int = 100) -> list[dict]:
    init_db()
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))]


def set_decision_outcome(decision_id, sonuc) -> None:
    init_db()
    with get_conn() as c:
        c.execute("UPDATE decisions SET sonuc=? WHERE id=?", (sonuc, decision_id))


if __name__ == "__main__":
    seed_default_sources()
    seed_users()
    print(f"DB: {DB_PATH}\n")
    print("Kullanicilar:", [u["ad"] for u in list_users()])
    print("Kaynaklar    :", [s["ad"] for s in list_sources()])


# ---- genel ayarlar (key-value) ----
_AYAR_SCHEMA = "CREATE TABLE IF NOT EXISTS ayar (anahtar TEXT PRIMARY KEY, deger TEXT)"


def _ensure_ayar():
    with get_conn() as c:
        c.execute(_AYAR_SCHEMA)


def get_setting(anahtar, default=None):
    _ensure_ayar()
    with get_conn() as c:
        r = c.execute("SELECT deger FROM ayar WHERE anahtar=?", (anahtar,)).fetchone()
        return r["deger"] if r else default


def set_setting(anahtar, deger):
    _ensure_ayar()
    with get_conn() as c:
        c.execute("INSERT INTO ayar (anahtar, deger) VALUES (?, ?) "
                  "ON CONFLICT(anahtar) DO UPDATE SET deger=excluded.deger",
                  (anahtar, str(deger)))
