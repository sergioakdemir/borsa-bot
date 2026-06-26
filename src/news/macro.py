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
    2026: ((1, 23), (3, 6), (4, 17), (6, 11), (7, 24), (9, 18), (10, 23), (12, 19)),
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
    """TCMB resmi sayfasi -> EVDS2; bulunamazsa (None, None). Deger + kaynak doner."""
    for url, ad in ((_TCMB_FAIZ_URL, "tcmb"), (_EVDS2_URL, "evds2")):
        v = _parse_politika_faizi(_fetch(url))
        if v is not None:
            return v, ad
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


def _evds_borsapy_policy_rate():
    """borsapy EVDS ile GUNCEL politika faizi (haftalik repo). Serileri sirayla dener;
    ilk MAKUL (30-45 araliginda, ~%37) degeri dondurur. Hicbiri calismazsa veya
    EVDS_API_KEY yoksa sabit _POLITIKA_FAIZI_FALLBACK (%37) dondurur."""
    key = os.environ.get("EVDS_API_KEY")
    if key:
        try:
            import borsapy as bp
            bp.set_evds_key(key)
            for kod in _EVDS_POLITIKA_SERILERI:
                try:
                    seri = bp.evds_series(kod)["Value"].dropna()
                except Exception:
                    continue                 # seri yok/erisilemez -> sonrakini dene
                if seri.empty:
                    continue
                v = round(float(seri.iloc[-1]), 2)
                if 30 <= v <= 45:            # guncel politika faizi makul bandi (7.0 bug'ini eler)
                    return v
        except Exception:
            pass
    return _POLITIKA_FAIZI_FALLBACK          # hicbiri calismadi -> sabit %37


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
# FRED_API_KEY yoksa veya cekilemezse sabit _FED_FAIZ_FALLBACK kullanilir.
# FEDFUNDS = efektif federal fon faizi (aylik); son 2 gozlemden degisim (bp) cikar.
# ---------------------------------------------------------------------------
_FRED_FEDFUNDS_URL = ("https://api.stlouisfed.org/fred/series/observations"
                      "?series_id=FEDFUNDS&file_type=json&sort_order=desc&limit=2")
_FED_FAIZ_FALLBACK = 5.25     # FRED erisilemezse son bilinen Fed faizi (%)


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
# Fed:  Polymarket Gamma API (ucretsiz, anahtarsiz) - olasilik-agirlikli bp.
#       CME FedWatch resmi API ucretli, web endpoint'i login-gate; kullanilmadi.
# TCMB: EVDS "Piyasa Katilimcilari Anketi" (TP.BEK.S* serisi) ucretsiz; ancak tam
#       seri kodu katalogdan teyit edilmeli -> TCMB_BEKLENTI_EVDS_KOD ile verilir.
# Her ikisi de env override (FED_BEKLENTI_BP / TCMB_BEKLENTI_BP) ile elle girilebilir;
# canli kaynak erisilemezse alan None kalir (analiz beklentisiz devam eder).
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


def _polymarket_fed_beklenti_bp():
    """Polymarket Gamma API ile Fed'in sonraki kararina dair olasilik-agirlikli
    beklenti (bp). Aktif 'Fed/FOMC rate' etkinligini bulur, her binary market'in
    YES olasiligini bp ile carpip toplar. Erisilemez/parse edilemezse None."""
    try:
        import requests as rq
        r = rq.get("https://gamma-api.polymarket.com/events",
                   params={"closed": "false", "limit": 200,
                           "order": "volume", "ascending": "false"},
                   proxies=_proxies(), timeout=20)
        if r.status_code != 200:
            return None
        events = r.json()
    except Exception:
        return None
    if not isinstance(events, list):
        return None
    ev = None
    for e in events:
        title = (e.get("title") or "").lower()
        if "fed" in title and any(t in title for t in ("rate", "fomc", "interest", "bps")):
            ev = e
            break
    if not ev:
        return None
    import json
    toplam, agirlik = 0.0, 0.0
    for mkt in ev.get("markets") or []:
        bp = _bp_from_question(mkt.get("question") or mkt.get("groupItemTitle") or "")
        if bp is None:
            continue
        fiyatlar = mkt.get("outcomePrices")
        if isinstance(fiyatlar, str):
            try:
                fiyatlar = json.loads(fiyatlar)
            except Exception:
                continue
        try:
            yes = float(fiyatlar[0])            # binary market: ilk outcome = "Yes"
        except (TypeError, ValueError, IndexError):
            continue
        toplam += yes * bp
        agirlik += yes
    if agirlik <= 0:
        return None
    return round(toplam / agirlik)               # olasilik-normalize beklenen bp


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


def fed_beklenti_bp():
    """Fed sonraki karar beklentisi (bp): env override -> Polymarket -> None."""
    ov = _env_bp("FED_BEKLENTI_BP")
    if ov is not None:
        return ov
    return _polymarket_fed_beklenti_bp()


def tcmb_beklenti_bp(mevcut_faiz=None):
    """TCMB sonraki karar beklentisi (bp): env override -> EVDS anketi -> None."""
    ov = _env_bp("TCMB_BEKLENTI_BP")
    if ov is not None:
        return ov
    return _evds_tcmb_beklenti_bp(mevcut_faiz)


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

    # 4) Fed (ABD) politika faizi - FRED (ucretsiz). Anahtar yoksa sabit fallback.
    fed_faiz, fed_degisim = _fred_fed_funds()
    if fed_faiz is not None:
        out["fed_faiz"] = fed_faiz
        out["fed_degisim_bp"] = fed_degisim
        taze["fed_faiz"] = fed_faiz
        if "FRED" not in out["kaynaklar"]:
            out["kaynaklar"].append("FRED")
    else:
        out["fed_faiz"] = son_bilinen.get("fed_faiz", _FED_FAIZ_FALLBACK)
        out["fed_degisim_bp"] = 0            # FRED yok -> degisim bilinmiyor, sabit varsay

    # 5) TCMB son PPK karar degisimi (data/ppk_kararlari.json)
    spk = son_ppk_karari()
    out["tcmb_degisim_bp"] = spk.get("karar_bp")
    if spk:
        out["son_ppk_tarihi"] = spk.get("tarih")
        if "ppk_kararlari" not in out["kaynaklar"]:
            out["kaynaklar"].append("ppk_kararlari")

    # 6) Beklenti (sonraki karara dair piyasa beklentisi, bp) - best-effort, ucretsiz
    out["fed_beklenti_bp"] = fed_beklenti_bp()
    out["tcmb_beklenti_bp"] = tcmb_beklenti_bp(out.get("politika_faizi"))

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
