"""Genel piyasa baglami: BIST-100 yonu + USD/TRY + piyasa genisligi (breadth).

Sabah brifinginden once bir kez hesaplanir ve hem AI'a (commentary.py piyasa
baglami) hem de brifing mesajina (morning.py) verilir.

Uretilen alanlar:
  bist100_gunluk_%   : XU100.IS gunluk yuzde degisim
  bist100_haftalik_% : XU100.IS ~5 islem gunu yuzde degisim
  usdtry_haftalik_%  : USD/TRY ~5 islem gunu yuzde degisim
  yukselen / dusen   : watchlist'te yukselen/dusen hisse sayisi (breadth)
  yon                : YUKSELIYOR / DUSUYOR / YATAY / BELIRSIZ
  ai_notu            : AI'a verilecek tek cumlelik yorum
  brifing_notu       : Telegram brifingine eklenecek tek cumlelik ozet
"""
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Istanbul")

_CACHE = {}
_TTL = 600.0   # 10 dk; brifing + alarm ayni kosuda tekrar cekmesin


def _pct_change(closes, n):
    """closes listesinde son deger ile n islem gunu oncesi arasi yuzde degisim."""
    if not closes or len(closes) < 2:
        return None
    ref = closes[-(n + 1)] if len(closes) >= n + 1 else closes[0]
    last = closes[-1]
    if not ref:
        return None
    return round((last - ref) / ref * 100, 2)


def _series_changes(symbol):
    """(gunluk_%, haftalik_%) dondurur; veri yoksa (None, None)."""
    from src.data.factory import get_data_source
    start = (datetime.now(_TZ).date() - timedelta(days=20)).isoformat()
    try:
        df = get_data_source().get_history(symbol, start=start)
    except Exception:
        return None, None
    if df is None or df.empty:
        return None, None
    # Hisse/endekste hacimsiz (tatil/eksik) barlar elenir; doviz parite (USDTRY=X)
    # barlarinda hacim daima 0 oldugundan filtre bos birakirsa ham veriye don.
    if "Volume" in df.columns:
        filt = df[df["Volume"] > 0]
        if len(filt) >= 2:
            df = filt
    closes = [float(x) for x in df["Close"].tolist()]
    if len(closes) < 2:
        return None, None
    return _pct_change(closes, 1), _pct_change(closes, 5)


def _breadth(changes=None):
    """Watchlist'te yukselen/dusen sayisi. changes verilirse onu kullanir
    (ekstra yfinance cagrisi yapmaz), yoksa watchlist'i tarar."""
    if not changes:
        from src.watchlist import load_index
        from src.alerts.engine import intraday_change
        changes = {}
        for t in load_index():
            info = intraday_change(t)
            if info:
                changes[t] = info["change"]
    yukselen = sum(1 for v in changes.values() if v is not None and v > 0)
    dusen = sum(1 for v in changes.values() if v is not None and v < 0)
    return yukselen, dusen


def _yon(bist_gunluk, yukselen, dusen):
    if bist_gunluk is None:
        # endeks gelmezse breadth ile karar ver
        if yukselen + dusen == 0:
            return "BELIRSIZ"
        if yukselen >= dusen * 2:
            return "YUKSELIYOR"
        if dusen >= yukselen * 2:
            return "DUSUYOR"
        return "YATAY"
    if bist_gunluk >= 0.5:
        return "YUKSELIYOR"
    if bist_gunluk <= -0.5:
        return "DUSUYOR"
    return "YATAY"


def get_market_overview(changes=None) -> dict:
    """Genel piyasa baglamini hesaplar. changes: {ticker: gunluk_%} (opsiyonel,
    breadth icin tekrar fiyat cekmemek amaciyla brifingden gecirilir)."""
    now = time.monotonic()
    # changes verilmediyse onbellek kullanilabilir (endeks/kur sabit kosuda)
    if changes is None:
        hit = _CACHE.get("overview")
        if hit and (now - hit[0]) < _TTL:
            return hit[1]

    bist_g, bist_h = _series_changes("XU100.IS")
    _, usdtry_h = _series_changes("USDTRY=X")
    yukselen, dusen = _breadth(changes)
    yon = _yon(bist_g, yukselen, dusen)

    # AI notu (her hisse degerlendirmesinde baglam olarak verilir)
    ai_parcalar = [f"Piyasa genel olarak {yon} yönünde"]
    if bist_g is not None:
        h_txt = f", haftalık %{bist_h:+g}" if bist_h is not None else ""
        ai_parcalar.append(f"(BIST-100 günlük %{bist_g:+g}{h_txt})")
    ai_parcalar.append(f"{yukselen} yükselen / {dusen} düşen hisse")
    if usdtry_h is not None:
        ai_parcalar.append(f"USD/TRY haftalık %{usdtry_h:+g}")
    ai_notu = (", ".join(ai_parcalar) + ". Bu hisse kararını buna göre değerlendir: "
               "piyasa düşüyorsa AL için daha seçici/temkinli ol, yükseliyorsa "
               "fırsatları daha cesur değerlendir.")

    # Brifing notu (haftalik yone gore savunmaci/firsatci ton)
    if bist_h is None:
        brifing_notu = f"Piyasa yönü: {yon} ({yukselen} yükselen / {dusen} düşen)."
    elif bist_h <= -3:
        brifing_notu = (f"Piyasa bu hafta %{abs(bist_h):g} düştü — savunmacı duruş "
                        "önerilir, AL kararlarında seçici olun.")
    elif bist_h >= 3:
        brifing_notu = (f"Piyasa bu hafta %{bist_h:g} yükseldi — güçlü görünüm, "
                        "fırsatlar değerlendirilebilir.")
    else:
        brifing_notu = (f"Piyasa bu hafta yatay (%{bist_h:+g}) — seçici ve "
                        "temkinli olun.")
    if usdtry_h is not None and abs(usdtry_h) >= 1:
        yon_kur = "yükseldi" if usdtry_h > 0 else "geriledi"
        brifing_notu += f" Dolar/TL bu hafta %{abs(usdtry_h):g} {yon_kur}."

    out = {
        "bist100_gunluk_%": bist_g,
        "bist100_haftalik_%": bist_h,
        "usdtry_haftalik_%": usdtry_h,
        "yukselen": yukselen,
        "dusen": dusen,
        "yon": yon,
        "ai_notu": ai_notu,
        "brifing_notu": brifing_notu,
        "available": bist_g is not None or (yukselen + dusen) > 0,
    }
    if changes is None:
        _CACHE["overview"] = (now, out)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(get_market_overview(), ensure_ascii=False, indent=2))
