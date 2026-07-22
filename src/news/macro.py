"""Makro veri: TR faiz (10 yillik tahvil getirisi) ve USD/TRY.

Birincil kaynak: investing.com (tr.investing.com) - KAP proxy fallback ile.
EVDS (TCMB) su an erisilemez oldugundan beklemede; anahtar + uygun ag gelince
_evds_series ile devreye alinabilir.

Genel piyasa baglami olarak commentary.py payload'ina eklenir (hisseye ozel degil).
"""
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Istanbul")

_INVESTING = {
    "usdtry": "https://tr.investing.com/currencies/usd-try",
    "tr_10y_faiz": "https://tr.investing.com/rates-bonds/turkey-10-year-bond-yield",
}

# investing.com cekilemezse Yahoo Finance alternatifi (yfinance sembolleri)
_YAHOO = {
    "usdtry": "TRY=X",
    "tr_10y_faiz": "^TYF",
}

# Hicbir kaynak gelmezse kullanilacak son bilinen degerlerin kalici deposu
_SON_BILINEN = Path(__file__).resolve().parents[2] / "data" / "macro_last.json"

# Makullik araliklari: kaynaktan (or. Borsa MCP) gelen deger bu bandin disindaysa
# "supheli veri" olarak loglanir ve macro_last.json'daki son bilinen deger kullanilir.
# Gecici scraping/parse hatalari (or. gram altinin 597 ya da 59770 gelmesi) yayilmasin.
_MAKUL_ARALIK = {
    "gram_altin": (5000, 10000),
    "brent": (40, 200),
}

# TCMB politika faizi (1 hafta repo): once resmi sayfa, sonra EVDS2
_TCMB_FAIZ_URL = ("https://www.tcmb.gov.tr/wps/wcm/connect/TR/tcmb+tr/main+menu/"
                  "para+politikasi/merkez+bankasi+faiz+oranlari")
_EVDS2_URL = "https://evds2.tcmb.gov.tr/index.php?lang=tr"
# Hicbir kaynak ve onceki deger yoksa kullanilacak guvenli sabit (en son bilinen)
_POLITIKA_FAIZI_FALLBACK = 37.0

# TCMB PPK (Para Politikasi Kurulu) toplanti tarihleri (ay, gun) - HER YIL MANUEL GUNCELLE
_PPK_TARIHLERI = {
    # 2026 TAM (resmi). Ikinci yari guncellendi: 23 Tem, 10 Eyl, 22 Eki, 10 Ara.
    2026: ((1, 23), (3, 6), (4, 17), (6, 11), (7, 23), (9, 10), (10, 22), (12, 10)),
    # 2027: TCMB yalniz yilin ILK YARISINI resmen acikladi (21 Oca, 18 Mar, 22 Nis,
    # 10 Haz). Ikinci yari (Tem-Ara) HENUZ ACIKLANMADI; asagidaki 4 tarih 2026
    # temposuna gore TAHMINIDIR — TCMB takvimi yayinlayinca guncellenmeli.
    # Kaynak: tcmb.gov.tr/takvim (2026 tam + 2027 ilk yari).
    2027: ((1, 21), (3, 18), (4, 22), (6, 10),          # resmi (2027 ilk yari)
           (7, 22), (9, 16), (10, 21), (12, 16)),       # TAHMINI (2027 ikinci yari)
}


def ppk_tarihleri(yil=None) -> list:
    """PPK toplanti tarihleri (date listesi, sirali). yil verilirse o yila filtreler."""
    out = []
    for y, gunler in _PPK_TARIHLERI.items():
        if yil is None or y == yil:
            out.extend(date(y, ay, gun) for ay, gun in gunler)
    return sorted(out)


def bugun_ppk_mi(gun=None) -> bool:
    """Verilen gun (vars. bugun) bir PPK toplanti gunu mu?"""
    gun = gun or datetime.now(_TZ).date()
    return gun in set(ppk_tarihleri())


def sonraki_ppk(gun=None, dahil=True):
    """Verilen gunden (vars. bugun) sonraki ilk PPK tarihi. dahil=False ise bugunu
    haric tutar (PPK gununde 'bir sonraki'yi gostermek icin). Yoksa None."""
    gun = gun or datetime.now(_TZ).date()
    for d in ppk_tarihleri():
        if (d >= gun) if dahil else (d > gun):
            return d
    return None


def canli_politika_faizi():
    """TCMB resmi sayfa -> EVDS2 ile guncel politika faizini ceker.
    (deger, kaynak) veya (None, None). PPK gunu otomasyonu kullanir."""
    return _politika_faizi()

# kucuk TTL onbellek (sayfalari her cagride tekrar cekme)
_CACHE = {}
_TTL = 300.0  # saniye


def _num(s):
    """'46,4339' / '1.234,56' -> float (TR ondalik: virgul = ondalik nokta)."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")   # 1.234,56 -> 1234.56
    elif "," in s:
        s = s.replace(",", ".")                     # 46,4339 -> 46.4339
    try:
        return round(float(s), 4)
    except ValueError:
        return None


def _proxies():
    url = os.environ.get("KAP_PROXY_URL")
    return {"http": url, "https": url} if url else None


def _fetch(url, timeout=20):
    """Sayfayi getirir: once dogrudan, sonra KAP proxy. Basarisizsa None."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return None
    for proxies in (None, _proxies()):
        try:
            r = creq.get(url, impersonate="chrome", proxies=proxies, timeout=timeout)
            if r.status_code == 200 and r.text:
                return r.text
        except Exception:
            continue
    return None


_HATA_LOG = Path(__file__).resolve().parents[2] / "logs" / "macro_hata.log"

# Sirayla denenecek fiyat selector'lari (ilki kirilirsa digerleri devreye girer)
_FIYAT_PATTERNS = (
    r'data-test="instrument-price-last"[^>]*>([^<]+)<',          # birincil (DOM)
    r'<meta[^>]+itemprop="price"[^>]+content="([\d.,]+)"',        # microdata meta
    r'"price"\s*:\s*"?([\d.,]+)"?',                               # JSON-LD / state
    r'"last"\s*:\s*"?([\d.,]+)',                                  # eski state alani
    r'(?:og:price:amount|twitter:data1)"[^>]+content="([\d.,]+)"',  # meta og/twitter
)


def _log_macro_hata(url, neden):
    """Sessiz kaybi gorunur kilmak icin macro_hata.log'a yaz."""
    try:
        _HATA_LOG.parent.mkdir(exist_ok=True)
        ts = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
        with _HATA_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] {neden} :: {url}\n")
    except Exception:
        pass


# Spam onleme durumu health_monitor ile AYNI dosyada tutulur (data/health_state.json):
# {sorun_anahtari: 'YYYY-MM-DD'}. Ayni hata icin gunde EN FAZLA 1 Telegram uyarisi;
# sonraki cagrilarda sadece loglanir. (Onceki macro_alert_state.json mekanizmasi spam
# onleyemiyordu -> health_state.json'a tasindi.)
_HEALTH_STATE = Path(__file__).resolve().parents[2] / "data" / "health_state.json"


def _health_state_yukle() -> dict:
    import json
    try:
        return json.loads(_HEALTH_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _health_state_kaydet(state: dict) -> None:
    import json
    try:
        _HEALTH_STATE.parent.mkdir(exist_ok=True)
        _HEALTH_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    except Exception:
        pass


def _uyar_admin_gunluk(anahtar: str, mesaj: str) -> None:
    """Ayni `anahtar` icin gunde EN FAZLA 1 kez yoneticilere (Serhat+Yigit) Telegram
    uyarisi gonderir; bugun zaten bildirildiyse SADECE return (cagiran zaten loglar).
    Durum data/health_state.json'da {anahtar: 'YYYY-MM-DD'} olarak tutulur. Telegram
    gonderilemezse state'e yazilmaz -> ertesi denemede tekrar denenir (spam degil)."""
    bugun = datetime.now(_TZ).date().isoformat()
    state = _health_state_yukle()
    if state.get(anahtar) == bugun:          # bugun zaten bildirildi -> sadece logla
        return
    try:
        from src.notify import telegram
        telegram.notify_admins(mesaj)
    except Exception:
        return                               # gonderilemedi -> state'e yazma, tekrar dene
    state[anahtar] = bugun
    _health_state_kaydet(state)


def _investing_last(url):
    """investing.com enstruman sayfasindan 'son fiyat'i parse eder.

    Birincil selector kirilirsa sirayla alternatifleri (meta/JSON-LD/og) dener.
    Sayfa cekilemezse veya hicbir selector tutmazsa logs/macro_hata.log'a yazar."""
    html = _fetch(url)
    if not html:
        _log_macro_hata(url, "FETCH_BASARISIZ (sayfa cekilemedi)")
        return None
    for i, pat in enumerate(_FIYAT_PATTERNS):
        m = re.search(pat, html)
        if m:
            v = _num(m.group(1))
            if v is not None:
                if i > 0:                        # birincil selector kirildi, alternatif tuttu
                    _log_macro_hata(url, f"BIRINCIL_SELECTOR_KIRIK (alternatif #{i} kullanildi)")
                return v
    _log_macro_hata(url, "TUM_SELECTORLAR_BASARISIZ (HTML geldi ama fiyat bulunamadi)")
    return None


def _yahoo_last(symbol):
    """Yahoo Finance (yfinance) son kapanis degeri; cekilemezse None."""
    if not symbol:
        return None
    try:
        import logging
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # gurultu kapali
        from src.data.factory import get_data_source
        start = (datetime.now(_TZ).date() - timedelta(days=10)).isoformat()
        df = get_data_source().get_history(symbol, start=start)
        if df is None or df.empty:
            return None
        return round(float(df["Close"].iloc[-1]), 4)
    except Exception:
        return None


def _makul_mu(ad, v) -> bool:
    """ad icin _MAKUL_ARALIK'ta band tanimliysa v'nin icinde olup olmadigini doner.
    Band tanimli degilse veya v None ise True (o alan icin kontrol yok)."""
    if v is None:
        return True
    band = _MAKUL_ARALIK.get(ad)
    if not band:
        return True
    return band[0] <= v <= band[1]


def _load_son_bilinen() -> dict:
    """data/macro_last.json'dan son bilinen makro degerleri okur."""
    try:
        import json
        return json.loads(_SON_BILINEN.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _kaydet_son_bilinen(degerler: dict):
    """Taze cekilen makro degerleri son bilinen depoya yazar (birlestirerek)."""
    if not degerler:
        return
    try:
        import json
        _SON_BILINEN.parent.mkdir(exist_ok=True)
        mevcut = _load_son_bilinen()
        mevcut.update(degerler)
        _SON_BILINEN.write_text(
            json.dumps(mevcut, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _parse_politika_faizi(html):
    """HTML'den 1 hafta repo (politika faizi) yuzdesini cikarir; bulunamazsa None."""
    if not html:
        return None
    t = re.sub(r"<[^>]+>", " ", html)
    t = re.sub(r"\s+", " ", t)
    # "1 Hafta Repo" / "Politika Faizi" / "Repo" anahtari yakininda makul bir yuzde ara
    for anahtar in ("1 Hafta Repo", "Hafta Repo", "Politika Faizi", "Repo Faiz", "Repo"):
        m = re.search(re.escape(anahtar) + r"[^\d%]{0,40}%?\s*([0-9]{1,2}(?:[.,][0-9]{1,2})?)",
                      t, re.IGNORECASE)
        if m:
            v = _num(m.group(1))
            if v is not None and 5 <= v <= 100:   # makul politika faizi araligi
                return v
    return None


def _politika_faizi():
    """Guncel TCMB politika faizi: (deger, kaynak) veya (None, None).

    ZINCIR: TCMB resmi sayfasi -> EVDS2 sayfasi -> EVDS serisi (borsapy).
    Ilk ikisi HTML scraping; ikisi de anti-bot/JS yuzunden 22 Tem 2026'da olu
    bulundu -> fonksiyon (None, None) donuyordu ve PPK gunu otomasyonu bu yuzden
    HER ZAMAN "faiz cekilemedi" diyecekti (sessiz bozukluk; PPK gunu gelmedigi
    icin fark edilmemisti). EVDS serisi calisiyor (%37 donduruyor) -> ucuncu
    halka olarak eklendi.

    DIKKAT: burada _evds_politika_faizi_kesin() kullanilir, _evds_borsapy_policy_rate()
    DEGIL. Ikincisi hicbir seri calismazsa sabit %37 dondurur; PPK gunu bu sabit
    "yeni karar" sanilirsa faiz degistigi halde eski deger duyurulur - uydurmanin
    en pahali hali. Kesin surum veri yoksa None doner.
    """
    for url, ad in ((_TCMB_FAIZ_URL, "tcmb"), (_EVDS2_URL, "evds2")):
        v = _parse_politika_faizi(_fetch(url))
        if v is not None:
            return v, ad
    v = _evds_politika_faizi_kesin()
    if v is not None:
        return v, "evds_seri"
    return None, None


def _borsapy_macro() -> dict:
    """borsapy (opsiyonel) ile TUFE (yillik) + politika faizi. Kutuphane yok/hata
    olursa {} doner -> mevcut fallback korunur. NOT: borsapy.policy_rate su an
    hatali (7.0 gibi) deger donebiliyor; makul aralik disi degerler REDDEDILIR."""
    out = {}
    try:
        import borsapy as bp
    except Exception:
        return out
    # TUFE (yillik) - calisiyor (bp.Inflation().latest())
    try:
        enf = bp.Inflation().latest()
        v = enf.get("yearly_inflation") if isinstance(enf, dict) else None
        if v is not None:
            v = round(float(v), 2)
            if 0 < v < 300:                 # makul yillik TUFE araligi
                out["tufe_yillik"] = v
    except Exception:
        pass
    # Politika faizi - makullik kontrolu (borsapy bazen 7.0 gibi hatali doner)
    try:
        pf = bp.policy_rate()
        if pf is not None:
            pf = round(float(pf), 2)
            if 20 <= pf <= 80:              # guncel TR politika faizi makul araligi
                out["politika_faizi"] = pf
    except Exception:
        pass
    return out


# TCMB politika faizi (1 hafta repo) EVDS serileri - SIRAYLA denenir, ilk gecerli
# deger kullanilir. DIKKAT: TP.APIFON4 GECELIK borc verme faizini (%40) verir,
# politika faizini DEGIL -> KALDIRILDI. Politika faizi = haftalik repo = %37
# (11 Haziran 2026 PPK karari). TP.MB.B.B00 / TP.TF.TG.A1 / TP.MB.B.G14 borsapy
# EVDS'te bulunamadi; TP.BISPOLFAIZ.TUR ("Merkez Bankalari Politika Faiz Orani" -
# Turkiye) %37 donduren tek calisan seri. Hicbiri donmezse sabit %37 (FALLBACK).
_EVDS_POLITIKA_SERILERI = (
    "TP.MB.B.B00", "TP.TF.TG.A1", "TP.MB.B.G14", "TP.BISPOLFAIZ.TUR")


def _evds_politika_faizi_kesin():
    """EVDS serilerinden politika faizi — VERI YOKSA None (sabit fallback YOK).

    _evds_borsapy_policy_rate()'in "uydurmayan" surumu. PPK gunu otomasyonu ve
    canli_politika_faizi() bunu kullanir: orada sabit %37 dondurmek, faiz degistigi
    gun eski degeri "yeni karar" diye duyurmak demektir.
    """
    key = os.environ.get("EVDS_API_KEY")
    if not key:
        return None
    try:
        import borsapy as bp
        bp.set_evds_key(key)
    except Exception:
        return None
    for kod in _EVDS_POLITIKA_SERILERI:
        try:
            seri = bp.evds_series(kod)["Value"].dropna()
        except Exception:
            continue                         # seri yok/erisilemez -> sonrakini dene
        if seri.empty:
            continue
        v = round(float(seri.iloc[-1]), 2)
        if 30 <= v <= 45:                    # makul band (borsapy'nin 7.0 bug'ini eler)
            return v
    return None


def _evds_borsapy_policy_rate():
    """GENEL kullanim: EVDS politika faizi; hicbiri calismazsa sabit
    _POLITIKA_FAIZI_FALLBACK (%37).

    Bu sabit, gunluk makro baglaminin faiz alani BOS kalmasin diye vardir ve
    gercek deger uzun suredir %37 oldugu icin pratikte dogrudur. PPK GUNU
    KARAR DUYURUSUNDA KULLANILMAZ -> orada _evds_politika_faizi_kesin().
    """
    v = _evds_politika_faizi_kesin()
    return v if v is not None else _POLITIKA_FAIZI_FALLBACK


def _investing_cpi_yoy(url=None):
    """investing.com ekonomik-takvim event sayfasindan TUFE (yillik) degerini ceker.

    Turkiye CPI (YoY) event URL'i ortamda erisilemez (JS/anti-scraping); bu yuzden
    URL yapilandirilabilir (TUFE_INVESTING_URL). Verilirse event sayfasindaki en
    guncel 'Gerceklesen' (yoksa 'Onceki') yuzde degeri parse edilir.
    """
    url = url or os.environ.get("TUFE_INVESTING_URL")
    if not url:
        return None
    html = _fetch(url)
    if not html:
        return None
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    for m in re.finditer(r"(?:Gerçekleşen|Önceki)\s*:?\s*%?\s*([\d][\d.,]*)", t):
        v = _num(m.group(1))
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------------------
# Fed (ABD Merkez Bankasi) politika faizi - FRED API (ucretsiz)
# https://fred.stlouisfed.org/docs/api/api_key.html adresinden ucretsiz anahtar.
# FRED_API_KEY yoksa veya cekilemezse fed_faiz BILINMIYOR (None) olur.
# FEDFUNDS = efektif federal fon faizi (aylik); son 2 gozlemden degisim (bp) cikar.
#
# NOT (23 Tem 2026): burada eskiden _FED_FAIZ_FALLBACK = 5.25 sabiti vardi ve FRED
# yokken bu sayi gercek veri gibi prompt'a giriyordu. KALDIRILDI - geri eklenmesin.
# Sabit bir "son bilinen faiz" tanimlamak, veriyi uydurmanin sessiz halidir.
# ---------------------------------------------------------------------------
_FRED_FEDFUNDS_URL = ("https://api.stlouisfed.org/fred/series/observations"
                      "?series_id=FEDFUNDS&file_type=json&sort_order=desc&limit=2")


def _fred_fed_funds():
    """FRED FEDFUNDS son 2 gozlemden (faiz%, degisim_bp) dondurur.

    FRED_API_KEY yoksa veya istek basarisizsa (None, None) -> get_macro sabit
    fallback'a duser. degisim_bp = (son - onceki) * 100 (yoksa 0)."""
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return None, None
    try:
        import requests as rq
        r = rq.get(_FRED_FEDFUNDS_URL + f"&api_key={key}",
                   proxies=_proxies(), timeout=20)
        if r.status_code != 200:
            return None, None
        obs = r.json().get("observations") or []
    except Exception:
        return None, None
    vals = []
    for o in obs:                              # sort_order=desc -> en yeni ilk
        try:
            vals.append(float(o.get("value")))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None, None
    faiz = round(vals[0], 2)
    degisim_bp = round((vals[0] - vals[1]) * 100) if len(vals) >= 2 else 0
    return faiz, degisim_bp


# ---------------------------------------------------------------------------
# Dunya piyasalari sabah gostergeleri (yfinance, tek batch):
#   ES=F  -> S&P 500 futures (gece kapanisi yonu, gunluk %)
#   ^VIX  -> korku endeksi (seviye; 20+ risk-off)
#   DX-Y.NYB -> dolar endeksi (seviye; ~104)
#   ^N225 -> Nikkei 225 (Asya, gunluk %)
#   000001.SS -> Shanghai Composite (Asya, gunluk %)
# ---------------------------------------------------------------------------
_DUNYA_SEMBOL = {
    "sp_futures": "ES=F",        # kullanici notu "/ES=F" -> yfinance sembolu "ES=F"
    "vix": "^VIX",
    "dxy": "DX-Y.NYB",
    "nikkei": "^N225",
    "shanghai": "000001.SS",
}


def _dunya_gostergeleri() -> dict:
    """S&P futures / VIX / DXY / Nikkei / Shanghai — TEK yfinance batch.

    Doner: {sp_futures_degisim, vix, dxy, nikkei_degisim, shanghai_degisim}.
    Futures/Nikkei/Shanghai gunluk % (son iki kapanis), VIX/DXY seviye (son kapanis).
    Her alan bagimsiz; cekilemeyen None kalir (get_macro son_bilinen'e duser)."""
    out = {"sp_futures_degisim": None, "vix": None, "dxy": None,
           "nikkei_degisim": None, "shanghai_degisim": None}
    try:
        import logging
        logging.getLogger("yfinance").setLevel(logging.CRITICAL)
        import yfinance as yf
        df = yf.download(list(_DUNYA_SEMBOL.values()), period="5d", interval="1d",
                         progress=False, threads=True, auto_adjust=True)
        closes = df["Close"]
    except Exception:
        return out

    def _son_degisim(sym):
        """(son_kapanis, gunluk_%) — veri yoksa (None, None)."""
        try:
            col = closes[sym].dropna()
        except Exception:
            return None, None
        if len(col) == 0:
            return None, None
        last = float(col.iloc[-1])
        deg = None
        if len(col) >= 2 and float(col.iloc[-2]):
            prev = float(col.iloc[-2])
            deg = round((last - prev) / prev * 100, 2)
        return round(last, 2), deg

    _, out["sp_futures_degisim"] = _son_degisim(_DUNYA_SEMBOL["sp_futures"])
    out["vix"], _ = _son_degisim(_DUNYA_SEMBOL["vix"])
    out["dxy"], _ = _son_degisim(_DUNYA_SEMBOL["dxy"])
    _, out["nikkei_degisim"] = _son_degisim(_DUNYA_SEMBOL["nikkei"])
    _, out["shanghai_degisim"] = _son_degisim(_DUNYA_SEMBOL["shanghai"])
    return out


# ---------------------------------------------------------------------------
# Turkiye CDS (5 yillik, basis point) - MacroVar.com (haftalik guncelleme).
# get_macro() CDS'i macro_last.json'dan (son bilinen) okur; guncelle_cds() Pazartesi
# 09:00 cron ile MacroVar'dan tazeler. CDS makro rejim skoruna girer (300+ risk-off,
# 150 alti risk-on) - bkz. kombinasyon.makro_rejim_skoru.
# ---------------------------------------------------------------------------
_MACROVAR_CDS_URL = "https://macrovar.com/turkey/turkey-credit-default-swaps/"
_CDS_MAKUL = (50, 1500)          # makul Turkiye 5y CDS bandi (bp) - parse hatasi eler


def _macrovar_cds():
    """MacroVar Turkiye CDS sayfasindan guncel 5 yillik CDS'i (basis point, int)
    ceker. Sayfadaki 'Turkey 5Y/5 Year CDS' / 'Credit Default Swap' yakininda makul
    bir sayi (50-1500 bp) aranir. MacroVar Cloudflare arkasinda oldugundan once
    curl_cffi (chrome taklidi), sonra requests denenir. Erisilemez/parse edilemezse
    None (get_macro son bilinen degere duser)."""
    html = _fetch(_MACROVAR_CDS_URL) or _fetch_html(_MACROVAR_CDS_URL)
    if not html:
        _log_macro_hata(_MACROVAR_CDS_URL, "FETCH_BASARISIZ (CDS sayfasi cekilemedi)")
        return None
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    # 'Turkey 5 Year CDS ... 305.20' / 'Credit Default Swap(s) ... 305' kaliplari
    for anahtar in ("Turkey 5Y", "Turkey 5 Year", "Turkey 5-Year", "5Y CDS",
                    "5 Year CDS", "Credit Default Swap", "CDS"):
        for m in re.finditer(re.escape(anahtar) + r"[^0-9]{0,40}([0-9]{2,4}(?:\.[0-9]+)?)",
                             t, re.IGNORECASE):
            try:
                v = int(round(float(m.group(1))))
            except (TypeError, ValueError):
                continue
            if _CDS_MAKUL[0] <= v <= _CDS_MAKUL[1]:
                return v
    _log_macro_hata(_MACROVAR_CDS_URL,
                    "CDS_PARSE_BASARISIZ (sayfa geldi ama makul CDS bulunamadi)")
    return None


def guncel_cds():
    """get_macro icin son bilinen Turkiye CDS (macro_last.json 'turkey_cds'); yoksa None.
    Canli scraping YAPMAZ (haftalik cron guncelledigi degeri okur)."""
    return _load_son_bilinen().get("turkey_cds")


def guncelle_cds(verbose: bool = True):
    """HAFTALIK CRON girisi (Pazartesi 09:00): MacroVar'dan Turkiye CDS'i ceker ve
    macro_last.json'a yazar. Basarisizsa son bilinen deger korunur. Cekilen degeri
    (veya None) dondurur."""
    v = _macrovar_cds()
    if v is not None:
        _kaydet_son_bilinen({"turkey_cds": v})
        if verbose:
            print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] Turkiye CDS guncellendi: {v} bp")
    else:
        eski = guncel_cds()
        if verbose:
            print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] Turkiye CDS alinamadi "
                  f"(MacroVar); son bilinen: {eski}")
    return v


# ---------------------------------------------------------------------------
# TCMB PPK kararlari - data/ppk_kararlari.json (gecmis kararlar + son degisim bp)
# ---------------------------------------------------------------------------
_PPK_KARARLARI = Path(__file__).resolve().parents[2] / "data" / "ppk_kararlari.json"


def ppk_kararlari() -> list:
    """data/ppk_kararlari.json'daki tum PPK kararlarini (tarihe gore eski->yeni
    sirali) dondurur. Dosya yoksa/bozuksa []."""
    try:
        import json
        data = json.loads(_PPK_KARARLARI.read_text(encoding="utf-8"))
        kararlar = data.get("kararlar") if isinstance(data, dict) else data
        if not isinstance(kararlar, list):
            return []
        return sorted(kararlar, key=lambda k: k.get("tarih", ""))
    except Exception:
        return []


def son_ppk_karari() -> dict:
    """En yeni PPK kararini ({tarih, karar_bp, faiz}) dondurur; yoksa {}."""
    k = ppk_kararlari()
    return k[-1] if k else {}


# ---------------------------------------------------------------------------
# Beklenti verisi (sonraki karara dair PIYASA beklentisi, bp cinsinden)
# Fed:  Polymarket Gamma API (ucretsiz, anahtarsiz) - public-search ile SONRAKI FOMC
#       ('Fed Decision in <ay>?') etkinligi bulunur, No change/cut/hike olasiliklari
#       cekilir. CME FedWatch resmi API ucretli, web endpoint'i login-gate; kullanilmadi.
# TCMB: KAYNAK 1 = EVDS Piyasa Katilimcilari Anketi (borsapy, politika faizi beklentisi);
#       KAYNAK 2 = borsagundem.com ekonomist anketi (scraping); KAYNAK 3 = Google News.
# Her ikisi de env override (FED_BEKLENTI_BP / TCMB_BEKLENTI_BP) ile elle girilebilir.
#
# ILKE - VERI UYDURMA YOK: hicbir kaynak veri vermezse tcmb_beklenti_bp None KALIR.
# (Eskiden 0'a dusuyordu; 0 "piyasa degisiklik beklemiyor" DEMEKTIR ve AI prompt'una
# gercek veri gibi giriyordu. Eksik veri, sahte veriden iyidir.) Basarisizlikta:
# alan None + gunde 1 Telegram uyarisi (sonrakiler sadece loglanir, health_state.json).
# ---------------------------------------------------------------------------
def _env_bp(ad):
    """FED_BEKLENTI_BP / TCMB_BEKLENTI_BP env override'ini int bp olarak okur."""
    raw = os.environ.get(ad)
    if not raw:
        return None
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return None


def _bp_from_question(q: str):
    """Polymarket outcome sorusundan isaretli bp cikarir.
    'No change/hold' -> 0; '25 bps decrease' -> -25; '50+ bps hike' -> +50."""
    ql = q.lower()
    m = re.search(r"(\d+)\s*\+?\s*bps?", ql)
    if not m:
        if any(t in ql for t in ("no change", "unchanged", "no hike", "no cut", "hold")):
            return 0
        return None
    bp = int(m.group(1))
    if any(t in ql for t in ("decrease", "cut", "lower")):
        return -bp
    if any(t in ql for t in ("increase", "hike", "raise")):
        return bp
    return None


# Polymarket Gamma API. SONRAKI FOMC kararini bulmak icin public-search
# ('Fed Decision in <ay>?' etkinlikleri) kullanilir; /events taramasi fallback'tir.
_POLYMARKET_SEARCH = "https://gamma-api.polymarket.com/public-search"
_POLYMARKET_ENDPOINTS = (
    "https://gamma-api.polymarket.com/events",
    "https://gamma-api.polymarket.com/events/pagination",
)
_FED_AYLAR = ("january", "february", "march", "april", "may", "june", "july",
              "august", "september", "october", "november", "december")


def _http_json(url, params):
    """GET -> JSON; once proxyli sonra proxysiz cikis dener. 200 disi/hata -> None."""
    import requests as rq
    for proxies in (_proxies(), None):
        try:
            r = rq.get(url, params=params, proxies=proxies, timeout=20)
            if r.status_code == 200:
                return r.json()
        except Exception:
            continue
    return None


def _polymarket_events():
    """Polymarket aktif etkinliklerini (volume azalan) dondurur. Birincil Gamma
    endpoint'i calismazsa alternatif endpoint'i ve proxysiz cikisi dener; hicbiri
    veri vermezse None. (public-search calismazsa fallback olarak kullanilir.)"""
    params = {"closed": "false", "limit": 200, "order": "volume", "ascending": "false"}
    for url in _POLYMARKET_ENDPOINTS:
        data = _http_json(url, params)
        if data is None:
            continue
        # /events -> list; /events/pagination -> {"data": [...]}
        events = data.get("data") if isinstance(data, dict) else data
        if isinstance(events, list) and events:
            return events
    return None


def _polymarket_fed_event(events):
    """events listesinden SONRAKI Fed toplanti karari etkinligini sec. Once
    'fed decision in <ay>' kalibi (sonraki toplanti); yoksa rate/fomc iceren ilk
    Fed etkinligi (yil-toplami 'cuts in'/'rate be at' haric)."""
    for e in events:
        t = (e.get("title") or "").lower() if isinstance(e, dict) else ""
        if "fed" in t and "decision" in t and any(ay in t for ay in _FED_AYLAR):
            return e
    for e in events:
        t = (e.get("title") or "").lower() if isinstance(e, dict) else ""
        if ("fed" in t and any(x in t for x in ("rate", "fomc", "interest", "bps"))
                and "cuts in" not in t and "rate be at" not in t):
            return e
    return None


def _polymarket_fed_decision_event():
    """SONRAKI FOMC kararini ('Fed Decision in <ay>?', su an Temmuz 2026) Polymarket
    Gamma'dan bulur ve market listesiyle birlikte dondurur.

    1) public-search (q='Fed decision') -> basligi 'fed decision in' ile baslayan,
       ACIK (closed=False) ve bitisi bugun/sonrasi olan etkinlikler arasindan EN
       YAKIN bitisli olani (= sonraki toplanti) secilir.
    2) public-search bos donerse /events taramasi (_polymarket_fed_event) fallback.
    Hicbiri vermezse None."""
    bugun = datetime.now(_TZ).date().isoformat()
    data = _http_json(_POLYMARKET_SEARCH, {"q": "Fed decision", "limit_per_type": 20})
    evs = data.get("events") if isinstance(data, dict) else None
    adaylar = []
    for e in evs or []:
        if not isinstance(e, dict):
            continue
        t = (e.get("title") or "").lower()
        end = (e.get("endDate") or "")[:10]      # ISO 'YYYY-MM-DD...' -> tarih kismi
        if t.startswith("fed decision in") and not e.get("closed") and end >= bugun:
            adaylar.append((end, e))
    if adaylar:
        adaylar.sort(key=lambda x: x[0])         # en yakin bitis = sonraki toplanti
        return adaylar[0][1]
    # Fallback: eski /events taramasi
    events = _polymarket_events()
    return _polymarket_fed_event(events) if isinstance(events, list) else None


def _polymarket_yes(mkt):
    """Bir market'in YES (ilk outcome) olasiligini float olarak dondurur; yoksa None."""
    import json
    fiyatlar = mkt.get("outcomePrices")
    if isinstance(fiyatlar, str):
        try:
            fiyatlar = json.loads(fiyatlar)
        except Exception:
            return None
    try:
        return float(fiyatlar[0])                # binary market: ilk outcome = "Yes"
    except (TypeError, ValueError, IndexError):
        return None


def _polymarket_fed_beklenti_bp():
    """Polymarket'tan SONRAKI FOMC kararina dair olasilik-agirlikli beklenti (bp).
    Erisilemez/parse edilemezse None."""
    ev = _polymarket_fed_decision_event()
    if not ev:
        return None
    toplam, agirlik = 0.0, 0.0
    for mkt in ev.get("markets") or []:
        bp = _bp_from_question(mkt.get("groupItemTitle") or mkt.get("question") or "")
        yes = _polymarket_yes(mkt)
        if bp is None or yes is None:
            continue
        toplam += yes * bp
        agirlik += yes
    if agirlik <= 0:
        return None
    return round(toplam / agirlik)               # olasilik-normalize beklenen bp


def _polymarket_fed_olasiliklar():
    """Polymarket'tan SONRAKI FOMC ('Fed Decision in <ay>?', su an Temmuz 2026) icin
    indirim/artis/sabit olasiliklari (%). Her market groupItemTitle ('No change' /
    'XX bps decrease' / 'XX bps increase') yonu, outcomePrices[0] (YES) olasiligi
    verir. Doner: {'indirme': int, 'artis': int, 'sabit': int, 'baslik': str} ya da
    None."""
    ev = _polymarket_fed_decision_event()
    if not ev:
        return None
    indirme = artis = sabit = 0.0
    sayac = 0
    for mkt in ev.get("markets") or []:
        bp = _bp_from_question(mkt.get("groupItemTitle") or mkt.get("question") or "")
        yes = _polymarket_yes(mkt)
        if bp is None or yes is None:
            continue
        if bp < 0:
            indirme += yes
        elif bp > 0:
            artis += yes
        else:
            sabit += yes
        sayac += 1
    if sayac == 0:
        return None
    return {"indirme": round(indirme * 100), "artis": round(artis * 100),
            "sabit": round(sabit * 100), "baslik": ev.get("title")}


def _evds_tcmb_beklenti_bp(mevcut_faiz):
    """EVDS Piyasa Katilimcilari Anketi'nden beklenen politika faizini (TP.BEK.S*
    serisi, env TCMB_BEKLENTI_EVDS_KOD) cekip mevcut faize gore bp farki dondurur.
    Kod/anahtar yoksa veya cekilemezse None."""
    kod = os.environ.get("TCMB_BEKLENTI_EVDS_KOD")
    key = os.environ.get("EVDS_API_KEY")
    if not kod or not key or mevcut_faiz is None:
        return None
    beklenen = _evds_series(kod, "avg", key)
    if beklenen is None:
        return None
    return round((beklenen - mevcut_faiz) * 100)


def _fetch_html(url: str, timeout: int = 15):
    """Bir sayfanin/feed'in metnini dondurur (curl_cffi chrome taklidi, yoksa
    requests). Basarisizsa None."""
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", proxies=_proxies(),
                     timeout=timeout, max_redirects=5)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception:
        pass
    try:
        import requests as rq
        r = rq.get(url, headers={"User-Agent": "Mozilla/5.0"},
                   proxies=_proxies(), timeout=timeout)
        if r.status_code == 200 and r.text:
            return r.text
    except Exception:
        pass
    return None


# Haber metninden beklenti cikarimi GURULTULUDUR. Kural: sadece NET ifade eslesir,
# belirsizde None doner (uydurma yok). Ozellikle:
#   - soru formu ("faiz degisecek mi?", "ne bekliyor?") -> None
#   - gerceklesmis karar ("TCMB faizi 100 bp indirdi") -> None (beklenti degil, sonuc)
#   - yon var ama miktar yok ("indirim bekleniyor") -> None (eskiden -250 UYDURUYORDU)
#   - Fed/ABD veya kredi/mevduat faizi hakkindaki basliklar -> None (konu disi)
_GECMIS_KALIP = ("indirdi", "artirdi", "arttirdi", "yukseltti", "dusurdu",
                 "sabit tuttu", "sabit birakti", "karar verdi", "indirime gitti",
                 "indirim yapti", "artirima gitti", "aciklandi", "acikladi")
# "bekl" koku bilerek kisa: bekleniyor / bekliyor / beklenti / bekleyen hepsini yakalar
# ("bekle" yazilsaydi "bekliyor" ESLESMEZDI - i/e harf degisimi).
_BEKLENTI_KALIP = ("bekl", "tahmin", "ongor", "anket", "olasilik", "fiyatliyor")
_TCMB_KALIP = ("tcmb", "merkez bankasi", "ppk", "para politikasi kurulu",
               "politika faizi")
_KONU_DISI = ("fed ", "fed'", "abd merkez", "fomc", "ecb", "avrupa merkez",
              "kredi faiz", "mevduat faiz", "konut kredi", "tasit kredi")


def _bp_from_tr_text(t: str):
    """Turkce haber metninden TCMB'nin SONRAKI karari icin beklenen faiz degisimini
    (bp) cikarir. Sadece net ifadeler eslesir; belirsiz/konu disi/gecmis ise None.

    'XXX baz puan indirim bekleniyor' -> -XXX; 'sabit tutmasi bekleniyor' -> 0.
    Yon belli ama miktar yoksa None (tahmini adim UYDURULMAZ)."""
    tl = _norm_tr(t or "")
    if not tl:
        return None
    # 1) Konu kontrolu: TCMB hakkinda olmali, Fed/kredi faizi hakkinda olmamali
    if not any(k in tl for k in _TCMB_KALIP):
        return None
    if any(k in tl for k in _KONU_DISI):
        return None
    # 2) Gerceklesmis karar haberi mi? (beklenti degil, sonuc -> kullanma)
    if any(k in tl for k in _GECMIS_KALIP):
        return None
    # 3) Beklenti ifadesi var mi? (yoksa cikarim yapma)
    if not any(k in tl for k in _BEKLENTI_KALIP):
        return None
    # 4) Yon: celiskili sinyal varsa (hem indirim hem artis) -> belirsiz
    indir = any(k in tl for k in ("indir", "dusur", "azalt", "gevseme"))
    artir = any(k in tl for k in ("artir", "arttir", "yukselt", "sikilas"))
    if indir and artir:
        return None
    # 5) Sabit tutma beklentisi -> 0 (net ifade; miktar gerekmez)
    if any(k in tl for k in ("sabit tut", "sabit birak", "faizi sabit",
                             "degisiklik yok", "degisiklige gitmeme",
                             "pas ge", "beklemede kal")):
        if not (indir or artir):
            return 0
    # 6) Miktar: 'XXX baz puan' / 'XXX bp' - yon ile birlikte olmali
    m = re.search(r"(\d{2,4})\s*(?:baz\s*puan|bp\b)", tl)
    if not m:
        return None                              # miktar yok -> UYDURMA, None don
    bp = int(m.group(1))
    if not (5 <= bp <= 2000):                    # makul adim bandi disi -> guvenme
        return None
    if indir:
        return -bp
    if artir:
        return bp
    return None                                  # miktar var ama yon yok -> belirsiz


def _norm_tr(s: str) -> str:
    """TR duyarsiz kucuk harf (ç/ğ/ı/ö/ş/ü -> ascii) - haber metni eslestirme."""
    s = (s or "")
    for a, b in (("İ", "i"), ("I", "i"), ("Ş", "s"), ("Ğ", "g"),
                 ("Ü", "u"), ("Ö", "o"), ("Ç", "c")):
        s = s.replace(a, b)
    s = s.lower()
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"), ("ü", "u"),
                 ("ö", "o"), ("ç", "c"), ("â", "a")):
        s = s.replace(a, b)
    return s


# TCMB Piyasa Katilimcilari Anketi (EVDS kategori 1004 / veri grubu bie_pkauo).
# "Uygun ortalama" (.U) serileri; aylik yayinlanir. DOGRULANMIS kodlar (2026-07):
#   TP.PKAUO.S04.C.U = Ilk Toplanti Icin TCMB Politika Faiz Orani Beklentisi (%)
#   TP.PKAUO.S04.F.U = Ikinci Toplanti Icin TCMB Politika Faiz Orani Beklentisi (%)
# Birincil olan "ilk toplanti"dir: sonraki PPK karari icin piyasa beklentisi = tam
# ihtiyacimiz olan sey. (Eski kodlar TP.PKA.POLFA / TP.BEK.S08* EVDS'te YOKTU -
# hepsi DataNotAvailableError donuyordu, yani bu kaynak hic calismamisti.)
_EVDS_ANKET_KODLARI = ("TP.PKAUO.S04.C.U", "TP.PKAUO.S04.F.U")

# Anket aylik; 70 gunden eski gozlem "bayat" sayilir (bir anket donemi kacmis
# demektir) -> beklenti olarak kullanilmaz, uydurma yerine None doner.
_ANKET_MAX_GUN = 70


def _borsapy_tcmb_beklenti_bp(mevcut_faiz):
    """borsapy EVDS ile TCMB Piyasa Katilimcilari Anketi'nden SONRAKI PPK toplantisi
    icin beklenen politika faizini cekip mevcut faize gore bp farki dondurur.
    EVDS_API_KEY yoksa, seri bulunamazsa veya veri bayatsa None."""
    key = os.environ.get("EVDS_API_KEY")
    if not key or mevcut_faiz is None:
        return None
    try:
        import borsapy as bp
        bp.set_evds_key(key)
    except Exception:
        return None
    for kod in _EVDS_ANKET_KODLARI:
        try:
            seri = bp.evds_series(kod, period="1y")["Value"].dropna()
        except Exception:
            continue
        if seri.empty:
            continue
        beklenen = round(float(seri.iloc[-1]), 2)
        if not (20 <= beklenen <= 80):           # makul politika faizi bandi disi
            continue
        # tazelik: son gozlem tarihi bugune yakin mi?
        try:
            son_gun = seri.index[-1].date()
            yas = (datetime.now(_TZ).date() - son_gun).days
        except Exception:
            yas = None
        if yas is not None and yas > _ANKET_MAX_GUN:
            _log_macro_hata(f"evds:{kod}",
                            f"ANKET_BAYAT (son gozlem {son_gun}, {yas} gun once; "
                            f"beklenti kullanilmadi)")
            continue
        return round((beklenen - mevcut_faiz) * 100)
    return None


def _reuters_tcmb_beklenti_bp(mevcut_faiz=None):
    """Reuters TCMB faiz anketi sayfasindan beklenen politika faizini scrape eder.
    Sayfa metninde 'to XX%'/'%XX' kalibi aranir. Erisilemez/parse edilemezse None."""
    url = ("https://www.reuters.com/markets/rates-bonds/"
           "turkey-central-bank-rate-decision-poll/")
    html = _fetch_html(url)
    if not html:
        return None
    import re as _re
    metin = _re.sub(r"<[^>]+>", " ", html)
    # 'keep/cut/hold ... to 37%' veya 'policy rate at 37%'
    m = _re.search(r"(?:to|at|of)\s*(\d{2}(?:\.\d+)?)\s*%", metin)
    if not m:
        return None
    beklenen = float(m.group(1))
    if not (20 <= beklenen <= 80):
        return None
    if mevcut_faiz is None:
        return 0
    return round((beklenen - mevcut_faiz) * 100)


_BORSAGUNDEM_TCMB_URL = ("https://www.borsagundem.com.tr/"
                         "ekonomistlerin-tcmb-faiz-ve-enflasyon-beklentilerinde-son-rakamlar")


# Devre kesici (circuit breaker): cokmus kaynak her get_macro cagrisini yavaslatmasin.
# borsagundem 522 verdiginde 4 deneme x 20s = ~78sn boslugu her cagride oduyordu
# (get_macro 30 dk'da bir cagriliyor). Ust uste _DEVRE_ESIK basarisizliktan sonra
# kaynak _DEVRE_SURE boyunca hic denenmez. Durum health_state.json'da tutulur.
_DEVRE_ESIK = 3                                  # ust uste kac basarisizliktan sonra
_DEVRE_SURE = 3600                               # kac saniye atlanacak (1 saat)
_BORSAGUNDEM_TIMEOUT = 5                         # eskiden 20s; cokuk sunucuda bekleme


def _devre_acik_mi(ad: str) -> bool:
    """Kaynak devre kesici ile gecici olarak devre disi mi? (suresi dolduysa sifirlar)"""
    st = _health_state_yukle()
    d = st.get(f"devre_{ad}") or {}
    if not isinstance(d, dict) or d.get("sayac", 0) < _DEVRE_ESIK:
        return False
    try:
        kalan = _DEVRE_SURE - (time.time() - float(d.get("ts", 0)))
    except (TypeError, ValueError):
        return False
    if kalan <= 0:                               # sure doldu -> devreyi kapat, tekrar dene
        st.pop(f"devre_{ad}", None)
        _health_state_kaydet(st)
        return False
    return True


def _devre_bildir(ad: str, basarili: bool) -> None:
    """Kaynak sonucunu devre kesiciye isler: basarili -> sifirla, degilse sayaci artir."""
    st = _health_state_yukle()
    anahtar = f"devre_{ad}"
    if basarili:
        if anahtar in st:
            st.pop(anahtar, None)
            _health_state_kaydet(st)
        return
    d = st.get(anahtar) if isinstance(st.get(anahtar), dict) else {}
    sayac = int(d.get("sayac", 0)) + 1
    st[anahtar] = {"sayac": sayac, "ts": time.time()}
    _health_state_kaydet(st)
    if sayac == _DEVRE_ESIK:
        _log_macro_hata(ad, f"DEVRE_KESICI_ACILDI ({sayac} ust uste basarisizlik "
                            f"-> {_DEVRE_SURE // 60} dk atlanacak)")


def _borsagundem_fetch():
    """borsagundem (TR sitesi) sayfasini getirir. TR cikis gerektiginden ONCE
    proxysiz (dogrudan), sonra proxyli dener. curl_cffi yoksa requests'e duser.

    Devre kesici: ust uste 3 basarisizliktan sonra 1 saat hic denenmez (cokmus
    sunucu her makro cagrisini ~78sn yavaslatmasin diye)."""
    if _devre_acik_mi("borsagundem"):
        return None
    for proxies in (None, _proxies()):           # TR site -> once dogrudan
        try:
            from curl_cffi import requests as creq
            r = creq.get(_BORSAGUNDEM_TCMB_URL, impersonate="chrome",
                         proxies=proxies, timeout=_BORSAGUNDEM_TIMEOUT)
            if r.status_code == 200 and r.text:
                _devre_bildir("borsagundem", True)
                return r.text
        except Exception:
            pass
        try:
            import requests as rq
            r = rq.get(_BORSAGUNDEM_TCMB_URL, headers={"User-Agent": "Mozilla/5.0"},
                       proxies=proxies, timeout=_BORSAGUNDEM_TIMEOUT)
            if r.status_code == 200 and r.text:
                _devre_bildir("borsagundem", True)
                return r.text
        except Exception:
            pass
    _devre_bildir("borsagundem", False)
    return None


def _borsagundem_tcmb_beklenti_bp(mevcut_faiz=None):
    """borsagundem.com ekonomist anketi sayfasindan beklenen politika faizini
    scrape eder. 'politika faiz(i) ... %XX,XX' (medyan/ortalama tahmin) kalibini
    yakalar, mevcut faize gore bp farkini dondurur. Erisilemez/parse edilemezse None."""
    html = _borsagundem_fetch()
    if not html:
        return None
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
    # 'politika faizi medyan tahmini %37,00' / 'politika faiz orani beklentisi %XX'
    m = re.search(r"politika faiz[^%]{0,60}%\s*([0-9]{1,2}(?:[.,][0-9]{1,2})?)",
                  t, re.IGNORECASE)
    if not m:
        return None
    beklenen = _num(m.group(1))
    if beklenen is None or not (20 <= beklenen <= 80):   # makul politika faizi bandi
        return None
    if mevcut_faiz is None:
        return 0
    return round((beklenen - mevcut_faiz) * 100)


def _gnews_tcmb_beklenti_bp():
    """Google News RSS 'TCMB faiz beklenti anketi' -> son haberlerden beklenen
    degisim (bp). Yalniz NET ifadeler eslesir (bkz. _bp_from_tr_text).

    Tek bir baslige guvenmez: ilk 20 haberdeki eslesmeler TOPLANIR ve hepsi ayni
    degeri gostermiyorsa None doner (celiskili haber akisindan sayi uydurmamak icin).
    Bu kaynak en zayif halkadir; EVDS/borsagundem bos donerse devreye girer."""
    url = ("https://news.google.com/rss/search?q="
           "TCMB+faiz+beklenti+anketi&hl=tr&gl=TR&ceid=TR:tr")
    text = _fetch_html(url)
    if not text:
        return None
    try:
        import feedparser
        feed = feedparser.parse(text)
    except Exception:
        return None
    bulunan = []
    for e in feed.entries[:20]:
        ozet = re.sub(r"<[^>]+>", "", e.get("summary") or "")
        bp = _bp_from_tr_text(f"{e.get('title') or ''} {ozet}")
        if bp is not None:
            bulunan.append(bp)
    if not bulunan:
        return None
    if len(set(bulunan)) > 1:                    # haberler celisiyor -> guvenme
        _log_macro_hata("gnews_tcmb_beklenti",
                        f"CELISKILI_BEKLENTI (haber basliklarindan {sorted(set(bulunan))} "
                        f"cikti; belirsiz -> kullanilmadi)")
        return None
    return bulunan[0]


def fed_beklenti_bp():
    """Fed sonraki karar beklentisi (bp): env override -> Polymarket -> None."""
    ov = _env_bp("FED_BEKLENTI_BP")
    if ov is not None:
        return ov
    return _polymarket_fed_beklenti_bp()


def _tcmb_beklenti_kaynaklari(mevcut_faiz=None):
    """TCMB beklenti kaynak zinciri: [(ad, cagrilabilir), ...] oncelik sirasiyla.
    Tek yerde tanimli ki hem veri cekimi hem saglik kontrolu AYNI listeyi kullansin
    (kaynak sayisi hakkinda yanilmayalim - bkz. beklenti_kaynak_sagligi)."""
    return [
        ("EVDS(anket)", lambda: _borsapy_tcmb_beklenti_bp(mevcut_faiz)),
        ("borsagundem", lambda: _borsagundem_tcmb_beklenti_bp(mevcut_faiz)),
        ("EVDS3(fe)", lambda: _evds_tcmb_beklenti_bp(mevcut_faiz)),
        ("Reuters", lambda: _reuters_tcmb_beklenti_bp(mevcut_faiz)),
        ("GoogleNews", _gnews_tcmb_beklenti_bp),
    ]


def _tcmb_beklenti_bp_ve_kaynak(mevcut_faiz=None):
    """TCMB sonraki karar beklentisi -> (bp, kaynak_adi). Sira: env override ->
    EVDS Piyasa Katilimcilari Anketi -> borsagundem -> EVDS3 /fe -> Reuters ->
    Google News. Hicbiri veri vermezse (None, None) - UYDURMA YOK."""
    ov = _env_bp("TCMB_BEKLENTI_BP")
    if ov is not None:
        return ov, "env"
    for ad, fn in _tcmb_beklenti_kaynaklari(mevcut_faiz):
        try:
            v = fn()
        except Exception:
            v = None
        if v is not None:
            return v, ad
    return None, None


def _tcmb_beklenti_bp_raw(mevcut_faiz=None):
    """Geriye donuk uyumluluk: sadece bp degerini doner (kaynak adi olmadan)."""
    return _tcmb_beklenti_bp_ve_kaynak(mevcut_faiz)[0]


def beklenti_kaynak_sagligi(mevcut_faiz=None) -> dict:
    """TCMB faiz beklentisi icin KAC KAYNAK gercekten calisiyor? Her kaynagi tek tek
    dener ve sonucu raporlar.

    Amac: "5 kaynakli yedek zincir var" yanilgisini onlemek. 21 Tem 2026'da zincirin
    5 kaynagindan 4'u sessizce oluydu (EVDS seri kodlari yanlis, EVDS3 kodu tanimsiz,
    Reuters 401, GNews parser hic eslesmiyor) - tek calisan kaynak borsagundem'di ve o
    cokunce veri tamamen kesildi. Bu fonksiyon o durumu ONCEDEN gorunur kilar.

    Donus: {"calisan": [ad...], "olu": [(ad, sebep)...], "sayi": int, "tek_nokta": bool}
    """
    if mevcut_faiz is None:
        try:
            mevcut_faiz = get_macro().get("politika_faizi")
        except Exception:
            mevcut_faiz = None
    calisan, olu = [], []
    for ad, fn in _tcmb_beklenti_kaynaklari(mevcut_faiz):
        try:
            v = fn()
        except Exception as e:
            olu.append((ad, f"{type(e).__name__}: {str(e)[:60]}"))
            continue
        if v is None:
            olu.append((ad, "veri yok"))
        else:
            calisan.append(ad)
    return {"calisan": calisan, "olu": olu, "sayi": len(calisan),
            "tek_nokta": len(calisan) == 1}


def tcmb_beklenti_bp(mevcut_faiz=None):
    """TCMB sonraki karar beklentisi (bp). Kaynaklar (bkz. _tcmb_beklenti_bp_raw)
    calismazsa HATA ATMAZ; None dondurur ve durumu logs/macro_hata.log'a yazar.

    ONEMLI: veri yoksa 0 DONDURMEZ. 0 = "piyasa degisiklik beklemiyor" seklinde
    gecerli bir beklentidir; uydurulmus 0 AI'ya gercek veri gibi gider. Bilinmiyorsa
    None doner ve cagiran taraf alani hic gostermez (bkz. get_macro 6c)."""
    v = _tcmb_beklenti_bp_raw(mevcut_faiz)
    if v is not None:
        return v
    _log_macro_hata("tcmb_beklenti",
                    "TCMB_BEKLENTI_ALINAMADI (EVDS/borsagundem/GNews bos) "
                    "-> beklenti BILINMIYOR (None; uydurma yok)")
    return None


def get_macro() -> dict:
    """Makro gostergeleri dondurur (iki kaynak birlesik).

    - USD/TRY ve TR 10 yillik faiz: investing.com (su an calisiyor)
    - Politika faizi ve TUFE: EVDS3 (EVDS_API_KEY + KYC'li proxy gelince otomatik)

    Hangi kaynak veri verirse o alan dolar; hicbiri gelmezse available=False.
    """
    now = time.monotonic()
    hit = _CACHE.get("macro")
    if hit and (now - hit[0]) < _TTL:
        return hit[1]

    out = {"available": False, "kaynaklar": []}
    son_bilinen = _load_son_bilinen()
    taze = {}

    # 0) BORSA MCP (BIRINCIL): USD/TRY + gram altin (+ EUR/TRY, brent bonus).
    # Basarisizsa asagidaki investing.com -> yahoo -> son_bilinen zinciri devralir.
    try:
        from src.news.borsa_mcp import get_macro as _mcp_macro
        mcp = _mcp_macro()
    except Exception:
        mcp = None
    if mcp:
        for kaynak_ad, hedef in (("usdtry", "usdtry"), ("gram_altin", "gram_altin"),
                                 ("eur_try", "eur_try"), ("brent", "brent")):
            v = mcp.get(kaynak_ad)
            if v is None:
                continue
            if not _makul_mu(hedef, v):
                # MCP supheli deger dondu -> logla + son bilinen degere dus, yayma
                sv = son_bilinen.get(hedef)
                _log_macro_hata(
                    f"borsa_mcp:{kaynak_ad}",
                    f"SUPHELI_VERI ({hedef}={v} makul aralik {_MAKUL_ARALIK[hedef]} "
                    f"disinda; son bilinen deger={sv} kullanildi)")
                if sv is not None:
                    out[hedef] = sv
                    if "son_bilinen" not in out["kaynaklar"]:
                        out["kaynaklar"].append("son_bilinen")
                continue
            out[hedef] = v
            taze[hedef] = v
        if any(out.get(a) is not None for a in
               ("usdtry", "gram_altin", "eur_try", "brent")):
            out["kaynaklar"].append("borsa_mcp")

    # 1) usdtry + tr_10y_faiz: Borsa MCP gelmediyse investing.com -> Yahoo -> son bilinen
    for ad, url in _INVESTING.items():
        if out.get(ad) is not None:        # MCP'den (or. usdtry) zaten geldi -> atla
            continue
        v = _investing_last(url)
        if v is not None:
            out[ad] = v
            taze[ad] = v
            if "investing.com" not in out["kaynaklar"]:
                out["kaynaklar"].append("investing.com")
            continue
        # investing.com basarisiz -> Yahoo Finance alternatifi
        yv = _yahoo_last(_YAHOO.get(ad))
        if yv is not None:
            out[ad] = yv
            taze[ad] = yv
            if "yahoo" not in out["kaynaklar"]:
                out["kaynaklar"].append("yahoo")
            continue
        # her iki kaynak da basarisiz -> son bilinen degere dus + logla
        sv = son_bilinen.get(ad)
        out[ad] = sv
        _log_macro_hata(_YAHOO.get(ad) or ad,
                        f"YEDEK_KULLANILDI (investing+yahoo basarisiz, "
                        f"son bilinen deger={sv})")
        if sv is not None and "son_bilinen" not in out["kaynaklar"]:
            out["kaynaklar"].append("son_bilinen")

    # 2) Politika faizi + TUFE
    out["politika_faizi"] = None
    out["tufe_yillik"] = None

    # 2-pre) POLITIKA FAIZI BIRINCIL: EVDS (borsapy ile, EVDS_API_KEY). borsapy'nin
    # policy_rate()'i yanlis (7.0) doner; EVDS serisi (TP.BISPOLFAIZ.TUR = TCMB
    # politika faizi, %37) dogru guncel degeri verir. Basarisizsa asagidaki
    # borsapy/EVDS3/TCMB zinciri devralir.
    pf_evds = _evds_borsapy_policy_rate()
    if pf_evds is not None:
        out["politika_faizi"] = pf_evds
        taze["politika_faizi"] = pf_evds
        if "EVDS(borsapy)" not in out["kaynaklar"]:
            out["kaynaklar"].append("EVDS(borsapy)")

    # 2a) borsapy (opsiyonel): TUFE + politika faizi. Zaten dolu alani EZMEZ;
    # politika faizi yukarida EVDS'ten geldiyse korunur. Basarisiz/yok ise atla.
    bpy = _borsapy_macro()
    for ad in ("tufe_yillik", "politika_faizi"):
        if bpy.get(ad) is not None and out.get(ad) is None:
            out[ad] = bpy[ad]
            taze[ad] = bpy[ad]
            if "borsapy" not in out["kaynaklar"]:
                out["kaynaklar"].append("borsapy")

    # 2b) EVDS3 (KYC sonrasi) - yalniz borsapy'den gelmeyen alanlari doldurur
    key = os.environ.get("EVDS_API_KEY")
    if key:
        evds_katki = False
        for ad in ("politika_faizi", "tufe_yillik"):
            if out.get(ad) is not None:
                continue
            code, agg = _EVDS_SERIES[ad]
            v = _evds_series(code, agg, key)
            if v is not None:
                out[ad] = v
                taze[ad] = v
                evds_katki = True
        if evds_katki and "EVDS3" not in out["kaynaklar"]:
            out["kaynaklar"].append("EVDS3")

    # 2c) Politika faizi hala yoksa: TCMB sayfasi -> EVDS2 -> son bilinen -> 46.0
    if out.get("politika_faizi") is None:
        pf, pk = _politika_faizi()
        if pf is not None:
            out["politika_faizi"] = pf
            taze["politika_faizi"] = pf
            if pk not in out["kaynaklar"]:
                out["kaynaklar"].append(pk)
        else:
            sv = son_bilinen.get("politika_faizi", _POLITIKA_FAIZI_FALLBACK)
            out["politika_faizi"] = sv
            _log_macro_hata(_TCMB_FAIZ_URL,
                            f"YEDEK_KULLANILDI (TCMB+EVDS2 basarisiz, "
                            f"politika faizi={sv})")
            if "son_bilinen" not in out["kaynaklar"]:
                out["kaynaklar"].append("son_bilinen")

    # 3) TUFE EVDS'ten gelmediyse investing.com event sayfasindan (TUFE_INVESTING_URL)
    if out.get("tufe_yillik") is None:
        tv = _investing_cpi_yoy()
        if tv is not None:
            out["tufe_yillik"] = tv
            out["kaynaklar"].append("investing.com(TUFE)")

    # 4) Fed (ABD) politika faizi - FRED (ucretsiz).
    # UYDURMA YASAGI (23 Tem 2026): eskiden FRED yoksa kodda gomulu sabit 5.25
    # doner ve GERCEK VERI GIBI prompt'a girerdi. FRED_API_KEY hic tanimlanmadigi
    # icin bu sayi 26 Haziran'dan beri hic degismeden besleniyordu - sessiz sahte
    # veri. Artik TCMB beklentisiyle AYNI kural: gercek veri yoksa alan None kalir,
    # prompt'ta "VERI YOK - yorum yapma" olarak isaretlenir (bkz.
    # commentary._makro_temizle). Daha once GERCEKTEN cekilmis bir deger varsa
    # (macro_last.json) o kullanilir - bu uydurma degil, bayat gercek veridir.
    fed_faiz, fed_degisim = _fred_fed_funds()
    if fed_faiz is not None:
        out["fed_faiz"] = fed_faiz
        out["fed_degisim_bp"] = fed_degisim
        taze["fed_faiz"] = fed_faiz
        if "FRED" not in out["kaynaklar"]:
            out["kaynaklar"].append("FRED")
    elif son_bilinen.get("fed_faiz") is not None:
        out["fed_faiz"] = son_bilinen["fed_faiz"]
        out["fed_degisim_bp"] = None          # degisim bilinmiyor -> 0 VARSAYMA
        if "son_bilinen" not in out["kaynaklar"]:
            out["kaynaklar"].append("son_bilinen")
        _log_macro_hata("fred", "YEDEK_KULLANILDI (FRED yok, son bilinen "
                                f"fed_faiz={son_bilinen['fed_faiz']})")
    else:
        out["fed_faiz"] = None                # UYDURMA YOK
        out["fed_degisim_bp"] = None
        _log_macro_hata("fred", "FED_FAIZI_ALINAMADI (FRED_API_KEY yok/istek "
                                "basarisiz, son bilinen deger de yok) "
                                "-> fed_faiz BILINMIYOR (None; uydurma yok)")
        _uyar_admin_gunluk("fred_hata",
                           "Fed politika faizi alınamadı (FRED_API_KEY tanımlı değil) "
                           "-> fed_faiz VERİ YOK olarak işaretlendi (sabit sayı beslenmiyor).")

    # 5) TCMB son PPK karar degisimi (data/ppk_kararlari.json)
    spk = son_ppk_karari()
    out["tcmb_degisim_bp"] = spk.get("karar_bp")
    if spk:
        out["son_ppk_tarihi"] = spk.get("tarih")
        if "ppk_kararlari" not in out["kaynaklar"]:
            out["kaynaklar"].append("ppk_kararlari")

    # 6) Beklenti (sonraki karara dair piyasa beklentisi, bp) - best-effort, ucretsiz
    out["fed_beklenti_bp"] = fed_beklenti_bp()
    # TCMB beklenti: veri yoksa UYDURMA -> None kalir (bkz. 6c). 0 gecerli bir
    # beklentidir ("degisiklik beklenmiyor"), varsayilan olarak kullanilamaz.
    tcmb_ham, tcmb_kaynak = _tcmb_beklenti_bp_ve_kaynak(out.get("politika_faizi"))
    out["tcmb_beklenti_bp"] = tcmb_ham
    out["tcmb_beklenti_kaynak"] = tcmb_kaynak    # veri hangi kaynaktan geldi (izlenebilirlik)
    if tcmb_kaynak and tcmb_kaynak not in out["kaynaklar"]:
        out["kaynaklar"].append(tcmb_kaynak)

    # 6b) Fed sonraki toplanti OLASILIKLARI (Polymarket): indirim/artis/sabit (%)
    fed_ol = _polymarket_fed_olasiliklar()
    if fed_ol:
        out["fed_beklenti_indirme"] = fed_ol["indirme"]
        out["fed_beklenti_artis"] = fed_ol["artis"]
        out["fed_beklenti_sabit"] = fed_ol["sabit"]
        if "Polymarket" not in out["kaynaklar"]:
            out["kaynaklar"].append("Polymarket")
    else:
        # Polymarket (alt endpoint dahil) veri vermedi -> her zaman logla; Telegram
        # uyarisi gunde EN FAZLA 1 kez (spam onleme, health_state.json "polymarket_hata").
        out["fed_beklenti_indirme"] = None
        out["fed_beklenti_artis"] = None
        out["fed_beklenti_sabit"] = None
        _log_macro_hata("polymarket", "FED_BEKLENTI_ALINAMADI (olasiliklar bos, alt endpoint dahil)")
        _uyar_admin_gunluk("polymarket_hata",
                           "Polymarket Fed beklenti olasılıkları alınamadı (macro.py).")

    # 6c) TCMB beklenti ham veri yoksa: alan None KALIR (uydurma yok) -> her zaman
    # logla; Telegram uyarisi gunde EN FAZLA 1 kez (health_state "tcmb_beklenti_hata").
    if tcmb_ham is None:
        _log_macro_hata("tcmb_beklenti",
                        "TCMB_BEKLENTI_ALINAMADI (EVDS/borsagundem/GNews bos) "
                        "-> beklenti BILINMIYOR (None; uydurma yok)")
        _uyar_admin_gunluk("tcmb_beklenti_hata",
                           "TCMB faiz beklenti verisi alınamadı (macro.py) "
                           "-> beklenti BİLİNMİYOR olarak işaretlendi (0 uydurulmadı).")

    # 7) DUNYA PIYASALARI (sabah): S&P futures / VIX / DXY / Nikkei / Shanghai.
    # Tek yfinance batch; cekilemeyen alan son_bilinen'e duser (bos kalmasin).
    dunya = _dunya_gostergeleri()
    for ad in ("sp_futures_degisim", "vix", "dxy",
               "nikkei_degisim", "shanghai_degisim"):
        v = dunya.get(ad)
        if v is not None:
            out[ad] = v
            taze[ad] = v
            if "yahoo(dunya)" not in out["kaynaklar"]:
                out["kaynaklar"].append("yahoo(dunya)")
        else:
            out[ad] = son_bilinen.get(ad)      # yedek: son bilinen (yoksa None)

    # 8) Turkiye CDS (bp) - haftalik cron gunceller; burada son bilineni oku.
    out["turkey_cds"] = guncel_cds()

    # taze cekilen degerleri kalici sakla (sonraki yedek icin)
    _kaydet_son_bilinen(taze)

    out["available"] = bool(out["kaynaklar"])
    if not out["available"]:
        out["neden"] = "makro veri alinamadi (investing.com + EVDS3 bos)"

    _CACHE["macro"] = (now, out)
    return out


# ---------------------------------------------------------------------------
# EVDS3 (TCMB) - yeni endpoint: POST https://evds3.tcmb.gov.tr/igmevdsms-dis/fe
# (SPA: getSeriVerileri => Le.post("/fe", body)). EVDS_PROXY_URL (TR cikisli)
# + EVDS_API_KEY ile cekilir. Bright Data sertifika MITM yaptigindan verify=False.
# ---------------------------------------------------------------------------
_EVDS3_FE = "https://evds3.tcmb.gov.tr/igmevdsms-dis/fe"
_EVDS_SERIES = {
    "usdtry": ("TP.DK.USD.A.YTL", "avg"),
    "politika_faizi": ("TP.TF.TG.A1", "avg"),
    "tufe_yillik": ("TP.FE.OKTG01", "avg"),
}


def _evds_proxies():
    """EVDS_PROXY_URL'i TR cikisli olacak sekilde dondurur (Bright Data -country-tr)."""
    raw = os.environ.get("EVDS_PROXY_URL")
    if not raw:
        return None
    try:
        pre, rest = raw.split("://", 1)
        cred, host = rest.split("@", 1)
        usr, pw = cred.split(":", 1)
        if ("superproxy" in host or usr.startswith("brd-")) and "-country-" not in usr:
            usr = usr + "-country-tr"
        raw = f"{pre}://{usr}:{pw}@{host}"
    except Exception:
        pass
    return {"http": raw, "https": raw}


def _evds_series(code: str, agg: str, key: str):
    """EVDS3 /fe POST ile bir serinin son degerini dondurur (yoksa None).

    Govde SPA'daki getSeriVerileri ile ayni alanlari tasir. Bright Data residential
    (no-KYC) hesabi TCMB'ye POST'u engelleyebilir (HTTP 402); o durumda None doner.
    """
    import requests as rq
    import urllib3
    urllib3.disable_warnings()
    today = datetime.now(_TZ).date()
    body = {
        "series": code,
        "aggregationTypes": agg or "avg",
        "formulas": "0",
        "startDate": (today - timedelta(days=60)).strftime("%d-%m-%Y"),
        "endDate": today.strftime("%d-%m-%Y"),
        "frequency": "1",
        "decimalSeperator": ".",
        "decimal": False,
    }
    headers = {"key": key, "Accept": "application/json",
               "Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    try:
        r = rq.post(_EVDS3_FE, json=body, headers=headers,
                    proxies=_evds_proxies(), timeout=30, verify=False)
        if r.status_code != 200:
            return None
        if "json" not in r.headers.get("content-type", "").lower():
            return None
        data = r.json()
    except Exception:
        return None
    items = data.get("items") or data.get("data") or (data if isinstance(data, list) else [])
    for row in reversed(items):
        if not isinstance(row, dict):
            continue
        for k, v in row.items():
            if any(t in k.upper() for t in ("TARIH", "DATE", "UNIXTIME")):
                continue
            if v in (None, "", "null", "ND"):
                continue
            try:
                return round(float(str(v).replace(",", ".")), 4)
            except (TypeError, ValueError):
                continue
    return None


def evds_macro() -> dict:
    """EVDS3'ten USD/TRY, politika faizi, TUFE ceker (EVDS_API_KEY gerekli)."""
    key = os.environ.get("EVDS_API_KEY")
    if not key:
        return {"available": False, "neden": "EVDS_API_KEY yok"}
    out = {"available": False, "kaynak": "EVDS3"}
    for ad, (code, agg) in _EVDS_SERIES.items():
        out[ad] = _evds_series(code, agg, key)
    if any(out.get(a) is not None for a in _EVDS_SERIES):
        out["available"] = True
    else:
        out["neden"] = "EVDS3 yanit vermedi (muhtemelen proxy POST kisiti / no-KYC)"
    return out


if __name__ == "__main__":
    import sys as _sys
    # .env yukle: modul olarak import edildiginde cagiran (briefing/alerts/app/
    # health_monitor) zaten yukluyor, ama CLI/cron ("python -m src.news.macro")
    # yuklemiyordu -> EVDS_API_KEY gorunmuyor ve EVDS anket kaynagi sessizce
    # devre disi kaliyordu. Ayni davranisi CLI'da da saglar.
    try:
        from dotenv import load_dotenv as _ld
        _ld(Path(__file__).resolve().parents[2] / ".env")
    except Exception:
        pass
    # Haftalik CDS guncelleme: `python -m src.news.macro cds` (Pazartesi 09:00 cron).
    if len(_sys.argv) > 1 and _sys.argv[1] == "cds":
        guncelle_cds()
    else:
        import json as _json
        print(_json.dumps(get_macro(), ensure_ascii=False, indent=2))
