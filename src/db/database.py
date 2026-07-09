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
    telegram_id INTEGER,
    sifre_hash  TEXT                       -- bcrypt hash; NULL ise ilk giriste belirlenir
);
-- Cihaz hatirlatma: sifre dogrulaninca uretilen kalici token (UUID).
-- localStorage'a yazilir; sonraki acilista token gecerliyse sifre sorulmaz.
CREATE TABLE IF NOT EXISTS device_tokens (
    token         TEXT PRIMARY KEY,
    kullanici_id  INTEGER NOT NULL REFERENCES kullanici(id),
    cihaz         TEXT,
    olusturma     TEXT NOT NULL,
    son_kullanim  TEXT
);
CREATE INDEX IF NOT EXISTS ix_devtok_kullanici ON device_tokens(kullanici_id);
CREATE TABLE IF NOT EXISTS portfoy (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    kullanici_id  INTEGER NOT NULL REFERENCES kullanici(id),
    ticker        TEXT NOT NULL,
    adet          REAL NOT NULL,
    alim_fiyati   REAL NOT NULL,
    alim_tarihi   TEXT DEFAULT CURRENT_DATE,
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
CREATE TABLE IF NOT EXISTS uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kullanici_id    INTEGER NOT NULL REFERENCES kullanici(id),
    dosya_yolu      TEXT NOT NULL,
    yukleme_tarihi  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_uploads_kullanici ON uploads(kullanici_id);
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    kullanici_id    INTEGER DEFAULT 0,
    karar           TEXT NOT NULL,
    entry_fiyat     REAL,
    stop_fiyat      REAL,
    hedef_fiyat     REAL,
    position_size   REAL,
    para_birimi     TEXT DEFAULT 'TL',
    acilis_tarihi   TEXT NOT NULL,
    kapanis_tarihi  TEXT,
    kapanis_fiyat   REAL,
    kapanis_sebep   TEXT,
    holding_days    INTEGER,
    rr_oran         REAL,
    max_drawdown    REAL,
    max_profit      REAL,
    pnl_yuzde       REAL,
    pnl_tl          REAL,
    durum           TEXT DEFAULT 'acik'
);
CREATE INDEX IF NOT EXISTS ix_trades_ticker_durum ON trades(ticker, durum);
CREATE INDEX IF NOT EXISTS ix_trades_kullanici ON trades(kullanici_id, durum);
-- Enstruman ana tablosu: ticker -> pazar/para birimi/borsa/saglayici. Tum is_us
-- tespiti ve yfinance sembol uretimi (suffix_rule) bu tablodan okunur.
--   suffix_rule: 'none'  -> sembol = ticker (US: NVDA)
--                '.IS'   -> sembol = ticker + '.IS' (BIST: THYAO -> THYAO.IS)
--                'custom'-> sembol = ticker'in kendisi (GMSTR.F gibi sonek gomulu)
CREATE TABLE IF NOT EXISTS instruments (
    ticker        TEXT PRIMARY KEY,
    market        TEXT NOT NULL,
    currency      TEXT NOT NULL,
    exchange      TEXT,
    data_provider TEXT,
    suffix_rule   TEXT DEFAULT 'none',
    is_active     INTEGER DEFAULT 1,
    aciklama      TEXT,
    sektor        TEXT
);
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
    if "sifre_hash" not in cols:            # sifre sistemi: NULL -> ilk giriste belirlenir
        c.execute("ALTER TABLE kullanici ADD COLUMN sifre_hash TEXT")
    cols_p = {r["name"] for r in c.execute("PRAGMA table_info(portfoy)")}
    if "para_birimi" not in cols_p:
        c.execute("ALTER TABLE portfoy ADD COLUMN para_birimi TEXT DEFAULT 'TL'")
    tbls0 = {r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "instruments" in tbls0:              # sirket aciklamasi (Bota Sor AI baglami)
        cols_i = {r["name"] for r in c.execute("PRAGMA table_info(instruments)")}
        if "aciklama" not in cols_i:
            c.execute("ALTER TABLE instruments ADD COLUMN aciklama TEXT")
        if "sektor" not in cols_i:          # sektor rotasyonu + karar motoru baglami
            c.execute("ALTER TABLE instruments ADD COLUMN sektor TEXT")
        # GMSTR.F Borsa Istanbul gumus BYF'sidir; eski kayit yanlislikla 'EU'/
        # 'Frankfurt' isaretliydi -> yfinance 'GMSTR.F' (Frankfurt) bos donuyordu.
        # BIST'e duzelt (canli fiyat zaten bigpara/MCP'den; alarm/trade yolu da artik
        # dogru borsayi sorar). instrument_symbol '.F' -> '.IS' cevirir.
        c.execute("UPDATE instruments SET market='BIST', exchange='BIST' "
                  "WHERE ticker='GMSTR.F' AND (market='EU' OR exchange='Frankfurt')")
    cols_d = {r["name"] for r in c.execute("PRAGMA table_info(decisions)")}
    if "yanlis_sebep" not in cols_d:
        c.execute("ALTER TABLE decisions ADD COLUMN yanlis_sebep TEXT")
    if "tahmini_sure" not in cols_d:        # TUT degerlendirme penceresi (AI tahmini, islem gunu)
        c.execute("ALTER TABLE decisions ADD COLUMN tahmini_sure INTEGER")
    if "ilk_gun_degisim" not in cols_d:     # AL/SAT 1. islem gunu fiyat degisimi (%)
        c.execute("ALTER TABLE decisions ADD COLUMN ilk_gun_degisim REAL")
    if "piyasa_farki" not in cols_d:        # hisse degisimi - BIST-100 degisimi (piyasaya gore)
        c.execute("ALTER TABLE decisions ADD COLUMN piyasa_farki REAL")
    if "kullanici_id" not in cols_d:        # karar kimin icin: 0=sistem geneli (brifing)
        c.execute("ALTER TABLE decisions ADD COLUMN kullanici_id INTEGER DEFAULT 0")
    if "strategy_version" not in cols_d:    # strateji surumu: mevcut kayitlar 'v1',
        # 7 Temmuz 2026 buyuk paketi sonrasi acilanlar 'v2' (bkz. commentary.STRATEGY_VERSION)
        c.execute("ALTER TABLE decisions ADD COLUMN strategy_version TEXT DEFAULT 'v1'")
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
    # trades: girisinden bu yana gorulen en yuksek/dusuk yuzde (gun ici takip)
    cols_t = {r["name"] for r in c.execute("PRAGMA table_info(trades)")}
    if "intraday_high_pct" not in cols_t:
        c.execute("ALTER TABLE trades ADD COLUMN intraday_high_pct REAL")
    if "intraday_low_pct" not in cols_t:
        c.execute("ALTER TABLE trades ADD COLUMN intraday_low_pct REAL")
    if "time_stop_adayi" not in cols_t:
        c.execute("ALTER TABLE trades ADD COLUMN time_stop_adayi INTEGER DEFAULT 0")
    if "hedef2_fiyat" not in cols_t:          # kademeli ikinci hedef (deterministik motor)
        c.execute("ALTER TABLE trades ADD COLUMN hedef2_fiyat REAL")
    if "yeniden_degerlendir" not in cols_t:   # karar BEKLE'ye dondu -> gozden gecir
        c.execute("ALTER TABLE trades ADD COLUMN yeniden_degerlendir INTEGER DEFAULT 0")
    if "strategy_version" not in cols_t:      # strateji surumu: mevcut trade'ler 'v1',
        # 7 Temmuz 2026 paketi sonrasi acilanlar 'v2' (bkz. commentary.STRATEGY_VERSION)
        c.execute("ALTER TABLE trades ADD COLUMN strategy_version TEXT DEFAULT 'v1'")
    if "brut_pnl_yuzde" not in cols_t:        # islem maliyeti oncesi brut getiri; ana
        # olcu pnl_yuzde NET'tir (brut - 0.3 puan komisyon+slippage, bkz. update_trades)
        c.execute("ALTER TABLE trades ADD COLUMN brut_pnl_yuzde REAL")
    # paper_trades / model_portfoy: para_birimi (ABD hisse destegi)
    for tbl in ("paper_trades", "model_portfoy"):
        if tbl in tbls:
            cs = {r["name"] for r in c.execute(f"PRAGMA table_info({tbl})")}
            if "para_birimi" not in cs:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN para_birimi TEXT DEFAULT 'TL'")
    # haber_etki: ABD haber havuzu icin kaynak + etki_yorumu (yon/etki ozeti). BIST KAP
    # kayitlarinda bos kalir (geriye uyumlu); ABD haberleri bunlarla saklanir.
    if "haber_etki" in tbls:
        cols_he = {r["name"] for r in c.execute("PRAGMA table_info(haber_etki)")}
        if "kaynak" not in cols_he:
            c.execute("ALTER TABLE haber_etki ADD COLUMN kaynak TEXT")
        if "etki_yorumu" not in cols_he:
            c.execute("ALTER TABLE haber_etki ADD COLUMN etki_yorumu TEXT")
    # decisions: her (ticker, tarih) icin TEK kayit. Index yoksa once mukerrerleri
    # temizle (en yuksek id = en son karar kalir), sonra UNIQUE index'i kur. Index
    # varken bu blok atlanir; record_decision INSERT OR REPLACE ile tekrar olusturmaz.
    idx = {r["name"] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    if "idx_decisions_ticker_tarih" not in idx:
        c.execute("DELETE FROM decisions WHERE id NOT IN "
                  "(SELECT MAX(id) FROM decisions GROUP BY ticker, tarih)")
        c.execute("CREATE UNIQUE INDEX idx_decisions_ticker_tarih "
                  "ON decisions(ticker, tarih)")


def init_db() -> None:
    with get_conn() as c:
        c.executescript(SCHEMA)
        _migrate(c)
    seed_instruments()


# ---- instruments (enstruman ana tablosu) ----
# Baslangic verileri. yeni ticker eklemek icin buraya bir satir ekle; init_db
# idempotent olarak (INSERT OR IGNORE) doldurur, var olani EZMEZ.
_US_TICKERS = ["SPCX", "NVDA", "AMD", "TSM", "ASML", "RKLB", "IONQ", "RGTI",
               "ACHR", "BFLY", "MU", "CNCK", "RXT", "OSS", "QQQ", "VOO"]
_BIST_TICKERS = [
    "THYAO", "GARAN", "ASELS", "KCHOL", "TUPRS", "EREGL", "AKBNK", "YKBNK",
    "SISE", "TCELL", "BIMAS", "FROTO", "TOASO", "EKGYO", "PETKM", "ARCLK",
    "SAHOL", "HALKB", "VAKBN", "ISCTR", "TAVHL", "PGSUS", "MGROS", "ULKER",
    "CCOLA", "DOHOL", "ENKAI", "KORDS", "TTKOM",
    # Watchlist genisletme (100 hisse hedefi) — BIST tarama evreni.
    "KRDMD", "VESBE", "VESTL", "TTRAK", "OTKAR", "DOAS",
    "LOGO", "NETAS", "ALARK", "QNBTR", "SKBNK", "TRGYO", "MPARK", "MAVI",
    "CRFSA", "SOKM", "TURSG", "ANSGR", "AKSEN", "ZOREN", "ENJSA",
    "CLEBI", "HEKTS", "ECZYT", "SELEC", "ISGYO", "ALGYO", "SNGYO", "ADESE",
    "METRO", "DEVA", "ECILC", "AEFES", "BRYAT", "AGHOL", "KFEIN",
    "GESAN", "BFREN", "EGEEN", "HURGZ", "IHLGM", "SILVR", "RAYSG",
    "DNISI", "GLBMD", "CEMAS", "TSKB", "ODAS", "EUPWR", "KONTR",
    "KAREL", "NUHCM", "PRKAB", "MERIT",
    "HLGYO", "GSDHO", "KARSN", "RTALB", "GUBRF",
    "AKSA", "SASA", "TKFEN",
]


# Sembol -> sirket aciklamasi. Bota Sor AI baglamina girer (or. "SPCX nedir?").
# SPCX gibi karistirilan/yeni semboller icin "ne oldugu"nu netlestirir.
_INSTRUMENT_ACIKLAMA = {
    "SPCX": "Space Exploration Technologies (SpaceX), NASDAQ'ta işlem görüyor, "
            "Haziran 2026'da halka arz oldu. Özel şirket DEĞİL.",
    "NVDA": "NVIDIA — yapay zeka ve grafik işlemci (GPU) üreticisi.",
    "AMD": "Advanced Micro Devices — CPU ve GPU üreticisi.",
    "TSM": "Taiwan Semiconductor (TSMC) — dünyanın en büyük çip üreticisi (ADR).",
    "ASML": "ASML — çip üretiminde kullanılan EUV litografi makineleri üreticisi (Hollanda).",
    "MU": "Micron Technology — bellek (DRAM/NAND) çip üreticisi.",
    "RKLB": "Rocket Lab — uzay fırlatma ve uydu şirketi.",
    "IONQ": "IonQ — kuantum bilgisayar şirketi.",
    "RGTI": "Rigetti Computing — kuantum bilgisayar şirketi.",
    "QQQ": "Invesco QQQ — Nasdaq-100 endeksini izleyen ETF (tek hisse değil).",
    "VOO": "Vanguard S&P 500 ETF — S&P 500 endeksini izleyen ETF (tek hisse değil).",
    "CNCK": "Coincheck — Japon kripto para borsası, NASDAQ'ta işlem görüyor.",
    "RXT": "Rackspace Technology — bulut bilişim şirketi, NASDAQ.",
    "THYAO": "Türk Hava Yolları — BIST'te işlem gören havacılık şirketi.",
    "ASELS": "Aselsan — savunma elektroniği üreticisi, BIST.",
    "GARAN": "Garanti BBVA — bankacılık, BIST.",
    # --- BIST hisseleri (sirket aciklamalari) ---
    "ADESE": "Adese — İttifak Holding'e bağlı, Konya merkezli perakende ve gayrimenkul şirketi.",
    "AEFES": "Anadolu Efes — Türkiye'nin en büyük bira üreticisi; Coca-Cola İçecek'in ana ortağı.",
    "AGHOL": "Anadolu Grubu Holding — bira, meşrubat, otomotiv ve perakende iştiraklerini yöneten çatı holding.",
    "AKBNK": "Akbank — Sabancı Holding'e bağlı, Türkiye'nin büyük özel mevduat bankalarından biri.",
    "AKSA": "Aksa Akrilik — dünyanın en büyük akrilik elyaf üreticilerinden; Akkök Holding bünyesinde kimya şirketi.",
    "AKSEN": "Aksa Enerji — Türkiye ve yurt dışında elektrik üreten bağımsız enerji şirketi (Kazancı Holding).",
    "ALARK": "Alarko Holding — taahhüt, enerji, gayrimenkul ve sanayi alanlarında faaliyet gösteren holding.",
    "ALGYO": "Alarko GYO — Alarko Holding'e bağlı gayrimenkul yatırım ortaklığı.",
    "ANSGR": "Anadolu Sigorta — İş Bankası grubuna bağlı elementer sigorta şirketi.",
    "ARCLK": "Arçelik — Koç Holding'e bağlı beyaz eşya ve dayanıklı tüketim üreticisi (Arçelik, Beko markaları).",
    "ASTOR": "Astor Enerji — transformatör ve elektrik ekipmanları üreticisi.",
    "BFREN": "Bosch Fren Sistemleri — ticari araçlar için fren sistemleri üreticisi (Bosch grubu).",
    "BIMAS": "BİM — Türkiye'nin en büyük indirimli (hard-discount) market zinciri.",
    "BRYAT": "Borusan Yatırım — Borusan Grubu'nun halka açık yatırım ve holding şirketi.",
    "CCOLA": "Coca-Cola İçecek — Coca-Cola markalarının Türkiye ve bölge ülkelerindeki üreticisi/şişeleyicisi.",
    "CEMAS": "Çemaş Döküm — çelik döküm ve demir-çelik ürünleri üreticisi.",
    "CLEBI": "Çelebi Hava Servisi — havalimanı yer hizmetleri (ground handling) şirketi.",
    "CRFSA": "CarrefourSA — Sabancı Holding ve Carrefour ortaklığındaki süpermarket zinciri.",
    "DEVA": "Deva Holding — jenerik ve orijinal ilaç üreticisi.",
    "DNISI": "Dinamik Isı — köpük bazlı ısı yalıtım malzemeleri üreticisi (sandviç panel, HVAC, endüstriyel yalıtım).",
    "DOAS": "Doğuş Otomotiv — Volkswagen grubu markalarının Türkiye distribütörü (otomotiv ithalat/dağıtım).",
    "DOHOL": "Doğan Holding — enerji, medya, sanayi ve gayrimenkul iştirakleri olan holding.",
    "ECILC": "Eczacıbaşı İlaç, Sınai ve Finansal Yatırımlar — Eczacıbaşı Topluluğu'nun yatırım holdingi.",
    "ECZYT": "Eczacıbaşı Yatırım — Eczacıbaşı Grubu'na bağlı yatırım holdingi.",
    "EGEEN": "Ege Endüstri — otomotiv yan sanayi; ağır vasıta aks ve parça üreticisi.",
    "EKGYO": "Emlak Konut GYO — Türkiye'nin en büyük konut odaklı gayrimenkul yatırım ortaklığı (TOKİ iştiraki).",
    "ENJSA": "Enerjisa Enerji — elektrik dağıtım ve perakende satış şirketi (Sabancı-E.ON ortaklığı).",
    "ENKAI": "Enka İnşaat — uluslararası inşaat taahhüt, enerji ve gayrimenkul şirketi.",
    "EREGL": "Ereğli Demir Çelik (Erdemir) — Türkiye'nin en büyük yassı çelik üreticisi (OYAK grubu).",
    "EUPWR": "Europower Enerji — transformatör ve elektrik dağıtım ekipmanları üreticisi.",
    "FROTO": "Ford Otosan — Ford ve Koç Holding ortaklığında otomotiv (ağırlıklı ticari araç) üreticisi.",
    "GESAN": "Girişim Elektrik — elektrik taahhüt, enerji ve altyapı projeleri şirketi.",
    "GLBMD": "Global Menkul Değerler — Global Yatırım Holding'e bağlı aracı kurum / yatırım hizmetleri şirketi.",
    "GSDHO": "GSD Holding — finans, denizcilik ve yatırım alanlarında faaliyet gösteren holding.",
    "GUBRF": "Gübre Fabrikaları (Gübretaş) — kimyevi gübre üreticisi ve dağıtıcısı.",
    "HALKB": "Türkiye Halk Bankası — kamu sermayeli mevduat bankası, KOBİ ve esnaf kredilerinde güçlü.",
    "HEKTS": "Hektaş — tarım ilaçları, tohum ve gübre üreticisi (OYAK grubu).",
    "HLGYO": "Halk GYO — Halkbank iştiraki gayrimenkul yatırım ortaklığı.",
    "HURGZ": "Hürriyet Gazetecilik — medya ve yayıncılık şirketi (Demirören grubu).",
    "IHLGM": "İhlas Gayrimenkul — İhlas Holding'e bağlı gayrimenkul geliştirme ve inşaat şirketi.",
    "ISCTR": "Türkiye İş Bankası — Türkiye'nin en büyük özel mevduat bankalarından biri.",
    "ISGYO": "İş GYO — Türkiye İş Bankası iştiraki gayrimenkul yatırım ortaklığı.",
    "KAREL": "Karel Elektronik — telekomünikasyon ve elektronik sistemleri üreticisi.",
    "KARSN": "Karsan Otomotiv — otobüs, minibüs ve ticari araç üreticisi.",
    "KCHOL": "Koç Holding — Türkiye'nin en büyük holdingi (enerji, otomotiv, dayanıklı tüketim, finans iştirakleri).",
    "KFEIN": "Kafein Yazılım — yazılım geliştirme ve bilişim danışmanlık şirketi.",
    "KONTR": "Kontrolmatik — enerji, otomasyon ve batarya teknolojileri şirketi.",
    "KORDS": "Kordsa — lastik güçlendirme (kord bezi) ve kompozit teknolojileri üreticisi (Sabancı Holding).",
    "KRDMD": "Kardemir — Karabük merkezli demir-çelik üreticisi; uzun ürün ve ray ağırlıklı.",
    "LOGO": "Logo Yazılım — kurumsal yazılım (ERP, muhasebe, bordro) üreticisi.",
    "MAVI": "Mavi Giyim — denim ağırlıklı hazır giyim markası ve perakendecisi.",
    "MERIT": "Merit Turizm — Kuzey Kıbrıs ve yurt dışında otel-kumarhane işletmeciliği (Net Holding).",
    "METRO": "Metro Holding — ulaşım, turizm ve enerji iştirakleri olan holding.",
    "MGROS": "Migros Ticaret — Türkiye'nin büyük süpermarket zincirlerinden biri.",
    "MPARK": "MLP Sağlık (Medical Park) — özel hastane zinciri işletmecisi (Medical Park, Liv Hospital).",
    "NETAS": "Netaş — telekomünikasyon ve bilişim sistemleri entegratörü.",
    "NUHCM": "Nuh Çimento — çimento ve hazır beton üreticisi.",
    "ODAS": "Odaş Elektrik — elektrik üretimi, doğalgaz ve madencilik alanlarında enerji şirketi.",
    "OTKAR": "Otokar — Koç Holding'e bağlı otobüs, ticari araç ve askeri/savunma aracı üreticisi.",
    "PETKM": "Petkim — Türkiye'nin en büyük petrokimya üreticisi (SOCAR grubu).",
    "PGSUS": "Pegasus — düşük maliyetli (low-cost) havayolu şirketi.",
    "PRKAB": "Türk Prysmian Kablo — enerji ve telekomünikasyon kabloları üreticisi.",
    "QNBTR": "QNB Bank (eski QNB Finansbank) — QNB grubuna bağlı özel mevduat bankası.",
    "RAYSG": "Ray Sigorta — elementer sigortacılık şirketi.",
    "RTALB": "RTA Laboratuvarları — biyoteknoloji, moleküler tanı kitleri ve laboratuvar ürünleri şirketi.",
    "SAHOL": "Sabancı Holding — Türkiye'nin en büyük holdinglerinden (banka, enerji, sanayi, perakende iştirakleri).",
    "SASA": "Sasa Polyester — polyester elyaf ve petrokimyasal hammadde üreticisi (Erdemoğlu Holding).",
    "SELEC": "Selçuk Ecza Deposu — Türkiye'nin en büyük ecza (ilaç) dağıtım şirketi.",
    "SILVR": "Silverline Endüstri — ankastre beyaz eşya ve mutfak cihazları üreticisi.",
    "SISE": "Şişecam — cam (düzcam, cam ev eşyası), soda ve kimyasal üreticisi.",
    "SKBNK": "Şekerbank — orta ölçekli mevduat bankası; tarım ve KOBİ finansmanı odaklı.",
    "SNGYO": "Sinpaş GYO — konut geliştirme odaklı gayrimenkul yatırım ortaklığı.",
    "SOKM": "ŞOK Marketler — indirimli market zinciri.",
    "TAVHL": "TAV Havalimanları — havalimanı işletmeciliği ve terminal hizmetleri şirketi.",
    "TCELL": "Turkcell — Türkiye'nin en büyük mobil ve dijital hizmet operatörü.",
    "TKFEN": "Tekfen Holding — taahhüt (inşaat), tarımsal sanayi (gübre) ve yatırım holdingi.",
    "TOASO": "Tofaş — Fiat/Stellantis ve Koç Holding ortaklığında otomobil ve hafif ticari araç üreticisi.",
    "TRGYO": "Torunlar GYO — AVM ve karma proje odaklı gayrimenkul yatırım ortaklığı.",
    "TSKB": "Türkiye Sınai Kalkınma Bankası — kalkınma ve yatırım bankası; proje ve yeşil finansman odaklı.",
    "TTKOM": "Türk Telekom — sabit, mobil ve genişbant telekomünikasyon operatörü.",
    "TTRAK": "Türk Traktör — traktör ve tarım makineleri üreticisi (Koç Holding-CNH ortaklığı).",
    "TUPRS": "Tüpraş — Türkiye'nin en büyük ham petrol rafinericisi (Koç Holding).",
    "TURSG": "Türkiye Sigorta — kamu sermayeli, Türkiye'nin en büyük sigorta şirketi.",
    "ULKER": "Ülker Bisküvi — bisküvi, çikolata ve şekerleme üreticisi (Yıldız Holding).",
    "VAKBN": "VakıfBank — kamu sermayeli büyük mevduat bankası.",
    "VESBE": "Vestel Beyaz Eşya — beyaz eşya üreticisi (Zorlu/Vestel grubu).",
    "VESTL": "Vestel Elektronik — televizyon, elektronik ve beyaz eşya üreticisi (Zorlu Holding).",
    "YKBNK": "Yapı ve Kredi Bankası — Koç Holding'e bağlı büyük özel mevduat bankası.",
    "ZOREN": "Zorlu Enerji — elektrik üretimi (yenilenebilir, jeotermal, doğalgaz) ve dağıtım şirketi.",
    # GMSTR fon kaydı tabloda 'GMSTR.F' sonekiyle tutulur; UPDATE bu anahtarla eşleşir.
    "GMSTR.F": "QNB Portföy Gümüş BYF — Borsa İstanbul'da işlem gören gümüş borsa "
               "yatırım fonu (ETF).",
}


def _instrument_seed() -> list[tuple]:
    """(ticker, market, currency, exchange, data_provider, suffix_rule) satirlari."""
    rows = []
    for t in _US_TICKERS:
        rows.append((t, "US", "USD", "NASDAQ", "yfinance", "none"))
    for t in _BIST_TICKERS:
        rows.append((t, "BIST", "TRY", "BIST", "yfinance", ".IS"))
    # GMSTR.F: Borsa Istanbul gumus BYF'si. Ticker'da '.F' fonu isaretler (Yahoo
    # Frankfurt eki DEGIL); instrument_symbol bunu 'GMSTR.IS'e cevirir. Fiyat
    # yfinance'te guvenilmez -> canli grafik bigpara/MCP'den gelir (bkz. app.py).
    rows.append(("GMSTR.F", "BIST", "TRY", "BIST", "yfinance", "custom"))
    return rows


# ABD enstrumanlarinin sektoru (SEKTOR_HISSE BIST'e ozel; US ayri tutulur ki
# BIST sektor-tavani mantigini etkilemesin). seed_instruments her calismada yazar.
_US_SEKTOR = {
    "NVDA": "Yarı İletken", "AMD": "Yarı İletken", "TSM": "Yarı İletken",
    "ASML": "Yarı İletken", "MU": "Yarı İletken",
    "IONQ": "Kuantum", "RGTI": "Kuantum",
    "RKLB": "Uzay", "SPCX": "Uzay",
    "CNCK": "Kripto",
    "QQQ": "ETF", "VOO": "ETF",
    "BFLY": "Sağlık Teknolojisi",
    "ACHR": "Havacılık",
    "OSS": "Donanım",
}


def seed_instruments() -> None:
    """Baslangic enstrumanlarini ekler (idempotent; var olani ezmez)."""
    from src.ai.learning import SEKTOR_HISSE      # sektor: tek kaynak (lazy import)
    with get_conn() as c:
        c.executemany(
            "INSERT OR IGNORE INTO instruments "
            "(ticker, market, currency, exchange, data_provider, suffix_rule, is_active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            _instrument_seed(),
        )
        # Aciklamalari her seferinde guncelle (var olan satirlari da kapsar; metin
        # degisirse yeni metni yazar). NULL/bilinmeyen semboller atlanir.
        c.executemany(
            "UPDATE instruments SET aciklama=? WHERE ticker=?",
            [(a, t) for t, a in _INSTRUMENT_ACIKLAMA.items()],
        )
        # Sektor bilgisini her seferinde SEKTOR_HISSE'den yaz (BIST hisseleri).
        # Ticker'daki '.F'/'.IS' eki onemsenmez (GMSTR.F -> GMSTR gibi taban kod).
        sektor_satir = []
        for tk in _BIST_TICKERS + ["GMSTR.F"]:
            sek = SEKTOR_HISSE.get(_norm_ticker(tk)) or SEKTOR_HISSE.get(
                _norm_ticker(tk).replace(".F", ""))
            if sek:
                sektor_satir.append((sek, tk))
        if sektor_satir:
            c.executemany(
                "UPDATE instruments SET sektor=? WHERE ticker=?", sektor_satir)
        # ABD enstrumanlarinin sektoru (yari iletken/kuantum/uzay/ETF...).
        c.executemany(
            "UPDATE instruments SET sektor=? WHERE ticker=?",
            [(s, t) for t, s in _US_SEKTOR.items()])


def _norm_ticker(ticker: str) -> str:
    """Sadece BIST '.IS' ekini soyar; GMSTR.F gibi gomulu sonekler korunur."""
    return str(ticker or "").upper().replace(".IS", "").strip()


def get_instrument(ticker: str) -> dict | None:
    """Ticker'in instruments kaydi (BIST '.IS' eki onemsenmez). Yoksa None."""
    t = _norm_ticker(ticker)
    if not t:
        return None
    with get_conn() as c:
        r = c.execute("SELECT * FROM instruments WHERE ticker=?", (t,)).fetchone()
        # Taban kod eşleşmezse gömülü sonekli kaydı dene ('GMSTR' -> 'GMSTR.F')
        # ki Bota Sor taban kodla aradığında fon kaydı/açıklaması bulunsun.
        if not r:
            r = c.execute("SELECT * FROM instruments WHERE ticker LIKE ?",
                          (t + ".%",)).fetchone()
    return dict(r) if r else None


def list_instruments(market=None, aktif=True) -> list[dict]:
    q = "SELECT * FROM instruments WHERE 1=1"
    args = []
    if market:
        q += " AND market=?"
        args.append(str(market).upper())
    if aktif is not None:
        q += " AND is_active=?"
        args.append(1 if aktif else 0)
    q += " ORDER BY market, ticker"
    with get_conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def is_us_instrument(ticker: str) -> bool:
    """Ticker ABD piyasasi mi? (market=US ya da currency=USD). Tabloda yoksa False."""
    inst = get_instrument(ticker)
    if not inst:
        return False
    return (inst.get("market") or "").upper() == "US" or \
           (inst.get("currency") or "").upper() == "USD"


def instrument_symbol(ticker: str) -> str:
    """yfinance sembolu (suffix_rule'a gore). Tabloda yoksa BIST varsayilir ('.IS').

    none -> ticker, '.IS' -> ticker+'.IS', custom -> ticker'in kendisi."""
    t = _norm_ticker(ticker)
    inst = get_instrument(ticker)
    if not inst:
        return f"{t}.IS"
    rule = (inst.get("suffix_rule") or "none").lower()
    base = inst["ticker"]
    # BYF/fon ic isareti '.F' -> BIST yfinance sembolu 'XXX.IS' (Yahoo Frankfurt
    # eki '.F' DEGIL; oyle sorulunca bos donuyordu). Or. 'GMSTR.F' -> 'GMSTR.IS'.
    if base.upper().endswith(".F"):
        return f"{base[:-2]}.IS"
    if rule in (".is", "bist", "is"):
        return f"{base}.IS"
    return base   # 'none' (US) -> ticker dogrudan


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
    for ad in ("serhat", "yigit", "ufuk", "gokay", "baris"):
        add_user(ad)
    seed_gokay_profile()
    seed_baris_profile()


def seed_baris_profile():
    """Baris: YENI kullanici. Profil bilgileri BOS birakilir (on-doldurma yok) ki
    standart onboarding akisi (yeni yatirimci) calissin. Telegram baglantisi yok;
    sifre sistemi aktif (sifre_hash NULL -> ilk giriste belirlenir; add_user yalniz
    'ad' yazdigi icin bu otomatik saglanir).

    Idempotent: kullanici elle profil doldurmussa (skor > 0) DOKUNMAZ. Aksi halde
    profili olusturmaz; get_profile None doner -> onboarding_done False (tamamlanmadi).
    seed_gokay_profile'in aksine kasitli olarak HICBIR alan on-doldurmaz."""
    uid = user_id_by_ad("baris")
    if uid is None:
        return
    # Profil zaten varsa (kullanici doldurmus) dokunma; yoksa bos/None birak.
    # Boylece onboarding tamamlanmamis (profil_guven_skoru 0) durumda kalir.
    return


def seed_gokay_profile():
    """Gokay: BIST'te deneyimli (10 yil) yatirimci. Profilini 'tecrubeli' olarak
    on-doldurur ki onboarding 'deneyimli yatirimci' akisini kullansin. Idempotent:
    profil zaten varsa (kullanici elle doldurmussa) EZMEZ."""
    uid = user_id_by_ad("gokay")
    if uid is None:
        return
    if get_profile(uid):                    # zaten profil var -> dokunma
        return
    upsert_profile(
        uid,
        tecrube_seviyesi="tecrubeli",       # ~10 yil deneyim
        ogrenme_seviyesi="ileri",
        aciklama_ister=0,                   # detayli teknik anlatim ister, basitlestirme yok
        risk_tercihi="dengeli",
        notlar="BIST odakli, 10 yil deneyimli yatirimci (onboarding'de deneyimli akis).")


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


def get_user_by_id(kullanici_id):
    """Kullaniciyi id'ye gore dondurur (tum alanlarla); yoksa None."""
    init_db()
    with get_conn() as c:
        r = c.execute("SELECT * FROM kullanici WHERE id=?",
                      (kullanici_id,)).fetchone()
        return dict(r) if r else None


# ---- sifre / cihaz token (giris sistemi) ----
def get_user(ad) -> dict | None:
    """Kullaniciyi ad'a gore (sifre_hash dahil tum alanlarla) dondurur; yoksa None."""
    init_db()
    with get_conn() as c:
        r = c.execute("SELECT * FROM kullanici WHERE LOWER(ad)=LOWER(?)",
                      (str(ad),)).fetchone()
        return dict(r) if r else None


def set_password_hash(ad, sifre_hash) -> bool:
    """Kullanicinin bcrypt sifre hash'ini kaydeder. Guncellenen satir varsa True."""
    init_db()
    with get_conn() as c:
        cur = c.execute("UPDATE kullanici SET sifre_hash=? WHERE LOWER(ad)=LOWER(?)",
                        (sifre_hash, str(ad)))
        return cur.rowcount > 0


def add_device_token(kullanici_id, token, cihaz=None) -> None:
    """Kalici cihaz token'i (UUID) kaydeder; 'beni hatirla' icin."""
    init_db()
    with get_conn() as c:
        c.execute(
            """INSERT OR REPLACE INTO device_tokens
                 (token, kullanici_id, cihaz, olusturma, son_kullanim)
               VALUES (?, ?, ?, ?, ?)""",
            (token, kullanici_id, (cihaz or "")[:200], _now(), _now()))


def user_by_device_token(token) -> dict | None:
    """Gecerli cihaz token'ina karsilik gelen kullaniciyi (ad dahil) dondurur; yoksa
    None. Bulununca son_kullanim damgasini tazeler."""
    if not token:
        return None
    init_db()
    with get_conn() as c:
        r = c.execute(
            """SELECT k.* FROM device_tokens d JOIN kullanici k ON k.id = d.kullanici_id
               WHERE d.token=?""", (str(token),)).fetchone()
        if not r:
            return None
        c.execute("UPDATE device_tokens SET son_kullanim=? WHERE token=?",
                  (_now(), str(token)))
        return dict(r)


def delete_device_token(token) -> bool:
    """Tek bir cihaz token'ini siler (cikis/unutma). Silinen varsa True."""
    init_db()
    with get_conn() as c:
        cur = c.execute("DELETE FROM device_tokens WHERE token=?", (str(token),))
        return cur.rowcount > 0


def device_token_kullanici_id(token):
    """Token gecerliyse sahibinin kullanici_id'sini dondurur; yoksa None.
    SALT-OKUNUR: her API isteginde cagrildigi icin son_kullanim'i GUNCELLEMEZ
    (yazma-yuku/lock olmasin). 'beni hatirla' tazeleme user_by_device_token'da."""
    if not token:
        return None
    init_db()
    with get_conn() as c:
        r = c.execute("SELECT kullanici_id FROM device_tokens WHERE token=?",
                      (str(token),)).fetchone()
        return r["kullanici_id"] if r else None


# ---- portfoy ----
def add_position(kullanici_id, ticker, adet, alim_fiyati, alim_tarihi=None,
                 notlar="", para_birimi="TL"):
    init_db()
    # Tarih verilmezse bugun (alim_tarihi NULL kalmasin -> 'son guncelleme' calissin)
    alim_tarihi = alim_tarihi or datetime.now(_TZ).date().isoformat()
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


def user_ad_by_id(uid):
    """kullanici_id -> ad (yoksa None). Cihaz token'i -> giris yapan kullanici adi
    cozumlemek icin (API guvenlik guard'i istenen kullanici ile eslesmeyi dogrular)."""
    if uid is None:
        return None
    init_db()
    with get_conn() as c:
        r = c.execute("SELECT ad FROM kullanici WHERE id=?", (int(uid),)).fetchone()
        return r["ad"] if r else None


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
                    gerekce=None, tarih=None, sonuc=None, tahmini_sure=None,
                    kullanici_id=0, strategy_version="v2") -> int:
    """Bir AL/TUT/SAT kararini gunluge yazar. sonuc ileride doldurulur (None).
    tahmini_sure: TUT kararinda AI'nin tahmin ettigi tutma penceresi (islem gunu).
    kullanici_id: karar kimin icin uretildi (0=sistem geneli/brifing; ileride kisiye ozel).
    strategy_version: karari ureten strateji surumu (yeni kayitlar 'v2'; eski kayitlar
    migration'da 'v1' olarak etiketlendi)."""
    init_db()
    tarih = tarih or datetime.now(_TZ).date().isoformat()
    with get_conn() as c:
        cur = c.execute(
            # OR REPLACE: ayni (ticker, tarih) zaten varsa eski kaydi silip yenisini
            # yazar (UNIQUE idx_decisions_ticker_tarih) -> gun ici tekrar uretimde
            # mukerrer olusmaz. NOT: sonuc/ilk_gun_degisim gibi alanlar yeniden
            # NULL'lanir; karar gunu (sonuc dolmadan once) yeniden uretildigi icin sorun degil.
            """INSERT OR REPLACE INTO decisions (ticker, karar, puan, risk, eminlik, gerekce,
                                      tarih, sonuc, tahmini_sure, kullanici_id, strategy_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(ticker).upper().replace(".IS", ""), karar, puan, risk,
             eminlik, gerekce, tarih, sonuc, tahmini_sure, kullanici_id, strategy_version))
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


def list_decisions_for(ticker: str, limit: int = 3) -> list[dict]:
    """Bir hisse icin EN SON kararlar (yeni->eski). Bota Sor 'gecmis oneri' icin."""
    init_db()
    t = str(ticker or "").upper().replace(".IS", "")
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM decisions WHERE ticker=? ORDER BY id DESC LIMIT ?",
            (t, limit))]


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


def record_us_haber(ticker, tarih, baslik, kaynak=None, etki_yorumu=None,
                    fiyat=None, kategori=None) -> int | None:
    """ABD hisse haberini KALICI havuza (haber_etki) yazar: ticker, tarih, baslik,
    kaynak, etki_yorumu (yon/etki ozeti). Dedup baslik+tarih(gun)+ticker'dan turetilen
    deterministik haber_id ile (UNIQUE); ayni haber tekrar yazilmaz (None doner).
    Fiyat-etki kolonlari (30dk/2saat/1gun) bos kalir; update_haber_etki ABD satirlarini
    atlar (BIST fiyatlanmaz). Bos baslik -> None."""
    baslik = (baslik or "").strip()
    if not baslik:
        return None
    import hashlib
    tkr = str(ticker).upper().replace(".IS", "")
    gun = str(tarih or "")[:10]
    hid = "US:" + hashlib.md5(
        f"{tkr}|{gun}|{baslik.lower()}".encode("utf-8")).hexdigest()[:20]
    init_db()
    with get_conn() as c:
        try:
            cur = c.execute(
                """INSERT INTO haber_etki
                     (ticker, haber_id, haber_tarihi, haber_kategori, baslik,
                      kaynak, etki_yorumu, fiyat_haber_ani, olusturma)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (tkr, hid, tarih, kategori or "ABD", baslik, kaynak, etki_yorumu,
                 fiyat, _now()))
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def haber_etki_eksikler(limit: int = 200) -> list[dict]:
    """30dk/2saat/1gun fiyatlarindan en az biri bos olan kayitlar (fiyat-etki takibi
    icin). ABD tickerlari HARIC: onlar BIST verisiyle fiyatlanamaz ve yalnizca kalici
    haber kaydidir; eksikler kuyrugunu doldurup BIST kayitlarini ac birakmasinlar."""
    init_db()
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM haber_etki WHERE (fiyat_30dk IS NULL OR fiyat_2saat IS NULL "
            "OR fiyat_1gun IS NULL) AND ticker NOT IN "
            "(SELECT ticker FROM instruments WHERE UPPER(market)='US') "
            "ORDER BY id LIMIT ?", (limit,))]


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


def hafiza_kv_get(kullanici_id, key, default=None):
    """kullanici_hafiza'yi tip-anahtarli basit KV deposu olarak okur (en son deger)."""
    init_db()
    with get_conn() as c:
        r = c.execute("SELECT icerik FROM kullanici_hafiza WHERE kullanici_id=? AND tip=? "
                      "ORDER BY id DESC LIMIT 1", (kullanici_id, key)).fetchone()
        return r["icerik"] if r else default


def hafiza_kv_set(kullanici_id, key, value) -> None:
    """kullanici_hafiza'ya tip-anahtarli tek deger yazar (upsert; eski satirlari siler)."""
    init_db()
    with get_conn() as c:
        c.execute("DELETE FROM kullanici_hafiza WHERE kullanici_id=? AND tip=?",
                  (kullanici_id, key))
        c.execute("INSERT INTO kullanici_hafiza (kullanici_id, tip, icerik, tarih) "
                  "VALUES (?,?,?,?)", (kullanici_id, key, str(value), _now()))


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


# ---- yuklenen fotograflar (Bota Sor gorsel analizi) ----
def add_upload(kullanici_id, dosya_yolu) -> int:
    """Yeni fotograf kaydi ekler; eklenen satirin id'sini doner."""
    init_db()
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO uploads (kullanici_id, dosya_yolu, yukleme_tarihi) "
            "VALUES (?, ?, ?)", (kullanici_id, str(dosya_yolu), _now()))
        return cur.lastrowid


def list_uploads(kullanici_id, limit: int = 100) -> list[dict]:
    """Kullanicinin fotograflari (en yeni once)."""
    init_db()
    with get_conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM uploads WHERE kullanici_id=? ORDER BY id DESC LIMIT ?",
            (kullanici_id, limit))]


def prune_uploads(kullanici_id, keep: int = 10) -> list[str]:
    """En fazla `keep` fotograf sakla; fazlasini (en eski) DB'den siler.
    Silinen dosyalarin yollarini doner (cagiran disk dosyasini siler)."""
    init_db()
    with get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, dosya_yolu FROM uploads WHERE kullanici_id=? "
            "ORDER BY id ASC", (kullanici_id,))]
        fazla = rows[:-keep] if len(rows) > keep else []
        for r in fazla:
            c.execute("DELETE FROM uploads WHERE id=?", (r["id"],))
        return [r["dosya_yolu"] for r in fazla]


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


# ---- trades (gercek islem defteri: AL kararindan acilan pozisyonlar) ----
def open_trade(ticker, karar, entry_fiyat, stop_fiyat=None, hedef_fiyat=None,
               position_size=None, para_birimi="TL", rr_oran=None,
               kullanici_id=0, acilis_tarihi=None, hedef2_fiyat=None,
               strategy_version="v2") -> int:
    """Yeni bir trade acar (durum='acik'). entry_fiyat o anki fiyat, stop/hedef
    verdict'ten gelen seviyeler, rr_oran = (hedef-entry)/(entry-stop). hedef2_fiyat
    doluysa kademeli hedef (deterministik motor) devrededir. strategy_version: trade'i
    acan strateji surumu (yeni trade'ler 'v2'; eski trade'ler migration'da 'v1')."""
    init_db()
    acilis_tarihi = acilis_tarihi or datetime.now(_TZ).date().isoformat()
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO trades
                 (ticker, kullanici_id, karar, entry_fiyat, stop_fiyat, hedef_fiyat,
                  hedef2_fiyat, position_size, para_birimi, acilis_tarihi, rr_oran,
                  strategy_version, durum)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'acik')""",
            (str(ticker).upper().replace(".IS", ""), kullanici_id, karar, entry_fiyat,
             stop_fiyat, hedef_fiyat, hedef2_fiyat, position_size,
             (para_birimi or "TL").upper(), acilis_tarihi, rr_oran, strategy_version))
        return cur.lastrowid


def get_open_trade(ticker, kullanici_id=0):
    """Hisseye ait acik trade (varsa, en yenisi)."""
    init_db()
    with get_conn() as c:
        r = c.execute(
            "SELECT * FROM trades WHERE ticker=? AND kullanici_id=? AND durum='acik' "
            "ORDER BY id DESC LIMIT 1",
            (str(ticker).upper().replace(".IS", ""), kullanici_id)).fetchone()
        return dict(r) if r else None


def list_trades(durum=None, kullanici_id=None, limit: int = 1000) -> list[dict]:
    init_db()
    q = "SELECT * FROM trades WHERE 1=1"
    args = []
    if durum:
        q += " AND durum=?"
        args.append(durum)
    if kullanici_id is not None:
        q += " AND kullanici_id=?"
        args.append(kullanici_id)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with get_conn() as c:
        return [dict(r) for r in c.execute(q, args)]


def update_trade_extremes(trade_id, max_drawdown, max_profit) -> None:
    """Acik trade'in max_drawdown / max_profit (yuzde) degerlerini gunceller."""
    init_db()
    with get_conn() as c:
        c.execute("UPDATE trades SET max_drawdown=?, max_profit=? WHERE id=?",
                  (max_drawdown, max_profit, trade_id))


def update_trade_intraday(trade_id, intraday_high_pct, intraday_low_pct) -> None:
    """Acik trade'in girisinden bu yana gorulen en yuksek/dusuk yuzdesini gunceller."""
    init_db()
    with get_conn() as c:
        c.execute("UPDATE trades SET intraday_high_pct=?, intraday_low_pct=? WHERE id=?",
                  (intraday_high_pct, intraday_low_pct, trade_id))


def update_trade_pnl(trade_id, pnl_yuzde) -> None:
    """ACIK trade'in anlik getirisini (guncel K/Z %) kaydeder. Her gece update_trades
    kosusunda guncellenir; boylece brifing/karne raporlari acik pozisyonlarin guncel
    K/Z'sini canli fiyat cekmeden DB'den okuyabilir. Kapanista close_trade nihai degeri
    yazar. Performans metrikleri yalniz durum='kapali' okudugu icin bu deger onlari
    etkilemez."""
    init_db()
    with get_conn() as c:
        c.execute("UPDATE trades SET pnl_yuzde=? WHERE id=?", (pnl_yuzde, trade_id))


def update_trade_stop(trade_id, stop_fiyat) -> None:
    """Acik trade'in stop_fiyat seviyesini gunceller (trailing stop icin)."""
    init_db()
    with get_conn() as c:
        c.execute("UPDATE trades SET stop_fiyat=? WHERE id=?", (stop_fiyat, trade_id))


def mark_time_stop(trade_id, deger: int = 1) -> None:
    """Acik trade'i time-stop adayi olarak isaretler (time_stop_adayi=1)."""
    init_db()
    with get_conn() as c:
        c.execute("UPDATE trades SET time_stop_adayi=? WHERE id=?", (deger, trade_id))


def update_trade_hedef(trade_id, hedef_fiyat) -> None:
    """Acik trade'in hedef_fiyat seviyesini gunceller (kademeli hedef ilerlemesi)."""
    init_db()
    with get_conn() as c:
        c.execute("UPDATE trades SET hedef_fiyat=? WHERE id=?", (hedef_fiyat, trade_id))


def set_yeniden_degerlendir(trade_id, deger: int = 1) -> None:
    """Acik trade'i 'yeniden degerlendir' olarak isaretler (karar BEKLE'ye dondu)."""
    init_db()
    with get_conn() as c:
        c.execute("UPDATE trades SET yeniden_degerlendir=? WHERE id=?", (deger, trade_id))


def close_trade(trade_id, kapanis_fiyat, kapanis_sebep=None, pnl_yuzde=None,
                pnl_tl=None, holding_days=None, tarih=None,
                brut_pnl_yuzde=None) -> None:
    """Trade'i kapatir (durum='kapali') ve pnl/sebep/holding bilgisini yazar.
    pnl_yuzde: NET getiri (islem maliyeti dusulmus, ana olcu). brut_pnl_yuzde:
    maliyet oncesi brut getiri (verilmezse dokunulmaz)."""
    init_db()
    tarih = tarih or datetime.now(_TZ).date().isoformat()
    with get_conn() as c:
        c.execute(
            "UPDATE trades SET durum='kapali', kapanis_fiyat=?, kapanis_tarihi=?, "
            "kapanis_sebep=?, pnl_yuzde=?, pnl_tl=?, holding_days=?, "
            "brut_pnl_yuzde=? WHERE id=?",
            (kapanis_fiyat, tarih, kapanis_sebep, pnl_yuzde, pnl_tl, holding_days,
             brut_pnl_yuzde, trade_id))


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


def ai_hata_inc(tarih=None) -> int:
    """Gunun AI cagri hata sayacini 1 artirir, yeni degeri doner (ayar tablosu,
    anahtar 'ai_hata:YYYY-MM-DD'). AI cagri exception'larinda cagrilir; health_monitor
    bu sayaci okuyup gunluk esik (>5) asilinca admin'e uyarir."""
    tarih = tarih or datetime.now(_TZ).date().isoformat()
    key = f"ai_hata:{tarih}"
    try:
        n = int(get_setting(key, 0) or 0) + 1
    except (TypeError, ValueError):
        n = 1
    set_setting(key, n)
    return n


def ai_hata_sayisi(tarih=None) -> int:
    """Verilen gunun (varsayilan bugun) AI cagri hata sayisini doner (yoksa 0)."""
    tarih = tarih or datetime.now(_TZ).date().isoformat()
    try:
        return int(get_setting(f"ai_hata:{tarih}", 0) or 0)
    except (TypeError, ValueError):
        return 0


def kalp_at(is_adi, zaman=None) -> str:
    """Gece isi BASARIYLA bittikten sonra 'heartbeat:<is>' ayarina zaman damgasi
    yazar (Europe/Istanbul). health_monitor bu damgayi okuyup 24s+ eskiyse admin'e
    uyarir (sessiz olum tespiti)."""
    z = zaman or datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
    set_setting(f"heartbeat:{is_adi}", z)
    return z


def kalp_yasi_saat(is_adi):
    """'<is>' isinin son basarili calismasindan bu yana gecen saat (float) veya
    hic damga yoksa None."""
    v = get_setting(f"heartbeat:{is_adi}")
    if not v:
        return None
    try:
        t = datetime.strptime(str(v), "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TZ)
        return (datetime.now(_TZ) - t).total_seconds() / 3600.0
    except (TypeError, ValueError):
        return None


def gunluk_sayac_arttir(ad, tarih=None) -> int:
    """Gunluk sayaci ('<ad>:YYYY-MM-DD') 1 artirir, yeni degeri doner (ayar tablosu)."""
    tarih = tarih or datetime.now(_TZ).date().isoformat()
    key = f"{ad}:{tarih}"
    try:
        n = int(get_setting(key, 0) or 0) + 1
    except (TypeError, ValueError):
        n = 1
    set_setting(key, n)
    return n


def gunluk_sayac(ad, tarih=None) -> int:
    """Gunluk sayacin ('<ad>:YYYY-MM-DD') degerini doner (yoksa 0)."""
    tarih = tarih or datetime.now(_TZ).date().isoformat()
    try:
        return int(get_setting(f"{ad}:{tarih}", 0) or 0)
    except (TypeError, ValueError):
        return 0
