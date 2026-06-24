"""data/fiyat_cache.json'i toplu gunceller (cron: borsa saatlerinde her 5 dk).

BIST-100 (config/bist100.json) + portfoy + watchlist'teki tum hisseleri ve sabit
ABD listesini (NVDA, SPCX, RXT, CNCK) TEK yf.download batch'i ile ceker. Sonuc:

  {"ASELS": {"fiyat": 367.5, "gunluk": -6.25,
             "guncelleme": "2026-06-24 14:30", "kapali": false}, ...}

Bota Sor (_anlik_fiyatlar) once bu cache'e bakar; boylece her soruda yfinance'e
gidilmez, 429 riski azalir. Borsa kapaliysa kapali=true ve deger son kapanistir.

Calistirma: python -m src.ops.update_fiyat_cache
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_TZ = ZoneInfo("Europe/Istanbul")
DATA = ROOT / "data"
CONFIG = ROOT / "config"
DB_PATH = DATA / "borsa.db"
CACHE_PATH = DATA / "fiyat_cache.json"
BIST100_PATH = CONFIG / "bist100.json"
WATCHLIST_PATH = CONFIG / "watchlist.json"

# Cache'e her zaman dahil edilecek sabit ABD hisseleri.
SABIT_ABD = ["NVDA", "SPCX", "RXT", "CNCK"]


def _piyasa_acik(market: str, now: datetime) -> bool:
    """O an ilgili borsa acik mi? BIST 10:00-18:00, ABD ~16:30-23:00 (IST), hafta ici."""
    if now.weekday() >= 5:               # Cumartesi/Pazar
        return False
    hm = now.hour * 60 + now.minute
    if market == "abd":
        return 16 * 60 + 30 <= hm <= 23 * 60     # NYSE ~16:30-23:00 Istanbul saati
    return 10 * 60 <= hm <= 18 * 60              # BIST 10:00-18:00


def _semboller() -> dict:
    """Cekilecek {ticker: market} haritasi (ticker = .IS'siz taban kod)."""
    out: dict[str, str] = {}

    # 1) BIST-100 listesi
    try:
        data = json.loads(BIST100_PATH.read_text(encoding="utf-8"))
        for t in data.get("hisseler", []):
            base = (t or "").upper().split(".")[0]
            if base:
                out.setdefault(base, "bist")
    except Exception as e:
        print(f"[uyari] bist100.json okunamadi: {type(e).__name__}: {e}")

    # 2) Portfoydeki tum hisseler (para_birimi -> market)
    try:
        with sqlite3.connect(DB_PATH) as c:
            for tk, pb in c.execute("SELECT DISTINCT ticker, para_birimi FROM portfoy"):
                base = (tk or "").upper().split(".")[0]
                if base:
                    out.setdefault(base, "abd" if (pb or "TL").upper() == "USD" else "bist")
    except sqlite3.Error as e:
        print(f"[uyari] portfoy okunamadi: {type(e).__name__}: {e}")

    # 3) Watchlist (kisisel/bist_endeks -> BIST; kisisel_diger -> market alanindan)
    try:
        wl = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
        for t in (wl.get("bist_endeks", []) + wl.get("kisisel", [])):
            base = (t or "").upper().split(".")[0]
            if base:
                out.setdefault(base, "bist")
        for d in wl.get("kisisel_diger", []):
            base = (d.get("ticker") or "").upper().split(".")[0]
            if base:
                out.setdefault(base, "abd" if d.get("market") == "abd" else "bist")
    except Exception as e:
        print(f"[uyari] watchlist.json okunamadi: {type(e).__name__}: {e}")

    # 4) Sabit ABD hisseleri
    for t in SABIT_ABD:
        out.setdefault(t.upper(), "abd")

    return out


def _yf_sembol(ticker: str, market: str) -> str:
    """Ticker -> yfinance sembolu. BIST '.IS' ekler, ABD oldugu gibi."""
    return ticker if market == "abd" else f"{ticker}.IS"


def _batch_cek(yf_syms: list[str]) -> dict:
    """yf.download tek batch -> {yf_sembol: {fiyat, gunluk}}.

    Spec'te period='1d' istendi; ancak gunluk % degisim icin ONCEKI kapanis da
    gerektiginden (tatil/hafta sonu bosluklarina dayanikli olsun diye) 5 is gunu
    cekip son iki kapanisi kullaniyoruz. Yine TEK batch istegidir."""
    if not yf_syms:
        return {}
    import yfinance as yf
    df = yf.download(yf_syms, period="5d", interval="1d", progress=False,
                     threads=True, auto_adjust=True)
    out = {}
    try:
        closes = df["Close"]
    except Exception:
        return {}
    tek = len(yf_syms) == 1
    for s in yf_syms:
        try:
            col = (closes.dropna() if tek else closes[s].dropna())
            if len(col) >= 2:
                prev, last = float(col.iloc[-2]), float(col.iloc[-1])
                chg = ((last - prev) / prev * 100) if prev else None
                out[s] = {"fiyat": round(last, 2),
                          "gunluk": round(chg, 2) if chg is not None else None}
            elif len(col) >= 1:
                out[s] = {"fiyat": round(float(col.iloc[-1]), 2), "gunluk": None}
        except Exception:
            continue
    return out


def guncelle() -> dict:
    """Tum hisseleri cekip data/fiyat_cache.json'a yazar; ozet sayilari dondurur."""
    now = datetime.now(_TZ)
    sembol_market = _semboller()
    # ticker -> yf_sembol ve ters harita (yf_sembol -> ticker)
    yf_map = {t: _yf_sembol(t, m) for t, m in sembol_market.items()}
    ters = {sym: t for t, sym in yf_map.items()}

    fiyatlar = _batch_cek(sorted(ters.keys()))

    cache = {}
    for t, market in sembol_market.items():
        d = fiyatlar.get(yf_map[t])
        if not d or d.get("fiyat") is None:
            continue
        cache[t] = {
            "fiyat": d["fiyat"],
            "gunluk": d.get("gunluk"),
            "guncelleme": now.strftime("%Y-%m-%d %H:%M"),
            "kapali": not _piyasa_acik(market, now),
        }

    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    return {"istenen": len(sembol_market), "cekilen": len(cache),
            "basarisiz": len(sembol_market) - len(cache), "dosya": str(CACHE_PATH)}


def main() -> None:
    now = datetime.now(_TZ)
    ozet = guncelle()
    print(f"[{now:%Y-%m-%d %H:%M}] fiyat cache guncellendi: "
          f"{ozet['cekilen']}/{ozet['istenen']} hisse cekildi "
          f"({ozet['basarisiz']} basarisiz) -> {ozet['dosya']}")


if __name__ == "__main__":
    main()
