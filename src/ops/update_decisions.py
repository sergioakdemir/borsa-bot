"""Karar sonuclarini otomatik doldurur (hafiza/ogrenme).

Her gece calisir (cron 23:30). Sonucu HENUZ BOS olan kararlar icin, karar gunundeki
kapanis ile KARAR TIPINE GORE N ISLEM GUNU sonraki kapanis arasindaki yuzde degisimi
hesaplar ve karara gore 'DOGRU/YANLIS' verir; decisions.sonuc kolonunu gunceller.
Yanlis cikan kararlar icin ucuz Haiku ile kisa 'neden yanlis' analizi yapilir
(decisions.yanlis_sebep). Degerlendirme penceresi: AL=5, SAT=3, TUT=10, BEKLE=5 islem gunu.

Kazanma kurali (karar yonune gore; tek kaynak: _verdict):
  AL / AL_TEMKINLI : hem KAR (degisim > 0) HEM ALPHA (piyasa_farki >= 0) -> DOGRU.
                     Benchmark: BIST -> XU100.IS, ABD -> SPY. Benchmark yoksa
                     yalniz mutlak yon (degisim > 0).
  SAT / GUCLU_SAT / AZALT : fiyat dustuyse DOGRU
  TUT              : piyasa_farki varsa endeksten 3 puandan fazla geri kalmadiysa
                     (>= -3); yoksa yatay bant (|degisim| <= %5)
  VETO / UZAK_DUR  : islemden kacinildi; endeksi gecmediyse (piyasa_farki <= 0) DOGRU
NOT: 'degisim' = DEGERLENDIRME PENCERESI degisimi (AL=5 islem gunu). Bu, mini_update'in
doldurdugu ilk_gun_degisim (1 GUNLUK hizli geri bildirim) ile AYNI SEY DEGILDIR;
alpha basari orani pencere degisimiyle olculur.

piyasa_farki (her yonlu karar icin) = hisse_getiri - benchmark_getiri (ayni pencere).
decisions.piyasa_farki kolonuna yazilir.
  * run()  : her gece; sonucu bos kararlari degerlendirir + piyasa_farki NULL
             kalanlari TEKRAR dener (alpha_backfill) -> kalici NULL olusmaz.
  * backfill_piyasa_farki() : elle; TUM degerlendirilmis kararlari GUNCEL kriterle
             yeniden hukumlendirir (esik degisikliginden sonra tutarlilik icin).
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")
TUT_BANT = 5.0           # TUT icin yatay sayilan +/- yuzde bandi
# Alpha olcum sagligi: degerlendirilmis kararlarin en fazla bu kadari piyasa_farki'siz
# olabilir; ustunde benchmark cekimi bozuk demektir (saglik karnesi alarm verir).
ALPHA_BOS_ESIK = 0.20
HABER_MODEL = "claude-haiku-4-5"   # 'neden yanlis' analizi: ucuz + hizli

# Karar tipine gore ISLEM GUNU bazli degerlendirme penceresi (sabit KAPANIS_GUN kaldirildi)
_KAPANIS_GUN = {"SAT": 3, "AL": 5, "BEKLE": 5, "TUT": 10}
KAPANIS_GUN_VARSAYILAN = 5


def _kapanis_gun(karar: str, tahmini_sure=None) -> int:
    """Karar tipine gore kac ISLEM gunu sonra degerlendirilecegini doner.
    AL=5, SAT=3, BEKLE=5 (AZALT -> SAT penceresi). TUT'ta AI'nin tahmini_sure'si
    varsa onu kullan (5-30 ile sinirli), yoksa sabit 10."""
    k = (karar or "").upper()
    if "SAT" in k or "AZALT" in k or "UZAK" in k:   # UZAK_DUR de SAT penceresi (3 ig)
        return _KAPANIS_GUN["SAT"]
    if "BEKLE" in k:
        return _KAPANIS_GUN["BEKLE"]
    if "AL" in k:                       # AL, AL_TEMKINLI
        return _KAPANIS_GUN["AL"]
    if "TUT" in k:
        if isinstance(tahmini_sure, (int, float)) and tahmini_sure:
            return max(5, min(30, int(tahmini_sure)))   # AI tahmini (5-30 islem gunu)
        return _KAPANIS_GUN["TUT"]
    return KAPANIS_GUN_VARSAYILAN


# ALPHA olcumu: AL DOGRU icin hem para kazandirmis (degisim > 0) HEM endeksi gecmis
# (piyasa_farki >= 0) olmali. 8 Temmuz 2026: esik -1.5'ten 0'a cekildi + mutlak kar
# sarti eklendi -> endeksi yenip yine de zarar ettiren AL artik YANLIS sayilir.
AL_PIYASA_ESIGI = 0
TUT_ALPHA_ESIGI = -3.0   # TUT DOGRU: piyasa_farki varsa >= -3 (endekse yakin yatay)


def _verdict(karar: str, degisim: float, piyasa_farki: float = None,
             bist_degisim: float = None) -> bool:
    """Karar dogru mu? ALPHA olcumu:
      AL   : hem kar (degisim>0) HEM alpha (piyasa_farki>=0). piyasa_farki yoksa
             yalniz mutlak yon (degisim>0).
      TUT  : piyasa_farki varsa endeksten -3 puandan cok geri kalmadiysa; yoksa
             yatay bant (|degisim| <= 5).
      UZAK_DUR/VETO: piyasa_farki varsa endeksi gecmediyse (<=0) dogru; yoksa
             mutlak yon (degisim<=0).
      SAT/AZALT: fiyat dustuyse dogru.
    bist_degisim geriye donuk uyum icin korunur (kullanilmaz)."""
    k = (karar or "").upper()
    if "VETO" in k or "UZAK" in k:
        if piyasa_farki is not None:
            return piyasa_farki <= 0
        return degisim <= 0
    if "SAT" in k or "AZALT" in k:   # SAT, GUCLU_SAT, AZALT
        return degisim < 0
    if "AL" in k:                    # AL: hem para kazandiracak hem endeksi gececek
        if piyasa_farki is not None:
            return degisim > 0 and piyasa_farki >= AL_PIYASA_ESIGI
        return degisim > 0             # benchmark verisi yok -> mutlak yon
    # TUT
    if piyasa_farki is not None:
        return piyasa_farki >= TUT_ALPHA_ESIGI
    return abs(degisim) <= TUT_BANT


def _market_for(ticker: str):
    """Ticker'in market nesnesini doner. decisions tablosunda para_birimi yok; sirayla:
    1) enstruman ana tablosu (instruments) ABD diyorsa US() (orn. NVDA/QQQ/VOO),
    2) portfoy tablosu USD ise US(),
    3) izleme listesi ABD isaretliyse US(),
    aksi halde BIST() (.IS ekler). Boylece ABD hisseleri icin de veri gelir."""
    from src.markets.bist import BIST
    from src.markets.us import US
    norm = (ticker or "").upper().replace(".IS", "").strip()
    # 1) Enstruman ana tablosu (kaynak-i hakikat)
    try:
        from src.db import database as db
        if db.get_instrument(norm) is not None:
            return US() if db.is_us_instrument(norm) else BIST()
    except Exception:
        pass
    # 2) Portfoyde USD pozisyon olarak tutuluyorsa ABD
    try:
        from src.db import database as db
        with db.get_conn() as c:
            row = c.execute(
                "SELECT para_birimi FROM portfoy "
                "WHERE UPPER(REPLACE(ticker, '.IS', '')) = ? "
                "ORDER BY (UPPER(para_birimi) = 'USD') DESC LIMIT 1",
                (norm,)).fetchone()
        if row and (row[0] or "").upper() == "USD":
            return US()
    except Exception:
        pass
    # 3) Izleme listesinde ABD piyasasi olarak tanimliysa (portfoyde olmasa da)
    try:
        from src.watchlist import is_us_ticker
        if is_us_ticker(norm):
            return US()
    except Exception:
        pass
    return BIST()


def _price_change(ticker: str, karar_tarihi: str, kapanis_gun: int):
    """Karar gunundeki kapanis -> kapanis_gun ISLEM GUNU sonraki kapanis yuzde degisimi.

    TAKVIM gunu degil ISLEM gunu bazlidir: yfinance yalniz islem gunlerini dondurdugu
    icin hafta sonu/tatil otomatik atlanir.
      - Baz bar  = karar tarihinde VEYA oncesindeki SON islem gunu kapanisi
                   (botun karar aninda gordugu fiyat; Cumartesi karari icin Cuma kapanisi).
      - Hedef bar = baz + kapanis_gun islem gunu (karar tipine gore: AL=5, SAT=3, TUT=10...).
      - Tam pencere (kapanis_gun islem gunu) HENUZ dolmadiysa None doner (bekle) -> boylece
        karar bari ile hedef bar AYNI olup %0 cikmaz ve karar tipinin penceresine uyulur.
    Veri yoksa None.
    """
    symbol = _market_for(ticker).to_symbol(ticker)
    return _symbol_change(symbol, karar_tarihi, kapanis_gun)


# --- Gecmis verisi: kosu-ici onbellek + retry (15 Tem 2026) -----------------
# SORUN (26 Haz - 15 Tem 2026): _symbol_change her KARAR icin get_history'yi
# yeniden cagiriyordu. Bir gece kosusu 111-385 karar degerlendiriyor -> ayni
# XU100.IS yuzlerce kez cekiliyordu. yf.download rate-limit'te EXCEPTION ATMAZ,
# BOS DataFrame doner -> benchmark None -> piyasa_farki NULL. Hisse cekimi
# basarisiz olsa karar 'bekliyor'da kalip ertesi gece tekrar deneniyordu, ama
# benchmark basarisizligi kalici NULL birakiyordu (asimetri) -> alpha olcumu
# 39 AL kararinin 23'unde kalici olarak kayboldu.
# COZUM: (1) sembol bazli onbellek -> kosu basina 385 cagri yerine 1;
#        (2) bos/hatali donuste kisa beklemeli retry;
#        (3) run() artik piyasa_farki NULL kalan kararlari TEKRAR dener.
_HIST_CACHE: dict = {}          # symbol -> (start_iso, DataFrame)
_HIST_RETRY = 3
_HIST_BEKLE_SN = 2.0


def _history(symbol: str, start: str):
    """Sembolun gunluk barlari; kosu boyunca onbellege alinir + retry'li ceker.

    Onbellekteki seri istenenden ERKEN basliyorsa yeniden cekmez (daha genis
    pencere istenen her pencereyi kapsar). Bos DataFrame de basarisizlik sayilir
    (yfinance throttle'da bos doner, hata atmaz)."""
    import time
    onbellek = _HIST_CACHE.get(symbol)
    if onbellek and onbellek[0] <= start:
        return onbellek[1]
    from src.data.factory import get_data_source
    df = None
    for deneme in range(_HIST_RETRY):
        try:
            df = get_data_source().get_history(symbol, start=start)
        except Exception:
            df = None
        if df is not None and not df.empty:
            break
        if deneme < _HIST_RETRY - 1:
            time.sleep(_HIST_BEKLE_SN * (deneme + 1))     # kademeli bekleme
    if df is None or df.empty:
        return None
    _HIST_CACHE[symbol] = (start, df)
    return df


def _hist_cache_temizle():
    """Kosu basinda cagrilir: onbellek gunler arasi bayatlamasin."""
    _HIST_CACHE.clear()


def _symbol_change(symbol: str, karar_tarihi: str, kapanis_gun: int):
    """Verilen yfinance sembolu icin karar gunu -> kapanis_gun islem gunu sonraki
    yuzde degisim (bkz. _price_change). Pencere dolmadiysa/veri yoksa None."""
    import pandas as pd

    # Karar tarihinden ONCEKI islem gununu de yakalamak icin genis pencere (uzun tatiller)
    start = (datetime.fromisoformat(karar_tarihi).date()
             - timedelta(days=12)).isoformat()
    df = _history(symbol, start)
    if df is None or df.empty:
        return None
    # Hacimsiz VE kapanisi NaN olan barlari ele (yfinance bazen guncel/yarim bari
    # Volume>0 ama Close=NaN dondurur -> aksi halde sonuc 'nan%' cikar)
    df = df[(df["Volume"] > 0) & df["Close"].notna()]
    if df.empty:
        return None

    kdate = datetime.fromisoformat(karar_tarihi).date()
    dates = [pd.Timestamp(ix).date() for ix in df.index]
    # Baz bar: kdate'te VEYA oncesindeki SON islem gunu (yoksa eldeki ilk bar)
    i0 = next((i for i in range(len(dates) - 1, -1, -1) if dates[i] <= kdate), None)
    if i0 is None:
        i0 = 0
    son = len(dates) - 1
    if son - i0 < kapanis_gun:                   # tam pencere dolmadi -> bekle
        return None
    i_eval = i0 + kapanis_gun                     # kapanis_gun islem gunu sonraki kapanis
    baz = float(df["Close"].iloc[i0])
    hedef = float(df["Close"].iloc[i_eval])
    if not baz:
        return None
    return round((hedef - baz) / baz * 100, 2)


def _is_bist(ticker: str) -> bool:
    """Karar BIST hissesi mi? (US ise BIST-100 kiyasi anlamsiz)."""
    from src.markets.bist import BIST
    return isinstance(_market_for(ticker), BIST)


# Benchmark sembol zinciri: ilki calismazsa sirayla denenir.
# 15 Tem 2026'da olculdu: XU100.IS calisiyor; ^XU100 / XU100 / BIST100.IS yfinance'te
# BOS donuyor -> BIST icin gercek bir alternatif sembol YOK, zincir tek elemanli.
# ABD tarafinda ^GSPC ve ^SPX calisiyor -> SPY dusesrse ^GSPC'ye dus.
# NOT: KAP icin kurdugumuz TR proxy buraya EKLENMEDI. Oradaki sorun cografi engel;
# buradaki sorun Yahoo rate-limit'iydi ve onbellek cagriyi kosu basina 385'ten 1'e
# indirdigi icin proxy gereksiz ek kirilganlik olurdu.
_BENCHMARK_ZINCIR = {
    "bist": ["XU100.IS"],
    "us": ["SPY", "^GSPC", "^SPX"],
}


def _benchmark_symbol(ticker: str) -> str:
    """Hissenin piyasa kiyas endeksi (zincirin ilki): BIST -> XU100.IS, ABD -> SPY."""
    return _BENCHMARK_ZINCIR["bist" if _is_bist(ticker) else "us"][0]


def _benchmark_change(ticker: str, karar_tarihi: str, kapanis_gun: int):
    """Hissenin benchmark'inin ayni pencere icindeki yuzde degisimi; sembol
    zincirini sirayla dener (ilki bos donerse digerine duser). Hicbiri veri
    vermezse None -> cagiran karari 'olculemedi' birakir ve SONRAKI kosuda
    tekrar dener (kalici NULL yok)."""
    for sembol in _BENCHMARK_ZINCIR["bist" if _is_bist(ticker) else "us"]:
        deg = _symbol_change(sembol, karar_tarihi, kapanis_gun)
        if deg is not None:
            return deg
    return None




# --- L2: 'Neden yanlis cikti?' kisa Haiku analizi ---
_YANLIS_SCHEMA = {
    "type": "object",
    "properties": {
        "kategori": {"type": "string",
                     "enum": ["haber", "teknik", "makro", "belirsiz"]},
        "aciklama": {"type": "string",
                     "description": "Tek cumle, kisa (en fazla ~12 kelime) Turkce sebep"},
    },
    "required": ["kategori", "aciklama"],
    "additionalProperties": False,
}


def _yanlis_analiz(ticker, karar, gerekce, degisim, client=None, acc=None):
    """Yanlis cikan kararin sebebini ucuz Haiku ile kategorize eder.
    Doner: 'kategori: aciklama' veya None (anahtar yok/hata)."""
    import os
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        client = client or anthropic.Anthropic()
        sistem = (
            "Sen bir borsa kararlarini denetleyen analistsin. Verilen karar GERCEKTE "
            "YANLIS cikti (fiyat beklenenin tersine gitti). Kararin gerekcesine ve "
            "gerceklesen fiyat degisimine bakarak hatanin ASIL kaynagini sec: 'haber' "
            "(beklenmedik/yanlis okunan haber), 'teknik' (teknik sinyal yaniltti), "
            "'makro' (genel piyasa/makro ortam) veya 'belirsiz'. aciklama tek kisa cumle.")
        icerik = (f"Hisse: {ticker}\nKarar: {karar}\nGerceklesen degisim: "
                  f"%{degisim:+g}\nKararin gerekcesi: {gerekce or '(yok)'}")
        resp = client.messages.create(
            model=HABER_MODEL, max_tokens=200, system=sistem,
            messages=[{"role": "user", "content": icerik}],
            output_config={"format": {"type": "json_schema", "schema": _YANLIS_SCHEMA}})
        if acc is not None:            # maliyet: token'lari toplayiciya ekle
            try:
                from src.ai import maliyet
                maliyet.ekle(acc, resp.usage)
            except Exception:
                pass
        import json
        text = next((b.text for b in resp.content if b.type == "text"), "")
        d = json.loads(text)
        kat, ac = d.get("kategori", "belirsiz"), (d.get("aciklama") or "").strip()
        return f"{kat}: {ac}" if ac else kat
    except Exception:
        return None


def alpha_backfill(verbose: bool = True, limit: int = None) -> dict:
    """Sonucu YAZILMIS ama piyasa_farki NULL kalan kararlari geriye donuk doldurur.

    Neden gerekli: 26 Haz - 15 Tem 2026 arasi benchmark cekimi rate-limit yuzunden
    sik sik bos dondu; run() sonucu yine de yaziyordu -> piyasa_farki KALICI NULL
    kaldi ve alpha olcumu 39 AL kararinin 23'unde kayboldu. Benchmark artik
    cekilebiliyor (onbellek + retry ile) -> o gunlerin verisiyle hesap yapilabilir.

    Doldururken sonuc metni ve DOGRU/YANLIS hukmu de YENIDEN hesaplanir: alpha
    bilgisi geldiginde hukum degisebilir (orn. 'endeksi yendi ama zarar' -> AL YANLIS).
    yanlis_sebep DOKUNULMAZ (Haiku cagrisi tekrar edilmez -> token harcanmaz).
    KILL_SWITCH / DEGERLENDIRME DISI kayitlar atlanir.
    """
    from src.db import database as db
    db.init_db()
    _hist_cache_temizle()
    with db.get_conn() as c:
        sql = ("SELECT * FROM decisions WHERE piyasa_farki IS NULL "
               "AND sonuc IS NOT NULL AND sonuc <> '' "
               "AND sonuc NOT LIKE '%DEĞERLENDİRME DIŞI%' "
               "AND UPPER(COALESCE(karar,'')) NOT LIKE '%KILL%' ORDER BY id")
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = [dict(r) for r in c.execute(sql)]

    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] alpha backfill: {len(rows)} aday karar")
    dolduruldu = hala_yok = hukum_degisti = 0
    for r in rows:
        kg = _kapanis_gun(r["karar"], r.get("tahmini_sure"))
        deg = _price_change(r["ticker"], r["tarih"], kg)
        bench = _benchmark_change(r["ticker"], r["tarih"], kg)
        if deg is None or bench is None:
            hala_yok += 1
            continue
        piyasa_farki = round(deg - bench, 2)
        dogru = _verdict(r["karar"], deg, piyasa_farki=piyasa_farki)
        sonuc = f"{deg:+.1f}% · {'DOGRU' if dogru else 'YANLIS'} · piyasa {piyasa_farki:+.1f}p"
        if ("AL" in (r["karar"] or "").upper() and piyasa_farki >= 0 and deg <= 0):
            sonuc += " · endeksi yendi ama zarar"
        eski_dogru = "DOGRU" in (r.get("sonuc") or "")
        if eski_dogru != dogru:
            hukum_degisti += 1
        # yanlis_sebep gecilmiyor -> mevcut deger korunur (Haiku cagrisi yok)
        db.set_decision_outcome(r["id"], sonuc, piyasa_farki=piyasa_farki)
        dolduruldu += 1
        if verbose and dolduruldu <= 5:
            print(f"  {r['ticker']:7} {r['karar']:9} {r['tarih'][:10]} -> {sonuc}")
    if verbose:
        print(f"  dolduruldu={dolduruldu} | hala olculemiyor={hala_yok} "
              f"| hukum degisti={hukum_degisti}")
    return {"aday": len(rows), "dolduruldu": dolduruldu, "hala_yok": hala_yok,
            "hukum_degisti": hukum_degisti}


ALPHA_SAGLIK_GUN = 14      # bkz. asagidaki gecikme notu


def alpha_olcum_sagligi(gun: int = ALPHA_SAGLIK_GUN) -> dict:
    """Son N gunde alpha olcumu ne kadar saglikli? (saglik karnesi + panel okur)

    Payda: degerlendirilmis (sonucu yazilmis, DEGERLENDIRME DISI olmayan) kararlar.
    NULL orani ALPHA_BOS_ESIK'i gecerse benchmark cekimi yine bozulmus demektir.

    NEDEN 14 GUN (7 degil): karar ancak penceresi dolunca degerlendirilir (AL=5,
    TUT=10 ISLEM gunu). Son 7 gunun kararlarinin cogunun sonucu HENUZ yazilmamis
    olur -> 7 gunluk pencere yapisal olarak bos cikar ve metrik hicbir bozulmayi
    yakalayamaz. 14 gun, degerlendirme gecikmesini kapsayan en kisa penceredir.
    """
    from src.db import database as db
    esik = (datetime.now(_TZ).date() - timedelta(days=gun)).isoformat()
    with db.get_conn() as c:
        r = c.execute(
            """SELECT COUNT(*),
                      SUM(CASE WHEN piyasa_farki IS NULL THEN 1 ELSE 0 END)
                 FROM decisions
                WHERE tarih >= ? AND sonuc IS NOT NULL AND sonuc <> ''
                  AND sonuc NOT LIKE '%DEĞERLENDİRME DIŞI%'
                  AND UPPER(COALESCE(karar,'')) NOT LIKE '%KILL%'""", (esik,)).fetchone()
    toplam, bos = r[0] or 0, r[1] or 0
    oran = (bos / toplam) if toplam else 0.0
    return {"gun": gun, "toplam": toplam, "dolu": toplam - bos, "bos": bos,
            "bos_oran": oran, "saglikli": (oran <= ALPHA_BOS_ESIK) if toplam else True}


def run(verbose: bool = True) -> int:
    from src.db import database as db
    db.init_db()
    _hist_cache_temizle()          # kosu basi: onbellek gunler arasi bayatlamasin
    today = datetime.now(_TZ).date()
    # Eligibility artik ISLEM GUNU bazli: gercek gating _price_change icinde yapilir
    # (MIN_ISLEM_GUNU islem gunu gecmediyse None doner). Burada yalniz bugun/gelecek
    # tarihli kararlari disla; hafta sonu kararlari da degerlendirmeye girer.
    cutoff = today.isoformat()

    with db.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM decisions WHERE (sonuc IS NULL OR sonuc='') "
            "AND tarih < ? ORDER BY id", (cutoff,))]

    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] degerlendirilecek karar: {len(rows)} "
              f"| AL basari esigi: piyasa_farki >= {AL_PIYASA_ESIGI}")
    from src.ai import maliyet
    guncellenen = 0
    _acc = maliyet.bos_acc()                 # maliyet: 'neden yanlis' Haiku cagrilarini topla
    for r in rows:
        # KILL_SWITCH kayitlari: veri eksikligiyle (fiyat yok/bayat) AI cagrilmadan
        # uretildi. TUT bandiyla degerlendirilip DOGRU/YANLIS istatistigini kirletmesin;
        # karnede kalir ama degerlendirme disi birakilir.
        if "KILL" in (r.get("karar") or "").upper():
            db.set_decision_outcome(r["id"], "DEĞERLENDİRME DIŞI · veri eksikliği")
            guncellenen += 1
            if verbose:
                print(f"  {r['ticker']:7} {r['karar']:11} {r['tarih']} "
                      f"-> DEĞERLENDİRME DIŞI (veri eksikligi)")
            continue
        kg = _kapanis_gun(r["karar"], r.get("tahmini_sure"))   # TUT'ta AI tahmini sure
        deg = _price_change(r["ticker"], r["tarih"], kg)
        if deg is None:
            if verbose:
                print(f"  {r['ticker']} ({r['tarih']}, {r['karar']}): "
                      f"{kg} islem gunu dolmadi / veri yok -> bekliyor")
            continue
        # PIYASAYA KARSI: benchmark (BIST -> XU100.IS, ABD -> SPY) ayni pencerede
        piyasa_farki = None
        bench_deg = _benchmark_change(r["ticker"], r["tarih"], kg)
        if bench_deg is not None:
            piyasa_farki = round(deg - bench_deg, 2)
        dogru = _verdict(r["karar"], deg, piyasa_farki=piyasa_farki)
        sonuc = f"{deg:+.1f}% · {'DOGRU' if dogru else 'YANLIS'}"
        if piyasa_farki is not None:
            sonuc += f" · piyasa {piyasa_farki:+.1f}p"
        # AL: endeksi yendi (piyasa_farki>=0) ama mutlak zarar var -> YANLIS + not
        if ("AL" in (r["karar"] or "").upper() and piyasa_farki is not None
                and piyasa_farki >= 0 and deg <= 0):
            sonuc += " · endeksi yendi ama zarar"
        # L2: yanlis cikan kararlar icin kisa Haiku sebep analizi
        yanlis_sebep = None
        if not dogru:
            yanlis_sebep = _yanlis_analiz(r["ticker"], r["karar"],
                                          r.get("gerekce"), deg, acc=_acc)
        db.set_decision_outcome(r["id"], sonuc, yanlis_sebep=yanlis_sebep,
                                piyasa_farki=piyasa_farki)
        guncellenen += 1
        if verbose:
            ek = f"  · sebep: {yanlis_sebep}" if yanlis_sebep else ""
            print(f"  {r['ticker']:7} {r['karar']:11} {r['tarih']} "
                  f"({kg}ig) -> {sonuc}{ek}")

    maliyet.logla(_acc, HABER_MODEL, etiket="update_decisions")   # maliyet: tek TOKEN OZET
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] {guncellenen} karar sonucu guncellendi.")

    # ALPHA BACKFILL: benchmark o an bos donduysa piyasa_farki NULL kalir. Eskiden
    # bu KALICIYDI (karar bir daha ele alinmazdi) -> alpha olcumu sessizce kayboldu.
    # Artik her kosuda tekrar denenir; benchmark sonradan gelince geriye donuk dolar.
    try:
        bf = alpha_backfill(verbose=verbose)
        if verbose and bf["dolduruldu"]:
            print(f"  [alpha] {bf['dolduruldu']} karar geriye donuk dolduruldu "
                  f"({bf['hukum_degisti']} hukum degisti)")
    except Exception as e:
        if verbose:
            print(f"  [alpha] backfill atlandi: {type(e).__name__}: {str(e)[:80]}")

    # ALPHA OLCUM SAGLIGI: NULL orani esigi asarsa benchmark yine bozuk demektir.
    try:
        s = alpha_olcum_sagligi()
        if verbose:
            print(f"  [alpha] olcum sagligi (7g): dolu={s['dolu']}/{s['toplam']} "
                  f"| bos %{s['bos_oran']*100:.0f} | {'OK' if s['saglikli'] else 'BOZUK'}")
        if not s["saglikli"] and s["toplam"] >= 10:
            from src.notify import telegram
            telegram.notify_admins(
                f"Alpha ölçümü bozuk: son {s['gun']} günde {s['bos']}/{s['toplam']} "
                f"kararda piyasa_farkı boş (%{s['bos_oran']*100:.0f} > "
                f"%{ALPHA_BOS_ESIK*100:.0f}). Benchmark (XU100.IS/SPY) çekimi "
                f"başarısız — alpha başarı oranları güvenilmez.", prefix="🔴")
    except Exception as e:
        if verbose:
            print(f"  [alpha] saglik kontrolu atlandi: {type(e).__name__}")

    # GUN VERI KALITESI: son 14 gunu damgala (yeni KIRLI gun -> admin Telegram) +
    # temiz karne ozeti (KIRLI gunler basari hesabina KATILMAZ). Karar kurallarina
    # dokunmaz; yalniz kayit/istatistik. Hata olsa da ana degerlendirme etkilenmez.
    try:
        from src.ops import gun_kalitesi
        from datetime import timedelta
        log_ist = gun_kalitesi._log_gun_istatistik()
        with db.get_conn() as c:
            son_gunler = [r[0] for r in c.execute(
                "SELECT DISTINCT tarih FROM decisions WHERE tarih >= ? ORDER BY tarih",
                ((datetime.now(_TZ).date() - timedelta(days=14)).isoformat(),))]
        for g in son_gunler:
            gun_kalitesi.damgala(g, log_ist=log_ist, alert=True, verbose=False)
        if verbose:
            gun_kalitesi.karne_ozet(verbose=True)
    except Exception as e:
        if verbose:
            print(f"  [gun_kalitesi] atlandi: {type(e).__name__}: {str(e)[:80]}")
    return guncellenen


def mini_update(verbose: bool = True) -> int:
    """1 GUNLUK MINI DEGERLENDIRME: AL ve SAT/AZALT/UZAK_DUR kararlarinin karar
    tarihinden 1 ISLEM GUNU sonraki fiyat degisimini (ilk_gun_degisim) doldurur.
    Sadece ilk_gun_degisim'i bos olan yonlu kararlar; bugun/gelecek tarihliler haric.
    Ana sonuc degerlendirmesini (run) ETKILEMEZ; ayri/hizli geri bildirimdir."""
    from src.db import database as db
    db.init_db()
    today = datetime.now(_TZ).date().isoformat()
    with db.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM decisions WHERE ilk_gun_degisim IS NULL AND tarih < ? "
            "ORDER BY id", (today,))]
    guncellenen = 0
    for r in rows:
        k = (r.get("karar") or "").upper()
        yonlu = k.startswith("AL") or "SAT" in k or "AZALT" in k or "UZAK" in k
        if not yonlu:                       # TUT/BEKLE/VETO/KILL -> 1.gun bakilmaz
            continue
        deg = _price_change(r["ticker"], r["tarih"], 1)   # 1 islem gunu sonrasi
        if deg is None:
            continue                         # 1 islem gunu dolmadi / veri yok -> bekle
        db.set_decision_ilk_gun(r["id"], deg)
        guncellenen += 1
        if verbose:
            print(f"  [mini] {r['ticker']:7} {k:11} {r['tarih']} -> 1.gun {deg:+.1f}%")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] mini_update: {guncellenen} karar "
              f"1.gun degisimi dolduruldu.")
    return guncellenen


def backfill_piyasa_farki(verbose: bool = True) -> int:
    """GERIYE DONUK piyasa_farki doldurma (+ AL sonuc yeniden degerlendirme).

    sonuc'u DOLU (degerlendirilmis) tum kararlar icin, karardan degerlendirme
    penceresine kadar olan hisse getirisi ile benchmark getirisi (BIST -> XU100.IS,
    ABD -> SPY) farkini hesaplar ve decisions.piyasa_farki'na yazar. Yeni AL basari
    kriteri (piyasa_farki >= -1.5) devreye girdigi icin sonuc metnindeki DOGRU/YANLIS
    de guncel _verdict ile yeniden turetilip yazilir (tutarlilik).

    KILL_SWITCH kararlari + fiyat/benchmark verisi gelmeyenler atlanir (mevcut
    kayit korunur). Tekrar calistirilabilir (idempotent)."""
    from src.db import database as db
    db.init_db()
    with db.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM decisions WHERE sonuc IS NOT NULL AND sonuc <> '' "
            "ORDER BY id")]
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] backfill piyasa_farki: "
              f"{len(rows)} degerlendirilmis karar taranacak")
    guncellenen = 0
    for r in rows:
        karar = (r.get("karar") or "").upper()
        if "KILL" in karar:                      # KILL_SWITCH -> degerlendirme yok
            continue
        kg = _kapanis_gun(r["karar"], r.get("tahmini_sure"))
        deg = _price_change(r["ticker"], r["tarih"], kg)
        if deg is None:                          # fiyat penceresi/veri yok -> koru
            continue
        bench_deg = _benchmark_change(r["ticker"], r["tarih"], kg)
        piyasa_farki = round(deg - bench_deg, 2) if bench_deg is not None else None
        dogru = _verdict(r["karar"], deg, piyasa_farki=piyasa_farki)
        sonuc = f"{deg:+.1f}% · {'DOGRU' if dogru else 'YANLIS'}"
        if piyasa_farki is not None:
            sonuc += f" · piyasa {piyasa_farki:+.1f}p"
        db.set_decision_outcome(r["id"], sonuc, piyasa_farki=piyasa_farki)
        guncellenen += 1
        if verbose:
            pf = f"{piyasa_farki:+.1f}p" if piyasa_farki is not None else "—"
            print(f"  {r['ticker']:7} {r['karar']:11} {r['tarih']} "
                  f"({kg}ig) -> {sonuc}  [piyasa_farki {pf}]")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] backfill: {guncellenen} karar "
              f"piyasa_farki + sonuc guncellendi.")
    return guncellenen


if __name__ == "__main__":
    import sys as _sys
    arg = _sys.argv[1] if len(_sys.argv) > 1 else ""
    if arg == "mini":
        mini_update()
    elif arg == "backfill":
        backfill_piyasa_farki()
    else:
        run()
        try:
            from src.db import database as _db
            _db.kalp_at("update_decisions")
        except Exception:
            pass
