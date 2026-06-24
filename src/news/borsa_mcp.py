"""Borsa MCP (https://borsamcp.fastmcp.app/mcp) — EK veri kaynagi.

Senkron sarmalayicilar (`get_price`, `get_kap_news`, `get_macro`); her biri hata
veya zaman asiminda SESSIZCE None doner -> cagiran taraf mevcut zincire (yfinance,
KAP scraping, investing.com) duser. Boylece MCP cevrimdisi olsa bile sistem calisir.

FastMCP Client async oldugundan, tek bir arka plan event-loop thread'inde TEK bir
baglanti acik tutulur (run_alerts gibi cok-ticker taramalarinda her cagride yeni MCP
handshake maliyetinden kacinmak icin). Baglanti koparsa bir kez otomatik yeniden
kurulur. fastmcp kurulu degilse / sunucu erisilemezse tum fonksiyonlar None doner.

Kullanilan MCP araclari:
- get_historical_data : gunluk OHLC (borsapy) -> son iki kapanis = fiyat + gunluk %
- get_news            : BIST hissesinin KAP haber listesi (mynet/KAP)
- get_fx_data         : doviz/kiymetli maden/emtia (USD, EUR, gram-altin, BRENT)
"""
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

MCP_URL = "https://borsamcp.fastmcp.app/mcp"
_TZ = ZoneInfo("Europe/Istanbul")
_CALL_TIMEOUT = 25.0          # tek bir MCP cagrisi icin azami bekleme (sn)

# --- arka plan event loop + kalici client (baglanti yeniden kullanimi) ---
_loop = None
_loop_lock = threading.Lock()
_client = None                # ayni loop thread'inde acik tutulan FastMCP Client


def _get_loop():
    """Arka plan asyncio loop'unu (gerekirse) baslatir ve dondurur."""
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            import asyncio
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True,
                             name="borsa-mcp-loop").start()
        return _loop


def _run(coro, timeout=_CALL_TIMEOUT):
    """Coroutine'i arka plan loop'unda calistirip sonucu senkron dondurur."""
    import asyncio
    fut = asyncio.run_coroutine_threadsafe(coro, _get_loop())
    return fut.result(timeout=timeout)


async def _aclient():
    """Acik MCP client'i dondurur; yoksa kurar (loop thread icinde calisir)."""
    global _client
    if _client is None:
        from fastmcp import Client
        c = Client(MCP_URL)
        await c.__aenter__()
        _client = c
    return _client


async def _areset():
    """Mevcut client'i kapatip sifirlar (kopuk baglanti sonrasi yeniden kurmak icin)."""
    global _client
    c, _client = _client, None
    if c is not None:
        try:
            await c.__aexit__(None, None, None)
        except Exception:
            pass


async def _acall(name: str, args: dict):
    """Bir MCP aracini cagirir; baglanti hatasinda bir kez yeniden baglanip dener.
    Sonuc verisini (yapilandirilmis dict/list) dondurur, basarisizsa None."""
    for deneme in (1, 2):
        try:
            client = await _aclient()
            res = await client.call_tool(name, args)
            return _result_data(res)
        except Exception:
            await _areset()
            if deneme == 2:
                return None
    return None


def _result_data(res):
    """FastMCP call_tool sonucundan yapilandirilmis veriyi cikarir (data ->
    structured_content -> text/JSON)."""
    d = getattr(res, "data", None)
    if d is None:
        d = getattr(res, "structured_content", None)
    if d is None:
        cont = getattr(res, "content", None)
        if cont:
            txt = getattr(cont[0], "text", None)
            if txt:
                import json
                try:
                    d = json.loads(txt)
                except Exception:
                    d = None
    return d


def _safe(name: str, args: dict):
    """`_acall`i senkron + tam sessiz sar (loop/timeout hatalari dahil None)."""
    try:
        return _run(_acall(name, args))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Genel API
# ---------------------------------------------------------------------------
def get_price(ticker: str, market: str = "bist") -> dict | None:
    """Bir hissenin guncel fiyatini + gunluk % degisimini Borsa MCP'den ceker.

    get_historical_data (borsapy) gunluk kapanis serisini verir; son iki kapanis
    fiyat ve gunluk degisimi icin kullanilir (yfinance 5g mantigiyla ayni). Doner:
    {"fiyat", "gunluk", "para_birimi", "kaynak": "borsa_mcp"} veya None."""
    t = (ticker or "").upper().split(".")[0].strip()
    if not t:
        return None
    mkt = "us" if market in ("us", "abd") else "bist"
    data = _safe("get_historical_data", {"symbol": t, "market": mkt})
    rows = (data or {}).get("data") if isinstance(data, dict) else None
    if not rows:
        return None
    try:
        rows = sorted(rows, key=lambda r: r.get("date") or "")
        kapanislar = [float(r["close"]) for r in rows
                      if r.get("close") is not None]
    except (TypeError, ValueError, KeyError):
        return None
    if not kapanislar:
        return None
    fiyat = round(kapanislar[-1], 2)
    gunluk = None
    if len(kapanislar) >= 2 and kapanislar[-2]:
        gunluk = round((kapanislar[-1] - kapanislar[-2]) / kapanislar[-2] * 100, 2)
    return {"fiyat": fiyat, "gunluk": gunluk,
            "para_birimi": "$" if mkt == "us" else "₺", "kaynak": "borsa_mcp"}


def get_prices_batch(items: list[tuple]) -> dict:
    """Birden cok (ticker, market) icin fiyatlari ESZAMANLI ceker (tek baglanti).

    update_fiyat_cache gibi toplu islerde 30+ ticker'i sirayla degil paralel ceker.
    Doner: {ticker: {fiyat, gunluk, para_birimi, kaynak}} (yalniz basarili olanlar)."""
    if not items:
        return {}
    # Toplu is uzun surer: tek-cagri zaman asimi degil, sayiya gore genis bir
    # butce ver (es zaman 10, beher ~6 sn). Asarsa o ana dek toplananlar yiter -> {}.
    timeout = max(120.0, len(items) * 2.0)
    try:
        return _run(_aget_prices_batch(items), timeout=timeout)
    except Exception:
        return {}


async def _aget_prices_batch(items: list[tuple]) -> dict:
    import asyncio
    sem = asyncio.Semaphore(10)       # MCP sunucusunu bogmamak icin es zaman siniri

    async def _one(ticker, market):
        t = (ticker or "").upper().split(".")[0].strip()
        if not t:
            return None
        mkt = "us" if market in ("us", "abd") else "bist"
        async with sem:
            data = await _acall("get_historical_data", {"symbol": t, "market": mkt})
        rows = (data or {}).get("data") if isinstance(data, dict) else None
        if not rows:
            return None
        try:
            rows = sorted(rows, key=lambda r: r.get("date") or "")
            kp = [float(r["close"]) for r in rows if r.get("close") is not None]
        except (TypeError, ValueError, KeyError):
            return None
        if not kp:
            return None
        gunluk = (round((kp[-1] - kp[-2]) / kp[-2] * 100, 2)
                  if len(kp) >= 2 and kp[-2] else None)
        return (t, {"fiyat": round(kp[-1], 2), "gunluk": gunluk,
                    "para_birimi": "$" if mkt == "us" else "₺",
                    "kaynak": "borsa_mcp"})

    sonuc = await asyncio.gather(*(_one(t, m) for t, m in items),
                                 return_exceptions=True)
    out = {}
    for r in sonuc:
        if isinstance(r, tuple) and r:
            out[r[0]] = r[1]
    return out


# Turkce ay kisaltmalari ("15 Haz 2026 20:09" -> datetime)
_TR_AY = {"oca": 1, "şub": 2, "sub": 2, "mar": 3, "nis": 4, "may": 5, "haz": 6,
          "tem": 7, "ağu": 8, "agu": 8, "eyl": 9, "eki": 10, "kas": 11, "ara": 12}


def _parse_tr_tarih(s: str):
    """'15 Haz 2026 20:09' -> tz-aware datetime (Europe/Istanbul). Cozulemezse None."""
    if not s:
        return None
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})(?:\s+(\d{1,2}):(\d{2}))?", s.strip())
    if not m:
        return None
    gun, ay_ad, yil, sa, dk = m.groups()
    ay = _TR_AY.get(ay_ad.strip().lower()[:3])
    if not ay:
        return None
    try:
        return datetime(int(yil), ay, int(gun), int(sa or 0), int(dk or 0), tzinfo=_TZ)
    except (ValueError, TypeError):
        return None


def get_kap_news(ticker: str, limit: int = 5) -> list | None:
    """Bir BIST hissesinin KAP haberlerini Borsa MCP'den ceker (get_news).

    Doner: [{"baslik", "url", "tarih" (iso|None), "published_at" (datetime|None),
    "kaynak", "id"}] (en yeni ilk). Hata/veri yoksa None."""
    t = (ticker or "").upper().split(".")[0].strip()
    if not t:
        return None
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 5
    data = _safe("get_news", {"symbol": t, "limit": limit})
    haberler = (data or {}).get("news") if isinstance(data, dict) else None
    if not haberler:
        return None
    out = []
    for h in haberler:
        if not isinstance(h, dict):
            continue
        baslik = (h.get("title") or "").strip()
        if not baslik:
            continue
        dt = _parse_tr_tarih(h.get("published_date") or "")
        out.append({
            "baslik": baslik,
            "url": h.get("url"),
            "tarih": dt.isoformat() if dt else None,
            "published_at": dt,
            "kaynak": h.get("source") or "KAP",
            "id": h.get("id"),
        })
    return out or None


def get_macro() -> dict | None:
    """Doviz/altin/petrol degerlerini Borsa MCP'den ceker (get_fx_data).

    Doner: {"usdtry", "eur_try", "gram_altin", "brent", "kaynak": "borsa_mcp"}
    (alinabilen alanlar dolu). Hicbiri gelmezse None."""
    data = _safe("get_fx_data",
                 {"data_type": "current", "symbol": ["USD", "EUR", "gram-altin", "BRENT"]})
    rates = (data or {}).get("rates") if isinstance(data, dict) else None
    if not rates:
        return None
    eslesme = {"USD": "usdtry", "EUR": "eur_try",
               "gram-altin": "gram_altin", "BRENT": "brent"}
    out = {}
    for r in rates:
        if not isinstance(r, dict):
            continue
        ad = eslesme.get(r.get("symbol"))
        val = r.get("sell")
        if val is None:
            val = r.get("buy") or r.get("price") or r.get("close")
        if ad and val is not None:
            try:
                out[ad] = round(float(val), 4)
            except (TypeError, ValueError):
                continue
    if not out:
        return None
    out["kaynak"] = "borsa_mcp"
    return out
