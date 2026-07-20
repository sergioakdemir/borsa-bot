"""Yukselis hafizasi: gun sonunda %3+ yukselen hisseleri analiz edip kaydeder."""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_TZ = ZoneInfo("Europe/Istanbul")
ESIK = 3.0
HAIKU = "claude-haiku-4-5-20251001"

_TABLO_SQL = """
CREATE TABLE IF NOT EXISTS yukselis_hafizasi (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    tarih           TEXT    NOT NULL,
    degisim_yuzde   REAL,
    kategori        TEXT,
    sebep           TEXT,
    piyasa_rejimi   TEXT,
    created_at      TEXT    DEFAULT (datetime('now'))
)
"""
_INDEKS_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_yukselis_tarih
    ON yukselis_hafizasi (ticker, tarih)
"""

def _get_conn():
    from src.db.database import get_conn
    return get_conn()

def _init_tablo():
    with _get_conn() as c:
        c.execute(_TABLO_SQL)
        c.execute(_INDEKS_SQL)

def _kaydet(ticker, tarih, degisim, kategori, sebep, rejim):
    with _get_conn() as c:
        c.execute(
            """INSERT INTO yukselis_hafizasi
               (ticker, tarih, degisim_yuzde, kategori, sebep, piyasa_rejimi)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(ticker, tarih) DO UPDATE SET
               kategori=excluded.kategori, sebep=excluded.sebep,
               piyasa_rejimi=excluded.piyasa_rejimi""",
            (ticker, tarih, degisim, kategori, sebep, rejim)
        )

def _yukselenler(esik=ESIK):
    cache_path = ROOT / "data" / "fiyat_cache.json"
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    sonuc = []
    for ticker, bilgi in data.items():
        if not isinstance(bilgi, dict):
            continue
        gunluk = bilgi.get("gunluk")
        if isinstance(gunluk, (int, float)) and gunluk >= esik:
            sonuc.append((ticker, round(gunluk, 2)))
    sonuc.sort(key=lambda x: -x[1])
    return sonuc

_SCHEMA = {
    "type": "object",
    "properties": {
        "kategori": {"type": "string", "enum": ["haber", "teknik", "makro", "sirket"]},
        "sebep": {"type": "string"}
    },
    "required": ["kategori", "sebep"],
    "additionalProperties": False
}

def _haiku_analiz(ticker, degisim, haberler, temel, makro_ozet, client=None, acc=None):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, None
    try:
        import anthropic
        client = client or anthropic.Anthropic()
        sistem = (
            "Sen bir borsa analistisin. Hissenin neden yükseldiğini tek kısa cümleyle açıkla. "
            "Kategori: haber (KAP/basın haberi), teknik (teknik kırılım), "
            "makro (piyasa geneli), sirket (analist/bilanço/temettü)."
        )
        icerik = {
            "hisse": ticker,
            "bugun_yukselis": f"%{degisim:+.1f}",
            "haberler": haberler[:3] if haberler else [],
            "sirket_sagligi": temel if temel else "veri yok",
            "piyasa": makro_ozet or "veri yok"
        }
        resp = client.messages.create(
            model=HAIKU, max_tokens=200, system=sistem,
            messages=[{"role": "user", "content": json.dumps(icerik, ensure_ascii=False)}],
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}}
        )
        if acc is not None:            # maliyet: token'lari toplayiciya ekle
            try:
                from src.ai import maliyet
                maliyet.ekle(acc, resp.usage)
            except Exception:
                pass
        text = next((b.text for b in resp.content if b.type == "text"), "")
        d = json.loads(text)
        return d.get("kategori", "belirsiz"), (d.get("sebep") or "").strip()
    except Exception as e:
        print(f"  [{ticker}] haiku hatasi: {type(e).__name__}")
        return None, None

def _load_dotenv():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def gecmis_ozet(ticker):
    try:
        _init_tablo()
        with _get_conn() as c:
            rows = c.execute(
                "SELECT tarih, degisim_yuzde, sebep FROM yukselis_hafizasi "
                "WHERE ticker=? ORDER BY tarih DESC LIMIT 3",
                (ticker.upper().replace(".IS", ""),)
            ).fetchall()
        if not rows:
            return None
        parcalar = [f"{r['tarih']} %{r['degisim_yuzde']:+.1f} ({r['sebep']})" for r in rows]
        return f"{ticker} gecmis yukselisler: " + " | ".join(parcalar)
    except Exception:
        return None

def run(verbose=True):
    _load_dotenv()
    _init_tablo()
    bugun = datetime.now(_TZ).date().isoformat()
    yukselenler = _yukselenler()
    if not yukselenler:
        if verbose:
            print(f"[{bugun}] Bugun %{ESIK}+ yukselen hisse yok.")
        return 0
    if verbose:
        print(f"[{bugun}] %{ESIK}+ yukselen: {len(yukselenler)} hisse")
    rejim_str = "Notr"
    try:
        from src.ai.kombinasyon import guncel_rejim
        rejim_str = guncel_rejim().get("rejim", "Notr")
    except Exception:
        pass
    makro_ozet = None
    try:
        from src.news.macro import get_macro
        m = get_macro() or {}
        p = []
        if m.get("sp_futures_degisim") is not None:
            p.append(f"S&P {m['sp_futures_degisim']:+.1f}%")
        if m.get("vix") is not None:
            p.append(f"VIX {m['vix']:.0f}")
        makro_ozet = " | ".join(p) if p else None
    except Exception:
        pass
    import anthropic
    from src.ai import maliyet
    client = anthropic.Anthropic()
    _acc = maliyet.bos_acc()                 # maliyet: tum Haiku cagrilarini topla
    kaydedilen = 0
    for ticker, degisim in yukselenler:
        try:
            haberler = []
            try:
                from src.watchlist import is_us_ticker
                if is_us_ticker(ticker):
                    from src.news.us_news import ticker_news
                    items = ticker_news(ticker, within_days=1) or []
                    haberler = [i.get("baslik", "") for i in items[:3]]
                else:
                    from src.news.borsa_mcp import get_kap_news
                    items = get_kap_news(ticker, limit=3) or []
                    haberler = [i.get("baslik", i.get("title", "")) for i in items]
            except Exception:
                pass
            temel = None
            try:
                from src.news.fundamental_source import get_fundamentals
                t = get_fundamentals(ticker)
                if t.get("available"):
                    temel = {k: t[k] for k in ("fk", "roe_%", "kar_marji_%") if t.get(k)}
            except Exception:
                pass
            kategori, sebep = _haiku_analiz(ticker, degisim, haberler, temel, makro_ozet, client=client, acc=_acc)
            _kaydet(ticker, bugun, degisim, kategori or "belirsiz", sebep or "", rejim_str)
            kaydedilen += 1
            if verbose:
                print(f"  {ticker:7} %{degisim:+.1f} -> {kategori}: {sebep}")
        except Exception as e:
            if verbose:
                print(f"  [{ticker}] hata: {type(e).__name__}: {e}")
    maliyet.logla(_acc, HAIKU, etiket="yukselis")   # maliyet: tek TOKEN OZET satiri
    if verbose:
        print(f"[{bugun}] Kaydedilen: {kaydedilen}/{len(yukselenler)}")
    try:                                 # heartbeat: gunluk saglik karnesi bunu okur
        from src.db import database as db
        db.kalp_at("yukselis_hafizasi")
    except Exception:
        pass
    return kaydedilen

if __name__ == "__main__":
    sys.exit(0 if run() >= 0 else 1)
