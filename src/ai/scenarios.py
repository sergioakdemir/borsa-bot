"""Tarihsel senaryo kutuphanesi.

Her hisse (sektor grubu) icin statik tarihsel baglam + makro kosullara bagli NITEL
egilimler. get_scenario_context, mevcut makro durumu (faiz yuksek mi, TL zayif mi,
petrol ne yapti) tespit edip eslesen kurallardan "bu kosulda gecmiste genelde X
yonde hareket etme egilimindeydi" tarzi NITEL metin uretir.

NOT: Bu egilimler kabaca gecmis davranisi temsil eder (kesin degildir). UYDURMA
OLASILIK TEMIZLIGI (2026-07): AI baglamina artik SAYISAL yuzde/olasilik verilmez
(kaydirilmis kesinlik izlenimi yaratmasin); yalniz nitel yon/egilim aktarilir.
"""

# Makro kosul etiketleri:
#   faiz_yuksek, faiz_dusuk, tl_zayif, tl_guclu, petrol_dustu, petrol_yukseldi,
#   celik_yukseldi
# (Petrol/celik icin canli kaynak yoksa o kurallar eslesmeZ; ozet yine gosterilir.)

# NITEL egilim kutuphanesi. 'ozet' ve kurallar SAYISAL olasilik ICERMEZ (bkz. modul
# docstring, 2026-07 temizligi); yalniz yon/egilim tutulur.
_GRUPLAR = {
    "havayolu": {
        "ozet": ("Faiz artışı dönemlerinde geçmişte genelde geriledi. Petrol "
                 "gerilerken (yakıt maliyeti düşer) çoğunlukla olumlu tepki verdi. "
                 "TL değer kaybında döviz geliriyle desteklenme eğilimindeydi."),
        "kurallar": [
            {"kosul": "faiz_yuksek", "yon": "düşüş", "aciklama": "faiz yüksekken"},
            {"kosul": "tl_zayif", "yon": "yükseliş", "aciklama": "TL değer kaybederken (döviz geliri)"},
            {"kosul": "petrol_dustu", "yon": "yükseliş", "aciklama": "petrol gerilerken (yakıt maliyeti)"},
            {"kosul": "petrol_yukseldi", "yon": "düşüş", "aciklama": "petrol yükselirken"},
        ],
    },
    "banka": {
        "ozet": ("Faiz artışında (marj baskısı) geçmişte genelde geriledi. TCMB "
                 "faiz indirimi beklentisinde çoğunlukla yükselme eğilimindeydi."),
        "kurallar": [
            {"kosul": "faiz_yuksek", "yon": "düşüş", "aciklama": "faiz yüksekken (marj baskısı)"},
            {"kosul": "faiz_dusuk", "yon": "yükseliş", "aciklama": "faiz düşüş/indirim beklentisinde"},
        ],
    },
    "savunma": {
        "ozet": ("Savunma bütçesi artışında geçmişte genelde yükseldi. TL "
                 "zayıflamasında (dolar ihracatı) çoğunlukla olumlu tepki verdi."),
        "kurallar": [
            {"kosul": "tl_zayif", "yon": "yükseliş", "aciklama": "TL zayıflarken (dolar ihracatı)"},
        ],
    },
    "rafineri": {
        "ozet": ("Ham petrol gerilerken geçmişte genelde baskılandı. Rafinaj marjı "
                 "genişlemesinde çoğunlukla yükseldi. TL zayıflamasında dolar bazlı "
                 "gelir desteklenir."),
        "kurallar": [
            {"kosul": "petrol_dustu", "yon": "düşüş", "aciklama": "ham petrol gerilerken"},
            {"kosul": "petrol_yukseldi", "yon": "yükseliş", "aciklama": "ham petrol yükselirken (stok/marj)"},
            {"kosul": "tl_zayif", "yon": "yükseliş", "aciklama": "TL zayıflarken (dolar bazlı gelir)"},
        ],
    },
    "celik": {
        "ozet": ("Global çelik fiyatı artarken geçmişte genelde yükseldi. Enerji "
                 "maliyeti artışında baskılanma eğilimindeydi. TL zayıflamasında "
                 "ihracat geliri desteklenir."),
        "kurallar": [
            {"kosul": "celik_yukseldi", "yon": "yükseliş", "aciklama": "global çelik fiyatı artarken"},
            {"kosul": "tl_zayif", "yon": "yükseliş", "aciklama": "TL zayıflarken (ihracat)"},
        ],
    },
    "gyo": {
        "ozet": ("Faiz düşüşünde geçmişte genelde yükseldi (konut talebi). Faiz "
                 "yüksekken (konut kredisi pahalı) baskılanma eğilimindeydi."),
        "kurallar": [
            {"kosul": "faiz_dusuk", "yon": "yükseliş", "aciklama": "faiz düşerken (konut talebi)"},
            {"kosul": "faiz_yuksek", "yon": "düşüş", "aciklama": "faiz yüksekken (konut kredisi pahalı)"},
        ],
    },
    "otomotiv": {
        "ozet": ("Faiz düşüşünde (kredili satış) geçmişte genelde yükseldi. TL "
                 "zayıflamasında ihracatçı modeller desteklenir ama ithal girdi "
                 "maliyeti artar (karışık)."),
        "kurallar": [
            {"kosul": "faiz_dusuk", "yon": "yükseliş", "aciklama": "faiz düşerken (taşıt kredisi)"},
            {"kosul": "faiz_yuksek", "yon": "düşüş", "aciklama": "faiz yüksekken (talep daralması)"},
        ],
    },
    "holding": {
        "ozet": ("Geniş piyasayla birlikte hareket eder; faiz indirimi/risk iştahı "
                 "arttığında genelde yükselme eğilimindeydi."),
        "kurallar": [
            {"kosul": "faiz_dusuk", "yon": "yükseliş", "aciklama": "faiz düşerken (risk iştahı)"},
            {"kosul": "faiz_yuksek", "yon": "düşüş", "aciklama": "faiz yüksekken"},
        ],
    },
    "perakende": {
        "ozet": ("Defansif yapı: faiz yüksek/belirsizlik dönemlerinde geçmişte "
                 "genelde piyasadan iyi performans gösterdi. Enflasyon ciroyu "
                 "nominal büyütür."),
        "kurallar": [
            {"kosul": "faiz_yuksek", "yon": "göreceli güçlü", "aciklama": "faiz yüksek/belirsizlikte (defansif)"},
        ],
    },
    "telekom": {
        "ozet": ("Defansif, enflasyona endeksli gelir: yüksek enflasyon/faiz "
                 "döneminde geçmişte genelde piyasaya göre dayanıklı kaldı."),
        "kurallar": [
            {"kosul": "faiz_yuksek", "yon": "göreceli güçlü", "aciklama": "faiz yüksekken (defansif, endeksli gelir)"},
        ],
    },
    "cam": {
        "ozet": ("Enerji maliyeti ve ihracata duyarlı: TL zayıflamasında geçmişte "
                 "genelde yükseldi (ihracat geliri); enerji artışında baskılandı."),
        "kurallar": [
            {"kosul": "tl_zayif", "yon": "yükseliş", "aciklama": "TL zayıflarken (ihracat geliri)"},
        ],
    },
    "altin": {
        "ozet": ("Ons altın ve TL'ye duyarlı: TL zayıflamasında geçmişte genelde "
                 "yükseldi (dolar bazlı gelir + güvenli liman talebi)."),
        "kurallar": [
            {"kosul": "tl_zayif", "yon": "yükseliş", "aciklama": "TL zayıflarken (dolar bazlı gelir)"},
        ],
    },
    "beyaz_esya": {
        "ozet": ("Faiz düşüşünde (kredili dayanıklı tüketim) geçmişte genelde "
                 "yükseldi. TL zayıflamasında ihracat vs. ithal girdi maliyeti "
                 "karışık etki yapar."),
        "kurallar": [
            {"kosul": "faiz_dusuk", "yon": "yükseliş", "aciklama": "faiz düşerken (kredili talep)"},
            {"kosul": "faiz_yuksek", "yon": "düşüş", "aciklama": "faiz yüksekken (talep daralması)"},
        ],
    },
    "taahhut": {
        "ozet": ("Yurt dışı projeler ve döviz gelirine dayalı: TL zayıflamasında "
                 "geçmişte genelde yükseldi."),
        "kurallar": [
            {"kosul": "tl_zayif", "yon": "yükseliş", "aciklama": "TL zayıflarken (döviz geliri)"},
        ],
    },
    "petrokimya": {
        "ozet": ("Ürün-nafta makası ve dolar kuruna duyarlı: petrol/nafta "
                 "gerilerken marj genişler; TL zayıflamasında dolar bazlı gelir."),
        "kurallar": [
            {"kosul": "petrol_dustu", "yon": "yükseliş", "aciklama": "nafta/petrol gerilerken (marj)"},
            {"kosul": "tl_zayif", "yon": "yükseliş", "aciklama": "TL zayıflarken (dolar bazlı gelir)"},
        ],
    },
    "kiymetli_maden": {
        "ozet": ("Altın/gümüş kıymetli maden BYF'i: fiyatı doğrudan maden fiyatına, "
                 "dolar/TL kuruna ve enflasyon beklentisine bağlı. Dolar güçlendiğinde "
                 "(TL değer kaybettiğinde) veya enflasyon yükseldiğinde genellikle yukarı "
                 "hareket eder; güvenli liman talebiyle desteklenir."),
        "kurallar": [
            {"kosul": "tl_zayif", "yon": "yükseliş", "aciklama": "dolar güçlenirken / TL değer kaybederken"},
            {"kosul": "enflasyon_yuksek", "yon": "yükseliş", "aciklama": "enflasyon yüksekken (değer saklama / güvenli liman)"},
        ],
    },
}

# Hisse -> grup
_TICKER_GRUP = {
    "THYAO": "havayolu", "PGSUS": "havayolu", "TAVHL": "havayolu",
    "GARAN": "banka", "AKBNK": "banka", "ISCTR": "banka", "YKBNK": "banka",
    "HALKB": "banka", "VAKBN": "banka",
    "ASELS": "savunma",
    "TUPRS": "rafineri",
    "PETKM": "petrokimya",
    "EREGL": "celik", "KRDMD": "celik", "KORDS": "celik",
    "EKGYO": "gyo",
    "FROTO": "otomotiv", "TOASO": "otomotiv",
    "KCHOL": "holding", "SAHOL": "holding", "DOHOL": "holding", "AGHOL": "holding",
    "BIMAS": "perakende", "MGROS": "perakende", "ULKER": "perakende", "CCOLA": "perakende",
    "TCELL": "telekom", "TTKOM": "telekom",
    "SISE": "cam",
    "KOZAL": "altin",
    "ARCLK": "beyaz_esya",
    "ENKAI": "taahhut",
    "GMSTR.F": "kiymetli_maden",
}


def _aktif_kosullar(macro_data: dict | None, overview: dict | None = None) -> set:
    """Mevcut makro durumdan aktif kosul etiketlerini cikarir."""
    macro_data = macro_data or {}
    overview = overview or {}
    aktif = set()

    # Faiz seviyesi (TR 10y veya politika faizi)
    faiz = macro_data.get("tr_10y_faiz")
    if faiz is None:
        faiz = macro_data.get("politika_faizi")
    if isinstance(faiz, (int, float)):
        if faiz >= 35:
            aktif.add("faiz_yuksek")
        elif faiz <= 28:
            aktif.add("faiz_dusuk")

    # Enflasyon seviyesi (TUFE yillik): yuksek enflasyon kiymetli madeni destekler
    tufe = macro_data.get("tufe_yillik")
    if isinstance(tufe, (int, float)) and tufe >= 30:
        aktif.add("enflasyon_yuksek")

    # TL yonu: haftalik USD/TRY degisimi (overview'dan); yoksa makro yok
    usdtry_h = overview.get("usdtry_haftalik_%")
    if isinstance(usdtry_h, (int, float)):
        if usdtry_h >= 0.5:
            aktif.add("tl_zayif")
        elif usdtry_h <= -0.5:
            aktif.add("tl_guclu")

    # Petrol/celik: canli kaynak yok -> opsiyonel disaridan verilebilir
    petrol_h = (overview.get("petrol_haftalik_%")
                if isinstance(overview.get("petrol_haftalik_%"), (int, float)) else None)
    if petrol_h is not None:
        if petrol_h <= -5:
            aktif.add("petrol_dustu")
        elif petrol_h >= 5:
            aktif.add("petrol_yukseldi")
    return aktif


def get_scenario_context(ticker: str, macro_data: dict | None = None,
                         overview: dict | None = None) -> dict:
    """Hisse icin tarihsel senaryo baglami (mevcut makroyla eslestirilmis).

    Doner: {available, grup, ozet, eslesen[], metin}
    """
    ticker = (ticker or "").upper().replace(".IS", "")
    grup_ad = _TICKER_GRUP.get(ticker)
    if not grup_ad:
        return {"available": False, "ticker": ticker}
    grup = _GRUPLAR[grup_ad]

    aktif = _aktif_kosullar(macro_data, overview)
    eslesen = [k for k in grup["kurallar"] if k["kosul"] in aktif]

    parcalar = [f"Tarihsel bağlam: {grup['ozet']}"]
    if eslesen:
        # NITEL: sayisal olasilik verilmez; yalniz gecmis egilim yonu aktarilir.
        durum = "; ".join(
            f"{k['aciklama']} geçmişte genelde {k['yon']} yönde hareket etme "
            "eğilimindeydi" for k in eslesen)
        parcalar.append(f"Şu anki koşullarda ({durum}).")
    metin = " ".join(parcalar)

    return {
        "available": True,
        "ticker": ticker,
        "grup": grup_ad,
        "ozet": grup["ozet"],
        "eslesen": [{"kosul": k["kosul"], "yon": k["yon"]} for k in eslesen],
        "metin": metin,
    }


if __name__ == "__main__":
    import json
    macro = {"tr_10y_faiz": 38.0}
    ov = {"usdtry_haftalik_%": 0.8, "petrol_haftalik_%": -7}
    for tk in ("THYAO", "GARAN", "TUPRS", "EKGYO", "ASELS", "BIMAS"):
        print(json.dumps(get_scenario_context(tk, macro, ov), ensure_ascii=False, indent=1))
