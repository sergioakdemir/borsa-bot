"""Deterministik coklu-faktor (zincir) analiz motoru.

Makro YON faktorlerini (dolar/petrol/bist yukseliyor-dusuyor, faiz yuksek/sabit)
gunluk degisimden hesaplar ve SEKTORE gore bir kombinasyon skoru (+/- puan) +
insan-okur aciklama uretir. AI'ya 'Coklu faktor skoru: +2 (...)' olarak verilir;
LLM'e bagli DEGIL, kural-tabanli ve deterministiktir.

Kullanim:
    from src.ai import kombinasyon
    skor, aciklamalar = kombinasyon.skor_for("TUPRS")        # canli faktorler (cache)
    # veya saf:  kombinasyon.kombinasyon_skoru(faktorler, "Enerji/Rafineri")
"""
import time

from src.ai.learning import _sektor_of

# Gunluk % degisim bu MUTLAK degeri gecince "yukseldi/dustu" sayilir (gurultu filtresi).
_ESIK = 0.3        # %
_FAIZ_YUKSEK = 30.0   # politika faizi >= bu ise "yuksek faiz ortami"

# Ihracat agirlikli (geliri doviz/ihracat hassas) sektorler.
_IHRACATCI = {"Savunma", "Otomotiv", "Demir-Çelik", "Enerji/Rafineri", "Cam",
              "Dayanıklı Tüketim"}


def makro_faktorler(usdtry_g=None, brent_g=None, bist_g=None,
                    faiz_10y_g=None, politika_faizi=None) -> dict:
    """Gunluk degisimlerden yon faktorleri (bool) uretir. Veri yoksa o faktor atlanir."""
    f = {}
    if usdtry_g is not None:
        f["dolar_yukseldi"] = usdtry_g > _ESIK
        f["dolar_dustu"] = usdtry_g < -_ESIK
    if brent_g is not None:
        f["petrol_yukseldi"] = brent_g > _ESIK
        f["petrol_dustu"] = brent_g < -_ESIK
    if bist_g is not None:
        f["bist_yukseliyor"] = bist_g > _ESIK
        f["bist_dusuyor"] = bist_g < -_ESIK
    if faiz_10y_g is not None:           # 10y tahvil getirisi (varsa) -> faiz yonu
        f["faiz_yukseliyor"] = faiz_10y_g > 1.0
        f["faiz_dusuyor"] = faiz_10y_g < -1.0
    # PPK seyrek degisir; gun ici "sabit" varsayilir (hike sinyali yoksa).
    f["faiz_sabit"] = not f.get("faiz_yukseliyor") and not f.get("faiz_dusuyor")
    if politika_faizi is not None:
        f["faiz_yuksek"] = politika_faizi >= _FAIZ_YUKSEK
    return f


# Kombinasyon kurallari: (kosul(faktorler)->bool, etkilenen_sektorler, puan, aciklama).
# En az 5 kural; tek faktorden cok COKLU faktor birlesimlerine oncelik.
_KURALLAR = [
    # 1) dolar yukseldi + petrol dustu -> rafineri marji genisler (urun $, girdi ucuz)
    (lambda f: f.get("dolar_yukseldi") and f.get("petrol_dustu"),
     {"Enerji/Rafineri"}, +2,
     "dolar yükselişi + petrol düşüşü rafineri marjına olumlu"),
    # 2) dolar yukseldi + faiz sabit -> ihracatci geliri TL'de buyur
    (lambda f: f.get("dolar_yukseldi") and f.get("faiz_sabit"),
     _IHRACATCI, +1,
     "dolar yükselişi (faiz sabit) ihracatçı gelirine olumlu"),
    # 3) bist dusuyor + yuksek faiz ortami -> bankacilik baski altinda
    (lambda f: f.get("bist_dusuyor") and f.get("faiz_yuksek"),
     {"Bankacılık"}, -2,
     "piyasa düşüşü + yüksek faiz ortamı bankacılığa olumsuz"),
    # 4) petrol yukseldi -> havacilik yakit maliyeti artar
    (lambda f: f.get("petrol_yukseldi"),
     {"Havacılık"}, -1,
     "petrol yükselişi havacılık yakıt maliyetine olumsuz"),
    # 5) dolar dustu -> ihracatci geliri TL'de kuculur
    (lambda f: f.get("dolar_dustu"),
     _IHRACATCI, -1,
     "dolar düşüşü ihracatçı gelirine olumsuz"),
    # 6) faiz dusuyor + bist yukseliyor -> banka/GYO rahatlar (varsa faiz yonu)
    (lambda f: f.get("faiz_dusuyor") and f.get("bist_yukseliyor"),
     {"Bankacılık", "Gayrimenkul"}, +2,
     "faiz düşüşü + piyasa yükselişi banka/GYO'ya olumlu"),
    # 7) petrol yukseldi + dolar yukseldi -> rafineri/enerji (urun fiyati $ bazli)
    (lambda f: f.get("petrol_yukseldi") and f.get("dolar_yukseldi"),
     {"Enerji/Rafineri"}, +1,
     "petrol + dolar birlikte yükselişi enerji/rafineri ürün fiyatına olumlu"),
]


def kombinasyon_skoru(faktorler: dict, sektor: str):
    """(skor:int, aciklamalar:list[(puan, metin)]) -> sektore uyan kurallarin toplami.
    Saf fonksiyon; faktorler makro_faktorler() ciktisidir."""
    if not sektor or not faktorler:
        return 0, []
    skor, aciklama = 0, []
    for kosul, sektorler, puan, metin in _KURALLAR:
        if sektor in sektorler:
            try:
                if kosul(faktorler):
                    skor += puan
                    aciklama.append((puan, metin))
            except Exception:
                continue
    return skor, aciklama


# --- Canli faktorler (surec ici onbellekli; ask_bot + commentary paylasir) ---
_CACHE = {"ts": 0.0, "faktorler": None, "ham": None}
_TTL = 300.0       # 5 dk (fiyat cache penceresiyle ayni)


def _gunluk_degisimler() -> dict:
    """USDTRY=X / BZ=F (brent) / XU100.IS (bist) gunluk % degisimi (yfinance tek batch).
    Veri gelmezse ilgili anahtar bos kalir (faktor uretilmez)."""
    out = {}
    syms = ["USDTRY=X", "BZ=F", "XU100.IS"]
    try:
        import yfinance as yf
        df = yf.download(syms, period="5d", progress=False, threads=True,
                         auto_adjust=True)
        closes = df["Close"]
        for s in syms:
            try:
                col = closes[s].dropna()
                if len(col) >= 2:
                    prev, last = float(col.iloc[-2]), float(col.iloc[-1])
                    if prev:
                        out[s] = round((last - prev) / prev * 100, 2)
            except Exception:
                continue
    except Exception:
        pass
    return out


def guncel_faktorler(ttl: float = _TTL) -> dict:
    """Canli makro yon faktorleri (5 dk onbellekli). Politika faizi makrodan alinir."""
    now = time.time()
    if _CACHE["faktorler"] is not None and (now - _CACHE["ts"]) < ttl:
        return _CACHE["faktorler"]
    deg = _gunluk_degisimler()
    politika = None
    try:
        from src.news.macro import get_macro
        politika = get_macro().get("politika_faizi")
    except Exception:
        politika = None
    ham = {"usdtry_g": deg.get("USDTRY=X"), "brent_g": deg.get("BZ=F"),
           "bist_g": deg.get("XU100.IS"), "politika_faizi": politika}
    f = makro_faktorler(ham["usdtry_g"], ham["brent_g"], ham["bist_g"],
                        None, politika)
    _CACHE.update(ts=now, faktorler=f, ham=ham)
    return f


def skor_for(ticker: str, faktorler: dict = None):
    """Bir hisse icin (skor, aciklamalar, sektor). faktorler verilmezse canli (cache)."""
    sektor = _sektor_of(ticker)
    if not sektor:
        return 0, [], None
    f = faktorler if faktorler is not None else guncel_faktorler()
    skor, aciklama = kombinasyon_skoru(f, sektor)
    return skor, aciklama, sektor


def baglam_metni(ticker: str, faktorler: dict = None) -> str:
    """AI baglamina eklenecek tek satir: 'Coklu faktor skoru: +2 (...)' veya ''."""
    skor, aciklama, sektor = skor_for(ticker, faktorler)
    if not aciklama:
        return ""
    gerekceler = "; ".join(m for _, m in aciklama)
    return f"Çoklu faktör skoru: {skor:+d} ({sektor}) — {gerekceler}"
