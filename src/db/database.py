"""SQLite veritabani.

Tablolar:
- kaynak_sicil : veri/haber kaynaklarinin durumu
- uyari_kayit  : gonderilen sicak uyarilar (spam onleme + haftalik ozet)
- kullanici    : kullanicilar (serhat, yigit, ufuk)
- portfoy      : kullanici bazli pozisyonlar

DB dosyasi: data/borsa.db (*.db .gitignore'da).
"""
import json
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
    sonuc     TEXT,
    yanlis_sebep TEXT
);
CREATE INDEX IF NOT EXISTS ix_decisions_ticker_tarih ON decisions(ticker, tarih);
CREATE TABLE IF NOT EXISTS paper_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    karar           TEXT NOT NULL,
    fiyat           REAL NOT NULL,
    adet_sanal      REAL NOT NULL,
    tarih           TEXT NOT NULL,
    kapanis_fiyati  REAL,
    kz_yuzde        REAL,
    durum           TEXT NOT NULL DEFAULT 'acik',
    kapanis_tarihi  TEXT,
    para_birimi     TEXT DEFAULT 'TL'
);
CREATE INDEX IF NOT EXISTS ix_paper_ticker_durum ON paper_trades(ticker, durum);
CREATE TABLE IF NOT EXISTS haber_etki (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker           TEXT NOT NULL,
    haber_id         TEXT,
    haber_tarihi     TEXT,
    fiyat_haber_ani  REAL,
    fiyat_30dk       REAL,
    fiyat_2saat      REAL,
    fiyat_1gun       REAL,
    etki_yuzde_1gun  REAL,
    haber_kategori   TEXT,
    baslik           TEXT,
    olusturma        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_haber_etki_hid ON haber_etki(haber_id);
CREATE TABLE IF NOT EXISTS kullanici_profil (
    kullanici_id        INTEGER PRIMARY KEY REFERENCES kullanici(id),
    portfoy_buyuklugu   REAL,
    aylik_birikim       REAL,
    ek_sermaye_mumkun   INTEGER,
    tecrube_seviyesi    TEXT,
    risk_toleransi      TEXT,
    panik_egilimi       TEXT,
    yatirim_vadesi      TEXT,
    nakit_ihtiyaci      TEXT,
    nakit_ihtiyac_tarihi TEXT,
    ana_hedef           TEXT,
    kayip_toleransi_yuzde REAL,
    ogrenme_seviyesi    TEXT,
    aciklama_ister      INTEGER,
    dusus_tepkisi_10    TEXT,
    dusus_tepkisi_20    TEXT,
    sektor_tercihi      TEXT,
    gunluk_takip_saat   REAL,
    ana_korku           TEXT,
    onceki_basari       TEXT,
    risk_tercihi        TEXT,
    profil_guven_skoru  INTEGER DEFAULT 0,
    eksik_alanlar       TEXT,
    notlar              TEXT,
    guncelleme_tarihi   TEXT
);
CREATE TABLE IF NOT EXISTS kullanici_hafiza (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kullanici_id  INTEGER NOT NULL REFERENCES kullanici(id),
    tip           TEXT NOT NULL,
    icerik        TEXT,
    tarih         TEXT NOT NULL,
    ticker        TEXT,
    sonuc         TEXT
);
CREATE INDEX IF NOT EXISTS ix_hafiza_kullanici ON kullanici_hafiza(kullanici_id, tip);
CREATE TABLE IF NOT EXISTS model_portfoy (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    adet            REAL NOT NULL,
    alis_fiyati     REAL NOT NULL,
    alis_tarihi     TEXT NOT NULL,
    guncel_fiyat    REAL,
    kz_tl           REAL,
    kz_yuzde        REAL,
    durum           TEXT NOT NULL DEFAULT 'acik',
    kapanis_fiyati  REAL,
    kapanis_tarihi  TEXT,
    karar_gerekce   TEXT,
    para_birimi     TEXT DEFAULT 'TL'
);
CREATE INDEX IF NOT EXISTS ix_model_ticker_durum ON model_portfoy(ticker, durum);
CREATE TABLE IF NOT EXISTS portfoy_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kullanici_id    INTEGER NOT NULL,
    tarih           TEXT NOT NULL,
    toplam_deger_tl REAL,
    bist_degeri     REAL,
    abd_degeri      REAL,
    olusturma       TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_portfoy_snapshot_uid_tarih
    ON portfoy_snapshot(kullanici_id, tarih);
CREATE TABLE IF NOT EXISTS fiyat_alarm (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    kullanici_id      INTEGER NOT NULL REFERENCES kullanici(id),
    ticker            TEXT NOT NULL,
    hedef_fiyat       REAL NOT NULL,
    yon               TEXT NOT NULL,            -- 'yukari' (cikarsa) | 'asagi' (duserse)
    para_birimi       TEXT DEFAULT 'TL',        -- 'TL' | 'USD'
    aktif             INTEGER DEFAULT 1,        -- 1 aktif, 0 tetiklendi/pasif
    olusturma_tarihi  TEXT,
    tetiklenme_tarihi TEXT
);
CREATE INDEX IF NOT EXISTS ix_fiyat_alarm_aktif ON fiyat_alarm(aktif);
"""

# Profil "cekirdek" alanlari (17) - guven skoru bu alanlarin doluluk oranindan hesaplanir
_PROFIL_CEKIRDEK = (
    "portfoy_buyuklugu", "aylik_birikim", "ek_sermaye_mumkun", "tecrube_seviyesi",
    "risk_toleransi", "panik_egilimi", "yatirim_vadesi", "nakit_ihtiyaci",
    "ana_hedef", "kayip_toleransi_yuzde", "dusus_tepkisi_10", "dusus_tepkisi_20",
    "sektor_tercihi", "gunluk_takip_saat", "ana_korku", "onceki_basari",
    "risk_tercihi",
)
# Eksik alan -> kullaniciya gosterilecek Turkce etiket
_PROFIL_ETIKET = {
    "portfoy_buyuklugu": "portföy büyüklüğü",
    "aylik_birikim": "aylık birikim",
    "ek_sermaye_mumkun": "ek sermaye koyabilir misin",
    "tecrube_seviyesi": "tecrübe seviyesi (kaç yıldır borsada)",
    "risk_toleransi": "risk toleransı",
    "panik_egilimi": "panik eğilimi",
    "yatirim_vadesi": "yatırım vadesi",
    "nakit_ihtiyaci": "yakın vadede nakit ihtiyacı",
    "ana_hedef": "ana hedef (hızlı kazanç / korunma / büyüme)",
    "kayip_toleransi_yuzde": "kayıp toleransı (%)",
    "dusus_tepkisi_10": "%10 düşüşte ne yaparsın",
    "dusus_tepkisi_20": "%20 düşüşte ne yaparsın",
    "sektor_tercihi": "hangi sektörleri takip ediyorsun",
    "gunluk_takip_saat": "günde kaç saat borsayla ilgileniyorsun",
    "ana_korku": "en büyük korkun (kayıp / fırsat kaçırmak / belirsizlik)",
    "onceki_basari": "daha önce başarılı bir yatırım deneyimin",
    "risk_tercihi": "risk/ödül tercihi (az-az / çok-çok)",
}


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
    cols_d = {r["name"] for r in c.execute("PRAGMA table_info(decisions)")}
    if "yanlis_sebep" not in cols_d:
        c.execute("ALTER TABLE decisions ADD COLUMN yanlis_sebep TEXT")
    if "tahmini_sure" not in cols_d:        # TUT degerlendirme penceresi (AI tahmini, islem gunu)
        c.execute("ALTER TABLE decisions ADD COLUMN tahmini_sure INTEGER")
    if "ilk_gun_degisim" not in cols_d:     # AL/SAT 1. islem gunu fiyat degisimi (%)
        c.execute("ALTER TABLE decisions ADD COLUMN ilk_gun_degisim REAL")
    if "piyasa_farki" not in cols_d:        # hisse degisimi - BIST-100 degisimi (piyasaya gore)
        c.execute("ALTER TABLE decisions ADD COLUMN piyasa_farki REAL")
    # kullanici_profil: derin onboarding alanlari (varsa atlanir)
    tbls = {r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "kullanici_profil" in tbls:
        cols_pr = {r["name"] for r in c.execute("PRAGMA table_info(kullanici_profil)")}
        for col, tip in (("dusus_tepkisi_10", "TEXT"), ("dusus_tepkisi_20", "TEXT"),
                         ("sektor_tercihi", "TEXT"), ("gunluk_takip_saat", "REAL"),
                         ("ana_korku", "TEXT"), ("onceki_basari", "TEXT"),
                         ("risk_tercihi", "TEXT")):
            if col not in cols_pr:
                c.execute(f"ALTER TABLE kullanici_profil ADD COLUMN {col} {tip}")
    # paper_trades / model_portfoy: para_birimi (ABD hisse destegi)
    for tbl in ("paper_trades", "model_portfoy"):
        if tbl in tbls:
            cs = {r["name"] for r in c.execute(f"PRAGMA table_info({tbl})")}
            if "para_birimi" not in cs:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN para_birimi TEXT DEFAULT 'TL'")


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


def update_telegram_id(kullanici_id, telegram_id) -> bool:
    """Kullanicinin telegram_id'sini id'ye gore gunceller. telegram_id None ise
    baglantiyi kaldirir. Guncellenen satir varsa True doner."""
    init_db()
    with get_conn() as c:
        cur = c.execute("UPDATE kullanici SET telegram_id=? WHERE id=?",
                        (telegram_id, kullanici_id))
        return cur.rowcount > 0


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


# ---- fiyat alarmi ----
def add_price_alarm(kullanici_id, ticker, hedef_fiyat, yon, para_birimi="TL") -> int:
    """Yeni fiyat alarmi ekler. yon: 'yukari' (cikarsa) | 'asagi' (duserse)."""
    init_db()
    yon = "yukari" if str(yon).lower().startswith("yuk") else "asagi"
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO fiyat_alarm
                 (kullanici_id, ticker, hedef_fiyat, yon, para_birimi, aktif, olusturma_tarihi)
               VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (kullanici_id, str(ticker).upper().replace(".IS", ""),
             float(hedef_fiyat), yon, (para_birimi or "TL").upper(), _now()))
        return cur.lastrowid


def list_price_alarms(kullanici_id=None, aktif=None) -> list[dict]:
    """Fiyat alarmlarini dondurur. aktif=True yalniz aktifleri, kullanici_id verilirse o kisininkileri."""
    init_db()
    q = "SELECT * FROM fiyat_alarm WHERE 1=1"
    args = []
    if kullanici_id is not None:
        q += " AND kullanici_id=?"
        args.append(kullanici_id)
    if aktif is not None:
        q += " AND aktif=?"
        args.append(1 if aktif else 0)
    q += " ORDER BY id DESC"
    with get_conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def deactivate_price_alarm(alarm_id, tetik=True) -> bool:
    """Alarmi pasif yapar; tetik=True ise tetiklenme_tarihi'ni isaretler."""
    with get_conn() as c:
        cur = c.execute(
            "UPDATE fiyat_alarm SET aktif=0, tetiklenme_tarihi=? WHERE id=?",
            (_now() if tetik else None, int(alarm_id)))
        return cur.rowcount > 0


def delete_price_alarm(alarm_id, kullanici_id=None) -> bool:
    """Alarmi siler. kullanici_id verilirse yalniz o kisinin alarmini siler (guvenlik)."""
    with get_conn() as c:
        if kullanici_id is not None:
            cur = c.execute("DELETE FROM fiyat_alarm WHERE id=? AND kullanici_id=?",
                            (int(alarm_id), kullanici_id))
        else:
            cur = c.execute("DELETE FROM fiyat_alarm WHERE id=?", (int(alarm_id),))
        return cur.rowcount > 0


def user_id_by_ad(ad):
    init_db()
    with get_conn() as c:
        r = c.execute("SELECT id FROM kullanici WHERE LOWER(ad)=LOWER(?)",
                      (str(ad),)).fetchone()
        return r["id"] if r else None


def portfolio_last_update(kullanici_id) -> str | None:
    """Kullanicinin portfoyundeki en yeni pozisyon tarihi (alim_tarihi, ISO).
    'Portfoyun N gun guncellenmedi' uyarisi icin proxy. Bos portfoy -> None."""
    init_db()
    with get_conn() as c:
        r = c.execute(
            "SELECT MAX(alim_tarihi) AS son FROM portfoy WHERE kullanici_id=?",
            (kullanici_id,)).fetchone()
        return r["son"] if r and r["son"] else None


def list_portfolio(kullanici_id=None) -> list[dict]:
    init_db()
    with get_conn() as c:
        if kullanici_id is not None:
            q = "SELECT * FROM portfoy WHERE kullanici_id=? ORDER BY id"
            return [dict(r) for r in c.execute(q, (kullanici_id,))]
        return [dict(r) for r in c.execute("SELECT * FROM portfoy ORDER BY kullanici_id, id")]


# ---- portfoy degeri snapshot (gunluk/haftalik/aylik getiri takibi) ----
def record_portfoy_snapshot(kullanici_id, tarih, toplam_deger_tl,
                            bist_degeri=None, abd_degeri=None) -> None:
    """O gunku portfoy kapanis degerini yazar (kullanici+tarih basina tek kayit)."""
    init_db()
    with get_conn() as c:
        c.execute(
            """INSERT INTO portfoy_snapshot
                 (kullanici_id, tarih, toplam_deger_tl, bist_degeri, abd_degeri, olusturma)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(kullanici_id, tarih) DO UPDATE SET
                 toplam_deger_tl=excluded.toplam_deger_tl,
                 bist_degeri=excluded.bist_degeri,
                 abd_degeri=excluded.abd_degeri,
                 olusturma=excluded.olusturma""",
            (kullanici_id, tarih, toplam_deger_tl, bist_degeri, abd_degeri, _now()))


def snapshot_on_or_before(kullanici_id, tarih) -> dict | None:
    """Verilen tarihe (dahil) en yakin ONCEKI portfoy snapshot'i (yoksa None)."""
    init_db()
    with get_conn() as c:
        r = c.execute(
            "SELECT * FROM portfoy_snapshot WHERE kullanici_id=? AND tarih<=? "
            "ORDER BY tarih DESC LIMIT 1", (kullanici_id, tarih)).fetchone()
        return dict(r) if r else None


def list_portfoy_snapshots(kullanici_id, limit: int = 90) -> list[dict]:
    init_db()
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM portfoy_snapshot WHERE kullanici_id=? ORDER BY tarih DESC LIMIT ?",
            (kullanici_id, limit))]


# ---- karar gunlugu (decisions) ----
def record_decision(ticker, karar, puan=None, risk=None, eminlik=None,
                    gerekce=None, tarih=None, sonuc=None, tahmini_sure=None) -> int:
    """Bir AL/TUT/SAT kararini gunluge yazar. sonuc ileride doldurulur (None).
    tahmini_sure: TUT kararinda AI'nin tahmin ettigi tutma penceresi (islem gunu)."""
    init_db()
    tarih = tarih or datetime.now(_TZ).date().isoformat()
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO decisions (ticker, karar, puan, risk, eminlik, gerekce,
                                      tarih, sonuc, tahmini_sure)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(ticker).upper().replace(".IS", ""), karar, puan, risk,
             eminlik, gerekce, tarih, sonuc, tahmini_sure))
        return cur.lastrowid


def set_decision_ilk_gun(decision_id, ilk_gun_degisim) -> None:
    """AL/SAT kararinin 1. islem gunu fiyat degisimini (%) kaydeder (mini_update)."""
    init_db()
    with get_conn() as c:
        c.execute("UPDATE decisions SET ilk_gun_degisim=? WHERE id=?",
                  (ilk_gun_degisim, decision_id))


def list_decisions(limit: int = 100) -> list[dict]:
    init_db()
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (limit,))]


def set_decision_outcome(decision_id, sonuc, yanlis_sebep=None,
                         piyasa_farki=None) -> None:
    """Karar sonucunu (DOGRU/YANLIS metni) yazar. piyasa_farki verilirse
    (hisse degisimi - BIST-100 degisimi) ayni kayda islenir."""
    init_db()
    with get_conn() as c:
        sets, args = ["sonuc=?"], [sonuc]
        if yanlis_sebep is not None:
            sets.append("yanlis_sebep=?")
            args.append(yanlis_sebep)
        if piyasa_farki is not None:
            sets.append("piyasa_farki=?")
            args.append(piyasa_farki)
        args.append(decision_id)
        c.execute(f"UPDATE decisions SET {', '.join(sets)} WHERE id=?", args)


def mark_last_decision_wrong(ticker, yanlis_sebep="kullanici_bildirimi"):
    """Bir hissenin EN SON kararini YANLIS olarak isaretler (kullanici geri bildirimi).
    Guncellenen kararin (id, karar) bilgisini doner; karar yoksa None."""
    init_db()
    t = str(ticker or "").upper().replace(".IS", "")
    with get_conn() as c:
        row = c.execute("SELECT id, karar FROM decisions WHERE ticker=? "
                        "ORDER BY id DESC LIMIT 1", (t,)).fetchone()
        if not row:
            return None
        c.execute("UPDATE decisions SET sonuc=?, yanlis_sebep=? WHERE id=?",
                  ("kullanıcı: YANLIS", yanlis_sebep, row["id"]))
        return {"id": row["id"], "karar": row["karar"], "ticker": t}


def last_decision_any():
    """DB'deki en son karari (id, ticker, karar) doner; yoksa None.
    'Bu karar yanlisti' gibi hissesi belirtilmeyen geri bildirimde kullanilir."""
    init_db()
    with get_conn() as c:
        row = c.execute("SELECT id, ticker, karar FROM decisions "
                        "WHERE karar NOT LIKE 'KILL%' ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def recent_decisions_for(ticker, limit: int = 10) -> list[dict]:
    """Bir hissenin en son N kararini (sonuc dahil) dondurur."""
    init_db()
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM decisions WHERE ticker=? ORDER BY id DESC LIMIT ?",
            (str(ticker).upper().replace(".IS", ""), limit))]


# ---- paper trading (sanal islem) ----
def open_paper_trade(ticker, karar, fiyat, adet_sanal, tarih=None,
                     para_birimi="TL") -> int:
    """Sanal bir AL pozisyonu acar (durum='acik'). fiyat TL bazlidir (ABD'de
    USD fiyat x kur ile TL'ye cevrilmis saklanir); para_birimi yfinance sembolu
    secimi icin tutulur."""
    init_db()
    tarih = tarih or datetime.now(_TZ).date().isoformat()
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO paper_trades
                 (ticker, karar, fiyat, adet_sanal, tarih, durum, para_birimi)
               VALUES (?, ?, ?, ?, ?, 'acik', ?)""",
            (str(ticker).upper().replace(".IS", ""), karar, fiyat, adet_sanal, tarih,
             (para_birimi or "TL").upper()))
        return cur.lastrowid


def get_open_paper_trade(ticker):
    """Hisseye ait acik sanal pozisyon (varsa) - en yenisi."""
    init_db()
    with get_conn() as c:
        r = c.execute(
            "SELECT * FROM paper_trades WHERE ticker=? AND durum='acik' "
            "ORDER BY id DESC LIMIT 1",
            (str(ticker).upper().replace(".IS", ""),)).fetchone()
        return dict(r) if r else None


def list_paper_trades(durum=None, limit: int = 500) -> list[dict]:
    init_db()
    with get_conn() as c:
        if durum:
            q = "SELECT * FROM paper_trades WHERE durum=? ORDER BY id DESC LIMIT ?"
            return [dict(r) for r in c.execute(q, (durum, limit))]
        return [dict(r) for r in c.execute(
            "SELECT * FROM paper_trades ORDER BY id DESC LIMIT ?", (limit,))]


def update_paper_running(trade_id, kz_yuzde) -> None:
    """Acik pozisyonun guncel (kagit) kar/zarar yuzdesini gunceller."""
    init_db()
    with get_conn() as c:
        c.execute("UPDATE paper_trades SET kz_yuzde=? WHERE id=?", (kz_yuzde, trade_id))


def close_paper_trade(trade_id, kapanis_fiyati, kz_yuzde, tarih=None) -> None:
    """Sanal pozisyonu kapatir (durum='kapali')."""
    init_db()
    tarih = tarih or datetime.now(_TZ).date().isoformat()
    with get_conn() as c:
        c.execute(
            "UPDATE paper_trades SET kapanis_fiyati=?, kz_yuzde=?, durum='kapali', "
            "kapanis_tarihi=? WHERE id=?",
            (kapanis_fiyati, kz_yuzde, tarih, trade_id))


# ---- haber etki (haber-fiyat korelasyonu) ----
def record_haber_etki(ticker, haber_id, haber_tarihi, fiyat_haber_ani,
                      haber_kategori=None, baslik=None) -> int | None:
    """Yeni KAP bildirimi tespitinde o anki fiyati kaydeder. Ayni haber_id varsa
    tekrar eklemez (None doner)."""
    init_db()
    with get_conn() as c:
        try:
            cur = c.execute(
                """INSERT INTO haber_etki
                     (ticker, haber_id, haber_tarihi, fiyat_haber_ani,
                      haber_kategori, baslik, olusturma)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (str(ticker).upper().replace(".IS", ""), haber_id, haber_tarihi,
                 fiyat_haber_ani, haber_kategori, baslik, _now()))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def haber_etki_eksikler(limit: int = 200) -> list[dict]:
    """30dk/2saat/1gun fiyatlarindan en az biri bos olan kayitlar."""
    init_db()
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM haber_etki WHERE fiyat_30dk IS NULL OR fiyat_2saat IS NULL "
            "OR fiyat_1gun IS NULL ORDER BY id LIMIT ?", (limit,))]


def update_haber_etki(row_id, **alanlar) -> None:
    """haber_etki satirinin verilen alanlarini gunceller."""
    if not alanlar:
        return
    izin = {"fiyat_30dk", "fiyat_2saat", "fiyat_1gun", "etki_yuzde_1gun",
            "haber_kategori"}
    setler = {k: v for k, v in alanlar.items() if k in izin}
    if not setler:
        return
    init_db()
    with get_conn() as c:
        cols = ", ".join(f"{k}=?" for k in setler)
        c.execute(f"UPDATE haber_etki SET {cols} WHERE id=?",
                  (*setler.values(), row_id))


def list_haber_etki(limit: int = 500) -> list[dict]:
    init_db()
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM haber_etki ORDER BY id DESC LIMIT ?", (limit,))]


# ---- kullanici profili ----
def _profil_guven(p: dict) -> tuple[int, list[str]]:
    """Cekirdek alanlarin doluluk oranindan 0-100 guven skoru + eksik etiketler."""
    dolu, eksik = 0, []
    for k in _PROFIL_CEKIRDEK:
        v = p.get(k)
        if v not in (None, "", []):
            dolu += 1
        else:
            eksik.append(_PROFIL_ETIKET.get(k, k))
    skor = round(dolu / len(_PROFIL_CEKIRDEK) * 100)
    return skor, eksik


def get_profile(kullanici_id) -> dict | None:
    init_db()
    with get_conn() as c:
        r = c.execute("SELECT * FROM kullanici_profil WHERE kullanici_id=?",
                      (kullanici_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    for k in ("eksik_alanlar", "notlar"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (ValueError, TypeError):
                pass
    return d


_PROFIL_KOLONLAR = (
    "portfoy_buyuklugu", "aylik_birikim", "ek_sermaye_mumkun", "tecrube_seviyesi",
    "risk_toleransi", "panik_egilimi", "yatirim_vadesi", "nakit_ihtiyaci",
    "nakit_ihtiyac_tarihi", "ana_hedef", "kayip_toleransi_yuzde", "ogrenme_seviyesi",
    "aciklama_ister", "dusus_tepkisi_10", "dusus_tepkisi_20", "sektor_tercihi",
    "gunluk_takip_saat", "ana_korku", "onceki_basari", "risk_tercihi", "notlar",
)


def upsert_profile(kullanici_id, **alanlar) -> dict:
    """Profili olusturur/gunceller (yalniz verilen, None olmayan alanlar). Guven
    skoru + eksik alanlari yeniden hesaplar. Guncel profili dondurur."""
    init_db()
    mevcut = get_profile(kullanici_id) or {"kullanici_id": kullanici_id}
    for k, v in alanlar.items():
        if k in _PROFIL_KOLONLAR and v is not None:
            mevcut[k] = json.dumps(v, ensure_ascii=False) if k == "notlar" and not isinstance(v, str) else v
    # guven skoru icin notlar'i dict olarak degerlendirme (cekirdekte yok); ham dict kullan
    skor, eksik = _profil_guven({k: mevcut.get(k) for k in _PROFIL_CEKIRDEK})
    mevcut["profil_guven_skoru"] = skor
    mevcut["eksik_alanlar"] = json.dumps(eksik, ensure_ascii=False)
    mevcut["guncelleme_tarihi"] = _now()

    cols = ["kullanici_id"] + [k for k in _PROFIL_KOLONLAR if k in mevcut] + \
           ["profil_guven_skoru", "eksik_alanlar", "guncelleme_tarihi"]
    cols = list(dict.fromkeys(cols))
    vals = [mevcut.get(c) for c in cols]
    ph = ", ".join("?" * len(cols))
    upd = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "kullanici_id")
    with get_conn() as c:
        c.execute(f"INSERT INTO kullanici_profil ({', '.join(cols)}) VALUES ({ph}) "
                  f"ON CONFLICT(kullanici_id) DO UPDATE SET {upd}", vals)
    return get_profile(kullanici_id)


# ---- kullanici hafizasi ----
def add_memory(kullanici_id, tip, icerik, ticker=None, sonuc=None, tarih=None) -> int:
    """Kullaniciyla ilgili bir hareketi/oneriyi/sohbeti hafizaya yazar.
    icerik dict veya str olabilir (dict ise JSON'a cevrilir)."""
    init_db()
    if not isinstance(icerik, str):
        icerik = json.dumps(icerik, ensure_ascii=False)
    tarih = tarih or _now()
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO kullanici_hafiza (kullanici_id, tip, icerik, tarih, ticker, sonuc) "
            "VALUES (?,?,?,?,?,?)", (kullanici_id, tip, icerik, tarih, ticker, sonuc))
        return cur.lastrowid


def list_memory(kullanici_id, tip=None, limit: int = 200) -> list[dict]:
    init_db()
    with get_conn() as c:
        if tip:
            q = ("SELECT * FROM kullanici_hafiza WHERE kullanici_id=? AND tip=? "
                 "ORDER BY id DESC LIMIT ?")
            rows = c.execute(q, (kullanici_id, tip, limit))
        else:
            rows = c.execute("SELECT * FROM kullanici_hafiza WHERE kullanici_id=? "
                             "ORDER BY id DESC LIMIT ?", (kullanici_id, limit))
        out = []
        for r in rows:
            d = dict(r)
            if d.get("icerik"):
                try:
                    d["icerik"] = json.loads(d["icerik"])
                except (ValueError, TypeError):
                    pass
            out.append(d)
        return out


def memory_by_id(mem_id):
    init_db()
    with get_conn() as c:
        r = c.execute("SELECT * FROM kullanici_hafiza WHERE id=?", (mem_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    if d.get("icerik"):
        try:
            d["icerik"] = json.loads(d["icerik"])
        except (ValueError, TypeError):
            pass
    return d


def clear_memory(kullanici_id) -> int:
    init_db()
    with get_conn() as c:
        cur = c.execute("DELETE FROM kullanici_hafiza WHERE kullanici_id=?", (kullanici_id,))
        return cur.rowcount


# ---- model portfoy (botun kendi sanal portfoyu) ----
def open_model_position(ticker, adet, alis_fiyati, karar_gerekce=None,
                        alis_tarihi=None, para_birimi="TL") -> int:
    init_db()
    alis_tarihi = alis_tarihi or datetime.now(_TZ).date().isoformat()
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO model_portfoy
                 (ticker, adet, alis_fiyati, alis_tarihi, guncel_fiyat, durum,
                  karar_gerekce, para_birimi)
               VALUES (?, ?, ?, ?, ?, 'acik', ?, ?)""",
            (str(ticker).upper().replace(".IS", ""), adet, alis_fiyati, alis_tarihi,
             alis_fiyati, karar_gerekce, (para_birimi or "TL").upper()))
        return cur.lastrowid


def get_open_model_position(ticker):
    init_db()
    with get_conn() as c:
        r = c.execute("SELECT * FROM model_portfoy WHERE ticker=? AND durum='acik' "
                      "ORDER BY id DESC LIMIT 1",
                      (str(ticker).upper().replace(".IS", ""),)).fetchone()
        return dict(r) if r else None


def list_model_positions(durum=None, limit: int = 500) -> list[dict]:
    init_db()
    with get_conn() as c:
        if durum:
            return [dict(r) for r in c.execute(
                "SELECT * FROM model_portfoy WHERE durum=? ORDER BY id DESC LIMIT ?",
                (durum, limit))]
        return [dict(r) for r in c.execute(
            "SELECT * FROM model_portfoy ORDER BY id DESC LIMIT ?", (limit,))]


def update_model_running(pos_id, guncel_fiyat, kz_tl, kz_yuzde) -> None:
    init_db()
    with get_conn() as c:
        c.execute("UPDATE model_portfoy SET guncel_fiyat=?, kz_tl=?, kz_yuzde=? WHERE id=?",
                  (guncel_fiyat, kz_tl, kz_yuzde, pos_id))


def close_model_position(pos_id, kapanis_fiyati, kz_tl, kz_yuzde, tarih=None) -> None:
    init_db()
    tarih = tarih or datetime.now(_TZ).date().isoformat()
    with get_conn() as c:
        c.execute(
            "UPDATE model_portfoy SET durum='kapali', kapanis_fiyati=?, guncel_fiyat=?, "
            "kz_tl=?, kz_yuzde=?, kapanis_tarihi=? WHERE id=?",
            (kapanis_fiyati, kapanis_fiyati, kz_tl, kz_yuzde, tarih, pos_id))


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
