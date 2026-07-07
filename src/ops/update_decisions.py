"""Karar sonuclarini otomatik doldurur (hafiza/ogrenme).

Her gece calisir (cron 23:30). Sonucu HENUZ BOS olan kararlar icin, karar gunundeki
kapanis ile KARAR TIPINE GORE N ISLEM GUNU sonraki kapanis arasindaki yuzde degisimi
hesaplar ve karara gore 'DOGRU/YANLIS' verir; decisions.sonuc kolonunu gunceller.
Yanlis cikan kararlar icin ucuz Haiku ile kisa 'neden yanlis' analizi yapilir
(decisions.yanlis_sebep). Degerlendirme penceresi: AL=5, SAT=3, TUT=10, BEKLE=5 islem gunu.

Kazanma kurali (karar yonune gore):
  AL / AL_TEMKINLI : ENDEKSTEN 1.5 puandan fazla geri kalmadiysa DOGRU
                     (piyasa_farki >= -1.5). Benchmark: BIST hisseleri -> XU100.IS,
                     ABD hisseleri -> SPY. Benchmark verisi yoksa mutlak yon (fiyat
                     yukseldiyse DOGRU) uygulanir.
  SAT / GUCLU_SAT / AZALT : fiyat dustuyse DOGRU
  TUT / BEKLE      : fiyat ~yatay kaldiysa (|degisim| <= %5) DOGRU
  VETO / UZAK_DUR  : islemden kacinildi; fiyat yukselmediyse (<= 0) DOGRU

piyasa_farki (her yonlu karar icin) = hisse_getiri - benchmark_getiri (ayni pencere).
BIST hisseleri XU100.IS'e, ABD hisseleri SPY'a gore olculur. decisions.piyasa_farki
kolonuna yazilir; gecmis kayitlar 'backfill' ile geriye donuk doldurulabilir.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")
TUT_BANT = 5.0           # TUT icin yatay sayilan +/- yuzde bandi
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


# AL basari esigi: hisse endeksten (BIST -> XU100.IS, ABD -> SPY) EN FAZLA bu kadar
# puan geri kalabilir. piyasa_farki >= -1.5 ise AL DOGRU sayilir; yani hisse endekse
# yakin/uzerinde performans gosterdiyse basarili (mutlak yukselis sarti kaldirildi).
AL_PIYASA_ESIGI = -1.5


def _verdict(karar: str, degisim: float, piyasa_farki: float = None,
             bist_degisim: float = None) -> bool:
    """Karar dogru mu? AL'da PIYASA KIYASI belirleyicidir: hisse benchmark'tan
    (BIST -> XU100.IS, ABD -> SPY) 1.5 puandan fazla geride kalmadiysa (piyasa_farki
    >= -1.5) AL DOGRU'dur. Benchmark verisi yoksa mutlak yon (fiyat yukseldiyse DOGRU)
    kullanilir. bist_degisim parametresi geriye donuk uyum icin korunur (kullanilmaz)."""
    k = (karar or "").upper()
    if "VETO" in k or "UZAK" in k:   # VETO / UZAK_DUR: girilmedi -> yukselmediyse dogru
        return degisim <= 0
    if "SAT" in k or "AZALT" in k:   # SAT, GUCLU_SAT, AZALT
        return degisim < 0
    if "AL" in k:           # AL, AL_TEMKINLI: endeksten 1.5 puandan cok geri kalmadiysa DOGRU
        if piyasa_farki is not None:
            return piyasa_farki >= AL_PIYASA_ESIGI
        return degisim > 0             # benchmark verisi yok -> mutlak yon
    return abs(degisim) <= TUT_BANT   # TUT


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


def _symbol_change(symbol: str, karar_tarihi: str, kapanis_gun: int):
    """Verilen yfinance sembolu icin karar gunu -> kapanis_gun islem gunu sonraki
    yuzde degisim (bkz. _price_change). Pencere dolmadiysa/veri yoksa None."""
    from src.data.factory import get_data_source
    import pandas as pd

    # Karar tarihinden ONCEKI islem gununu de yakalamak icin genis pencere (uzun tatiller)
    start = (datetime.fromisoformat(karar_tarihi).date()
             - timedelta(days=12)).isoformat()
    try:
        df = get_data_source().get_history(symbol, start=start)
    except Exception:
        return None
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


def _benchmark_symbol(ticker: str) -> str:
    """Hissenin piyasa kiyas endeksi: BIST -> XU100.IS, ABD -> SPY."""
    return "XU100.IS" if _is_bist(ticker) else "SPY"


def _benchmark_change(ticker: str, karar_tarihi: str, kapanis_gun: int):
    """Hissenin benchmark'inin (BIST -> XU100.IS, ABD -> SPY) ayni pencere icindeki
    yuzde degisimi. Veri yoksa None. Hisse degisimi ile ayni bar mantigini kullanir."""
    return _symbol_change(_benchmark_symbol(ticker), karar_tarihi, kapanis_gun)




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


def _yanlis_analiz(ticker, karar, gerekce, degisim, client=None):
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
        import json
        text = next((b.text for b in resp.content if b.type == "text"), "")
        d = json.loads(text)
        kat, ac = d.get("kategori", "belirsiz"), (d.get("aciklama") or "").strip()
        return f"{kat}: {ac}" if ac else kat
    except Exception:
        return None


def run(verbose: bool = True) -> int:
    from src.db import database as db
    db.init_db()
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
    guncellenen = 0
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
        # L2: yanlis cikan kararlar icin kisa Haiku sebep analizi
        yanlis_sebep = None
        if not dogru:
            yanlis_sebep = _yanlis_analiz(r["ticker"], r["karar"],
                                          r.get("gerekce"), deg)
        db.set_decision_outcome(r["id"], sonuc, yanlis_sebep=yanlis_sebep,
                                piyasa_farki=piyasa_farki)
        guncellenen += 1
        if verbose:
            ek = f"  · sebep: {yanlis_sebep}" if yanlis_sebep else ""
            print(f"  {r['ticker']:7} {r['karar']:11} {r['tarih']} "
                  f"({kg}ig) -> {sonuc}{ek}")

    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] {guncellenen} karar sonucu guncellendi.")
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
