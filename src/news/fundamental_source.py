"""Temel (bilanco) veri kaynagi - borsapy (Is Yatirim/KAP) birincil, yfinance yedek.

get_fundamentals(ticker) sirket temel oranlarini dondurur:
  - fk            : F/K orani
  - pddd          : PD/DD
  - roe_%         : ozsermaye karliligi
  - kar_marji_%   : net kar marji
  - borc_ozsermaye: finansal borc / ozsermaye (%)
  - gelir_buyume_%: yillik gelir (satis) buyumesi
  - favok_marji_% : FAVOK marji

ONCE borsapy denenir: BIST'te yfinance F/K-PD/DD'yi sik sik yanlis verirken
(or. THYAO: yfinance F/K 32.9, borsapy 3.2) borsapy bunlari .info'dan dogru;
ROE/kar marji/borc-ozsermaye/gelir buyumesi/FAVOK ise KAP bilanco+gelir tablosundan
hesaplanir. borsapy bir alani veremezse (or. bankalarda Is Yatirim bilancosu yok)
o alan yfinance'ten doldurulur. ABD hisseleri dogrudan yfinance kullanir.

Kaynak alani: 'borsapy', 'yfinance' veya 'borsapy+yfinance'. Veri yoksa ilgili alan
None, hicbiri yoksa available=False.
"""
import time

_CACHE = {}
_TTL = 3600.0   # bilanco verisi yavas degisir; 1 saat onbellek

_METRIKLER = ("fk", "pddd", "roe_%", "kar_marji_%",
              "borc_ozsermaye", "gelir_buyume_%", "favok_marji_%")


def _sym(ticker: str, market: str = "bist") -> str:
    t = (ticker or "").upper().strip().replace(".IS", "")
    if t.endswith(".F"):              # fon/BYF eki ('GMSTR.F' -> 'GMSTR')
        t = t[:-2]
    if market in ("us", "abd"):
        return t                      # ABD: yfinance'te son ek yok
    return f"{t}.IS"


def _f(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _pct(v):
    """0-1 araligindaki orani yuzdeye cevirir (0.1556 -> 15.56)."""
    try:
        return round(float(v) * 100, 2)
    except (TypeError, ValueError):
        return None


def _row_series(df, label):
    """KAP tablosundan (satir=kalem, sutun=yil; en yeni yil ilk sutun) bir kalemin
    SAYISAL deger listesini dondurur. Ayni etiket birden fazla satirda varsa (or.
    kisa+uzun vadeli 'Finansal Borclar') sutun bazinda TOPLANIR. Bulunamazsa []."""
    if df is None:
        return []
    target = (label or "").strip()
    acc = None
    try:
        index = list(df.index)
        values = df.values                     # 2B dizi: satir x sutun (yil)
        for i, idx in enumerate(index):
            if str(idx).strip() != target:
                continue
            vals = [float(v) if isinstance(v, (int, float)) and v == v else None
                    for v in values[i]]
            if acc is None:
                acc = vals
            else:                              # ayni etiketli ek satir -> topla
                acc = [(a or 0) + (b or 0) if (a is not None or b is not None) else None
                       for a, b in zip(acc, vals)]
        return acc or []
    except Exception:
        return []


def _first(seq):
    """Listedeki ilk None-olmayan degeri (en yeni yil) dondurur."""
    for v in seq:
        if v is not None:
            return v
    return None


def _borsapy_fundamentals(ticker: str) -> dict:
    """borsapy (Is Yatirim/KAP) ile temel oranlar (yalniz BIST). Hata/veri yoksa {}.

    F/K + PD/DD dogrudan .info'dan; ROE/kar marji/borc-ozsermaye/gelir buyumesi
    KAP bilanco+gelir tablosundan; FAVOK marji EV/EBITDA uzerinden hesaplanir."""
    try:
        import borsapy as bp
        t = bp.Ticker(ticker)
        info = t.info.todict()
    except Exception:
        return {}
    out = {"fk": _f(info.get("trailingPE")), "pddd": _f(info.get("priceToBook"))}

    # Gelir tablosu + bilanco (bankalarda/bazi hisselerde olmayabilir -> sessizce atla)
    inc = bs = None
    try:
        inc = t.income_stmt
    except Exception:
        inc = None
    try:
        bs = t.balance_sheet
    except Exception:
        bs = None

    rev = _row_series(inc, "Satış Gelirleri")
    net = _first(_row_series(inc, "DÖNEM KARI (ZARARI)"))
    eq = _first(_row_series(bs, "Özkaynaklar"))
    borc = _first(_row_series(bs, "Finansal Borçlar"))
    rev0 = rev[0] if len(rev) >= 1 else None
    rev1 = rev[1] if len(rev) >= 2 else None

    if rev0 and rev1:
        out["gelir_buyume_%"] = round((rev0 - rev1) / abs(rev1) * 100, 2)
    if rev0 and net is not None:
        out["kar_marji_%"] = round(net / rev0 * 100, 2)
    if eq and net is not None:
        out["roe_%"] = round(net / eq * 100, 2)
    if eq and borc is not None:
        out["borc_ozsermaye"] = round(borc / eq * 100, 2)
    # FAVOK marji: EV/EBITDA + EV(=piyasa degeri + net borc) -> EBITDA -> / gelir
    eve = _f(info.get("enterpriseToEbitda"))
    mcap, nd = info.get("marketCap"), info.get("netDebt")
    if eve and rev0 and mcap is not None and nd is not None and eve != 0:
        ebitda = (float(mcap) + float(nd)) / eve
        out["favok_marji_%"] = round(ebitda / rev0 * 100, 2)

    return {k: v for k, v in out.items() if v is not None}


def _yf_fundamentals(ticker: str, market: str) -> dict:
    """yfinance .info ile temel oranlar (borsapy'nin dolduramadigi alanlar icin yedek)."""
    info = {}
    try:
        import yfinance as yf
        info = yf.Ticker(_sym(ticker, market)).get_info() or {}
    except Exception:
        info = {}
    return {
        "fk": _f(info.get("trailingPE")),
        "pddd": _f(info.get("priceToBook")),
        "roe_%": _pct(info.get("returnOnEquity")),
        "kar_marji_%": _pct(info.get("profitMargins")),
        "borc_ozsermaye": _f(info.get("debtToEquity")),
        "gelir_buyume_%": _pct(info.get("revenueGrowth")),
        "favok_marji_%": _pct(info.get("ebitdaMargins")),
    }


def get_fundamentals(ticker: str, market: str = "bist") -> dict:
    """Hisse temel oranlarini dondurur. ONCE borsapy, eksik alanlar yfinance. TTL onbellekli."""
    ticker = (ticker or "").upper().replace(".IS", "")
    now = time.monotonic()
    ck = f"{ticker}:{market}"
    hit = _CACHE.get(ck)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]

    out = {"ticker": ticker}
    kaynaklar = []

    # 1) borsapy (yalniz BIST): KAP bilancolarindan dogru oranlar
    bpy = _borsapy_fundamentals(ticker) if market not in ("us", "abd") else {}
    for k in _METRIKLER:
        if bpy.get(k) is not None:
            out[k] = bpy[k]
    if bpy:
        kaynaklar.append("borsapy")

    # 2) borsapy'nin veremedigi alanlari yfinance ile doldur
    if any(out.get(k) is None for k in _METRIKLER):
        yfd = _yf_fundamentals(ticker, market)
        eklendi = False
        for k in _METRIKLER:
            if out.get(k) is None and yfd.get(k) is not None:
                out[k] = yfd[k]
                eklendi = True
        if eklendi:
            kaynaklar.append("yfinance")

    for k in _METRIKLER:
        out.setdefault(k, None)
    out["available"] = any(out.get(k) is not None for k in _METRIKLER)
    out["kaynak"] = "+".join(kaynaklar) if kaynaklar else "yok"
    _CACHE[ck] = (now, out)
    return out


_VOL_CACHE = {}
_VOL_TTL = 600.0   # gun ici hacim degisir; 10 dk onbellek


def get_volume_anomaly(ticker: str, market: str = "bist") -> dict:
    """Bugunku hacmi son 5 gunun ortalamasiyla kiyaslar.

    - ort_5g  : bugun haric onceki 5 islem gununun ortalama hacmi
    - kat     : bugunku hacim / 5g ortalama
    - seviye  : kat>=3 -> 'COK YUKSEK', kat>=2 -> 'YUKSEK', altinda 'NORMAL'
    Veri yetersizse available=False.
    """
    ticker = (ticker or "").upper().replace(".IS", "")
    now = time.monotonic()
    ck = f"{ticker}:{market}"
    hit = _VOL_CACHE.get(ck)
    if hit and (now - hit[0]) < _VOL_TTL:
        return hit[1]

    vols = []
    try:
        import yfinance as yf
        df = yf.Ticker(_sym(ticker, market)).history(period="1mo")
        if df is not None and not df.empty:
            vols = [float(v) for v in df["Volume"].tolist() if v and v > 0]
    except Exception:
        vols = []

    if len(vols) < 6:
        out = {"ticker": ticker, "available": False,
               "neden": "Yeterli hacim verisi yok"}
        _VOL_CACHE[ck] = (now, out)
        return out

    bugun = vols[-1]
    onceki5 = vols[-6:-1]                 # bugun haric son 5 islem gunu
    ort = sum(onceki5) / len(onceki5)
    kat = round(bugun / ort, 2) if ort else None
    if kat is not None and kat >= 3:
        seviye = "COK YUKSEK"
    elif kat is not None and kat >= 2:
        seviye = "YUKSEK"
    else:
        seviye = "NORMAL"

    out = {
        "ticker": ticker, "available": True,
        "bugun_hacim": int(bugun),
        "ort_5g_hacim": int(ort),
        "kat": kat,
        "seviye": seviye,
        "anomali": seviye != "NORMAL",
    }
    _VOL_CACHE[ck] = (now, out)
    return out


# ----------------------------------------------------------------------------
# Sektor korelasyonu (statik): hisse -> hangi makro gostergeyle iliskili + yon
# ----------------------------------------------------------------------------
_GOSTERGE_AD = {
    "petrol": "Petrol fiyatı",
    "dolar": "Dolar/TL",
    "faiz": "Faiz",
    "celik_demir": "Çelik/Demir fiyatı",
}

# her hisse: [(gosterge, yon)] - yon: 'pozitif' veya 'ters'
_SECTOR_CORR = {
    # Havayolu: yakit (petrol) maliyeti -> ters; dovizli gelir -> dolar pozitif
    "THYAO": [("petrol", "ters"), ("dolar", "pozitif")],
    "PGSUS": [("petrol", "ters"), ("dolar", "ters")],
    # Bankalar: faiz artisi -> ters
    "GARAN": [("faiz", "ters")],
    "AKBNK": [("faiz", "ters")],
    "ISCTR": [("faiz", "ters")],
    "YKBNK": [("faiz", "ters")],
    "HALKB": [("faiz", "ters")],
    "VAKBN": [("faiz", "ters")],
    # Savunma/teknoloji: dovizli gelir -> dolar pozitif
    "ASELS": [("dolar", "pozitif")],
    # Demir-celik: emtia fiyati -> pozitif
    "EREGL": [("celik_demir", "pozitif")],
    "KRDMD": [("celik_demir", "pozitif")],
    "KORDS": [("celik_demir", "pozitif")],
    # Rafineri/gaz: petrol -> pozitif
    "TUPRS": [("petrol", "pozitif")],
    "AYGAZ": [("petrol", "pozitif")],
}


def get_sector_correlation(ticker: str) -> dict:
    """Hissenin hangi makro gostergeyle (ve hangi yonde) iliskili oldugunu dondurur."""
    ticker = (ticker or "").upper().replace(".IS", "")
    pairs = _SECTOR_CORR.get(ticker)
    if not pairs:
        return {"ticker": ticker, "available": False}
    korelasyonlar = [{"gosterge": _GOSTERGE_AD.get(g, g), "yon": y} for g, y in pairs]
    ozet = ", ".join(
        f"{_GOSTERGE_AD.get(g, g)} ile {'pozitif' if y == 'pozitif' else 'ters'}"
        for g, y in pairs)
    return {"ticker": ticker, "available": True,
            "korelasyonlar": korelasyonlar, "ozet": ozet}


if __name__ == "__main__":
    import json
    import sys
    for tk in (sys.argv[1:] or ["THYAO"]):
        print(json.dumps(get_fundamentals(tk), ensure_ascii=False, indent=2))
        print(json.dumps(get_volume_anomaly(tk), ensure_ascii=False, indent=2))
        print(json.dumps(get_sector_correlation(tk), ensure_ascii=False, indent=2))
