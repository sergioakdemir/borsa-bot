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


# --- Fed + TCMB surpriz analizi (saf, deterministik) -------------------------

def fed_tcmb_analiz(fed_degisim_bp=None, fed_beklenti_bp=None,
                    tcmb_degisim_bp=None, tcmb_beklenti_bp=None) -> dict:
    """Fed ve TCMB son faiz kararlarini (ve varsa piyasa beklentisini) yorumlar.

    surpriz_bp = gerceklesen_degisim - beklenti. Pozitif sürpriz = beklenenden
    SAHIN (daha az indirim / daha cok artirim) -> risk-off egilimi; negatif =
    GUVERCIN (beklenenden fazla gevseme) -> risk-on egilimi. Beklenti yoksa yon
    kararin kendi isaretinden okunur. Saf fonksiyon; veri yoksa o banka None.
    Donus: {"fed": {...}|None, "tcmb": {...}|None, "ozet": str}."""

    def _banka(ad, degisim, beklenti):
        if degisim is None:
            return None
        surpriz = (degisim - beklenti) if beklenti is not None else None
        ref = surpriz if surpriz is not None else degisim
        yon = "şahin" if ref > 0 else "güvercin" if ref < 0 else "nötr"
        if degisim > 0:
            kr = f"{ad} +{degisim}bp artırım"
        elif degisim < 0:
            kr = f"{ad} {degisim}bp indirim"
        else:
            kr = f"{ad} sabit (0bp)"
        if surpriz is not None and surpriz != 0:
            isaret = f"+{surpriz}" if surpriz > 0 else str(surpriz)
            kr += f", beklentiye göre {isaret}bp {yon} sürpriz"
        elif beklenti is not None:
            kr += ", beklentiyle uyumlu"
        return {"degisim_bp": degisim, "beklenti_bp": beklenti,
                "surpriz_bp": surpriz, "yon": yon, "metin": kr}

    fed = _banka("Fed", fed_degisim_bp, fed_beklenti_bp)
    tcmb = _banka("TCMB", tcmb_degisim_bp, tcmb_beklenti_bp)
    parcalar = [b["metin"] for b in (fed, tcmb) if b]
    return {"fed": fed, "tcmb": tcmb, "ozet": " | ".join(parcalar)}


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def makro_rejim_skoru(usdtry_g=None, bist_g=None, brent_g=None,
                      fed=None, tcmb=None, cds=None) -> dict:
    """Piyasa risk istahini 0-100 arasi tek skora indirger (100 = tam Risk-On).

    50 notr taban; her makro bilesen katki ekler/cikarir. fed/tcmb =
    fed_tcmb_analiz ciktisindaki banka dict'leri (sürpriz/karar yonu icin).
    cds = Turkiye 5y CDS (bp): 300+ ise risk-off (-5), 150 alti ise risk-on (+3).
    Skora gore rejim: >=60 Risk-On, <=40 Risk-Off, arasi Nötr.
    Donus: {"skor": int, "rejim": str, "bilesenler": [(ad, katki)]}."""
    skor = 50.0
    bilesenler = []

    def _ekle(ad, katki):
        nonlocal skor
        if katki:
            skor += katki
            bilesenler.append((ad, round(katki, 1)))

    if bist_g is not None:
        _ekle("BIST yönü", _clamp(bist_g * 5, -15, 15))
    if usdtry_g is not None:
        # TL değer kazanır (usdtry düşer) -> risk-on (+); değer kaybı -> risk-off (-)
        _ekle("TL/dolar", _clamp(-usdtry_g * 4, -12, 12))
    if brent_g is not None:
        # petrol yükselişi ithalatçı TR için hafif risk-off
        _ekle("petrol", _clamp(-brent_g * 1.5, -6, 6))
    if cds is not None:
        # yüksek CDS (ülke risk primi) risk-off; düşük CDS risk-on
        if cds >= 300:
            _ekle("CDS (yüksek risk primi)", -5)
        elif cds < 150:
            _ekle("CDS (düşük risk primi)", +3)
    for banka, ad in ((fed, "Fed"), (tcmb, "TCMB")):
        if not banka:
            continue
        s = banka.get("surpriz_bp")
        if s is not None:
            # güvercin sürpriz (negatif bp) risk-on (+); şahin (pozitif) risk-off (-)
            _ekle(f"{ad} sürpriz", _clamp(-s / 5.0, -15, 15))
        elif banka.get("degisim_bp") is not None:
            _ekle(f"{ad} kararı", _clamp(-banka["degisim_bp"] / 10.0, -8, 8))

    skor = int(round(_clamp(skor, 0, 100)))
    rejim = "Risk-On" if skor >= 60 else "Risk-Off" if skor <= 40 else "Nötr"
    return {"skor": skor, "rejim": rejim, "bilesenler": bilesenler}


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


def guncel_rejim(ttl: float = _TTL) -> dict:
    """Canli piyasa rejimi (makro_rejim_skoru'nu guncel gunluk degisimlerle besler).

    guncel_faktorler ile ayni 5 dk onbellegi paylasir (ham gunluk degisimler).
    Donus: makro_rejim_skoru ciktisina ek olarak 'bist_g' (BIST gunluk %) icerir;
    rejim/skor sabah brifinginde 'Piyasa Rejimi' blogunda kullanilir."""
    guncel_faktorler(ttl)                     # _CACHE['ham']'i doldurur/tazeler
    ham = _CACHE.get("ham") or {}
    cds = None
    try:
        from src.news.macro import get_macro
        cds = get_macro().get("turkey_cds")
    except Exception:
        cds = None
    rejim = makro_rejim_skoru(usdtry_g=ham.get("usdtry_g"),
                              bist_g=ham.get("bist_g"),
                              brent_g=ham.get("brent_g"), cds=cds)
    rejim["bist_g"] = ham.get("bist_g")
    return rejim


def skor_for(ticker: str, faktorler: dict = None):
    """Bir hisse icin (skor, aciklamalar, sektor). faktorler verilmezse canli (cache)."""
    sektor = _sektor_of(ticker)
    if not sektor:
        return 0, [], None
    f = faktorler if faktorler is not None else guncel_faktorler()
    skor, aciklama = kombinasyon_skoru(f, sektor)
    return skor, aciklama, sektor


# --- Canli makro rejim (market-wide; Fed/TCMB + gunluk degisim, 5 dk cache) ---
_MAKRO_CACHE = {"ts": 0.0, "durum": None}


def makro_durum(ttl: float = _TTL) -> dict:
    """Canli makro rejim skoru + Fed/TCMB surpriz analizi (5 dk onbellekli).

    Hisseden bagimsiz (market-wide). get_macro()'dan Fed/TCMB karar+beklenti,
    yfinance'tan gunluk USD/BIST/Brent degisimini alir. Donus:
    {"rejim": makro_rejim_skoru(...), "fed_tcmb": fed_tcmb_analiz(...)}."""
    now = time.time()
    if _MAKRO_CACHE["durum"] is not None and (now - _MAKRO_CACHE["ts"]) < ttl:
        return _MAKRO_CACHE["durum"]
    deg = _gunluk_degisimler()
    try:
        from src.news.macro import get_macro
        m = get_macro() or {}
    except Exception:
        m = {}
    analiz = fed_tcmb_analiz(
        m.get("fed_degisim_bp"), m.get("fed_beklenti_bp"),
        m.get("tcmb_degisim_bp"), m.get("tcmb_beklenti_bp"))
    rejim = makro_rejim_skoru(
        usdtry_g=deg.get("USDTRY=X"), bist_g=deg.get("XU100.IS"),
        brent_g=deg.get("BZ=F"), fed=analiz.get("fed"), tcmb=analiz.get("tcmb"),
        cds=m.get("turkey_cds"))
    durum = {"rejim": rejim, "fed_tcmb": analiz}
    _MAKRO_CACHE.update(ts=now, durum=durum)
    return durum


def baglam_metni(ticker: str, faktorler: dict = None, makro: dict = None) -> str:
    """AI baglamina eklenecek satir(lar): sektor kombinasyon skoru + makro rejim
    + Fed/TCMB sürpriz ozeti. Her parca veri varsa eklenir; hicbiri yoksa ''.

    makro: onceden hesaplanmis makro_durum() ciktisi (tekrar cekmemek icin
    gecirilebilir); None ise burada (cache'li) hesaplanir."""
    satirlar = []
    skor, aciklama, sektor = skor_for(ticker, faktorler)
    if aciklama:
        gerekceler = "; ".join(m for _, m in aciklama)
        satirlar.append(f"Çoklu faktör skoru: {skor:+d} ({sektor}) — {gerekceler}")
    try:
        durum = makro if makro is not None else makro_durum()
    except Exception:
        durum = None
    rej = (durum or {}).get("rejim") or {}
    if rej.get("rejim"):
        satirlar.append(f"Makro rejim: {rej['rejim']} ({rej['skor']}/100)")
    ozet = ((durum or {}).get("fed_tcmb") or {}).get("ozet")
    if ozet:
        satirlar.append(f"Fed/TCMB faiz: {ozet}")
    return "\n".join(satirlar)
