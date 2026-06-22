"""Tam analiz zinciri: tum veri kaynaklarini birlestirip AI yorumu uretir.

VERI KAYNAKLARI
  1. yfinance  : fiyat, hacim, 10/50 gunluk ortalama, 52 hafta yuksek/dusuk
  2. KAP proxy : son 30 gunluk bildirimler (src/news/kap_source.py)
  3. Haber     : src/news/ kaynaklarindan son 7 gunluk (filtreden gecmis) haberler

AI YORUMU
  Tum veri birlestirilip Claude'a (claude-sonnet-4-6, max_tokens=1000) gonderilir.
  Cikti: karar (AL/TUT/SAT), puan(1-10), risk(1-10), eminlik(Dusuk/Orta/Yuksek),
  gerekce, neden_simdi, fiyatlanmis_mi.
  Risk ajani: risk 9+ ve karar AL ise -> VETO.

CIKTI
  data/ai_commentary.json (web arayuzu bu dosyayi okur) + decisions tablosu.

Calistir:  python -m src.ai.commentary [TICKER ...]
"""
import json
import os
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")
ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = ROOT / "data" / "ai_commentary.json"

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1000

SYSTEM = (
    "Sen 25 yillik tecrubeli bir Turk borsa uzmanisin. Jargon kullanma "
    "(RSI/MACD yasak). Net karar ver: AL/TUT/SAT. Gerekceni 2-3 cumlede soyle. "
    "Veri yoksa yorum yapma. Hata yaparsan kabul et.\n\n"
    "JEOPOLITIK/MAKRO HABER YONU: Jeopolitik haberin yonunu analiz et. Olumsuz haber "
    "+ dogrudan etki = riski artir. Olumlu haber + dogrudan fayda = riski azalt. "
    "Haberin icerigini OKU, sadece 'jeopolitik haber var' deme.\n"
    "Kurallar:\n"
    "- OLUMSUZ haber (kapanma, ambargo, savas, catisma, kriz) VE hisse DOGRUDAN "
    "etkileniyorsa: risk +2 uygula ve AL verme (en fazla TUT).\n"
    "- OLUMLU haber (ateskes, anlasma, acilma, normallesme) VE hisse DOGRUDAN "
    "fayda goruyorsa: risk -1 uygula ve karari AL lehine degerlendir.\n"
    "- Ayni olay bir sektore olumsuz, digerine olumlu olabilir (orn. petrol "
    "fiyati artisi havayoluna olumsuz, rafineri/uretici icin olumlu; TL'nin "
    "zayiflamasi ihracatciya olumlu, doviz borclusuna olumsuz).\n"
    "- Etki dolayli veya belirsizse yonu 'etkisiz/belirsiz' say ve karari teknik "
    "veriye dayandir.\n"
    "Gerekcede ilgili haberin yonunu ACIKCA belirt (orn. 'Hurmuz anlasmasi THY icin "
    "olumlu: yakit/guzergah riski azaliyor').\n\n"
    "ANALIST KONSENSUSU: Veride 'analist_konsensus' varsa dikkate al (kac kurum, "
    "ortalama hedef fiyat, getiri potansiyeli, AL/TUT/SAT dagilimi). Guclu bir "
    "konsensus puani destekler; senin teknik gorusunle celisiyorsa nedenini kisaca "
    "belirt. Hedef fiyati kendi rakamin gibi sunma, 'analistlerin ortalama hedefi' de.\n\n"
    "TEMEL VERILER: Veride 'temel_veriler' varsa sirketin mali sagligini da yorumla "
    "(F/K, PD/DD, ROE, kar marji, borc/ozsermaye, gelir buyumesi, FAVOK marji). "
    "Yuksek F/K/PD/DD pahalilik, dusuk ve pozitif degerler ucuzluk/saglam karlilik "
    "isaret edebilir; yuksek borc/ozsermaye riski artirir; gelir buyumesi ve marjlar "
    "olumlu sinyaldir. Sade dille (jargon yok) acikla; sayilari girdiden birebir al.\n\n"
    "HACIM ANOMALISI: Veride 'hacim_anomalisi' varsa degerlendir. Bugunku hacim son 5 "
    "gun ortalamasinin kac kati (kat) ve seviye (NORMAL/YUKSEK/COK YUKSEK). Yuksek "
    "hacim, fiyat hareketine veya bir habere guclu katilim/ilgi demektir; yonu (yukari/"
    "asagi) fiyat degisimiyle birlikte yorumla. COK YUKSEK hacim dikkatle izlenmeli.\n\n"
    "SEKTOR KORELASYONU: Veride 'sektor_korelasyonu' varsa, hissenin hangi makro "
    "gostergeyle (petrol, dolar, faiz, celik/demir) ve hangi yonde (pozitif/ters) "
    "iliskili oldugunu dikkate al. Piyasa baglamindaki makro veriyle (USD/TRY, faiz) "
    "birlestir: orn. faizle ters iliskili bankada faiz yuksekse bu olumsuzdur; petrolle "
    "ters havayolu icin petrol artisi olumsuzdur. Iliskiyi sade dille gerekceye yansit.\n\n"
    "SEKTOR NOTU: Veride 'sektor_notu' varsa, o sektorde kritik olan faktorleri "
    "(orn. bankada faiz marji/kredi buyumesi/NPL; havacilikta yakit/yolcu/kur) "
    "degerlendirmenin merkezine al. Bu faktorlerden veride ipucu varsa gerekcede "
    "ona deginerek karar ver.\n\n"
    "GENEL PIYASA YONU: Veride 'piyasa_baglami.genel_piyasa' varsa (BIST-100 yonu, "
    "haftalik degisim, yukselen/dusen sayisi, USD/TRY) dikkate al. Piyasa DUSUYORSA "
    "AL kararinda daha secici ve temkinli ol, eminligi abartma; piyasa YUKSELIYORSA "
    "firsatlari daha cesur degerlendir. Genel yonu hissenin kendi verisiyle dengele, "
    "tek basina belirleyici yapma.\n\n"
    "MAKRO GOSTERGELER: Veride 'piyasa_baglami.makro' varsa (USD/TRY, TR 10 yillik "
    "tahvil faizi, TCMB politika faizi, TUFE) dikkate al. Yuksek/yukselen politika "
    "faizi ve tahvil getirisi borsa icin baski yaratir (ozellikle borca/faize duyarli "
    "sektorler: GYO, bankacilik dengesi, yuksek borclu sirketler); faiz dusus beklentisi "
    "destekleyicidir. Kuru ihracatci (lehte) / doviz borclusu (aleyhte) ayrimiyla yorumla. "
    "Bu gostergeleri tek basina belirleyici yapma; hisse verisiyle birlikte degerlendir.\n\n"
    "KENDI KARAR GECMISIN: Veride 'karar_gecmisi_uyari' varsa, bu hissede gecmis "
    "kararlarinin isabetini gosterir. Gecmiste sik yanildiysan ayni yonde israr etme; "
    "daha temkinli ol ve eminligini buna gore ayarla.\n\n"
    "YABANCI YATIRIMCI: Veride 'piyasa_baglami.yabanci_yatirimci' varsa (haftalik net "
    "alim/satim, yabanci payi, yon) dikkate al. Yabanci NET ALICI ise piyasaya guven "
    "isareti (destekleyici), NET SATICI ise baski/cikis isareti (temkinli). Bunu genel "
    "yon ve hisse verisiyle birlikte degerlendir, tek basina belirleyici yapma.\n\n"
    "TARIHSEL SENARYO: Veride 'tarihsel_senaryo' varsa, bu hissenin BENZER makro "
    "kosullarda (faiz/TL/petrol) gecmiste hangi yonde ve hangi olasilikla hareket "
    "ettigini gosterir. Bunu bir egilim/taban olasilik olarak kullan; guncel veri "
    "bu egilimi destekliyorsa eminligi artir, celisiyorsa nedenini belirt. Olasiliklari "
    "kesin gercek gibi sunma ('gecmiste cogunlukla ... egilimindeydi' de)."
)

# --- Sektor bazli statik notlar (hangi faktorler kritik) ---
SEKTOR_NOTLARI = {
    # Havacilik
    "THYAO": "Havacılıkta yakıt maliyeti, yolcu trafiği ve kur riski kritiktir",
    "PGSUS": "Havacılıkta yakıt maliyeti, yolcu trafiği ve kur riski kritiktir",
    "TAVHL": "Havacılıkta yakıt maliyeti, yolcu trafiği ve kur riski kritiktir",
    # Bankacilik
    "GARAN": "Bankacılıkta faiz marjı, kredi büyümesi ve NPL oranı kritiktir",
    "AKBNK": "Bankacılıkta faiz marjı, kredi büyümesi ve NPL oranı kritiktir",
    "ISCTR": "Bankacılıkta faiz marjı, kredi büyümesi ve NPL oranı kritiktir",
    "YKBNK": "Bankacılıkta faiz marjı, kredi büyümesi ve NPL oranı kritiktir",
    "HALKB": "Bankacılıkta faiz marjı, kredi büyümesi ve NPL oranı kritiktir",
    "VAKBN": "Bankacılıkta faiz marjı, kredi büyümesi ve NPL oranı kritiktir",
    # Savunma / teknoloji ihracati
    "ASELS": "Savunmada döviz geliri ve ihracat sözleşmeleri kritiktir",
    "AGHOL": "Savunmada döviz geliri ve ihracat sözleşmeleri kritiktir",
    # Rafineri / gaz
    "TUPRS": "Rafineride ham petrol-ürün makası ve dolar kuru kritiktir",
    "AYGAZ": "Rafineride ham petrol-ürün makası ve dolar kuru kritiktir",
    # Demir-celik
    "EREGL": "Çelikte global fiyat ve enerji maliyeti kritiktir",
    "KRDMD": "Çelikte global fiyat ve enerji maliyeti kritiktir",
    "KORDS": "Çelikte global fiyat ve enerji maliyeti kritiktir",
    # GYO / insaat
    "EKGYO": "Gayrimenkulde faiz, konut talebi ve maliyet enflasyonu kritiktir",
    # Otomotiv
    "TOASO": "Otomotivde iç talep, ihracat ve kur/maliyet dengesi kritiktir",
    "FROTO": "Otomotivde iç talep, ihracat ve kur/maliyet dengesi kritiktir",
    # Cam / sanayi
    "SISE": "Cam sanayinde enerji maliyeti, ihracat ve kapasite kullanımı kritiktir",
    # Petrokimya
    "PETKM": "Petrokimyada ürün-nafta makası ve dolar kuru kritiktir",
    # Perakende / gida
    "BIMAS": "Perakendede enflasyon, ciro büyümesi ve mağaza trafiği kritiktir",
    "MGROS": "Perakendede enflasyon, ciro büyümesi ve mağaza trafiği kritiktir",
    "ULKER": "Gıdada girdi maliyeti, fiyatlama gücü ve ihracat kritiktir",
    "CCOLA": "İçecekte hacim büyümesi, döviz geliri ve girdi maliyeti kritiktir",
    # Dayanikli tuketim
    "ARCLK": "Beyaz eşyada iç talep, ihracat ve kur/maliyet dengesi kritiktir",
    # Telekom
    "TCELL": "Telekomda abone büyümesi, ARPU ve enflasyona endeksli fiyatlama kritiktir",
    "TTKOM": "Telekomda abone büyümesi, ARPU ve enflasyona endeksli fiyatlama kritiktir",
    # Holding
    "KCHOL": "Holdingde iştiraklerin (enerji, otomotiv, finans) toplam performansı kritiktir",
    "SAHOL": "Holdingde iştiraklerin (banka, enerji, sanayi) toplam performansı kritiktir",
    "DOHOL": "Holdingde iştiraklerin (enerji, otomotiv, medya) toplam performansı kritiktir",
    # Altin madencilik
    "KOZAL": "Altın madenciliğinde ons altın fiyatı, üretim ve dolar kuru kritiktir",
    # Taahhut / insaat
    "ENKAI": "Taahhütte yurt dışı projeler, döviz geliri ve enerji yatırımları kritiktir",
}


def _load_dotenv():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------------------
# 1) yfinance piyasa verisi (fiyat, hacim, MA10/50, 52h yuksek/dusuk)
# ---------------------------------------------------------------------------
def _trend(pct):
    if pct is None:
        return "belirsiz"
    return "yukselen" if pct > 1 else ("dusen" if pct < -1 else "yatay")


def _volume_signal(pct):
    if pct is None:
        return "belirsiz"
    return "yuksek" if pct > 25 else ("dusuk" if pct < -25 else "normal")


# Turkiye sabit tarihli resmi tatilleri (ay, gun) - borsa kapali
_TR_SABIT_TATIL = ((1, 1), (4, 23), (5, 1), (5, 19), (7, 15), (8, 30), (10, 29))
# Degisken tarihli dini bayramlar: her yil resmi ilan sonrasi MANUEL eklenir.
_TR_BAYRAM = {
    2026: ((3, 20), (3, 21), (3, 22),                 # Ramazan Bayrami
           (6, 5), (6, 6), (6, 7), (6, 8), (6, 9)),   # Kurban Bayrami
}


def _tr_tatilleri(start, end) -> set:
    """BIST tatilleri: sabit resmi gunler + manuel dini bayram listesi."""
    hols = set()
    for yil in range(start.year, end.year + 1):
        for ay, gun in _TR_SABIT_TATIL:
            hols.add(date(yil, ay, gun))
        for ay, gun in _TR_BAYRAM.get(yil, ()):
            hols.add(date(yil, ay, gun))
    return hols


def _piyasa_tatilleri(market: str, start, end) -> set:
    """Iki tarih arasindaki BORSA tatillerini dondurur (hafta sonu haric).
    ABD icin NYSE tatilleri (federal + Good Friday, Juneteenth dahil); BIST icin
    Turkiye resmi + dini bayram tatilleri. Tatiller iş gunu sayilirsa yanlis
    KILL_SWITCH olusur."""
    if market in ("us", "abd"):
        try:
            import pandas as pd
            from pandas.tseries.holiday import USFederalHolidayCalendar, GoodFriday
            hols = {h.date() for h in
                    USFederalHolidayCalendar().holidays(start=start, end=end)}
            gf = GoodFriday.dates(pd.Timestamp(start), pd.Timestamp(end))
            hols |= {pd.Timestamp(d).date() for d in gf}
            return hols
        except Exception:
            return set()
    return _tr_tatilleri(start, end)


def _veri_bayat(last_date, now=None, market: str = "bist") -> bool:
    """KILL SWITCH: yfinance son bar tarihi 'bayat' mi?
    24 saatten eski VE son bardan sonra en az bir TAM is gunu gecmisse bayattir
    (hafta sonu/borsa tatili tek basina bayat saymaz, yanlis kill onlenir)."""
    now = now or datetime.now(_TZ)
    try:
        last_dt = datetime.combine(last_date, datetime.min.time(), tzinfo=_TZ)
    except Exception:
        return False
    if (now - last_dt).total_seconds() / 3600 <= 24:
        return False
    tatiller = _piyasa_tatilleri(market, last_date, now.date())
    d, biz = last_date + timedelta(days=1), 0
    while d < now.date():
        if d.weekday() < 5 and d not in tatiller:
            biz += 1
        d += timedelta(days=1)
    return biz >= 1


def _kill_kaydi(ticker: str, market: str, neden: str) -> dict:
    """KILL SWITCH kaydi: AI cagrilmaz, decisions'a KILL_SWITCH yazilir."""
    return {
        "ticker": (ticker or "").upper().replace(".IS", ""),
        "market": "abd" if market in ("us", "abd") else "bist",
        "skipped": True, "kill_switch": True,
        "final_decision": "KILL_SWITCH",
        "mesaj": "Sağlıklı analiz yapılamıyor — " + neden,
        "reason": neden,
    }


def market_data(ticker: str, market: str = "bist") -> dict | None:
    """yfinance'den ~1 yillik veriyle kompakt teknik ozet uretir. Veri yoksa None."""
    from src.data.factory import get_data_source

    if market in ("us", "abd"):
        from src.markets.us import US
        symbol = US().to_symbol(ticker)
    else:
        from src.markets.bist import BIST
        symbol = BIST().to_symbol(ticker)
    start = (datetime.now(_TZ).date() - timedelta(days=400)).isoformat()
    try:
        df = get_data_source().get_history(symbol, start=start)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df = df[df["Volume"] > 0]
    if len(df) < 2:
        return None

    # KILL SWITCH icin: son bar tarihi + bayatlik kontrolu
    try:
        last_ts = df.index[-1]
        son_bar = last_ts.date() if hasattr(last_ts, "date") else None
    except Exception:
        son_bar = None
    bayat = _veri_bayat(son_bar, market=market) if son_bar else False

    closes = [float(x) for x in df["Close"].tolist()]
    highs = [float(x) for x in df["High"].tolist()]
    lows = [float(x) for x in df["Low"].tolist()]
    vols = [float(x) for x in df["Volume"].tolist()]

    last, prev = closes[-1], closes[-2]
    gunluk = round((last - prev) / prev * 100, 2) if prev else None

    def ma(n):
        seg = closes[-n:]
        return round(sum(seg) / len(seg), 2) if seg else None

    ma10, ma50 = ma(10), ma(50)
    win = closes[-252:] if len(closes) >= 252 else closes
    hwin = highs[-252:] if len(highs) >= 252 else highs
    lwin = lows[-252:] if len(lows) >= 252 else lows
    hafta52_yuksek = round(max(hwin), 2)
    hafta52_dusuk = round(min(lwin), 2)

    ref = closes[-22] if len(closes) >= 22 else closes[0]   # ~1 ay
    donem = round((last - ref) / ref * 100, 2) if ref else None

    vwin = vols[-20:]
    avg_vol = sum(vwin) / len(vwin) if vwin else 0
    hacim_vs = round((vols[-1] / avg_vol - 1) * 100, 2) if avg_vol else None

    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] * 100
            for i in range(max(1, len(closes) - 20), len(closes)) if closes[i - 1]]
    vol_std = round(statistics.pstdev(rets), 2) if len(rets) >= 2 else 0.0

    rng = hafta52_yuksek - hafta52_dusuk
    konum = round((last - hafta52_dusuk) / rng * 100, 1) if rng > 0 else None

    return {
        "sembol": symbol,
        "son_kapanis": round(last, 2),
        "onceki_kapanis": round(prev, 2),
        "gunluk_degisim_%": gunluk,
        "donem_degisim_%": donem,
        "ma10": ma10,
        "ma50": ma50,
        "hafta52_yuksek": hafta52_yuksek,
        "hafta52_dusuk": hafta52_dusuk,
        "fiyat_konumu_%": konum,
        "son_hacim": int(vols[-1]),
        "ortalama_hacim": int(avg_vol),
        "hacim_vs_ort_%": hacim_vs,
        "hacim_sinyali": _volume_signal(hacim_vs),
        "volatilite_%": vol_std,
        "trend": _trend(donem),
        "bar_sayisi": len(closes),
        "son_bar_tarihi": son_bar.isoformat() if son_bar else None,
        "bayat": bayat,
    }


# ---------------------------------------------------------------------------
# 2+3) KAP bildirimleri (30 gun) + filtreden gecmis haberler (7 gun)
# ---------------------------------------------------------------------------
def gather_news(ticker: str, news_src=None, rss_src=None, market: str = "bist") -> dict:
    """KAP 30g bildirimler + RSS (24s) + son 7 gun haberleri tek listede birlestirir.

    Tum kaynaklar mevcut filtreden gecer: tazelik (YENI/GUNCEL/ESKI = kademe 0-1-2)
    ve fiyatlanma (FIYATLANDI/FIYATLANMADI/VERI_YOK).

    ABD hisseleri icin KAP ve Turkce RSS uygulanmaz (eslesme olmaz); bos doner.
    """
    if market in ("us", "abd"):
        return {"bildirimler": [], "haberler": []}

    from src.news.service import get_news_source
    from src.news.freshness import check_news_freshness
    from src.news.priced_in import check_priced_in

    if news_src is None:
        news_src, _ = get_news_source(verbose=False)

    now = datetime.now(_TZ)
    cutoff7 = now - timedelta(days=7)

    # KAP (30 gun) + RSS (24 saat, hisseye gore filtrelenmis)
    items = []
    try:
        items += news_src.get_news(ticker, limit=20)
    except Exception:
        pass
    if rss_src is not None:
        try:
            items += rss_src.get_news(ticker, limit=10)
        except Exception:
            pass

    bildirimler, haberler, seen = [], [], set()
    for it in items:
        key = (it.title or "").strip().lower()
        if key in seen:
            continue
        seen.add(key)
        fr = check_news_freshness(it.published_at, now=now)
        try:
            pi_status = check_priced_in(it).status
        except Exception:
            pi_status = "VERI_YOK"
        rec = {
            "baslik": it.title,
            "tarih": it.published_at.strftime("%Y-%m-%d %H:%M"),
            "kaynak": it.source,
            "url": getattr(it, "url", None),
            "ozet": getattr(it, "summary", None),
            "tazelik": fr.status.value,
            "fiyatlanma": pi_status,
        }
        bildirimler.append(rec)
        if it.published_at >= cutoff7:
            haberler.append(rec)
    return {"bildirimler": bildirimler, "haberler": haberler}


def market_context(rss_src=None, overview=None) -> dict:
    """Hisseden bagimsiz genel piyasa baglami: son ekonomi basliklari + EVDS makro
    + genel piyasa yonu (BIST-100/USD-TRY/breadth).

    overview: onceden hesaplanmis get_market_overview ciktisi (brifing breadth'i
    tekrar cekmemek icin gecirilebilir); None ise burada hesaplanir.
    """
    from src.news.macro import get_macro

    gundem = []
    if rss_src is not None:
        try:
            for e in rss_src._all_entries()[:6]:
                gundem.append(f"[{e['kaynak']}] {e['baslik']}")
        except Exception:
            pass
    try:
        makro = get_macro()
    except Exception:
        makro = {"available": False}
    if overview is None:
        try:
            from src.news.market_overview import get_market_overview
            overview = get_market_overview()
        except Exception:
            overview = {"available": False}
    try:
        from src.news.foreign_investor import get_foreign_flow
        yabanci = get_foreign_flow()
    except Exception:
        yabanci = {"available": False}
    return {"piyasa_gundemi": gundem, "makro": makro, "genel_piyasa": overview,
            "yabanci_yatirimci": yabanci}


# ---------------------------------------------------------------------------
# AI yorumu (Claude sonnet-4-6)
# ---------------------------------------------------------------------------
from pydantic import BaseModel, Field


class Verdict(BaseModel):
    karar: Literal["AL", "TUT", "SAT"] = Field(description="Net karar")
    puan: int = Field(description="1-10 puan; 10 en olumlu")
    risk: int = Field(description="1-10 risk; 10 en riskli")
    eminlik: Literal["Düşük", "Orta", "Yüksek"] = Field(description="Yorum eminligi")
    gerekce: str = Field(description="2-3 cumle gerekce; sadece verilen veriden")
    neden_simdi: str = Field(description="Bu durum neden BUGUN dikkate deger")
    fiyatlanmis_mi: bool = Field(description="Haber/durum fiyata yansimis mi")


def _ai_verdict(ticker: str, payload: dict, client=None) -> Verdict:
    import anthropic
    client = client or anthropic.Anthropic()
    resp = client.messages.parse(
        model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
        messages=[{"role": "user", "content": (
            f"{ticker} hissesini degerlendir. Yalnizca asagidaki veriyi kullan, "
            "veri uydurma:\n\n" + json.dumps(payload, ensure_ascii=False, indent=2))}],
        output_format=Verdict,
    )
    return resp.parsed_output


_LABEL = {"AL": "AL", "TUT": "TUT", "SAT": "SAT"}


# Verdict pydantic semasinin Batch API icin acik JSON-schema karsiligi
# (batch'te messages.parse yok; output_config.format ile dogrulanir).
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "karar": {"type": "string", "enum": ["AL", "TUT", "SAT"],
                  "description": "Net karar"},
        "puan": {"type": "integer",
                 "description": "1-10 arasi puan; 10 en olumlu (kesinlikle 1-10)"},
        "risk": {"type": "integer",
                 "description": "1-10 arasi risk; 10 en riskli (kesinlikle 1-10)"},
        "eminlik": {"type": "string", "enum": ["Düşük", "Orta", "Yüksek"],
                    "description": "Yorum eminligi"},
        "gerekce": {"type": "string",
                    "description": "2-3 cumle gerekce; sadece verilen veriden"},
        "neden_simdi": {"type": "string",
                        "description": "Bu durum neden BUGUN dikkate deger"},
        "fiyatlanmis_mi": {"type": "boolean",
                           "description": "Haber/durum fiyata yansimis mi"},
    },
    "required": ["karar", "puan", "risk", "eminlik", "gerekce", "neden_simdi",
                 "fiyatlanmis_mi"],
    "additionalProperties": False,
}


def _user_prompt(ticker: str, payload: dict) -> str:
    return (f"{ticker} hissesini degerlendir. Yalnizca asagidaki veriyi kullan, "
            "veri uydurma:\n\n" + json.dumps(payload, ensure_ascii=False, indent=2))


def _prepare_payload(ticker: str, news_src=None, rss_src=None, context=None,
                     market: str = "bist", learning_note=None):
    """Bir hisse icin AI cagrisi oncesi TUM veriyi toplar ve payload kurar.

    Doner: (kill_kaydi | None, payload | None, ctx | None). Kill durumunda
    (kayit, None, None); aksi halde (None, payload, ctx).
    """
    ticker = ticker.upper().replace(".IS", "")
    is_us = market in ("us", "abd")
    sig = market_data(ticker, market=market)
    # --- KILL SWITCH: fiyat verisi yok / bayat ise AI cagrilmaz ---
    if sig is None:
        return _kill_kaydi(ticker, market, "fiyat verisi hiç gelmiyor"), None, None
    if sig.get("bayat"):
        return (_kill_kaydi(ticker, market,
                f"fiyat verisi 24 saatten eski (son veri {sig.get('son_bar_tarihi')})"),
                None, None)

    news = gather_news(ticker, news_src=news_src, rss_src=rss_src, market=market)
    # Analist konsensusu (hedeffiyat + borsaveyatirim) - yalniz BIST
    analist = {"available": False}
    if not is_us:
        try:
            from src.news.analyst_source import get_analyst_consensus
            analist = get_analyst_consensus(ticker)
        except Exception:
            analist = {"available": False}
    # Temel (bilanco) veriler (yfinance .info) - BIST + ABD
    try:
        from src.news.fundamental_source import get_fundamentals
        temel = get_fundamentals(ticker, market=market)
    except Exception:
        temel = {"available": False}
    # Hacim anomalisi (bugun vs son 5 gun ortalamasi) - BIST + ABD
    try:
        from src.news.fundamental_source import get_volume_anomaly
        hacim_anom = get_volume_anomaly(ticker, market=market)
    except Exception:
        hacim_anom = {"available": False}
    # Sektor korelasyonu (statik makro iliski tablosu) - yalniz BIST
    sektor = {"available": False}
    if not is_us:
        try:
            from src.news.fundamental_source import get_sector_correlation
            sektor = get_sector_correlation(ticker)
        except Exception:
            sektor = {"available": False}

    payload = {
        "ticker": ticker,
        "piyasa": sig,
        "kap_bildirimleri_30g": news["bildirimler"],
        "haberler_son": news["haberler"],
    }
    if temel.get("available"):
        payload["temel_veriler"] = {k: temel[k] for k in (
            "fk", "pddd", "roe_%", "kar_marji_%", "borc_ozsermaye",
            "gelir_buyume_%", "favok_marji_%") if temel.get(k) is not None}
    if hacim_anom.get("available"):
        payload["hacim_anomalisi"] = {
            "bugun_hacim": hacim_anom.get("bugun_hacim"),
            "ort_5g_hacim": hacim_anom.get("ort_5g_hacim"),
            "kat": hacim_anom.get("kat"),
            "seviye": hacim_anom.get("seviye"),
        }
    if sektor.get("available"):
        payload["sektor_korelasyonu"] = {
            "ozet": sektor.get("ozet"),
            "korelasyonlar": sektor.get("korelasyonlar"),
        }
    if analist.get("available"):
        payload["analist_konsensus"] = {
            "analist_sayisi": analist.get("analist_sayisi"),
            "ortalama_hedef": analist.get("ortalama_hedef"),
            "potansiyel_%": analist.get("potansiyel"),
            "al": analist.get("al_sayisi"), "tut": analist.get("tut_sayisi"),
            "sat": analist.get("sat_sayisi"), "konsensus": analist.get("konsensus"),
        }
    if context:
        payload["piyasa_baglami"] = context
    # Sektor notu (statik): hangi faktorler kritik - yalniz BIST
    if not is_us:
        sektor_notu = SEKTOR_NOTLARI.get(ticker)
        if sektor_notu:
            payload["sektor_notu"] = sektor_notu
        # Tarihsel senaryo (makro kosullarla eslestirilmis) - yalniz BIST
        try:
            from src.ai.scenarios import get_scenario_context
            _ctx = context or {}
            sen = get_scenario_context(
                ticker, macro_data=_ctx.get("makro"),
                overview=_ctx.get("genel_piyasa"))
            if sen.get("available"):
                payload["tarihsel_senaryo"] = sen.get("metin")
        except Exception:
            pass
    # Kendi karar gecmisi uyarisi (ogrenme)
    if learning_note:
        payload["karar_gecmisi_uyari"] = learning_note

    ctx = {"ticker": ticker, "is_us": is_us, "sig": sig, "news": news,
           "analist": analist, "temel": temel, "hacim_anom": hacim_anom,
           "sektor": sektor}
    return None, payload, ctx


def _finalize_record(ctx: dict, v: "Verdict") -> dict:
    """AI verdict'ini (tek-cagri veya batch) web uyumlu kayda donusturur."""
    ticker = ctx["ticker"]
    is_us = ctx["is_us"]
    sig = ctx["sig"]
    news = ctx["news"]
    analist = ctx["analist"]
    temel = ctx["temel"]
    hacim_anom = ctx["hacim_anom"]
    sektor = ctx["sektor"]

    # Risk ajani: AL + risk>=9 -> VETO
    vetoed = (v.karar == "AL" and v.risk >= 9)
    if vetoed:
        final_decision = "VETO"
        final_label = f"VETO (risk {v.risk}/10) -> islem yok"
    else:
        final_decision = v.karar
        final_label = _LABEL[v.karar]

    gozlemler = [v.neden_simdi]
    if news["haberler"]:
        gozlemler.append(
            f"{len(news['haberler'])} taze haber; fiyatlanmis_mi={v.fiyatlanmis_mi}")

    return {
        "ticker": ticker,
        "symbol": sig["sembol"],
        "market": "abd" if is_us else "bist",
        "para_birimi": "$" if is_us else "₺",
        "skipped": False,
        # --- AI ham ciktisi ---
        "karar": v.karar,
        "puan": v.puan,
        "risk_ai": v.risk,
        "eminlik": v.eminlik,
        "gerekce": v.gerekce,
        "neden_simdi": v.neden_simdi,
        "fiyatlanmis_mi": v.fiyatlanmis_mi,
        # --- web arayuzu uyumlu alanlar ---
        "score": v.puan,
        "risk": {"score": v.risk, "veto": vetoed,
                 "message": f"Risk {v.risk}/10." + (" VETO." if vetoed else "")},
        "vetoed": vetoed,
        "final_decision": final_decision,
        "final_label": final_label,
        "gozlemler": gozlemler,
        "haber_sayisi": len(news["haberler"]),
        "haberler": news["haberler"],
        "kullanilan_on_sinyal": sig,
        "analist": analist if analist.get("available") else None,
        "temel": temel if temel.get("available") else None,
        "hacim_anomalisi": hacim_anom if hacim_anom.get("available") else None,
        "sektor_korelasyonu": sektor if sektor.get("available") else None,
    }


def analyze_stock(ticker: str, news_src=None, rss_src=None, client=None,
                  context=None, market: str = "bist", learning_note=None) -> dict:
    """Tek hisse icin tam zincir (tek AI cagrisi). Web uyumlu kayit dondurur.

    market='bist' (varsayilan) veya 'us'/'abd'. ABD'de KAP/Turkce haber, analist
    konsensusu ve sektor korelasyon tablosu uygulanmaz.
    """
    kill, payload, ctx = _prepare_payload(
        ticker, news_src=news_src, rss_src=rss_src, context=context,
        market=market, learning_note=learning_note)
    if kill is not None:
        return kill
    v = _ai_verdict(ctx["ticker"], payload, client=client)
    return _finalize_record(ctx, v)


# ---------------------------------------------------------------------------
# Zinciri calistir + kaydet + decisions tablosu
# ---------------------------------------------------------------------------
def _persist(results, save: bool, verbose: bool):
    """Sonuclari decisions tablosuna yazar + ai_commentary.json'a kaydeder."""
    from src.db import database as db
    today = datetime.now(_TZ).date().isoformat()
    for r in results:
        try:
            if r.get("kill_switch"):
                db.record_decision(
                    ticker=r["ticker"], karar="KILL_SWITCH", puan=None, risk=None,
                    eminlik=None, gerekce=r.get("mesaj"), tarih=today)
            elif not r.get("skipped"):
                db.record_decision(
                    ticker=r["ticker"], karar=r["final_decision"],
                    puan=r.get("score"), risk=(r.get("risk") or {}).get("score"),
                    eminlik=r.get("eminlik"), gerekce=r.get("gerekce"), tarih=today)
        except Exception as e:
            if verbose:
                print(f"  [{r.get('ticker')}] karar kaydi yazilamadi: {type(e).__name__}")
    if save:
        OUT_PATH.parent.mkdir(exist_ok=True)
        OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        if verbose:
            print(f"\nKaydedildi: {OUT_PATH} ({len(results)} kayit)")


def _verbose_satir(t, r):
    if r.get("kill_switch"):
        return f"  {t:7} KILL_SWITCH ({r.get('reason')})"
    if r.get("skipped"):
        return f"  {t:7} ATLANDI ({r.get('reason')})"
    return (f"  {t:7} {r['final_decision']:5} puan {r['score']}/10 "
            f"risk {r['risk']['score']}/10 {r['eminlik']} haber={r['haber_sayisi']}")


def run_batch(tickers: list[str], save: bool = True, verbose: bool = True,
              overview=None, learning=None, poll_interval: int = 30,
              max_wait: int = 1800) -> list[dict]:
    """Sabah brifingi icin TOPLU (Batch API) calistirma. Tum hisse verilerini
    hazirlar, AI yorumlarini TEK batch isteginde gonderir (%50 daha ucuz),
    batch bitene kadar polling yapar (varsayilan 30 dk, 30 sn'de bir) ve
    sonuclari run() ile AYNI formatta dondurur."""
    from src.news.service import get_news_source
    from src.news.rss_source import RSSNewsSource
    import anthropic
    import time
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY yok - AI yorumu uretilemez.")

    news_src, is_sample = get_news_source(verbose=verbose)
    rss_src = RSSNewsSource()
    context = market_context(rss_src=rss_src, overview=overview)
    learning = learning or {}
    if verbose:
        gp = context.get("genel_piyasa") or {}
        print(f"  [batch] 24s haber: {rss_src.recent_count()} | "
              f"makro: {context['makro'].get('available')} | "
              f"piyasa: {gp.get('yon')}")

    # 1) Her hisse icin veriyi hazirla (AI cagrisi yok)
    order = []                 # [(cid, t)]
    final = {}                 # cid -> kayit (kill/skip dahil)
    ctxs = {}                  # cid -> ctx (AI bekleyenler)
    requests = []
    for i, raw in enumerate(tickers):
        t, _, mk = str(raw).partition(":")
        t = t.strip()
        market = (mk.strip().lower() or "bist")
        cid = f"{i}-{t.upper().replace('.IS', '')}"
        order.append((cid, t))
        try:
            kill, payload, ctx = _prepare_payload(
                t, news_src=news_src, rss_src=rss_src, context=context,
                market=market, learning_note=learning.get(t.upper().replace(".IS", "")))
        except Exception as e:
            final[cid] = {"ticker": t.upper(), "skipped": True,
                          "reason": f"Hata: {type(e).__name__}"}
            if verbose:
                print(f"  [{t}] hazirlik HATA: {type(e).__name__}: {str(e)[:80]}")
            continue
        if kill is not None:
            final[cid] = kill
            continue
        ctxs[cid] = ctx
        requests.append(Request(
            custom_id=cid,
            params=MessageCreateParamsNonStreaming(
                model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
                messages=[{"role": "user", "content": _user_prompt(ctx["ticker"], payload)}],
                output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
            )))

    # 2) Batch gonder + polling
    if requests:
        client = anthropic.Anthropic()
        batch = client.messages.batches.create(requests=requests)
        if verbose:
            print(f"  [batch] {len(requests)} istek gonderildi (id={batch.id}); bekleniyor...")
        waited = 0
        status = batch.processing_status
        while status != "ended":
            if waited >= max_wait:
                if verbose:
                    print(f"  [batch] {max_wait}s doldu, durum={status}; bekleyenler atlanacak.")
                break
            time.sleep(poll_interval)
            waited += poll_interval
            status = client.messages.batches.retrieve(batch.id).processing_status
            if verbose:
                print(f"  [batch] {waited}s · durum={status}")

        # 3) Sonuclari topla (sira garantisi yok -> custom_id ile esle)
        if status == "ended":
            for res in client.messages.batches.results(batch.id):
                cid = res.custom_id
                ctx = ctxs.get(cid)
                if ctx is None:
                    continue
                if res.result.type == "succeeded":
                    try:
                        msg = res.result.message
                        text = next((b.text for b in msg.content if b.type == "text"), "")
                        v = Verdict(**json.loads(text))
                        final[cid] = _finalize_record(ctx, v)
                    except Exception as e:
                        final[cid] = {"ticker": ctx["ticker"], "skipped": True,
                                      "reason": f"Batch parse: {type(e).__name__}"}
                else:
                    final[cid] = {"ticker": ctx["ticker"], "skipped": True,
                                  "reason": f"Batch {res.result.type}"}

    # Hala sonuc gelmeyenler (timeout vb.) -> skipped
    for cid, ctx in ctxs.items():
        if cid not in final:
            final[cid] = {"ticker": ctx["ticker"], "skipped": True,
                          "reason": "Batch sonuc gelmedi (timeout)"}

    # 4) Orijinal sirada birlestir + kaydet
    results = [final[cid] for cid, _ in order if cid in final]
    if verbose:
        for cid, t in order:
            r = final.get(cid)
            if r:
                print(_verbose_satir(t, r))
    _persist(results, save=save, verbose=verbose)
    return results
def run(tickers: list[str], save: bool = True, verbose: bool = True,
        overview=None, learning=None) -> list[dict]:
    from src.news.service import get_news_source
    from src.db import database as db
    import anthropic

    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY yok - AI yorumu uretilemez.")

    from src.news.rss_source import RSSNewsSource

    news_src, is_sample = get_news_source(verbose=verbose)
    rss_src = RSSNewsSource()                       # Bloomberg HT + Investing + Mynet
    # genel piyasa baglami (1 kez); brifing onceden hesaplamissa onu kullan
    context = market_context(rss_src=rss_src, overview=overview)
    learning = learning or {}
    if verbose:
        gp = context.get("genel_piyasa") or {}
        print(f"  [rss] 24s haber: {rss_src.recent_count()} | "
              f"makro: {context['makro'].get('available')} | "
              f"piyasa: {gp.get('yon')} (BIST %{gp.get('bist100_gunluk_%')})")
    client = anthropic.Anthropic()
    today = datetime.now(_TZ).date().isoformat()

    results = []
    for raw in tickers:
        # "TICKER" (bist) veya "TICKER:us"/"TICKER:abd" formatini destekle
        t, _, mk = str(raw).partition(":")
        t = t.strip()
        market = (mk.strip().lower() or "bist")
        try:
            r = analyze_stock(t, news_src=news_src, rss_src=rss_src,
                              client=client, context=context, market=market,
                              learning_note=learning.get(t.upper().replace(".IS", "")))
        except Exception as e:
            if verbose:
                print(f"  [{t}] HATA: {type(e).__name__}: {str(e)[:100]}")
            r = {"ticker": t.upper(), "skipped": True,
                 "reason": f"Hata: {type(e).__name__}"}
        results.append(r)
        if verbose:
            if r.get("kill_switch"):
                print(f"  {t:7} KILL_SWITCH ({r.get('reason')})")
            elif r.get("skipped"):
                print(f"  {t:7} ATLANDI ({r.get('reason')})")
            else:
                print(f"  {t:7} {r['final_decision']:5} puan {r['score']}/10 "
                      f"risk {r['risk']['score']}/10 {r['eminlik']} "
                      f"haber={r['haber_sayisi']}")
        # Karari decisions tablosuna yaz (sonuc=None)
        try:
            if r.get("kill_switch"):
                db.record_decision(
                    ticker=r["ticker"], karar="KILL_SWITCH", puan=None, risk=None,
                    eminlik=None, gerekce=r.get("mesaj"), tarih=today)
            elif not r.get("skipped"):
                db.record_decision(
                    ticker=r["ticker"], karar=r["final_decision"],
                    puan=r.get("score"), risk=(r.get("risk") or {}).get("score"),
                    eminlik=r.get("eminlik"), gerekce=r.get("gerekce"), tarih=today)
        except Exception as e:
            if verbose:
                print(f"  [{t}] karar kaydi yazilamadi: {type(e).__name__}")

    if save:
        OUT_PATH.parent.mkdir(exist_ok=True)
        OUT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        if verbose:
            print(f"\nKaydedildi: {OUT_PATH} ({len(results)} kayit)")
    return results


def main():
    tickers = sys.argv[1:] or ["THYAO", "GARAN", "ASELS", "KCHOL", "TUPRS"]
    print(f"Tam analiz zinciri: {tickers}\n")
    run(tickers)


if __name__ == "__main__":
    main()
