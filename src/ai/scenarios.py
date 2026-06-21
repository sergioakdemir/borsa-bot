"""Tarihsel senaryo kutuphanesi.

Her hisse (sektor grubu) icin statik tarihsel baglam + makro kosullara bagli
olasiliksal egilimler. get_scenario_context, mevcut makro durumu (faiz yuksek mi,
TL zayif mi, petrol ne yapti) tespit edip eslesen kurallardan
"Bu kosulda bu hisse gecmiste %X olasilikla X yonde hareket etti" metni uretir.

NOT: Olasiliklar tarihsel egilimi temsil eden statik tahminlerdir (kesin degildir);
AI'a baglam olarak verilir, tek basina karar olcutu degildir.
"""

# Makro kosul etiketleri:
#   faiz_yuksek, faiz_dusuk, tl_zayif, tl_guclu, petrol_dustu, petrol_yukseldi,
#   celik_yukseldi
# (Petrol/celik icin canli kaynak yoksa o kurallar eslesmeZ; ozet yine gosterilir.)

_GRUPLAR = {
    "havayolu": {
        "ozet": ("Faiz artışında tarihin %60'ında düştü. Petrol -%10'da tarihin "
                 "%75'inde yükseldi. TL değer kaybında tarihin %65'inde yükseldi "
                 "(döviz geliri)."),
        "kurallar": [
            {"kosul": "faiz_yuksek", "yon": "düşüş", "olasilik": 60, "aciklama": "faiz yüksekken"},
            {"kosul": "tl_zayif", "yon": "yükseliş", "olasilik": 65, "aciklama": "TL değer kaybederken (döviz geliri)"},
            {"kosul": "petrol_dustu", "yon": "yükseliş", "olasilik": 75, "aciklama": "petrol gerilerken (yakıt maliyeti)"},
            {"kosul": "petrol_yukseldi", "yon": "düşüş", "olasilik": 70, "aciklama": "petrol yükselirken"},
        ],
    },
    "banka": {
        "ozet": ("Faiz artışında tarihin %70'inde düştü. TCMB faiz indirimi "
                 "beklentisinde tarihin %80'inde yükseldi."),
        "kurallar": [
            {"kosul": "faiz_yuksek", "yon": "düşüş", "olasilik": 70, "aciklama": "faiz yüksekken (marj baskısı)"},
            {"kosul": "faiz_dusuk", "yon": "yükseliş", "olasilik": 80, "aciklama": "faiz düşüş/indirim beklentisinde"},
        ],
    },
    "savunma": {
        "ozet": ("Savunma bütçesi artışında tarihin %85'inde yükseldi. TL "
                 "zayıflamasında tarihin %70'inde yükseldi (dolar ihracatı)."),
        "kurallar": [
            {"kosul": "tl_zayif", "yon": "yükseliş", "olasilik": 70, "aciklama": "TL zayıflarken (dolar ihracatı)"},
        ],
    },
    "rafineri": {
        "ozet": ("Ham petrol -%10'da tarihin %65'inde düştü. Rafinaj marjı "
                 "genişlemesinde tarihin %80'inde yükseldi. TL zayıflamasında "
                 "dolar bazlı gelir desteklenir."),
        "kurallar": [
            {"kosul": "petrol_dustu", "yon": "düşüş", "olasilik": 65, "aciklama": "ham petrol gerilerken"},
            {"kosul": "petrol_yukseldi", "yon": "yükseliş", "olasilik": 60, "aciklama": "ham petrol yükselirken (stok/marj)"},
            {"kosul": "tl_zayif", "yon": "yükseliş", "olasilik": 60, "aciklama": "TL zayıflarken (dolar bazlı gelir)"},
        ],
    },
    "celik": {
        "ozet": ("Global çelik fiyatı +%10'da tarihin %75'inde yükseldi. Enerji "
                 "maliyeti artışında tarihin %60'ında düştü. TL zayıflamasında "
                 "ihracat geliri desteklenir."),
        "kurallar": [
            {"kosul": "celik_yukseldi", "yon": "yükseliş", "olasilik": 75, "aciklama": "global çelik fiyatı artarken"},
            {"kosul": "tl_zayif", "yon": "yükseliş", "olasilik": 60, "aciklama": "TL zayıflarken (ihracat)"},
        ],
    },
    "gyo": {
        "ozet": ("Faiz düşüşünde tarihin %80'inde yükseldi. Konut satışları "
                 "yüksekken tarihin %70'inde yükseldi."),
        "kurallar": [
            {"kosul": "faiz_dusuk", "yon": "yükseliş", "olasilik": 80, "aciklama": "faiz düşerken (konut talebi)"},
            {"kosul": "faiz_yuksek", "yon": "düşüş", "olasilik": 65, "aciklama": "faiz yüksekken (konut kredisi pahalı)"},
        ],
    },
    "otomotiv": {
        "ozet": ("Faiz düşüşünde (kredili satış) tarihin %75'inde yükseldi. TL "
                 "zayıflamasında ihracatçı modeller desteklenir ama ithal girdi "
                 "maliyeti artar (karışık)."),
        "kurallar": [
            {"kosul": "faiz_dusuk", "yon": "yükseliş", "olasilik": 75, "aciklama": "faiz düşerken (taşıt kredisi)"},
            {"kosul": "faiz_yuksek", "yon": "düşüş", "olasilik": 60, "aciklama": "faiz yüksekken (talep daralması)"},
        ],
    },
    "holding": {
        "ozet": ("Geniş piyasa ile birlikte hareket eder; faiz indirimi/risk "
                 "iştahı arttığında tarihin %70'inde yükseldi."),
        "kurallar": [
            {"kosul": "faiz_dusuk", "yon": "yükseliş", "olasilik": 70, "aciklama": "faiz düşerken (risk iştahı)"},
            {"kosul": "faiz_yuksek", "yon": "düşüş", "olasilik": 60, "aciklama": "faiz yüksekken"},
        ],
    },
    "perakende": {
        "ozet": ("Defansif yapı: faiz yüksek/belirsizlik dönemlerinde tarihin "
                 "%65'inde piyasadan iyi performans gösterdi. Enflasyon ciroyu "
                 "nominal büyütür."),
        "kurallar": [
            {"kosul": "faiz_yuksek", "yon": "göreceli güçlü", "olasilik": 65, "aciklama": "faiz yüksek/belirsizlikte (defansif)"},
        ],
    },
    "telekom": {
        "ozet": ("Defansif, enflasyona endeksli gelir: yüksek enflasyon/faiz "
                 "döneminde tarihin %65'inde piyasaya göre dayanıklı kaldı."),
        "kurallar": [
            {"kosul": "faiz_yuksek", "yon": "göreceli güçlü", "olasilik": 65, "aciklama": "faiz yüksekken (defansif, endeksli gelir)"},
        ],
    },
    "cam": {
        "ozet": ("Enerji maliyeti ve ihracata duyarlı: TL zayıflamasında tarihin "
                 "%65'inde yükseldi (ihracat geliri); enerji artışında baskılandı."),
        "kurallar": [
            {"kosul": "tl_zayif", "yon": "yükseliş", "olasilik": 65, "aciklama": "TL zayıflarken (ihracat geliri)"},
        ],
    },
    "altin": {
        "ozet": ("Ons altın ve TL'ye duyarlı: TL zayıflamasında tarihin %70'inde "
                 "yükseldi (dolar bazlı gelir + güvenli liman talebi)."),
        "kurallar": [
            {"kosul": "tl_zayif", "yon": "yükseliş", "olasilik": 70, "aciklama": "TL zayıflarken (dolar bazlı gelir)"},
        ],
    },
    "beyaz_esya": {
        "ozet": ("Faiz düşüşünde (kredili dayanıklı tüketim) tarihin %70'inde "
                 "yükseldi. TL zayıflamasında ihracat artısı vs. ithal girdi "
                 "maliyeti karışık etki yapar."),
        "kurallar": [
            {"kosul": "faiz_dusuk", "yon": "yükseliş", "olasilik": 70, "aciklama": "faiz düşerken (kredili talep)"},
            {"kosul": "faiz_yuksek", "yon": "düşüş", "olasilik": 60, "aciklama": "faiz yüksekken (talep daralması)"},
        ],
    },
    "taahhut": {
        "ozet": ("Yurt dışı projeler ve döviz gelirine dayalı: TL zayıflamasında "
                 "tarihin %70'inde yükseldi."),
        "kurallar": [
            {"kosul": "tl_zayif", "yon": "yükseliş", "olasilik": 70, "aciklama": "TL zayıflarken (döviz geliri)"},
        ],
    },
    "petrokimya": {
        "ozet": ("Ürün-nafta makası ve dolar kuruna duyarlı: petrol/nafta "
                 "gerilerken marj genişler; TL zayıflamasında dolar bazlı gelir."),
        "kurallar": [
            {"kosul": "petrol_dustu", "yon": "yükseliş", "olasilik": 60, "aciklama": "nafta/petrol gerilerken (marj)"},
            {"kosul": "tl_zayif", "yon": "yükseliş", "olasilik": 60, "aciklama": "TL zayıflarken (dolar bazlı gelir)"},
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
        durum = "; ".join(
            f"{k['aciklama']} geçmişte %{k['olasilik']} olasılıkla {k['yon']} yönde "
            "hareket etti" for k in eslesen)
        parcalar.append(f"Şu anki koşullarda ({durum}).")
    metin = " ".join(parcalar)

    return {
        "available": True,
        "ticker": ticker,
        "grup": grup_ad,
        "ozet": grup["ozet"],
        "eslesen": [{"kosul": k["kosul"], "yon": k["yon"],
                     "olasilik": k["olasilik"]} for k in eslesen],
        "metin": metin,
    }


if __name__ == "__main__":
    import json
    macro = {"tr_10y_faiz": 38.0}
    ov = {"usdtry_haftalik_%": 0.8, "petrol_haftalik_%": -7}
    for tk in ("THYAO", "GARAN", "TUPRS", "EKGYO", "ASELS", "BIMAS"):
        print(json.dumps(get_scenario_context(tk, macro, ov), ensure_ascii=False, indent=1))
