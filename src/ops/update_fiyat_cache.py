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
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.watchlist import is_us_market


def _load_dotenv():
    """KAP_PROXY_URL (bigpara icin) gibi degiskenleri .env'den ortama yukler."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

_TZ = ZoneInfo("Europe/Istanbul")
DATA = ROOT / "data"
CONFIG = ROOT / "config"
DB_PATH = DATA / "borsa.db"
CACHE_PATH = DATA / "fiyat_cache.json"
BIST100_PATH = CONFIG / "bist100.json"
WATCHLIST_PATH = CONFIG / "watchlist.json"

# Cache'e her zaman dahil edilecek sabit ABD hisseleri.
SABIT_ABD = ["NVDA", "SPCX", "RXT", "CNCK"]

# yfinance'in YANLIS fiyatladigi enstrumanlar -> bigpara detay slug'i. yfinance'de
# (or. GMSTR.IS) eksik bar/yanlis bolunme nedeniyle anormal deger geliyor (ornek:
# %1000+ gunluk); bunlari yfinance batch'inden cikarip bigpara'dan cekiyoruz.
# Anahtar = .IS'siz taban kod (portfoy 'GMSTR.F' -> taban 'GMSTR').
BIGPARA_ONLY = {"GMSTR": "gmstr-qnb-portfoy-gumus-katilim-byf-detay"}

# Ayni enstrumanlar icin BIRINCIL kaynak: Investing.com enstruman sayfasi. yfinance
# yanlis, bigpara ara sira erisilemez/eski olabiliyor; investing.com guvenilir fiyat
# verir. Once buradan denenir, olmazsa BIGPARA_ONLY slug'ina (bigpara) duser.
INVESTING_SOURCES = {"GMSTR": "https://tr.investing.com/etfs/non-financial-istanbul-20"}

# Tek gunde mantikli sayilan azami |% degisim|. Ustu yfinance veri hatasi sayilir
# (BIST gunluk fiyat limiti ~%10; fonlarda biraz daha genis tutuyoruz) -> gunluk
# bilgisi guvenilmez kabul edilip None yazilir (fiyat korunur).
_MAKUL_GUNLUK_LIMIT = 30.0


def _piyasa_acik(market: str, now: datetime) -> bool:
    """O an ilgili borsa acik mi? (BIST 10:00-18:00 + resmi tatil, ABD 16:30-23:00 IST)

    Mantik src.piyasa_takvim'de TEK kaynakta; burasi ince sarmalayici. Tatil
    takvimi 20 Tem 2026'da baglandi (once yalniz hafta sonu bakiliyordu)."""
    from src.piyasa_takvim import borsa_acik
    return borsa_acik(now, market)


def _semboller() -> dict:
    """Cekilecek {ticker: market} haritasi (ticker = .IS'siz taban kod)."""
    out: dict[str, str] = {}

    # 1) BIST-100 listesi ('pasif' altindakiler ATLANIR)
    # 23 Tem 2026: 8 sembol (KOZAL, SOYLM, ZORLU, TIRE, KERVT, YKGYO, ADNAC, FINBN)
    # yfinance'te 1 yildir veri dondurmuyor -> her kosuda "possibly delisted" hatasi
    # uretiyor ve 154/154 hedefini hep 146'da birakiyordu. Listeden SILINMEDILER
    # (BIST-100 uyeligi bilgisi kaybolmasin); bist100.json'daki 'pasif' altina alindi.
    try:
        data = json.loads(BIST100_PATH.read_text(encoding="utf-8"))
        pasif = {k.upper() for k in (data.get("pasif") or {})}
        for t in data.get("hisseler", []):
            base = (t or "").upper().split(".")[0]
            if base and base not in pasif:
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
                # market alani 'abd'/'us'/'nasdaq'... -> ABD (yfinance'te '.IS' ekleme)
                out.setdefault(base, "abd" if is_us_market(d.get("market")) else "bist")
    except Exception as e:
        print(f"[uyari] watchlist.json okunamadi: {type(e).__name__}: {e}")

    # 4) Sabit ABD hisseleri
    for t in SABIT_ABD:
        out.setdefault(t.upper(), "abd")

    return out


def _yf_sembol(ticker: str, market: str) -> str:
    """Ticker -> yfinance sembolu. BIST '.IS' ekler, ABD oldugu gibi."""
    return ticker if (market == "abd" or is_us_market(market)) else f"{ticker}.IS"


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
                # SANITE: anormal gunluk (or. yfinance eksik bar/bolunme) -> veriyi
                # yazma; yanlis '%1000' degeri cache'e dusmesin.
                if chg is not None and abs(chg) > _MAKUL_GUNLUK_LIMIT:
                    print(f"[uyari] {s} anormal gunluk %{chg:.0f} -> atlandi "
                          f"(prev={prev:g}, last={last:g})")
                    continue
                out[s] = {"fiyat": round(last, 2),
                          "gunluk": round(chg, 2) if chg is not None else None}
            elif len(col) >= 1:
                out[s] = {"fiyat": round(float(col.iloc[-1]), 2), "gunluk": None}
        except Exception:
            continue
    return out


def _sma_batch(yf_map: dict) -> dict:
    """Her hisse icin SMA20/50/200 trendini TEK yf.download batch'i (1y) ile hesaplar.

    yf_map: {ticker: yf_sembol}. Doner: {ticker: {"sma_trend": "güçlü yükseliş" /
    "güçlü düşüş" / "yatay/belirsiz", "sma20_uzeri": bool}}. Boylece sabah brifingi
    (presignal + market breadth) SMA'yi cache'den okur, yfinance'e tekrar gitmez.
    Veri yetersiz/hatali hisseler cikti disinda kalir."""
    if not yf_map:
        return {}
    from src.ai.presignal import _sma, _sma_trend_label
    import yfinance as yf
    syms = sorted(set(yf_map.values()))
    try:
        df = yf.download(syms, period="1y", interval="1d", progress=False,
                         threads=True, auto_adjust=True)
        closes_all = df["Close"]
    except Exception:
        return {}
    tek = len(syms) == 1
    out = {}
    for t, sym in yf_map.items():
        try:
            col = (closes_all.dropna() if tek else closes_all[sym].dropna())
            closes = [float(x) for x in col.tolist() if x == x and x]   # NaN/0 ele
        except Exception:
            continue
        if len(closes) < 20:                     # SMA20 icin bile yetersiz -> atla
            continue
        last = closes[-1]
        label = _sma_trend_label(last, _sma(closes, 20), _sma(closes, 50),
                                 _sma(closes, 200))
        s20 = _sma(closes, 20)
        out[t] = {"sma_trend": label,
                  "sma20_uzeri": bool(s20 is not None and last > s20)}
    return out


def _investing_fiyat(url: str):
    """Investing.com enstruman sayfasindan {fiyat, gunluk} ceker (requests + bs4).

    GMSTR.F gibi yfinance'in yanlis fiyatladigi BYF'ler icin BIRINCIL kaynak.
    Once dogrudan requests (tarayici basligiyla); 403/engel olursa curl_cffi
    (chrome taklidi) ile yeniden dener. Fiyat 'data-test=instrument-price-last',
    gunluk degisim 'instrument-price-change-percent' elementlerinden okunur.
    Basarisizsa None -> cagiran bigpara'ya duser."""
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"),
        "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
    }
    html_txt = None
    try:
        import requests
        r = requests.get(url, headers=headers, timeout=20)
        if r.status_code == 200 and r.text:
            html_txt = r.text
    except Exception:
        html_txt = None
    if html_txt is None:                       # engel/timeout -> chrome taklidi yedek
        try:
            from curl_cffi import requests as creq
            r = creq.get(url, impersonate="chrome", timeout=25)
            if r.status_code == 200 and r.text:
                html_txt = r.text
        except Exception:
            return None
    if not html_txt:
        return None
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_txt, "html.parser")
    except Exception:
        return None

    def _num(s):
        s = (s or "").strip().strip("()").replace("%", "").replace("+", "").strip()
        if not s:
            return None
        s = s.replace(".", "").replace(",", ".")     # TR sayi: 1.234,56 -> 1234.56
        try:
            return float(s)
        except ValueError:
            return None

    el = soup.select_one('[data-test="instrument-price-last"]')
    fiyat = _num(el.get_text(strip=True)) if el else None
    if fiyat is None:
        return None
    ch = soup.select_one('[data-test="instrument-price-change-percent"]')
    gunluk = _num(ch.get_text(strip=True)) if ch else None
    return {"fiyat": round(fiyat, 2),
            "gunluk": round(gunluk, 2) if gunluk is not None else None}


def _bigpara_fiyat(slug: str):
    """bigpara hisse/fon detay sayfasindan {fiyat, gunluk}. yfinance'in yanlis
    fiyatladigi enstrumanlar (or. GMSTR.F) icin yedek kaynak. Hata -> None."""
    proxy = os.environ.get("KAP_PROXY_URL")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    url = f"https://bigpara.hurriyet.com.tr/borsa/hisse-fiyatlari/{slug}/"
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", proxies=proxies, timeout=20)
        if r.status_code != 200:
            return None
        html_txt = r.text
    except Exception:
        return None
    import re
    pairs = dict(re.findall(
        r'<span class="name">([^<]+)</span>\s*<span class="value"[^>]*>([^<]+)</span>',
        html_txt))

    def _num(s):
        s = (s or "").replace("%", "").replace("&#x2B;", "+").strip()
        if not s:
            return None
        s = s.replace(".", "").replace(",", ".")     # TR sayi: 1.234,56 -> 1234.56
        try:
            return float(s)
        except ValueError:
            return None

    fiyat = _num(pairs.get("Son İşlem Fiyatı") or pairs.get("Satış"))
    if fiyat is None:
        return None
    gunluk = _num(pairs.get("Günlük Değişim %"))
    return {"fiyat": round(fiyat, 2),
            "gunluk": round(gunluk, 2) if gunluk is not None else None}


def _mcp_batch(tickers_market: dict) -> dict:
    """Borsa MCP'den toplu fiyat ceker. {ticker: {fiyat, gunluk, kaynak}}.

    MCP/fastmcp yoksa veya erisilmezse {} doner (sessizce) -> yfinance devralir."""
    if not tickers_market:
        return {}
    try:
        from src.news.borsa_mcp import get_prices_batch
        return get_prices_batch(list(tickers_market.items()))
    except Exception as e:
        print(f"[uyari] Borsa MCP toplu fiyat alinamadi: {type(e).__name__}: {e}")
        return {}


def guncelle() -> dict:
    """Tum hisseleri cekip data/fiyat_cache.json'a yazar; ozet sayilari dondurur.

    Once Borsa MCP'den (borsapy) toplu fiyat denenir; MCP'nin getiremedigi hisseler
    yfinance batch'ine, o da yanlis fiyatladiklari ise bigpara'ya duser.
    """
    now = datetime.now(_TZ)
    sembol_market = _semboller()
    zaman = now.strftime("%Y-%m-%d %H:%M")
    cache = {}

    # 0) BORSA MCP (birincil): BIGPARA_ONLY enstrumanlar haric tum hisseler
    mcp_aday = {t: m for t, m in sembol_market.items() if t not in BIGPARA_ONLY}
    mcp_sonuc = _mcp_batch(mcp_aday)
    mcp_sayi = 0
    for t, market in mcp_aday.items():
        d = mcp_sonuc.get(t)
        if not d or d.get("fiyat") is None:
            continue
        gunluk = d.get("gunluk")
        if gunluk is not None and abs(gunluk) > _MAKUL_GUNLUK_LIMIT:
            gunluk = None                 # anormal gunluk -> guvenilmez, fiyati koru
        cache[t] = {
            "fiyat": d["fiyat"],
            "gunluk": gunluk,
            "guncelleme": zaman,
            "kapali": not _piyasa_acik(market, now),
            "kaynak": "borsa_mcp",
        }
        mcp_sayi += 1

    # 1) yfinance batch (MCP'den GELMEYEN + BIGPARA_ONLY haric hisseler)
    yf_tickers = {t: m for t, m in sembol_market.items()
                  if t not in BIGPARA_ONLY and t not in cache}
    yf_map = {t: _yf_sembol(t, m) for t, m in yf_tickers.items()}
    ters = {sym: t for t, sym in yf_map.items()}
    fiyatlar = _batch_cek(sorted(ters.keys())) if yf_map else {}
    for t, market in yf_tickers.items():
        d = fiyatlar.get(yf_map[t])
        if not d or d.get("fiyat") is None:
            continue
        cache[t] = {
            "fiyat": d["fiyat"],
            "gunluk": d.get("gunluk"),
            "guncelleme": zaman,
            "kapali": not _piyasa_acik(market, now),
            "kaynak": "yfinance",
        }

    # 2) yfinance'in yanlis fiyatladigi enstrumanlar -> ONCE Investing.com,
    #    olmazsa bigpara'dan dogru fiyat
    for t, slug in BIGPARA_ONLY.items():
        if t not in sembol_market:
            continue
        d, kaynak = None, None
        inv_url = INVESTING_SOURCES.get(t)
        if inv_url:
            d = _investing_fiyat(inv_url)
            if d and d.get("fiyat") is not None:
                kaynak = "investing"
            else:
                d = None
        if d is None:                                  # investing yok/basarisiz -> bigpara
            d = _bigpara_fiyat(slug)
            kaynak = "bigpara"
        if not d or d.get("fiyat") is None:
            print(f"[uyari] {t} fiyati alinamadi (investing+bigpara)")
            continue
        gunluk = d.get("gunluk")
        if gunluk is not None and abs(gunluk) > _MAKUL_GUNLUK_LIMIT:
            gunluk = None                              # anormal gunluk -> guvenilmez
        cache[t] = {
            "fiyat": d["fiyat"],
            "gunluk": gunluk,
            "guncelleme": zaman,
            "kapali": not _piyasa_acik("bist", now),
            "kaynak": kaynak,
        }
        print(f"[bilgi] {t} fiyati {kaynak}'ten: {d['fiyat']} "
              f"(gunluk %{gunluk if gunluk is not None else '—'})")

    # 3) SMA20/50/200 trendini onceden hesapla (TEK batch) ve cache'e goml.
    #    BIGPARA_ONLY fonlari (yfinance yanlis fiyatlar) SMA'dan disla. presignal +
    #    market breadth bu degeri cache'den okur -> sabah brifingi 0 ek ag istegi.
    sma_map = {t: _yf_sembol(t, sembol_market[t])
               for t in cache if t not in BIGPARA_ONLY}
    sma_sonuc = _sma_batch(sma_map)
    sma_sayi = 0
    for t, d in sma_sonuc.items():
        if t in cache:
            cache[t]["sma_trend"] = d.get("sma_trend")
            cache[t]["sma20_uzeri"] = d.get("sma20_uzeri")
            sma_sayi += 1

    CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    return {"istenen": len(sembol_market), "cekilen": len(cache),
            "borsa_mcp": mcp_sayi, "diger": len(cache) - mcp_sayi, "sma": sma_sayi,
            "basarisiz": len(sembol_market) - len(cache), "dosya": str(CACHE_PATH)}


def main() -> None:
    now = datetime.now(_TZ)
    ozet = guncelle()
    print(f"[{now:%Y-%m-%d %H:%M}] fiyat cache guncellendi: "
          f"{ozet['cekilen']}/{ozet['istenen']} hisse cekildi "
          f"(borsa_mcp={ozet.get('borsa_mcp', 0)}, diger={ozet.get('diger', 0)}, "
          f"sma={ozet.get('sma', 0)}, {ozet['basarisiz']} basarisiz) -> {ozet['dosya']}")


if __name__ == "__main__":
    main()
