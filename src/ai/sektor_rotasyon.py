"""Sektor rotasyonu: son 5 islem gununde hangi sektor guclu, hangisi zayif?

Watchlist (BIST) hisselerini SEKTOR_HISSE haritasiyla sektorlere gruplar; her sektorun
son 5 islem gunu ORTALAMA getirisini hesaplar. "Bu hafta bankacilik guclu, enerji zayif"
tespiti uretir. Sabah brifingine "📊 Sektör rotasyonu: Bankacılık +%3.2 | Enerji -%1.8"
satiri olarak eklenir; AL kararlarinda guclu sektor tercih edilir (commentary baglami).

Tek yfinance batch (period='7d') ile ~5 islem gunu getirisi cekilir; surec ici 15 dk
onbellek (sabah brifingi icinde birden fazla cagride tekrar cekilmesin).
"""
import time

from src.ai.learning import SEKTOR_HISSE, _sektor_of

_MIN_SEKTOR_HISSE = 1        # bir sektoru siralamaya katmak icin gereken min hisse
_GUCLU_ESIK = 1.0            # >= bu 5g ortalama getiri -> "güçlü"
_ZAYIF_ESIK = -1.0          # <= bu -> "zayıf"

_CACHE = {"ts": 0.0, "veri": None}
_TTL = 900.0                # 15 dk


def _bes_gun_getiriler(tickerlar) -> dict:
    """{ticker: son ~5 islem gunu yuzde getirisi} - TEK yf.download batch (7d).
    Veri gelmeyen hisse cikti disinda kalir."""
    syms = sorted({f"{t}.IS" for t in tickerlar if t})
    if not syms:
        return {}
    try:
        import logging
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        import yfinance as yf
        df = yf.download(syms, period="7d", interval="1d", progress=False,
                         threads=True, auto_adjust=True)
        closes = df["Close"]
    except Exception:
        return {}
    tek = len(syms) == 1
    out = {}
    for t in tickerlar:
        sym = f"{t}.IS"
        try:
            col = (closes.dropna() if tek else closes[sym].dropna())
            vals = [float(x) for x in col.tolist() if x == x and x]
        except Exception:
            continue
        if len(vals) < 2:
            continue
        ref = vals[-6] if len(vals) >= 6 else vals[0]     # ~5 islem gunu once
        if not ref:
            continue
        out[t] = round((vals[-1] - ref) / ref * 100, 2)
    return out


def _hesapla(tickerlar=None) -> dict:
    """Sektor rotasyonunu hesaplar (onbelleksiz). Doner:
    {"sektorler": {sektor: ort_5g_%}, "siralama": [(sektor, %)...] (azalan),
     "guclu": (sektor, %)|None, "zayif": (sektor, %)|None}."""
    if tickerlar is None:
        try:
            from src.watchlist import load_index, load_personal
            tickerlar = list({t for t in (load_index() + load_personal()) if t})
        except Exception:
            tickerlar = list(SEKTOR_HISSE.keys())
    # Yalniz sektor haritasinda tanimli (BIST) hisseler
    tickerlar = [t for t in tickerlar if _sektor_of(t)]
    getiriler = _bes_gun_getiriler(tickerlar)
    grup = {}
    for t, g in getiriler.items():
        sek = _sektor_of(t)
        grup.setdefault(sek, []).append(g)
    sektorler = {sek: round(sum(v) / len(v), 2)
                 for sek, v in grup.items() if len(v) >= _MIN_SEKTOR_HISSE}
    siralama = sorted(sektorler.items(), key=lambda x: x[1], reverse=True)
    guclu = siralama[0] if siralama else None
    zayif = siralama[-1] if len(siralama) >= 2 else None
    return {"sektorler": sektorler, "siralama": siralama,
            "guclu": guclu, "zayif": zayif}


def sektor_rotasyonu(ttl: float = _TTL) -> dict:
    """Sektor rotasyonu (15 dk onbellekli). Bkz. _hesapla."""
    now = time.time()
    if _CACHE["veri"] is not None and (now - _CACHE["ts"]) < ttl:
        return _CACHE["veri"]
    veri = _hesapla()
    _CACHE.update(ts=now, veri=veri)
    return veri


def sektor_gucu(ticker: str, ttl: float = _TTL):
    """Bir hissenin sektorunun bu haftaki gucu: 'güçlü' / 'zayıf' / 'nötr' (getiriye
    gore) ya da None (sektor bilinmiyor/veri yok). commentary AL baglaminda kullanir."""
    sek = _sektor_of(ticker)
    if not sek:
        return None
    ort = sektor_rotasyonu(ttl).get("sektorler", {}).get(sek)
    if ort is None:
        return None
    if ort >= _GUCLU_ESIK:
        return "güçlü"
    if ort <= _ZAYIF_ESIK:
        return "zayıf"
    return "nötr"


def brifing_satiri(ttl: float = _TTL):
    """Sabah brifingi satiri: "📊 Sektör rotasyonu: Bankacılık +%3.2 | Enerji -%1.8".
    En guclu + en zayif sektoru gosterir. Yeterli veri yoksa None."""
    veri = sektor_rotasyonu(ttl)
    guclu, zayif = veri.get("guclu"), veri.get("zayif")
    if not guclu:
        return None
    parcalar = [f"{guclu[0]} {guclu[1]:+.1f}%"]
    if zayif and zayif[0] != guclu[0]:
        parcalar.append(f"{zayif[0]} {zayif[1]:+.1f}%")
    return "📊 Sektör rotasyonu: " + " | ".join(parcalar)


if __name__ == "__main__":
    import json
    print(json.dumps(sektor_rotasyonu(), ensure_ascii=False, indent=2))
    print(brifing_satiri())
