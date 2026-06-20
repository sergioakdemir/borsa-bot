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
from datetime import datetime, timedelta
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
    "ters havayolu icin petrol artisi olumsuzdur. Iliskiyi sade dille gerekceye yansit."
)


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


def market_data(ticker: str) -> dict | None:
    """yfinance'den ~1 yillik veriyle kompakt teknik ozet uretir. Veri yoksa None."""
    from src.data.factory import get_data_source
    from src.markets.bist import BIST

    market = BIST()
    symbol = market.to_symbol(ticker)
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
    }


# ---------------------------------------------------------------------------
# 2+3) KAP bildirimleri (30 gun) + filtreden gecmis haberler (7 gun)
# ---------------------------------------------------------------------------
def gather_news(ticker: str, news_src=None, rss_src=None) -> dict:
    """KAP 30g bildirimler + RSS (24s) + son 7 gun haberleri tek listede birlestirir.

    Tum kaynaklar mevcut filtreden gecer: tazelik (YENI/GUNCEL/ESKI = kademe 0-1-2)
    ve fiyatlanma (FIYATLANDI/FIYATLANMADI/VERI_YOK).
    """
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


def market_context(rss_src=None) -> dict:
    """Hisseden bagimsiz genel piyasa baglami: son ekonomi basliklari + EVDS makro."""
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
    return {"piyasa_gundemi": gundem, "makro": makro}


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


def analyze_stock(ticker: str, news_src=None, rss_src=None, client=None,
                  context=None) -> dict:
    """Tek hisse icin tam zincir. Web uyumlu kayit dondurur (veri yoksa skipped)."""
    ticker = ticker.upper().replace(".IS", "")
    sig = market_data(ticker)
    if sig is None:
        return {"ticker": ticker, "skipped": True,
                "reason": "Piyasa verisi yok - yorum yapilmadi."}

    news = gather_news(ticker, news_src=news_src, rss_src=rss_src)
    # Analist konsensusu (hedeffiyat + borsaveyatirim)
    try:
        from src.news.analyst_source import get_analyst_consensus
        analist = get_analyst_consensus(ticker)
    except Exception:
        analist = {"available": False}
    # Temel (bilanco) veriler (yfinance .info)
    try:
        from src.news.fundamental_source import get_fundamentals
        temel = get_fundamentals(ticker)
    except Exception:
        temel = {"available": False}
    # Hacim anomalisi (bugun vs son 5 gun ortalamasi)
    try:
        from src.news.fundamental_source import get_volume_anomaly
        hacim_anom = get_volume_anomaly(ticker)
    except Exception:
        hacim_anom = {"available": False}
    # Sektor korelasyonu (statik makro iliski tablosu)
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
    v = _ai_verdict(ticker, payload, client=client)

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


# ---------------------------------------------------------------------------
# Zinciri calistir + kaydet + decisions tablosu
# ---------------------------------------------------------------------------
def run(tickers: list[str], save: bool = True, verbose: bool = True) -> list[dict]:
    from src.news.service import get_news_source
    from src.db import database as db
    import anthropic

    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY yok - AI yorumu uretilemez.")

    from src.news.rss_source import RSSNewsSource

    news_src, is_sample = get_news_source(verbose=verbose)
    rss_src = RSSNewsSource()                       # Bloomberg HT + Investing + Mynet
    context = market_context(rss_src=rss_src)        # genel piyasa baglami (1 kez)
    if verbose:
        print(f"  [rss] 24s haber: {rss_src.recent_count()} | "
              f"makro: {context['makro'].get('available')}")
    client = anthropic.Anthropic()
    today = datetime.now(_TZ).date().isoformat()

    results = []
    for t in tickers:
        try:
            r = analyze_stock(t, news_src=news_src, rss_src=rss_src,
                              client=client, context=context)
        except Exception as e:
            if verbose:
                print(f"  [{t}] HATA: {type(e).__name__}: {str(e)[:100]}")
            r = {"ticker": t.upper(), "skipped": True,
                 "reason": f"Hata: {type(e).__name__}"}
        results.append(r)
        if verbose:
            if r.get("skipped"):
                print(f"  {t:7} ATLANDI ({r.get('reason')})")
            else:
                print(f"  {t:7} {r['final_decision']:5} puan {r['score']}/10 "
                      f"risk {r['risk']['score']}/10 {r['eminlik']} "
                      f"haber={r['haber_sayisi']}")
        # Karari decisions tablosuna yaz (sonuc=None)
        if not r.get("skipped"):
            try:
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
