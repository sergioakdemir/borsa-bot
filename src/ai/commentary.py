"""Tam analiz zinciri: tum veri kaynaklarini birlestirip AI yorumu uretir.

VERI KAYNAKLARI
  1. yfinance  : fiyat, hacim, 10/50 gunluk ortalama, 52 hafta yuksek/dusuk
  2. KAP proxy : son 30 gunluk bildirimler (src/news/kap_source.py)
  3. Haber     : src/news/ kaynaklarindan son 7 gunluk (filtreden gecmis) haberler

AI YORUMU
  Tum veri birlestirilip Claude'a (claude-sonnet-4-6, max_tokens=1000) gonderilir.
  Cikti: karar (AL/TUT/BEKLE/AZALT/UZAK_DUR), puan(1-10), risk(1-10), eminlik(Dusuk/Orta/Yuksek),
  gerekce, neden_simdi, fiyatlanmis_mi.
  Risk ajani: risk 9+ ve karar AL ise -> VETO.

CIKTI
  data/ai_commentary.json (web arayuzu bu dosyayi okur) + decisions tablosu.

Calistir:  python -m src.ai.commentary [TICKER ...]
"""
import json
import math
import os
import re
import statistics
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.ai import maliyet   # ortak maliyet hesabi + TOKEN OZET loglama

_TZ = ZoneInfo("Europe/Istanbul")
ROOT = Path(__file__).resolve().parents[2]
OUT_PATH = ROOT / "data" / "ai_commentary.json"

MODEL = "claude-sonnet-4-6"
# 1500: yapisal cikti (Verdict) uzun gerekcede 1000 token'i asip JSON'u kesiyordu
# -> parse hatasi/sessiz verdict kaybi. Fatura URETILEN token'dan oldugu icin
# tavani yukseltmek maliyeti degistirmez, yalniz truncation'i onler.
MAX_TOKENS = 1500

# --- AI cagri hatasi siniflandirmasi (15 Tem 2026) ---------------------------
# 15 Tem 2026: kredi bitince batch cagrisi BadRequestError verdi, evaluate_all
# tek-tek yola dustu ve 92 hisse "Hata: BadRequestError" ile atlandi; sessiz
# basarisizlik alarmi YALNIZ run_batch icinde oldugu icin kimse haberdar olmadi
# ve icerigi bos brifing kullaniciya gitti. Ayrica 92 cagri bosuna denendi.
# Cozum: (1) AI hatasi olan atlamalari 'ai_hata' bayragiyla isaretle,
# (2) kredi bitti sinyalini yakalayip GUN BOYU devre kesici uygula,
# (3) alarmi iki yolun da (batch + tek-tek) kullandigi ortak yardimciya tasi.

# Bu exception adlari AI cagrisinin kendisinin basarisiz oldugunu gosterir
# (veri hazirligi hatasi degil). anthropic SDK sinif adlari.
_AI_CAGRI_HATALARI = (
    "BadRequestError", "RateLimitError", "AuthenticationError",
    "PermissionDeniedError", "NotFoundError", "APIStatusError",
    "APIConnectionError", "APITimeoutError", "InternalServerError",
    "OverloadedError", "APIError",
)

# Kredi/bakiye tukendi imzalari (Anthropic 400 invalid_request_error govdesi).
_KREDI_IMZALARI = ("credit balance is too low", "plans & billing",
                   "purchase credits", "billing")


def _ai_cagri_hatasi_mi(e) -> bool:
    """Exception AI cagrisinin basarisizligi mi (veri hazirligi hatasi degil)?"""
    return type(e).__name__ in _AI_CAGRI_HATALARI


def kredi_bitti_mi(e) -> bool:
    """Exception 'kredi/bakiye bitti' hatasi mi? (para harcamadan reddedilen cagri)"""
    m = str(e).lower()
    return any(s in m for s in _KREDI_IMZALARI)


def kredi_freni_aktif_mi() -> bool:
    """Bugun kredi bitti bayragi konmus mu? Konmussa yeni AI cagrisi YAPILMAZ
    (bosa deneme yok). Bayrak gunluk; ertesi gun kendiliginden dusler."""
    try:
        from src.db import database as db
        bugun = datetime.now(_TZ).date().isoformat()
        return bool(db.get_setting(f"ai_kredi_bitti:{bugun}"))
    except Exception:
        return False


def kredi_freni_koy(sebep: str = "") -> None:
    """Kredi bitti -> gun sonuna kadar AI cagrilarini durdur + admin'e bildir.
    Ayni gun icinde bir kez bildirir (bayrak zaten varsa tekrar bildirmez)."""
    try:
        from src.db import database as db
        bugun = datetime.now(_TZ).date().isoformat()
        anahtar = f"ai_kredi_bitti:{bugun}"
        if db.get_setting(anahtar):
            return                       # bugun zaten isaretlendi + bildirildi
        db.set_setting(anahtar, sebep[:200] or "1")
    except Exception:
        return
    try:
        from src.notify import telegram
        telegram.notify_admins(
            "KREDİ BİTTİ VE OTOMATİK YENİLEME ÇALIŞMADI: Anthropic API bakiyesi "
            "tükendi — AI çağrıları gün sonuna kadar DURDURULDU (boşa deneme "
            "yapılmayacak), karar üretimi durdu. Otomatik yenileme ($5→$20) "
            "devrede olmasına rağmen bakiye dolmadı — MANUEL KONTROL gerekiyor "
            "(ödeme yöntemi/limit?). Bakiye yüklenince otomatik devam eder.",
            prefix="🔴")
    except Exception as e:
        print(f"  [kredi] admin bildirimi gonderilemedi: {type(e).__name__}: "
              f"{str(e)[:80]}")


def kredi_freni_kaldir() -> None:
    """Basarili bir AI cagrisindan sonra bayragi dusur (bakiye yuklenmis)."""
    try:
        from src.db import database as db
        bugun = datetime.now(_TZ).date().isoformat()
        if db.get_setting(f"ai_kredi_bitti:{bugun}"):
            db.set_setting(f"ai_kredi_bitti:{bugun}", "")
    except Exception:
        pass


def atlama_ozeti(results, tickers=None) -> dict:
    """Bir brifing kosusunun atlama karnesi. Cagiranlar: run/run_batch alarmi ve
    morning.main (bos brifing engeli).

    ai_hata  : AI cagrisi basarisiz oldugu icin karar uretilemeyen hisse sayisi
    veri_freni: KILL_SWITCH (fiyat verisi yok/bayat) — bu SAGLIKLI bir fren,
                bos brifing sayilmaz, ai_hata'dan ayri tutulur.
    uretilen : gercekten karar uretilen hisse sayisi (brifingde yazilabilecek tek sayi)
    """
    kayitlar = [r for r in (results or []) if isinstance(r, dict)]
    toplam = len(tickers) if tickers else len(kayitlar)
    ai_hata = [r for r in kayitlar if r.get("skipped") and r.get("ai_hata")]
    veri_freni = sum(1 for r in kayitlar if r.get("kill_switch"))
    uretilen = sum(1 for r in kayitlar if not r.get("skipped"))
    ornek = next((str(r.get("reason") or "") for r in ai_hata), "")
    return {
        "toplam": toplam,
        "uretilen": uretilen,
        "ai_hata": len(ai_hata),
        "veri_freni": veri_freni,
        "ai_hata_orani": (len(ai_hata) / toplam) if toplam else 0.0,
        "ornek_hata": ornek,
        "kredi_bitti": kredi_freni_aktif_mi(),
    }


def _atlama_alarmi(results, tickers, verbose: bool = True) -> dict:
    """Sessiz basarisizlik alarmi — run() ve run_batch() ORTAK kullanir.

    Watchlist'in >%10'u AI cagri hatasiyla dustuyse o brifingde karar
    uretilememis demektir -> yoneticilere Telegram uyarisi. ">%5 SARI (yalniz
    log), >%10 KIRMIZI (anlik admin Telegram)" esikleri 12 Tem 2026 denetiminden.
    "N hisse tarandi" yalani bir daha sessiz kalmasin.
    """
    ozet = atlama_ozeti(results, tickers)
    toplam, ai_hata, oran = ozet["toplam"], ozet["ai_hata"], ozet["ai_hata_orani"]
    us_n = sum(1 for t in (tickers or []) if str(t).lower().endswith(":us"))
    pazar = "US" if toplam and us_n > toplam / 2 else "BIST"
    if oran > 0.10:
        neden = " (kredi bitti)" if ozet["kredi_bitti"] else ""
        mesaj = (f"{pazar} brifingi: {ai_hata}/{toplam} hisse (%{oran*100:.0f}) "
                 f"AI çağrı hatasıyla atlandı, karar üretilmedi{neden}. "
                 f"Örnek: {ozet['ornek_hata'] or '-'}")
        if verbose:
            print(f"  [KIRMIZI ALARM] {mesaj}")
        # Kredi freni zaten kendi (daha net) bildirimini gonderdi -> tekrarlama.
        if not ozet["kredi_bitti"]:
            try:
                from src.notify import telegram
                telegram.notify_admins(mesaj, prefix="🔴")
            except Exception as e:
                print(f"  [ALARM] telegram gonderilemedi: {type(e).__name__}: "
                      f"{str(e)[:80]}")
    elif oran > 0.05:
        if verbose:
            print(f"  [SARI UYARI] {pazar} brifingi: {ai_hata}/{toplam} "
                  f"(%{oran*100:.0f}) hisse atlandi (gunluk karneye yansir).")
    return ozet

# Strateji surumu: bu sabit, uretilen her yeni karar ve trade'e etiketlenir. 7 Temmuz
# 2026 buyuk paketiyle (deterministik stop/hedef, cift risk vetosu, BEKLE pozisyon
# yonetimi, sektor tavani) 'v2'ye gecildi. Migration'dan once yazilan tum kayitlar 'v1'.
# Performans karsilastirmasi (brifing/karne) bu etikete gore v1/v2 ayrimi yapar.
#
# 15 Tem 2026 -> 'v2.1': momentum kor noktasi kapatildi (momentum_profili girdisi +
# "zayif bilanco TEK BASINA UZAK_DUR sebebi degildir" prompt kurali). Karar esikleri,
# filtreler ve vetolar DEGISMEDI — bu yuzden ana surum v2 kaldi, alt surum .1 oldu.
# Test donemi bu etiketle olculur (bkz. src/ops/test_donemi.py).
# NOT: paper_trades tablosunda strategy_version kolonu YOK; surum karsilastirmasi
# decisions + trades uzerinden yapilir.
STRATEGY_VERSION = "v2.1"
HABER_MODEL = "claude-haiku-4-5"     # haber etki analizi: ucuz + hizli
_HABER_ETKI_DENEME = 2               # gecici hatada toplam deneme sayisi
_HABER_ETKI_BEKLE = 2.0              # denemeler arasi bekleme (sn)
# NOT: Maliyet/fiyat hesabi src/ai/maliyet.py'ye tasindi (tek kaynak, dogru
# model + tier). run_batch batch=True, run (senkron fallback) batch=False ile
# loglar. Eski _BATCH_FIYAT_* sabitleri kaldirildi.

# Her haberin bu hisseye etkisini etiketleyen ucuz Haiku cagrisi semasi
_HABER_ETKI_SCHEMA = {
    "type": "object",
    "properties": {
        "analizler": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "olumlu_mu": {"type": "boolean",
                                  "description": "Haber bu hisse icin olumlu mu"},
                    "etki_buyuklugu": {"type": "string",
                                       "enum": ["dusuk", "orta", "yuksek"]},
                    "etki_yonu": {"type": "string",
                                  "enum": ["yukari", "asagi", "belirsiz"]},
                },
                "required": ["olumlu_mu", "etki_buyuklugu", "etki_yonu"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["analizler"],
    "additionalProperties": False,
}


def _haber_etki_analizi(ticker: str, haberler: list, client=None) -> list:
    """Her haberi UCUZ Haiku cagrisiyla bu hisse acisindan etiketler:
    olumlu_mu (bool), etki_buyuklugu (dusuk/orta/yuksek), etki_yonu (yukari/asagi/belirsiz).
    Alanlar haber dict'lerine eklenir. Hata/anahtar yoksa haberler degismeden doner.

    16 Tem 2026: bu fonksiyon hatayi TAMAMEN yutuyordu (sayac artar, log yok).
    09:00-09:30 arasi 28 cagri patladi ve gerceklesen hata TIPI hicbir yerde
    kalmadigi icin sonradan teshis EDILEMEDI — tek kanit etiketsiz kalan 27
    hisseydi. Sessiz yutma yerine: (1) hata tipi+mesaji loga yazilir,
    (2) gecici hatalarda bir kez tekrar denenir (patlama gecici cikti: ayni
    veriyle 40 dk sonra hepsi sorunsuz gecti; tek deneme = etiketler bosuna
    kayip), (3) kredi bittiyse tekrar denenmez (bosa cagri).
    """
    if not haberler:
        return haberler
    try:                                 # anahtar yoksa/SDK kurulu degilse: sessiz gec
        import anthropic
        client = client or anthropic.Anthropic()
    except Exception as e:
        print(f"  [haber_etki] {ticker}: AI istemcisi kurulamadi -> etiketsiz "
              f"({type(e).__name__}: {str(e)[:120]})")
        return haberler
    ozet_liste = [{"no": i + 1, "baslik": h.get("baslik"),
                   "ozet": (h.get("ozet") or "")[:300]}
                  for i, h in enumerate(haberler)]
    sys_p = (
        "Sen bir finans-haberi etki siniflandiricisin. Verilen her haberi YALNIZCA "
        f"{ticker} hissesi acisindan etiketle. Her haber icin: olumlu_mu (true/false), "
        "etki_buyuklugu (dusuk/orta/yuksek), etki_yonu (yukari/asagi/belirsiz). "
        "Haberlerin sirasini KORU ve her haber icin bir analiz dondur. Dolayli/zayif "
        "iliskide 'dusuk' ve 'belirsiz' kullan; abartma.")
    for deneme in range(1, _HABER_ETKI_DENEME + 1):
        try:
            resp = client.messages.create(
                model=HABER_MODEL, max_tokens=700, system=sys_p,
                messages=[{"role": "user",
                           "content": json.dumps(ozet_liste, ensure_ascii=False)}],
                output_config={"format": {"type": "json_schema",
                                          "schema": _HABER_ETKI_SCHEMA}})
            text = next((b.text for b in resp.content if b.type == "text"), "")
            analizler = json.loads(text).get("analizler", [])
            for h, a in zip(haberler, analizler):
                h["olumlu_mu"] = a.get("olumlu_mu")
                h["etki_buyuklugu"] = a.get("etki_buyuklugu")
                h["etki_yonu"] = a.get("etki_yonu")
            if deneme > 1:
                print(f"  [haber_etki] {ticker}: {deneme}. denemede basarili")
            return haberler
        except Exception as e:
            son = deneme >= _HABER_ETKI_DENEME or kredi_bitti_mi(e)
            print(f"  [haber_etki] {ticker}: {len(haberler)} haber etiketlenemedi "
                  f"({deneme}/{_HABER_ETKI_DENEME}) {type(e).__name__}: {str(e)[:200]}")
            if not son:
                time.sleep(_HABER_ETKI_BEKLE)
                continue
            try:                                 # gunluk AI hata sayaci (health_monitor okur)
                from src.db import database as _db
                _db.ai_hata_inc()
            except Exception:
                pass
            return haberler
    return haberler


def _save_results(results, verbose=False):
    """ai_commentary.json'a yazar. Bu kosunun market(ler)indeki eski kayitlari
    yenisiyle degistirir, DIGER market'lerin kayitlarini KORUR. Boylece BIST
    (09:00) ve ABD (15:30) brifingleri birbirinin verisini ezmez."""
    OUT_PATH.parent.mkdir(exist_ok=True)
    try:
        mevcut = json.loads(OUT_PATH.read_text(encoding="utf-8"))
        if not isinstance(mevcut, list):
            mevcut = []
    except Exception:
        mevcut = []
    yeni_marketler = {(r.get("market") or "").lower() for r in results}
    korunan = [r for r in mevcut if (r.get("market") or "").lower() not in yeni_marketler]
    birlesik = korunan + list(results)
    OUT_PATH.write_text(json.dumps(birlesik, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    if verbose:
        print(f"\nKaydedildi: {OUT_PATH} ({len(results)} yeni · {len(birlesik)} toplam)")
    return birlesik

SYSTEM = (
    "Sen Max'sin: 40 yasinda, 25 yillik tecrubeli bir Turk borsa uzmani. Direkt ve "
    "net karar verirsin, gereksiz yumusatmazsin; piyasayi iyi okur, kullaniciyi "
    "korur, gerektiginde sert uyarirsin. Kendini tanitma, dogrudan ise gir. Jargon "
    "kullanma (RSI/MACD yasak). Net karar ver. SADECE su 5 karardan BIRINI ver: "
    "AL, TUT, BEKLE, AZALT, UZAK_DUR. SAT, GUCLU_SAT, NOTR, IZLE, EKLE, RADARDA gibi "
    "eski/baska kodlar YASAK (gerekcede de bu kelimeler ve 'risk 5', 'skor 8/10' "
    "gibi ifadeler kullanilmaz). "
    "'sade_yorum' alani KULLANICIYA gosterilir: 1-2 kisa cumle, gunluk dil, HICBIR "
    "sayi/oran/yuzde/analist sayisi icermez (ROE, F/K, MA10, MA50, RSI YASAK); teknik "
    "rakamlari yalniz 'gerekce'de tut. "
    "TEKNIK ANALIZ TERIMLERINI KULLANICIYA GOSTERME: MA10, MA50, 52 hafta zirvesi/dibi, "
    "RSI, MACD, direnc/destek gibi terimleri arka planda kullan ama sonucu sade yaz. "
    "Ornek: ortalamalarin altinda ve dusus varsa 'Hisse zayif gidiyor' ya da "
    "'Trend asagi'; ortalamalarin uzerinde ve yukselis varsa 'Hisse guclu gidiyor' "
    "ya da 'Trend yukari' gibi gunluk dille anlat. "
    "Anlamlar: AL=al / pozisyon ac; TUT=elindekini koru; BEKLE=teyit/katalizor bekle; "
    "AZALT=pozisyonu kismen kucult; UZAK_DUR=bu hisseden uzak dur (elinde varsa sat, "
    "yoksa girme). Gerekceni 2-3 cumlede soyle. Veri yoksa yorum yapma. Hata yaparsan "
    "kabul et.\n"
    "AL CESARETI: Guclu teknik sinyal + olumlu temel veri bir arada ise AL karari "
    "vermekten cekinme. Temkinli olmak iyidir ama surekli TUT demek de bir hata "
    "turudur. Puan 7 ve uzerinde guclu bir gorunum varsa AL'i dusun; her seyin "
    "mukemmel hizalanmasini bekleme.\n"
    "YUKSEK PUAN DISIPLINI (Puan 8): Puan 8 vermek icin su dort kosuldan EN AZ IKISI "
    "net saglanmali: (1) guclu yukselis trendi (fiyat tum ortalamalarin uzerinde), "
    "(2) analist konsensusu AL ve en az 10 analist, (3) son 24 saatte olumlu haber/KAP "
    "bildirimi, (4) F/K sektor ortalamasinin altinda (ucuz). Bu dortten en az ikisi net "
    "DEGILSE puani 7'de tut. Yuksek puan, sadece iyimserlik degil; ayrisan ustun veriyle "
    "DESTEKLENMELI.\n"
    "AL ESIGI (piyasaya gore): BIST hisselerinde AL demek icin puan 8+ ara (daha "
    "secici ol). ABD hisselerinde puan 7+ yeterli. Esigin altinda guclu bir gorunum "
    "olsa bile AL yerine TUT/BEKLE tercih et.\n"
    "BEKLE karari: SADECE gercekten belirsiz durumlarda (yon belirsiz, kritik bir "
    "veri/katalizor bekleniyor ya da sinyal olgunlasmadiysa) BEKLE de; diger tum "
    "durumlarda AL/TUT/AZALT/UZAK_DUR'dan birini tercih et. BEKLE secersen 'tekrar_bak_kosulu' "
    "alanina hangi "
    "somut kosul olusunca tekrar bakilmasi gerektigini yaz (orn. 'fiyat 50 gunluk "
    "ortalamayi yukari gecerse' veya 'bilanco aciklaninca'). Diger kararlarda bu alan bos.\n\n"
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
    "HABER ETKI ALANLARI: 'haberler_son' bolumunde her haberin olumlu_mu, "
    "etki_buyuklugu (dusuk/orta/yuksek) ve etki_yonu (yukari/asagi/belirsiz) alanlarina "
    "BAK. Yuksek etkili OLUMLU haber (olumlu_mu=true, etki_buyuklugu='yuksek') varsa AL "
    "kararina YAKLAS. Yuksek etkili OLUMSUZ haber (olumlu_mu=false, etki_buyuklugu="
    "'yuksek') varsa AZALT/UZAK_DUR dusun. Dusuk etkili veya belirsiz haberleri kararda asiri "
    "agirliklandirma.\n\n"
    "ANALIST KONSENSUSU: Veride 'analist_konsensus' varsa dikkate al (kac kurum, "
    "ortalama hedef fiyat, getiri potansiyeli, AL/TUT/SAT dagilimi). Guclu bir "
    "konsensus puani destekler; senin teknik gorusunle celisiyorsa nedenini kisaca "
    "belirt. Hedef fiyati kendi rakamin gibi sunma, 'analistlerin ortalama hedefi' de. "
    "Veride 'analist_konsensus.veri_kalitesi' 'yetersiz' ise (3'ten az analist) bu "
    "veriye az agirlik ver ve kesin sonuc cikarma; 'bayat' veri zaten verilmez.\n\n"
    "SIRKET SAGLIGI (AGIRLIK ~%40): Veride 'sirket_sagligi' varsa bu sirketin FINANSAL "
    "SAGLIGINI bu verilerle degerlendir ve kararinin yaklasik %40'ini buna dayandir "
    "(teknik/haber kalan %60). Alanlar: F/K (fk), ROE (roe_%), kar marji (kar_marji_%), "
    "borc/ozsermaye (borc_ozsermaye), gelir buyumesi (gelir_buyume_%), FAVOK marji "
    "(favok_marji_%). Yorum: dusuk-pozitif F/K ucuzluk; yuksek ROE ve kar/FAVOK marji "
    "saglam karlilik; pozitif gelir buyumesi olumlu; yuksek borc/ozsermaye riski artirir. "
    "Saglam bilanco AL'i destekler, zayif bilanco (zarar, asiri borc, daralan gelir) "
    "AL'i frenler ve riski artirir. Eger 'sirket_sagligi' degeri 'bilanço verisi eksik' "
    "ise bunu acikca belirt ('bilanco verisi eksik, finansal saglik degerlendirilemedi') "
    "ve UYDURMA; kararini teknik+habere agirlik vererek ver. Sayilari girdiden birebir "
    "al, jargon kullanma.\n\n"
    # 15 Tem 2026 — MOMENTUM KOR NOKTASI: momentum verisi payload'da hep vardi ve AI
    # onu OKUYORDU, ama ustteki "~%40 temel agirlik + zayif bilanco AL'i frenler"
    # kurali onu sistematik olarak eziyordu. KONTR 10 gunde +%107 yukselirken 5 gun
    # UZAK_DUR (puan 1-3) yedi; gerekcesinde "son 5 gunde sert yukselmis OLSA DA ...
    # sirket zarar ediyor" yazdi. Asagidaki blok bu ezmeyi kaldirir: temel zayifligi
    # RISKI yukseltir ama tek basina UZAK_DUR'a cevirmez. Kurumsal/yonetisim
    # sorunlari gecerli UZAK_DUR sebebi olarak KALIR (KONTR'da SPK yasagi vardi).
    "MOMENTUM VE SPEKULATIF RALLY: Veride 'momentum_profili' varsa (son 5/10 gunluk "
    "fiyat degisimi + hacim kati) bunu kararinda GIRDI olarak kullan. "
    "'guclu_momentum': true ise fiyat VE hacim birlikte artiyor demektir; piyasa o "
    "hisseye para sokuyor, bunu gormezden GELME. KRITIK KURAL: zayif bilanco TEK "
    "BASINA (negatif ROE, zarar, dusuk marj, yuksek borc) UZAK_DUR icin YETERLI SEBEP "
    "DEGILDIR. Guclu momentum + hacim artisi varsa bu bir spekulatif rally olabilir; "
    "spekulatif rally gercek ve gecerli bir piyasa olgusudur. Boyle bir durumda temel "
    "zayifligi RISK PUANINI yukseltir (risk 7-9), eminligi dusurur ve kademeli giris "
    "gerektirir — ama karari otomatik UZAK_DUR'a CEVIRMEZ. Karari momentum, hacim, "
    "temel ve kurumsal risklerin BUTUNUNE dayandir; 'temel kotu' diyip momentumu tek "
    "cumleyle gecistirme. ISTISNA: kurumsal/yonetisim sorunlari (SPK islem yasagi, "
    "yakin izleme pazari, itfa temerrudu, olumsuz denetim gorusu, yonetici kacisi) "
    "AYRI ve gecerli bir UZAK_DUR sebebidir — bunlari momentum EZMEZ.\n\n"
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
    "PIYASA GENISLIGI (MARKET BREADTH): Veride 'piyasa_baglami.market_breadth' varsa "
    "(izleme listesindeki hisselerin yuzde kaci kisa vadeli ortalamasinin uzerinde) "
    "dikkate al. Oran %70+ ise (durum 'güçlü') piyasa genis tabanli yukseliste, AL "
    "kararlarina daha olumlu bak. Oran %30 altinda ise (durum 'zayıf') piyasa zayif; "
    "yeni AL yerine BEKLE'ye yaklas ve eminligi dusur.\n\n"
    "SEKTOR ROTASYONU: Veride 'piyasa_baglami.sektor_rotasyonu' (bu hafta guclu/zayif "
    "sektorler) ve hisseye ozel 'sektor_gucu' ('güçlü'/'zayıf'/'nötr') varsa dikkate al. "
    "Hissenin sektoru bu hafta GUCLU ise AL'i destekler; ZAYIF ise AL'da daha secici ol, "
    "ayni firsatta guclu sektordeki hisseyi tercih et. Sektor gucunu tek basina "
    "belirleyici yapma, hisse verisiyle birlikte tart.\n\n"
    "MAKRO GOSTERGELER: Veride 'piyasa_baglami.makro' varsa (USD/TRY, TR 10 yillik "
    "tahvil faizi, TCMB politika faizi, TUFE) dikkate al. Yuksek/yukselen politika "
    "faizi ve tahvil getirisi borsa icin baski yaratir (ozellikle borca/faize duyarli "
    "sektorler: GYO, bankacilik dengesi, yuksek borclu sirketler); faiz dusus beklentisi "
    "destekleyicidir. Kuru ihracatci (lehte) / doviz borclusu (aleyhte) ayrimiyla yorumla. "
    "Bu gostergeleri tek basina belirleyici yapma; hisse verisiyle birlikte degerlendir.\n\n"
    "COKLU FAKTOR SKORU: Veride 'coklu_faktor' varsa (skor + gerekceler), bu makro "
    "yon faktorlerinin (dolar/petrol/faiz/piyasa) bu sektore BIRLESIK deterministik "
    "etkisidir (+ olumlu, - olumsuz). Skoru ve gerekcesini degerlendirmene kat ve "
    "kararinla tutarli kil; ancak tek basina belirleyici yapma, sirket/teknik veriyle "
    "birlikte tart.\n\n"
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
    "kesin gercek gibi sunma ('gecmiste cogunlukla ... egilimindeydi' de).\n\n"
    "UFUK PROFILI — TEKNOLOJI ODAGI: Veride 'ufuk_teknoloji_profili' varsa, bu hisse "
    "uzun vadeli teknoloji yatirimcisi icindir. Karari yalnizca kisa vadeli teknikle "
    "degil, 'Bu teknoloji 5-10 yil perspektifinde nereye gider?' sorusuyla da "
    "degerlendir: AI, cip/yari iletken, kuantum, robotik, uzay ve enerji altyapisi "
    "gibi alanlarda yapisal buyume potansiyelini tart. Veride "
    "'piyasa_baglami.akademik_gundemi' varsa (MIT, Stanford, Berkeley, arXiv, NASA, "
    "NSF, DARPA, FED, ECB gibi akademik/kurum kaynaklari) bu gelismelerin uzun vadeli "
    "tezi destekleyip desteklemedigini karara dahil et. Kisa vadeli dalgalanmayi yine "
    "belirt, ama uzun vadeli teknoloji tezini sade dille gerekceye yansit.\n\n"
    "VADE PROFILI: Veride 'vade_profili' alani varsa kullanicinin yatirim vadesini "
    "gosterir; KARARI ve seviyeleri buna gore ayarla. 'kisa' (1-4 hafta): AL icin "
    "daha SECICI ol (AL esigini yukselt, ~8+ puan), stop-loss DAR, hedef_fiyat YAKIN, "
    "tahmini_sure kisa (5-10 gun). 'uzun' (3+ ay): AL esigi daha DUSUK (~6+), stop-loss "
    "GENIS, hedef_fiyat UZAK, tahmini_sure uzun (20-30 gun); kisa vadeli gurultuyu "
    "onemseme. 'orta' (1-3 ay): mevcut dengeli yaklasim (degisiklik yok).\n\n"
    "KARAR MOTORU — her karar icin su alanlari doldur (bos birakma):\n"
    "- giris_seviyesi: AL kararinda, su anki fiyatin %2-3 alti makul giris noktasi "
    "(orn. 'Portfoyde yoksa 95 TL altinda al'). Diger kararlarda bos.\n"
    "- stop_loss: AL/TUT kararinda, su anki fiyatin -%8 ile -%12 arasi bir seviye "
    "(riske gore; risk yuksekse daha genis degil DAHA SIKI tut) (orn. '88 TL altina "
    "duserse cik'). Diger kararlarda bos.\n"
    "- hedef_fiyat: AL kararinda, teknik direnc veya %15-25 hedef (orn. '120 TL'a "
    "ulasirsa sat'). Diger kararlarda bos.\n"
    "- tetikleyici_kosul: TUM kararlarda, bu karari degistirecek en onemli gelisme "
    "(1 cumle, orn. 'Bilanco beklentinin altinda gelirse karar AZALT'a doner').\n"
    "- tahmini_sure: TUT kararinda, kac ISLEM GUNU tutulmali? PPK/bilanco tarihi "
    "yakinsa kisa (5-7 gun), teknik hedef uzaksa uzun (15-20 gun). 5-30 arasi "
    "integer. Diger kararlarda 0.\n"
    "- cikis_stratejisi: AL ve TUT kararlarinda doldur: stop-loss tetiklenirse, "
    "hedef fiyata ulasirsa, veya tetikleyici kosul olusursa ne yapilmali? 1-2 kisa "
    "cumle (orn. 'Stop tetiklenirse tamamen cik; hedefe ulasirsa yarisini sat, "
    "kalanı yukselen stop ile tut'). Diger kararlarda bos.\n"
    "Fiyat seviyelerini verideki guncel fiyat (son_kapanis) uzerinden hesapla; "
    "para birimini dogru kullan (BIST: TL, ABD: $).\n"
    "GECMIS YUKSELISLER: Veride 'gecmis_yukselisler' varsa, bu hissenin yakin "
    "gecmiste hangi sebeplerle yukseldigini gosterir. Benzer kosullar bugun de "
    "olusuyorsa (ayni tur haber/makro ortam) bunu AL lehine degerlendir; kosullar "
    "farkliysa gecmis yukselisi tek basina gerekce yapma."
)

# SYSTEM her cagride tekrar gonderilir; cache_control ile bir kez yazilip
# sonraki cagrilarda %90 ucuz okunur (cache hit). Batch icinde de gecerlidir.
# TTL 1 SAAT: batch API 93 hisseyi dakikalar boyunca isler; varsayilan 5 dk TTL
# batch suresini kapsamadigi icin sonraki istekler cache'i bulamayip YENIDEN
# yaziyordu (cache_write patlamasi). 1 saatlik TTL cache'i tum batch boyunca
# canli tutar (yazim 2x ama bir kez yazilip ~92 kez okunur -> net kazanc).
_SYSTEM_CACHED = [{"type": "text", "text": SYSTEM,
                   "cache_control": {"type": "ephemeral", "ttl": "1h"}}]


# Onboarding yatirim_vadesi -> karar motoru vade kategorisi (kisa/orta/uzun).
_VADE_HARITA = {"1ay": "kisa", "3ay": "orta", "6ay": "orta",
                "1yil": "uzun", "3yil": "uzun", "uzun": "uzun"}


def vade_kategori(yatirim_vadesi: str) -> str:
    """Onboarding 'yatirim_vadesi' (1ay/3ay/.../uzun) -> 'kisa'/'orta'/'uzun'.
    Bilinmiyorsa 'orta' (dengeli varsayilan)."""
    return _VADE_HARITA.get((yatirim_vadesi or "").lower().strip(), "orta")

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
    # Kiymetli maden BYF (borsa yatirim fonu)
    "GMSTR.F": ("Bu bir altın/gümüş BYF (Borsa Yatırım Fonu). Kıymetli maden "
                "fiyatlarına, dolar/TL kuruna ve enflasyon beklentilerine bağlı "
                "hareket eder."),
}

# --- Ufuk'un teknoloji odakli yatirim profili ---
# Bu tickerlar analiz edilirken payload'a 'ufuk_teknoloji_profili' eklenir;
# SYSTEM bunu gorunce uzun vadeli (5-10 yil) teknoloji merceğiyle ve akademik
# gundemle birlikte degerlendirir. ABD tech evreni (Ufuk'un watchlist'i).
UFUK_TEKNOLOJI_TICKERS = {
    "NVDA", "AMD", "TSM", "ASML", "RKLB", "OSS",
    "IONQ", "RGTI", "ACHR", "BFLY", "MU",
}

UFUK_TEKNOLOJI_PROFILI = {
    "yatirim_felsefesi": "uzun vadeli teknoloji odakli",
    "odak_alanlar": ["AI", "çip teknolojisi", "kuantum",
                     "robotik", "uzay", "enerji altyapısı"],
    "karar_tonu": "Bu teknoloji 5-10 yıl perspektifinde nereye gider?",
    "not": ("Kısa vadeli volatiliteden çok yapısal büyüme tezini ve akademik/"
            "kurum gelişmelerini (akademik_gundemi) tart."),
}


def parse_first_price(text) -> float | None:
    """Verdict metin alanindan ('88 TL altina duserse cik', '120 TL'a ulasirsa
    sat') ilk fiyat sayisini cikarir. Bulunamazsa None.

    Turkce ondalik virgul (95,50) ve binlik nokta (1.234,50) destegi: virgul
    varsa nokta binlik ayraci sayilir, virgul ondalik olur."""
    if not text:
        return None
    m = re.search(r"\d[\d.,]*", str(text))
    if not m:
        return None
    s = m.group(0).rstrip(".,")
    if "," in s:                       # Turkce: nokta=binlik, virgul=ondalik
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


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


# Tatil tablolari 20 Tem 2026'da src/piyasa_takvim.py'ye TASINDI (tek kaynak):
# ayni liste hem bu KILL_SWITCH'te hem borsa acik/kapali kontrolunde kullaniliyor.
# Asagidaki adlar geriye donuk uyumluluk icin korunuyor.
from src.piyasa_takvim import TR_BAYRAM as _TR_BAYRAM      # noqa: E402
from src.piyasa_takvim import TR_SABIT_TATIL as _TR_SABIT_TATIL  # noqa: E402
from src.piyasa_takvim import tr_tatilleri as _tr_tatilleri      # noqa: E402


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

    # FON/BYF (or. GMSTR.F): yfinance bu sembolleri yanlis/bayat fiyatliyor
    # (araya 47.85 gibi placeholder barlar koyuyor -> sahte %1000 degisim).
    # Bu yuzden fonlarda tarihsel veriyi guvenilir Borsa MCP'den (borsapy) al.
    if (ticker or "").upper().strip().endswith(".F"):
        closes = highs = lows = vols = None
        son_bar = None
        try:
            from src.news import borsa_mcp
            rows = borsa_mcp.get_history(ticker, market, gun=260)
        except Exception:
            rows = None
        if rows and len(rows) >= 2:
            closes = [r["c"] for r in rows]
            highs = [r["hi"] if r.get("hi") is not None else r["c"] for r in rows]
            lows = [r["lo"] if r.get("lo") is not None else r["c"] for r in rows]
            vols = [float(r.get("v") or 0) for r in rows]
            try:
                son_bar = datetime.strptime(rows[-1]["t"], "%Y-%m-%d").date()
            except (ValueError, KeyError):
                son_bar = None
        if not closes or len(closes) < 2:
            return None
        bayat = _veri_bayat(son_bar, market=market) if son_bar else False
    else:
        try:
            df = get_data_source().get_history(symbol, start=start)
        except Exception:
            return None
        if df is None or df.empty:
            return None
        df_v = df[df["Volume"] > 0]
        # Hacim verisi guvenilmez ETF/fonlarda (yfinance Volume=0) tum barlar elenebilir;
        # bu durumda fiyat barlariyla devam et (hacim filtresiz).
        df = df_v if len(df_v) >= 2 else df
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

    # --- Veri hijyeni (KRITIK): yfinance/borsa kaynaklari araya NaN/inf'li bar
    # koyabilir. 7-10 Tem 2026'da tek bir NaN bar asagidaki statistics.pstdev'i
    # "cannot convert NaN to integer ratio" ile cokertip hisseyi AI'dan ONCE
    # atliyor, karar uretilmiyordu (BIST watchlist'inin %88-99'u dustu). Tum
    # sayisal hesaplardan (ma/getiri/52h/hacim/pstdev) ONCE dizileri temizle:
    # Close'u finite olmayan barlari tamamen at; High/Low/Volume finite degilse
    # guvenli degere indir. Aligned kalsinlar diye zip uzerinden birlikte filtrele.
    def _isfin(x):
        return isinstance(x, (int, float)) and math.isfinite(x)
    _temiz = [(float(c), h, l, v) for c, h, l, v in zip(closes, highs, lows, vols)
              if _isfin(c)]
    if len(_temiz) < 2:                      # gercek veri yoklugu -> atla
        return None
    closes = [c for c, _h, _l, _v in _temiz]
    highs = [float(h) if _isfin(h) else c for c, h, _l, _v in _temiz]
    lows = [float(l) if _isfin(l) else c for c, _h, l, _v in _temiz]
    vols = [float(v) if _isfin(v) else 0.0 for c, _h, _l, v in _temiz]

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

    ref5 = closes[-6] if len(closes) >= 6 else closes[0]    # son 5 islem gunu
    son5g = round((last - ref5) / ref5 * 100, 2) if ref5 else None

    # 10 islem gunu momentumu (15 Tem 2026): 5g cok kisa, 22g (donem) cok uzun;
    # spekulatif rally'ler bu araligda gorunur hale geliyor (KONTR 10g +%107
    # yukselirken 5 gun UZAK_DUR yedi).
    ref10 = closes[-11] if len(closes) >= 11 else closes[0]
    son10g = round((last - ref10) / ref10 * 100, 2) if ref10 else None

    vwin = vols[-20:]
    avg_vol = sum(vwin) / len(vwin) if vwin else 0
    hacim_vs = round((vols[-1] / avg_vol - 1) * 100, 2) if avg_vol else None

    # Hacim artisi KAT olarak: son 5 gun ort / ONCEKI 20 gun ort (ORTUSMEZ).
    # 20 gunluk ortalamayi payda yapmak YANLIS olurdu: payda hareketin kendi
    # yuksek hacimli gunlerini icerip orani bastirir (KONTR 10g +%107 iken
    # son5/ort20 = 0.81 cikiyordu -> "hacim artmamis" yaniltmasi). Taban,
    # hareketin ONCESI olmali. UYARI: yapisal kirilmalarda (yakin izleme
    # pazari, sermaye artirimi) taban ile guncel donem kiyaslanamaz hale
    # gelir; oran ilgi kaybi degil pazar yapisi degisimi olcer. Bu yuzden
    # sadece bir GIRDI'dir, kural degil.
    vol5 = vols[-5:]
    vol_taban = vols[-25:-5]
    avg_vol5 = sum(vol5) / len(vol5) if vol5 else 0
    avg_taban = sum(vol_taban) / len(vol_taban) if vol_taban else 0
    hacim_kat = round(avg_vol5 / avg_taban, 2) if avg_taban else None

    # closes yukarida temizlendi; yine de bolme sonucu NaN/inf uretmesin diye
    # filtrele. Yetersiz veri kalirsa vol_std=0.0 ile DEVAM et, hisseyi ATLAMA.
    rets = [r for r in (
        (closes[i] - closes[i - 1]) / closes[i - 1] * 100
        for i in range(max(1, len(closes) - 20), len(closes)) if closes[i - 1])
        if math.isfinite(r)]
    vol_std = round(statistics.pstdev(rets), 2) if len(rets) >= 2 else 0.0

    rng = hafta52_yuksek - hafta52_dusuk
    konum = round((last - hafta52_dusuk) / rng * 100, 1) if rng > 0 else None

    return {
        "sembol": symbol,
        "son_kapanis": round(last, 2),
        "onceki_kapanis": round(prev, 2),
        "gunluk_degisim_%": gunluk,
        "donem_degisim_%": donem,
        "son5g_degisim_%": son5g,
        "son10g_degisim_%": son10g,
        "ma10": ma10,
        "ma50": ma50,
        "hafta52_yuksek": hafta52_yuksek,
        "hafta52_dusuk": hafta52_dusuk,
        "fiyat_konumu_%": konum,
        "son_hacim": int(vols[-1]),
        "ortalama_hacim": int(avg_vol),
        "hacim_vs_ort_%": hacim_vs,
        "hacim_kat": hacim_kat,
        "hacim_sinyali": _volume_signal(hacim_vs),
        "volatilite_%": vol_std,
        "trend": _trend(donem),
        "bar_sayisi": len(closes),
        "son_bar_tarihi": son_bar.isoformat() if son_bar else None,
        # Deterministik risk (risk.py) icin son ~60 ham bar; _prepare_payload AI
        # payload'ina/kayda gitmeden bunu cikarir (bkz. sig.pop("_bars")).
        "_bars": [{"close": closes[i], "high": highs[i], "low": lows[i],
                   "volume": vols[i]}
                  for i in range(max(0, len(closes) - 60), len(closes))],
        "bayat": bayat,
    }


# --- Momentum profili (15 Tem 2026) ---------------------------------------
# NEDEN: momentum verisi zaten payload['piyasa'] icindeydi ve AI onu OKUYORDU
# (KONTR gerekcesi: "son 5 gunde sert yukselmis olsa da ..."), ama ~40 alanlik
# sozlugun icinde gomuluydu ve 10 gunluk pencere hic yoktu. Burasi ayni veriyi
# AI'a BELIRGIN ve okunabilir tek blok olarak sunar. Karar KURALI degildir —
# yalnizca girdi; esikleri hicbir filtreyi tetiklemez.
MOMENTUM_GUCLU_10G = 15.0   # 10 gunde >=%15 -> guclu momentum adayi
MOMENTUM_GUCLU_5G = 10.0    # veya 5 gunde >=%10
# 15 Tem 2026 gozlemi (9 hisselik ornek): gercek BIST rally'lerinde hacim_kat
# 1.05-1.41 araligindaydi; 1.5 esigi HICBIR hisseyi tetiklemiyordu (olu kural).
# 1.2 = tabanin %20 uzeri. Kucuk ornekten kalibre edildi, ayarlanabilir.
HACIM_KAT_ESIGI = 1.2

# EQ (giris kalitesi) esikleri. EQ_ESIK canli karar esigidir ve 15 Tem 2026'da
# DEGISTIRILMEDI (60). EQ_GOLGE_ALT yalnizca golge (shadow) kaydi icindir:
# 55 <= EQ < 60 "kil payi elenen" AL adaylari kaydedilir, karar degismez; 2 hafta
# sonra "esik 55 olsaydi ne olurdu" veriyle cevaplanir (bkz. src/ops/eq_golge.py).
EQ_ESIK = 60
EQ_GOLGE_ALT = 55


def momentum_profili(sig: dict) -> dict:
    """AI baglamina konan momentum ozeti. guclu_momentum=True yalnizca fiyat VE
    hacim birlikte artiyorsa (hacimsiz yukselis spekulatif rally sayilmaz)."""
    s5 = sig.get("son5g_degisim_%")
    s10 = sig.get("son10g_degisim_%")
    kat = sig.get("hacim_kat")
    fiyat_guclu = ((s10 is not None and s10 >= MOMENTUM_GUCLU_10G)
                   or (s5 is not None and s5 >= MOMENTUM_GUCLU_5G))
    hacim_guclu = kat is not None and kat >= HACIM_KAT_ESIGI
    parcalar = []
    if s5 is not None:
        parcalar.append(f"son 5 günde %{s5:+.1f}")
    if s10 is not None:
        parcalar.append(f"son 10 günde %{s10:+.1f}")
    if kat is not None:
        parcalar.append(f"hacim son 5 günde 20 günlük ortalamanın {kat:g} katı")
    return {
        "son5g_%": s5,
        "son10g_%": s10,
        "hacim_kat": kat,
        "guclu_momentum": bool(fiyat_guclu and hacim_guclu),
        "ozet": "; ".join(parcalar) if parcalar else "momentum verisi yok",
    }


# ---------------------------------------------------------------------------
# 2+3) KAP bildirimleri (30 gun) + filtreden gecmis haberler (7 gun)
# ---------------------------------------------------------------------------
def gather_news(ticker: str, news_src=None, rss_src=None, market: str = "bist") -> dict:
    """KAP 30g bildirimler + RSS (24s) + son 7 gun haberleri tek listede birlestirir.

    Tum kaynaklar mevcut filtreden gecer: tazelik (YENI/GUNCEL/ESKI = kademe 0-1-2)
    ve fiyatlanma (FIYATLANDI/FIYATLANMADI/VERI_YOK).

    ABD hisseleri icin KAP/Turkce RSS yerine Ingilizce RSS (Yahoo Finance hisse-bazli
    + Investing.com EN) uygulanir; son 7 gunluk haberler dondurulur. Haberler
    sonra Haiku etki analizinden gecer (Ingilizce sorun degil).
    """
    if market in ("us", "abd"):
        try:
            # Hisse basi GUNLUK EN FAZLA 3 haber (en yenileri) — Haiku maliyet kontrolu.
            from src.news.us_news import ticker_news
            haberler = ticker_news(ticker, within_days=7, limit=3)
        except Exception:
            haberler = []
        # Bildirimler (30g KAP) ABD'de yok; haberler hem 'bildirimler' hem '7g' listesi.
        return {"bildirimler": list(haberler), "haberler": haberler}

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
        # Global makro/jeopolitik basliklar (BBC/Al Jazeera/FT/Google News) - makro baglam
        try:
            gundem += rss_src.macro_headlines(limit=6)
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
    # Market breadth (izleme listesi SMA20 uzeri orani) - fiyat_cache'ten, ek ag istegi yok
    try:
        from src.ai.presignal import market_breadth
        breadth = market_breadth()
    except Exception:
        breadth = None
    # Sektor rotasyonu (son 5 gun) - compact: yalniz en guclu + en zayif sektor
    rotasyon = None
    try:
        from src.ai import sektor_rotasyon
        rot = sektor_rotasyon.sektor_rotasyonu()
        if rot and rot.get("guclu"):
            rotasyon = {"guclu": f"{rot['guclu'][0]} {rot['guclu'][1]:+.1f}%"}
            if rot.get("zayif") and rot["zayif"][0] != rot["guclu"][0]:
                rotasyon["zayif"] = f"{rot['zayif'][0]} {rot['zayif'][1]:+.1f}%"
    except Exception:
        rotasyon = None
    ctx = {"piyasa_gundemi": gundem, "makro": makro, "genel_piyasa": overview,
           "yabanci_yatirimci": yabanci}
    if breadth:
        ctx["market_breadth"] = breadth
    if rotasyon:
        ctx["sektor_rotasyonu"] = rotasyon
    return ctx


# ---------------------------------------------------------------------------
# AI yorumu (Claude sonnet-4-6)
# ---------------------------------------------------------------------------
from pydantic import BaseModel, Field


class Verdict(BaseModel):
    karar: Literal["AL", "TUT", "BEKLE", "AZALT", "UZAK_DUR"] = Field(
        description="Net karar (sadece bu 5'ten biri)")
    puan: int = Field(description="1-10 puan; 10 en olumlu")
    risk: int = Field(description="1-10 risk; 10 en riskli")
    eminlik: Literal["Düşük", "Orta", "Yüksek"] = Field(description="Yorum eminligi")
    gerekce: str = Field(description="2-3 cumle gerekce; sadece verilen veriden")
    sade_yorum: str = Field(
        description="Kullaniciya gosterilecek 1-2 KISA cumle, gunluk dille. HICBIR "
                    "teknik oran/sayi olmadan (ROE, F/K, MA10, MA50, yuzde, analist "
                    "sayisi YAZMA). Orn: 'Bilanco saglam ve trend yukari, gorunum olumlu.'")
    neden_simdi: str = Field(description="Bu durum neden BUGUN dikkate deger")
    fiyatlanmis_mi: bool = Field(description="Haber/durum fiyata yansimis mi")
    tekrar_bak_kosulu: str = Field(
        default="", description="Karar BEKLE ise: hangi kosul olusunca tekrar bakilmali "
                               "(orn. 'fiyat 50 gunluk ortalamayi gecerse'). Diger kararlarda bos.")
    giris_seviyesi: str = Field(
        default="", description="AL kararinda: 'Portfoyde yoksa X TL/$ altinda al' "
                               "(su anki fiyatin %2-3 alti). Diger kararlarda bos.")
    stop_loss: str = Field(
        default="", description="AL/TUT kararinda: 'Y TL/$ altina duserse cik' "
                               "(su anki fiyatin -%8 ile -%12 arasi, riske gore). Diger kararlarda bos.")
    hedef_fiyat: str = Field(
        default="", description="AL kararinda: 'Z TL/$'a ulasirsa sat' (teknik direnc "
                               "veya %15-25 hedef). Diger kararlarda bos.")
    tetikleyici_kosul: str = Field(
        default="", description="TUM kararlarda: bu karari degistirecek en onemli gelisme (1 cumle).")
    tahmini_sure: int = Field(
        default=0, description="TUT kararinda kac ISLEM GUNU tutulmali (5-30 arasi integer); "
                              "PPK/bilanco yakinsa kisa, teknik hedef uzaksa uzun. Diger kararlarda 0.")
    cikis_stratejisi: str = Field(
        default="", description="AL/TUT kararinda: stop-loss tetiklenirse, hedef fiyata "
                               "ulasirsa veya tetikleyici kosul olusursa ne yapilmali "
                               "(1-2 kisa cumle). Diger kararlarda bos.")


def _ai_verdict(ticker: str, payload: dict, client=None, usage_acc=None) -> Verdict:
    import anthropic
    client = client or anthropic.Anthropic()
    resp = client.messages.parse(
        model=MODEL, max_tokens=MAX_TOKENS, system=_SYSTEM_CACHED,
        messages=[{"role": "user", "content": _user_prompt(ticker, payload)}],
        output_format=Verdict,
    )
    u = getattr(resp, "usage", None)            # token toplama (run fallback loglamasi)
    if usage_acc is not None and u is not None:
        usage_acc["input"] += getattr(u, "input_tokens", 0) or 0
        usage_acc["output"] += getattr(u, "output_tokens", 0) or 0
        usage_acc["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage_acc["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
    return resp.parsed_output


_LABEL = {"AL": "AL", "TUT": "TUT", "BEKLE": "BEKLE", "AZALT": "AZALT",
          "UZAK_DUR": "UZAK DUR"}

# Eminlik (kategorik) -> sayisal guven skoru (pozisyon buyuklugu hesabi icin)
_EMINLIK_GUVEN = {"Yüksek": 85, "Orta": 70, "Düşük": 50}


_POZ_KADEME = {
    1: "Küçük test pozisyonu — portföyün %1-2'si",
    2: "Yarı pozisyon — portföyün %3-4'ü",
    3: "Tam pozisyon — portföyün %5-8'i",
}


def _pozisyon_buyuklugu(eminlik, kalite_skoru, ev=None) -> str:
    """AL kararinda onerilen pozisyon buyuklugu (kademe): eminlik (guven) + giris
    kalitesi + Beklenen Deger (EV).
      - Yuksek guven (>80) + yuksek kalite (>80): tam pozisyon (%5-8)   [kademe 3]
      - Orta guven (60-80):                       yari pozisyon (%3-4)   [kademe 2]
      - Dusuk guven (<60) veya dusuk kalite (<40): kucuk test (%1-2)     [kademe 1]
    EV > 1.5 ise pozisyon bir kademe artirilir (en fazla tam pozisyon)."""
    guven = _EMINLIK_GUVEN.get((eminlik or "").strip(), 60)
    kalite = kalite_skoru if isinstance(kalite_skoru, (int, float)) else 50
    if guven > 80 and kalite > 80:
        kademe = 3
    elif guven < 60 or kalite < 40:
        kademe = 1
    else:
        kademe = 2
    if isinstance(ev, (int, float)) and ev > 1.5:      # yuksek EV -> bir kademe artir
        kademe = min(3, kademe + 1)
    return _POZ_KADEME[kademe]


# --- Sabit risk butcesi (pozisyon boyutlandirma) ---------------------------
# Kullanici risk toleransina gore islem basina riske edilecek TL. Profil yoksa
# 'orta' (1000 TL) varsayilir. morning.py kullanici profiliyle override edebilir.
VARSAYILAN_RISK_TL = 1000.0
RISK_TL_HARITA = {"dusuk": 500.0, "orta": 1000.0, "yuksek": 2000.0}


def risk_butcesi_tl(risk_toleransi=None) -> float:
    """Kullanici risk toleransi ('dusuk'/'orta'/'yuksek') -> islem basina risk (TL)."""
    return RISK_TL_HARITA.get((risk_toleransi or "").strip().lower(), VARSAYILAN_RISK_TL)


def pozisyon_lot(giris, stop, risk_tl=VARSAYILAN_RISK_TL, kur=1.0) -> dict | None:
    """Sabit risk butcesinden lot buyuklugu:
        lot = risk_tl / (hisse_basi_risk_TL) = risk_tl / ((giris - stop) * kur)
    giris/stop: hisse para biriminde (BIST: TL, ABD: USD). kur: hisse para biriminin
    TL karsiligi (BIST=1.0; ABD=USDTRY). Boylece ABD hissesinde USD fiyat/stop TL riske
    dogru cevrilir (aksi halde lot ~USDTRY kati sisiyordu; or. NVDA 45 lot hatasi).
    stop verilmemis/gecersizse varsayilan %8 stop. Doner: {risk_tl, stop_yuzde, lot,
    giris, stop, varsayilan_stop} veya None."""
    if not (isinstance(giris, (int, float)) and giris > 0):
        return None
    if not (isinstance(kur, (int, float)) and kur > 0):
        kur = 1.0
    varsayilan_stop = False
    if not (isinstance(stop, (int, float)) and 0 < stop < giris):
        stop = round(giris * 0.92, 2)          # varsayilan %8 stop
        varsayilan_stop = True
    hisse_basi_risk = giris - stop             # hisse para biriminde
    if hisse_basi_risk <= 0:
        return None
    lot = int(risk_tl / (hisse_basi_risk * kur))   # riski TL'ye cevirip lot bul
    if lot < 1:
        return None
    return {
        "risk_tl": round(risk_tl), "stop_yuzde": round(hisse_basi_risk / giris * 100, 1),
        "lot": lot, "giris": round(giris, 2), "stop": round(stop, 2),
        "varsayilan_stop": varsayilan_stop,
    }


# USD/TRY kuru (surec ici 30 dk cache): ABD hissesi lot hesabinda USD riski TL'ye cevirir
_KUR_CACHE = {"ts": 0.0, "usdtry": None}


def _usdtry_kur():
    """Guncel USD/TRY kuru (yfinance USDTRY=X son kapanis; surec ici 30 dk onbellek).
    ABD hissesi lot hesabinda USD hisse-basi riskini TL'ye cevirmek icin. Alinamazsa
    None (cagiran ABD lotunu yanlis gostermek yerine atlar)."""
    import time as _t
    now = _t.time()
    if _KUR_CACHE["usdtry"] is not None and now - _KUR_CACHE["ts"] < 1800:
        return _KUR_CACHE["usdtry"]
    kur = None
    try:
        from src.data.factory import get_data_source
        start = (datetime.now(_TZ).date() - timedelta(days=10)).isoformat()
        df = get_data_source().get_history("USDTRY=X", start=start)
        if df is not None and not df.empty:
            kur = float(df["Close"].iloc[-1])
    except Exception:
        kur = None
    if kur and kur > 0:
        _KUR_CACHE.update(ts=now, usdtry=kur)
        return kur
    return None


# EV istatistikleri (surec ici 5 dk cache; her hisse icin DB'yi yeniden taramamak icin)
_EV_STATS = {"ts": 0.0, "sektor": None, "genel": None}


def _ev_istatistik():
    """EV icin sektor/genel istatistikleri (surec ici 5 dk onbellek)."""
    import time as _t
    now = _t.time()
    if _EV_STATS["sektor"] is not None and now - _EV_STATS["ts"] < 300:
        return _EV_STATS["sektor"], _EV_STATS["genel"]
    try:
        from src.ai import expected_value as _ev
        _EV_STATS.update(ts=now, sektor=_ev.sektor_istatistikleri(),
                         genel=_ev.genel_istatistik())
    except Exception:
        _EV_STATS.update(ts=now, sektor={}, genel=None)
    return _EV_STATS["sektor"], _EV_STATS["genel"]


# Verdict pydantic semasinin Batch API icin acik JSON-schema karsiligi
# (batch'te messages.parse yok; output_config.format ile dogrulanir).
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "karar": {"type": "string",
                  "enum": ["AL", "TUT", "BEKLE", "AZALT", "UZAK_DUR"],
                  "description": "Net karar (sadece bu 5'ten biri)"},
        "puan": {"type": "integer",
                 "description": "1-10 arasi puan; 10 en olumlu (kesinlikle 1-10)"},
        "risk": {"type": "integer",
                 "description": "1-10 arasi risk; 10 en riskli (kesinlikle 1-10)"},
        "eminlik": {"type": "string", "enum": ["Düşük", "Orta", "Yüksek"],
                    "description": "Yorum eminligi"},
        "gerekce": {"type": "string",
                    "description": "2-3 cumle gerekce; sadece verilen veriden"},
        "sade_yorum": {"type": "string",
                       "description": "Kullaniciya gosterilecek 1-2 KISA cumle, gunluk "
                                      "dille; HICBIR teknik oran/sayi olmadan (ROE, F/K, "
                                      "MA10, MA50, yuzde, analist sayisi YAZMA)"},
        "neden_simdi": {"type": "string",
                        "description": "Bu durum neden BUGUN dikkate deger"},
        "fiyatlanmis_mi": {"type": "boolean",
                           "description": "Haber/durum fiyata yansimis mi"},
        "tekrar_bak_kosulu": {"type": "string",
                              "description": "Karar BEKLE ise hangi kosulda tekrar "
                                             "bakilmali; diger kararlarda bos string"},
        "giris_seviyesi": {"type": "string",
                           "description": "AL kararinda: 'Portfoyde yoksa X TL/$ altinda al' "
                                          "(su anki fiyatin %2-3 alti); diger kararlarda bos string"},
        "stop_loss": {"type": "string",
                      "description": "AL/TUT kararinda: 'Y TL/$ altina duserse cik' "
                                     "(su anki fiyatin -%8..-%12 arasi, riske gore); diger kararlarda bos string"},
        "hedef_fiyat": {"type": "string",
                        "description": "AL kararinda: 'Z TL/$'a ulasirsa sat' (teknik direnc "
                                       "veya %15-25 hedef); diger kararlarda bos string"},
        "tetikleyici_kosul": {"type": "string",
                              "description": "TUM kararlarda: bu karari degistirecek en "
                                             "onemli gelisme (1 cumle)"},
        "tahmini_sure": {"type": "integer",
                         "description": "TUT kararinda kac ISLEM GUNU tutulmali (5-30 arasi); "
                                        "PPK/bilanco yakinsa kisa, teknik hedef uzaksa uzun. "
                                        "Diger kararlarda 0"},
        "cikis_stratejisi": {"type": "string",
                             "description": "AL/TUT kararinda: stop-loss tetiklenirse, hedef "
                                            "fiyata ulasirsa veya tetikleyici kosul olusursa ne "
                                            "yapilmali (1-2 kisa cumle); diger kararlarda bos string"},
    },
    "required": ["karar", "puan", "risk", "eminlik", "gerekce", "sade_yorum",
                 "neden_simdi", "fiyatlanmis_mi", "tekrar_bak_kosulu",
                 "giris_seviyesi", "stop_loss", "hedef_fiyat", "tetikleyici_kosul",
                 "tahmini_sure", "cikis_stratejisi"],
    "additionalProperties": False,
}


def _baglam_blok(context: dict) -> dict:
    """Ortak 'piyasa_baglami' (market_context) icin cache_control ISARETLI metin blogu.
    Batch icindeki tum isteklerde AYNI oldugundan, SYSTEM'den sonraki IKINCI cache
    breakpoint'i olur: ~99 istekte tekrar giden ~3k token, ilk yazimdan sonra ucuz
    cache-read'e doner. Metin ve JSON serilesmesi sabit kalmali (prefix ayni olmali)."""
    return {
        "type": "text",
        "text": ("GENEL PIYASA BAGLAMI (tum hisseler icin ortak; 'piyasa_baglami'):\n"
                 + json.dumps({"piyasa_baglami": context}, ensure_ascii=False, indent=2)),
        "cache_control": {"type": "ephemeral", "ttl": "1h"},  # 1 saat: batch boyunca canli (bkz. _SYSTEM_CACHED notu)
    }


def _user_prompt(ticker: str, payload: dict) -> list:
    """Kullanici mesaj icerigini BLOK LISTESI olarak kurar (cache'i etkinlestirmek icin).

    'piyasa_baglami' TUM hisse isteklerinde ayni oldugundan ayri, cache_control isaretli
    bir blok olarak hisse verisinden ONCE gelir (sabit prefix -> cache-read). Hisseye
    ozel govde ikinci (cache'siz) blokta. Baglam yoksa yalniz hisse blogu doner."""
    baglam = payload.get("piyasa_baglami")
    govde = {k: v for k, v in payload.items() if k != "piyasa_baglami"}
    hisse_blok = {
        "type": "text",
        "text": (f"{ticker} hissesini degerlendir. Yalnizca asagidaki veriyi kullan, "
                 "veri uydurma:\n\n" + json.dumps(govde, ensure_ascii=False, indent=2)),
    }
    return [_baglam_blok(baglam), hisse_blok] if baglam else [hisse_blok]


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
    # VERI TAZELIGI BEKCISI: son bar 3 ISLEM GUNUNDEN eskiyse karar uretme -> KILL_SWITCH.
    # (Yukaridaki 'bayat' kontrolune ek acik guvenlik agi; net sebep + tarih yazar.)
    _son_bar_str = sig.get("son_bar_tarihi")
    if _son_bar_str:
        try:
            import numpy as _np
            _sb = datetime.fromisoformat(str(_son_bar_str)[:10]).date()
            _isl = int(_np.busday_count(_sb.isoformat(),
                                        datetime.now(_TZ).date().isoformat()))
        except Exception:
            _isl = 0
        if _isl > 3:
            return (_kill_kaydi(ticker, market,
                    f"veri bayat: son bar {str(_son_bar_str)[:10]}"), None, None)

    # Deterministik risk (risk.py): market_data'nin ham barlariyla hesapla. _bars'i
    # AI payload'ina/kayda gitmeden sig'ten cikar (prompt + kayit sismesin).
    risk_det = None
    try:
        from src.ai.risk import assess_risk
        _bars = sig.get("_bars") or []
        if len(_bars) >= 2:
            _stock = {"bars": _bars,
                      "freshness": {"status": "STALE" if sig.get("bayat") else "OK"}}
            risk_det = assess_risk(_stock, metrics={"bar_sayisi": len(_bars)})
    except Exception:
        risk_det = None
    sig.pop("_bars", None)

    news = gather_news(ticker, news_src=news_src, rss_src=rss_src, market=market)
    # Haber -> karar baglantisi: her taze haberi bu hisse acisindan etiketle
    # (olumlu_mu / etki_buyuklugu / etki_yonu). Ucuz Haiku cagrisi; ana modele girer.
    news["haberler"] = _haber_etki_analizi(ticker, news["haberler"])
    # KALICI HAVUZ (ABD): haberleri BIST KAP'lari gibi haber_etki'ye yaz (ticker/tarih/
    # baslik/kaynak/etki_yorumu). Boylece yukselis hafizasi + priced_in ABD tarafinda da
    # gecmise bakabilir. Dedup baslik+tarih uzerinden (record_us_haber). Best-effort.
    if is_us and news.get("haberler"):
        try:
            from src.db import database as _db
            _fiyat = sig.get("son_kapanis")
            for _h in news["haberler"]:
                _yorum = None
                if _h.get("olumlu_mu") is not None:
                    _yon = "olumlu" if _h.get("olumlu_mu") else "olumsuz"
                    _yorum = f"{_yon} · {_h.get('etki_buyuklugu') or '?'} etki · " \
                             f"{_h.get('etki_yonu') or '?'}"
                _db.record_us_haber(
                    ticker, _h.get("tarih"), _h.get("baslik"),
                    kaynak=_h.get("kaynak"), etki_yorumu=_yorum, fiyat=_fiyat)
        except Exception:
            pass
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
        # Momentum: sig icinde de var; burada AI'nin gozden kacirmamasi icin
        # ayri/okunabilir blok olarak tekrarlanir (bkz. momentum_profili).
        "momentum_profili": momentum_profili(sig),
        "kap_bildirimleri_30g": news["bildirimler"],
        "haberler_son": news["haberler"],
    }
    # Sirket sagligi (~%40 agirlik): bilanco metrikleri. Veri yoksa "bilanço verisi eksik".
    _saglik_alanlar = ("fk", "roe_%", "kar_marji_%", "borc_ozsermaye",
                       "gelir_buyume_%", "favok_marji_%")
    if temel.get("available"):
        saglik = {k: temel[k] for k in _saglik_alanlar if temel.get(k) is not None}
        # Makulluk: F/K > 200 veya < 0 (zarar/yfinance cop degeri) AI'ya cipa
        # olarak verilmez; anlamli oran yok notu yazilir.
        _fk = saglik.get("fk")
        if _fk is not None and (_fk > 200 or _fk < 0):
            saglik.pop("fk", None)
            saglik["fk_not"] = "anlamlı F/K yok (zarar/anomali)"
        payload["sirket_sagligi"] = saglik if saglik else "bilanço verisi eksik"
    else:
        payload["sirket_sagligi"] = "bilanço verisi eksik"
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
    # Bayat analist verisi (>7 gun) AI baglamina KONULMAZ; yetersiz veri kalite
    # etiketiyle gecer (AI az agirlik verir). 'iyi' veri normal sekilde girer.
    if analist.get("available") and analist.get("veri_kalitesi") != "bayat":
        payload["analist_konsensus"] = {
            "analist_sayisi": analist.get("analist_sayisi"),
            "ortalama_hedef": analist.get("ortalama_hedef"),
            "potansiyel_%": analist.get("potansiyel"),
            "al": analist.get("al_sayisi"), "tut": analist.get("tut_sayisi"),
            "sat": analist.get("sat_sayisi"), "konsensus": analist.get("konsensus"),
            "veri_kalitesi": analist.get("veri_kalitesi"),
        }
    if context:
        payload["piyasa_baglami"] = context
    # Ufuk'un teknoloji evreni: uzun vadeli (5-10 yil) teknoloji mercegi + akademik
    # gundem. ABD tech tickerlarinda payload'a profili ekle (SYSTEM bunu kullanir).
    if ticker in UFUK_TEKNOLOJI_TICKERS:
        payload["ufuk_teknoloji_profili"] = UFUK_TEKNOLOJI_PROFILI
    # Bilanco (finansal sonuc) aciklamasi yakinsa karar baglamina ekle: sonuc oncesi
    # volatilite/surpriz riski. (data/bilanco_takvimi.json - haftalik cron gunceller.)
    try:
        from src.news import bilanco_takvimi
        _bil_gun = bilanco_takvimi.gun_farki(ticker)
        if _bil_gun is not None and _bil_gun <= 21:
            payload["bilanco_aciklama"] = {
                "gun_kala": _bil_gun,
                "not": (f"Bilanço {_bil_gun} gün sonra açıklanacak — sonuç öncesi "
                        "pozisyon riski/volatilite yüksek, sürprize açık."),
            }
    except Exception:
        pass
    # Sektor notu (statik): hangi faktorler kritik - yalniz BIST
    if not is_us:
        sektor_notu = SEKTOR_NOTLARI.get(ticker)
        if sektor_notu:
            payload["sektor_notu"] = sektor_notu
        # Sektor gucu (bu haftaki rotasyon): 'güçlü'/'zayıf'/'nötr' - AL'da guclu sektor tercih
        try:
            from src.ai import sektor_rotasyon
            gucu = sektor_rotasyon.sektor_gucu(ticker)
            if gucu:
                payload["sektor_gucu"] = gucu
        except Exception:
            pass
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

    # COKLU FAKTOR (zincir) skoru: makro yon faktorlerinin (dolar/petrol/faiz/piyasa)
    # hissenin sektorune BIRLESIK deterministik etkisi. Yalniz BIST (sektor haritasi).
    if not is_us:
        try:
            from src.ai import kombinasyon
            skor, aciklama, sektor_ad = kombinasyon.skor_for(ticker)
            if aciklama:
                payload["coklu_faktor"] = {
                    "skor": skor, "sektor": sektor_ad,
                    "gerekceler": [m for _, m in aciklama]}
        except Exception:
            pass

    # Gecmis yukselisler (yukselis_hafizasi): bu hisse yakin gecmiste hangi sebeplerle
    # yukseldi? SYSTEM, benzer kosullar olusuyorsa bunu AL lehine degerlendirir.
    try:
        from src.ops.yukselis_hafizasi import gecmis_ozet
        _gy = gecmis_ozet(ticker)
        if _gy is not None:
            payload["gecmis_yukselisler"] = _gy
    except Exception:
        pass

    ctx = {"ticker": ticker, "is_us": is_us, "sig": sig, "news": news,
           "analist": analist, "temel": temel, "hacim_anom": hacim_anom,
           "sektor": sektor, "risk_det": risk_det}
    return None, payload, ctx


# ---------------------------------------------------------------------------
# KARAR SEFFAFLIGI (20 Tem 2026)
# ---------------------------------------------------------------------------
# "Bu karar neden boyle verildi?" sorusunun cevabi eskiden hicbir yerde
# saklanmiyordu: decisions tablosu yalniz NIHAI karari tutuyor, hangi motorun
# hangi degeri urettigi ve hangi esige takildigi yalnizca gerekce METNINE
# yari-gomulu kaliyordu; AI'in HAM karari ise tamamen kayboluyordu (_al_to_bekle
# r["karar"]'i yerinde eziyor). Artik her karar icin -- AL olsun olmasin --
# motor motor iz tutulur ve db.karar_denetim'e yazilir.
#
# sonuc degerleri:
#   gecti        : motor calisti, karar esigi asti
#   takildi      : motor calisti, karari DEGISTIRDI (AL -> BEKLE/VETO)
#   uygulanmadi  : motor bu karar icin calismadi (or. karar AL degil, veri yok)
#   bilgi        : esik kontrolu degil, olculen deger (AI puani, EV, stop/hedef)

# Motor izinde beklenen sira (panelde bu sirayla gosterilir; eksikler
# "uygulanmadi" olarak tamamlanir -> bkz. _denetim_tamamla).
_DENETIM_MOTORLARI = [
    "AI kararı", "Risk vetosu", "Haber akışı", "AL puan eşiği",
    "Market breadth", "Giriş kalitesi (EQ)", "Bilanço freni",
    "Tekrarlı sinyal", "Sektör tavanı",
    "Beklenen değer (EV)", "Stop/hedef motoru",
]


def _dn(iz, motor, deger=None, esik=None, sonuc="bilgi", aciklama=None) -> None:
    """Motor izine bir satir ekler (ayni motor tekrar gelirse gunceller)."""
    kayit = {"motor": motor, "deger": deger, "esik": esik,
             "sonuc": sonuc, "aciklama": aciklama}
    for i, mevcut in enumerate(iz):
        if mevcut.get("motor") == motor:
            iz[i] = kayit
            return
    iz.append(kayit)


def _dn_r(r, motor, **kw) -> None:
    """_dn'in kayit (r) uzerinde calisan hali; iz yoksa olusturur."""
    if r.get("_denetim") is None:
        r["_denetim"] = []
    _dn(r["_denetim"], motor, **kw)


def _denetim_tamamla(r) -> list:
    """Izde hic gecmeyen motorlari 'uygulanmadi' olarak tamamlar ve
    _DENETIM_MOTORLARI sirasina dizer (sonda listede olmayan ek motorlar)."""
    iz = list(r.get("_denetim") or [])
    var = {k.get("motor") for k in iz}
    ham = r.get("karar_ham")
    for motor in _DENETIM_MOTORLARI:
        if motor in var:
            continue
        # Neden calismadi? AL disi kararlarda AL filtreleri hic devreye girmez.
        neden = ("Karar AL değil; bu filtre yalnız AL kararlarına uygulanır."
                 if ham != "AL" else
                 "Önceki bir filtre kararı zaten düşürdüğü için çalışmadı.")
        iz.append({"motor": motor, "deger": None, "esik": None,
                   "sonuc": "uygulanmadi", "aciklama": neden})
    sira = {m: i for i, m in enumerate(_DENETIM_MOTORLARI)}
    return sorted(iz, key=lambda k: sira.get(k.get("motor"), 99))


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
    risk_det = ctx.get("risk_det")
    risk_det_skor = getattr(risk_det, "score", None)
    # FAIL-SAFE (6c): deterministik risk hesaplanamadiysa kayda durum yaz + gunluk
    # sayaci artir; health_monitor bunu gunluk ozet olarak admin'e bildirir.
    risk_det_durum = "OK" if risk_det is not None else "HESAPLANAMADI"
    if risk_det is None:
        try:
            from src.db import database as _db_rd
            _db_rd.gunluk_sayac_arttir("risk_det_fail")
        except Exception:
            pass

    # Enstruman aciklamasi (instruments tablosu) -> AI baglamina girer. SPCX gibi
    # karistirilan semboller icin "ne oldugu" netlessin diye kayda eklenir.
    try:
        from src.db import database as db
        aciklama = (db.get_instrument(ticker) or {}).get("aciklama") or ""
    except Exception:
        aciklama = ""

    # CIFT RISK VETOSU (yalniz AL): AI riski >=9 VEYA deterministik risk (risk.py) >=9
    # ise VETO. Mesajda vetoyu hangi kaynagin tetikledigi belirtilir.
    ai_veto = (v.karar == "AL" and v.risk >= 9)
    det_veto = (v.karar == "AL" and risk_det_skor is not None and risk_det_skor >= 9)
    vetoed = ai_veto or det_veto
    if vetoed:
        final_decision = "VETO"
        if ai_veto and det_veto:
            _kaynak = f"AI risk {v.risk}/10 + deterministik risk {risk_det_skor}/10"
        elif ai_veto:
            _kaynak = f"AI risk {v.risk}/10"
        else:
            _kaynak = f"deterministik risk {risk_det_skor}/10"
        final_label = f"VETO ({_kaynak}) -> islem yok"
    else:
        # AI kararina guven: TUT dediyse TUT kalir. AL esigi (puan 7+) zaten prompt'ta
        # ('AL CESARETI'); kararin uzerine kod ile yazmiyoruz.
        final_decision = v.karar
        final_label = _LABEL[v.karar]

    gozlemler = [v.neden_simdi]
    if news["haberler"]:
        gozlemler.append(
            f"{len(news['haberler'])} taze haber; fiyatlanmis_mi={v.fiyatlanmis_mi}")

    # Giris kalitesi skoru (yalniz AL kararinda): trend/volatilite/likidite/
    # momentum/risk bilesenlerinden 0-100 skor + yildiz + oneri.
    entry_quality = None
    position_size_oneri = None
    expected_value = None
    position_size_tl = None
    # Gosterilecek stop/hedef metni: varsayilan AI alanlari; ABD AL'da asagida
    # deterministik motor (stop_hedef.hesapla) ciktisiyla degistirilir.
    stop_loss_txt = (getattr(v, "stop_loss", "") or "").strip()
    hedef_fiyat_txt = (getattr(v, "hedef_fiyat", "") or "").strip()
    if final_decision == "AL":
        try:
            from src.ai import entry_quality as eq
            entry_quality = eq.hesapla(sig, v.risk)
        except Exception:
            entry_quality = None
        kalite_skoru = (entry_quality or {}).get("skor")
        # Beklenen Deger (EV): sektor/genel hit_rate + karara ozel hedef/stop R/R
        guncel = sig.get("son_kapanis")
        hedef_num = parse_first_price(getattr(v, "hedef_fiyat", ""))
        stop_num = parse_first_price(getattr(v, "stop_loss", ""))
        try:
            from src.ai import expected_value as _ev
            sektor_ist, genel_ist = _ev_istatistik()
            expected_value = _ev.karar_ev(ticker, guncel=guncel, hedef=hedef_num,
                                          stop=stop_num, sektor_ist=sektor_ist,
                                          genel_ist=genel_ist)
        except Exception:
            expected_value = None
        ev_deger = (expected_value or {}).get("ev")
        # Pozisyon buyuklugu onerisi: eminlik + giris kalitesi + EV kademesi
        position_size_oneri = _pozisyon_buyuklugu(v.eminlik, kalite_skoru, ev_deger)
        # Deterministik stop/hedef (motor). ABD kartlarinda gosterilen stop/hedef bunu
        # kullanir (AI metni motor tavanini %5 asabiliyordu; or. NVDA stop %10.8) ve ABD
        # lotu USD->TL kur ile buradan hesaplanir.
        try:
            from src.ai import stop_hedef as _sh_mod
            sh = _sh_mod.hesapla(sig, v.risk)
        except Exception:
            sh = None
        if sh is None and guncel:
            # FAIL-SAFE (6b): motor stop/hedef uretemedi -> AI metnine dusmek yerine
            # varsayilan vol %4 kurali (stop -%4, hedef +%8 ~ RR 2:1) + admin log.
            sh = {"stop": round(guncel * 0.96, 2), "stop_pct": 4.0,
                  "hedef1": round(guncel * 1.08, 2), "hedef1_pct": 8.0,
                  "_fallback": True}
            print(f"  [fail-safe] {ticker}: stop_hedef motoru üretemedi -> "
                  f"varsayılan vol %4 (stop {sh['stop']}, hedef {sh['hedef1']})")
        if is_us:
            kur = _usdtry_kur()                      # USD hisse-basi riskini TL'ye cevir
            lot_stop = sh["stop"] if sh else stop_num
            position_size_tl = (pozisyon_lot(guncel, lot_stop, kur=kur) if kur else None)
            if sh:                                   # gosterilen stop/hedef = deterministik
                stop_loss_txt = f"{sh['stop']:g} $ altına inerse çık (-%{sh['stop_pct']:g})"
                hedef_fiyat_txt = (f"{sh['hedef1']:g} $'a ulaşırsa değerlendir "
                                   f"(+%{sh['hedef1_pct']:g})")
        else:
            position_size_tl = pozisyon_lot(guncel, stop_num)   # BIST: TL, kur=1

    # --- Karar tipine gore aksiyon + stop-loss (deterministik) ---
    son_kapanis = sig.get("son_kapanis")
    aksiyon = None
    stop_loss_seviyesi = None
    tekrar_bak_kosulu = (getattr(v, "tekrar_bak_kosulu", "") or "").strip()
    if final_decision == "AL" and v.risk >= 7:
        aksiyon = "Kademeli gir, tek seferde değil"
    elif final_decision in ("AZALT", "UZAK_DUR", "SAT", "GUCLU_SAT"):
        aksiyon = "Kademeli çık (özellikle büyük pozisyonda)"
    elif final_decision == "BEKLE":
        aksiyon = tekrar_bak_kosulu or "Koşullar netleşince tekrar değerlendir"
    elif final_decision == "TUT" and son_kapanis:
        # Stop-loss: guncel fiyatin -%8'i (alis fiyati bilinmiyorsa referans guncel)
        stop_loss_seviyesi = round(son_kapanis * 0.92, 2)

    # --- VERI GUVENI DAMGASI (kayit; karar kurallarina DOKUNMAZ) ---
    # veri_guveni: teknik veri butunlugu 0-100. entry_quality yalniz AL'da hesaplaniyor;
    # burada TUM kararlar icin bagimsiz (yan etkisiz) hesaplanir ki damga her karara dussun.
    veri_guveni = None
    try:
        from src.ai import entry_quality as _eq
        veri_guveni = _eq.hesapla(sig, v.risk).get("veri_guveni")
    except Exception:
        veri_guveni = None
    # eksik_veriler: karar aninda BOS olan kaynaklar (dolu olanlar yazilmaz).
    _eksik = []
    if not news.get("haberler"):
        _eksik.append("haber")
    if (not is_us) and not news.get("bildirimler"):   # KAP bildirimleri yalniz BIST
        _eksik.append("kap")
    if not temel.get("available"):                    # bilanco/finansal saglik
        _eksik.append("bilanco")
    if not (analist.get("available") and analist.get("veri_kalitesi") != "bayat"):
        _eksik.append("analist")
    eksik_veriler = ",".join(_eksik) or None

    # --- KARAR SEFFAFLIGI (20 Tem 2026): motor motor iz ---
    # Bu asamada calisan motorlar buraya yazilir; esik kontrollu filtreler
    # _apply_karar_filtreleri icinde kendi satirlarini ekler. Iz HER hisse icin
    # tutulur (yalniz AL degil) — bkz. db.karar_denetim.
    denetim = []
    _dn(denetim, "AI kararı", deger=v.karar, sonuc="bilgi",
        aciklama=f"puan {v.puan}/10, risk {v.risk}/10, eminlik {v.eminlik}")
    _dn(denetim, "Risk vetosu",
        deger=f"AI {v.risk}/10, deterministik "
              f"{risk_det_skor if risk_det_skor is not None else 'yok'}/10",
        esik="≥9 (yalnız AL)",
        sonuc=("takildi" if vetoed else
               ("gecti" if v.karar == "AL" else "uygulanmadi")),
        aciklama=(final_label if vetoed else
                  ("Risk eşiğin altında." if v.karar == "AL"
                   else "Karar AL değil; veto yalnız AL'a uygulanır.")))
    if risk_det_durum != "OK":
        _dn(denetim, "Deterministik risk", deger=None, sonuc="uygulanmadi",
            aciklama="risk.py skoru hesaplanamadı (yalnız AI riski kullanıldı).")
    _dn(denetim, "Haber akışı",
        deger=f"{len(news.get('haberler') or [])} taze haber, "
              f"{len(news.get('bildirimler') or [])} KAP",
        sonuc="bilgi",
        aciklama=f"AI bağlamına girdi; fiyatlanmış_mı={v.fiyatlanmis_mi}")
    if final_decision == "AL":
        # Not: esik kontrolu _apply_karar_filtreleri'nde; burasi olculen ham skor
        # (filtre ayni motor adiyla bu satiri gunceller).
        _dn(denetim, "Giriş kalitesi (EQ)",
            deger=(entry_quality or {}).get("skor"), sonuc="bilgi",
            aciklama=f"ölçüldü — yıldız: {(entry_quality or {}).get('yildiz')}")
        _dn(denetim, "Beklenen değer (EV)",
            deger=(expected_value or {}).get("ev"), sonuc="bilgi",
            aciklama=f"pozisyon önerisi: {position_size_oneri}")
        _dn(denetim, "Stop/hedef motoru",
            deger=(f"stop {sh['stop']:g} (-%{sh['stop_pct']:g}), "
                   f"hedef {sh['hedef1']:g} (+%{sh['hedef1_pct']:g})" if sh else None),
            sonuc="bilgi",
            aciklama=("fail-safe varsayılan (%4 vol) kullanıldı"
                      if (sh or {}).get("_fallback") else "volatilite×2, %3-5 kırpma"))

    return {
        "ticker": ticker,
        "symbol": sig["sembol"],
        "market": "abd" if is_us else "bist",
        # KARAR SEFFAFLIGI: ham karar + motor izi (filtreler bunlari gunceller)
        "karar_ham": v.karar,
        "_denetim": denetim,
        # veri guveni damgasi (record_decision/open_trade bunlari yazar)
        "veri_guveni": veri_guveni,
        "eksik_veriler": eksik_veriler,
        "para_birimi": "$" if is_us else "₺",
        "aciklama": aciklama,
        "skipped": False,
        # --- AI ham ciktisi ---
        "karar": v.karar,
        "puan": v.puan,
        "risk_ai": v.risk,
        "risk_deterministik": risk_det_skor,
        "risk_det_durum": risk_det_durum,
        "eminlik": v.eminlik,
        "gerekce": v.gerekce,
        "sade_yorum": getattr(v, "sade_yorum", "") or "",
        "neden_simdi": v.neden_simdi,
        "fiyatlanmis_mi": v.fiyatlanmis_mi,
        # --- web arayuzu uyumlu alanlar ---
        "score": v.puan,
        "risk": {"score": v.risk, "veto": vetoed,
                 "message": f"Risk {v.risk}/10." + (" VETO." if vetoed else "")},
        "vetoed": vetoed,
        "final_decision": final_decision,
        "final_label": final_label,
        "aksiyon": aksiyon,
        "stop_loss_seviyesi": stop_loss_seviyesi,
        "tekrar_bak_kosulu": tekrar_bak_kosulu or None,
        # --- Karar motoru: AI'nin metinsel giris/stop/hedef/tetikleyici alanlari ---
        "giris_seviyesi": (getattr(v, "giris_seviyesi", "") or "").strip(),
        # stop/hedef: ABD AL'da deterministik motor metni, aksi halde AI metni (bkz. yukari)
        "stop_loss": stop_loss_txt,
        "hedef_fiyat": hedef_fiyat_txt,
        "tetikleyici_kosul": (getattr(v, "tetikleyici_kosul", "") or "").strip(),
        # Cikis stratejisi (AL/TUT): stop/hedef/tetikleyici olusunca ne yapilmali
        "cikis_stratejisi": (getattr(v, "cikis_stratejisi", "") or "").strip(),
        # Pozisyon buyuklugu onerisi (yalniz AL); diger kararlarda None
        "position_size_oneri": position_size_oneri,
        # Sabit risk butcesi lot (yalniz AL): {risk_tl, stop_yuzde, lot, ...} veya None
        "position_size_tl": position_size_tl,
        # Beklenen Deger (yalniz AL): {ev, hit_rate, ort_kazanc, ort_kayip, ...} veya None
        "expected_value": expected_value,
        # Giris kalitesi (yalniz AL): {skor, yildiz, oneri, kirilim} veya None
        "entry_quality": entry_quality,
        # TUT degerlendirme penceresi (AI tahmini, islem gunu); diger kararlarda 0
        "tahmini_sure": getattr(v, "tahmini_sure", 0) or 0,
        "gozlemler": gozlemler,
        "haber_sayisi": len(news["haberler"]),
        "haberler": news["haberler"],
        "kullanilan_on_sinyal": sig,
        "analist": (analist if (analist.get("available")
                                 and analist.get("veri_kalitesi") != "bayat") else None),
        "temel": temel if temel.get("available") else None,
        "hacim_anomalisi": hacim_anom if hacim_anom.get("available") else None,
        "sektor_korelasyonu": sektor if sektor.get("available") else None,
    }


def analyze_stock(ticker: str, news_src=None, rss_src=None, client=None,
                  context=None, market: str = "bist", learning_note=None,
                  usage_acc=None) -> dict:
    """Tek hisse icin tam zincir (tek AI cagrisi). Web uyumlu kayit dondurur.

    market='bist' (varsayilan) veya 'us'/'abd'. ABD'de KAP/Turkce haber, analist
    konsensusu ve sektor korelasyon tablosu uygulanmaz.
    usage_acc: token toplama sozlugu (run fallback TOKEN OZET icin)."""
    kill, payload, ctx = _prepare_payload(
        ticker, news_src=news_src, rss_src=rss_src, context=context,
        market=market, learning_note=learning_note)
    if kill is not None:
        return kill
    v = _ai_verdict(ctx["ticker"], payload, client=client, usage_acc=usage_acc)
    return _finalize_record(ctx, v)


# ---------------------------------------------------------------------------
# Zinciri calistir + kaydet + decisions tablosu
# ---------------------------------------------------------------------------
def _trade_telegram(mesaj: str, kullanici_id=0) -> None:
    """Trade olayini Telegram'a gonderir (sahibi varsa ona, yoksa yoneticilere).
    Sessiz: Telegram yoksa/hata olursa hicbir sey yapmaz."""
    try:
        from src.notify import telegram
        from src.db import database as db
        if not telegram.is_configured():
            return
        chat_id = None
        if kullanici_id:
            u = db.get_user_by_id(kullanici_id)
            if u and u.get("telegram_id"):
                chat_id = u["telegram_id"]
        if chat_id:
            telegram.send_message(mesaj, chat_id=chat_id)
        else:
            telegram.notify_admins(mesaj, prefix="")
    except Exception:
        pass


def _record_trades(results, verbose: bool = False, tarih=None):
    """Karar sonuclarini gercek islem defterine (trades) yazar.

    AL  -> acik trade yoksa yeni pozisyon ac (entry=son_kapanis, stop/hedef=verdict,
           rr_oran=(hedef-entry)/(entry-stop)).
    AZALT/UZAK_DUR/SAT/GUCLU_SAT -> acik trade varsa kapat (pnl + holding_days).
    kullanici_id=0 (sistem geneli). Fiyatlar yerel para biriminde (BIST: TL, ABD: USD).
    """
    from src.db import database as db
    bugun = tarih or datetime.now(_TZ).date().isoformat()
    acilan = kapanan = 0
    for r in results or []:
        if r.get("skipped") or r.get("kill_switch"):
            continue
        ticker = (r.get("ticker") or "").upper().replace(".IS", "")
        if not ticker:
            continue
        karar = (r.get("final_decision") or "").upper()
        sig = r.get("kullanilan_on_sinyal") or {}
        fiyat = sig.get("son_kapanis")
        para_birimi = "USD" if (r.get("market") or "bist").lower() in ("us", "abd") else "TL"
        # Enstruman ana tablosu varsa para birimini oradan dogrula (kaynak-i hakikat)
        try:
            if db.get_instrument(ticker) is not None:
                para_birimi = "USD" if db.is_us_instrument(ticker) else "TL"
        except Exception:
            pass
        try:
            acik = db.get_open_trade(ticker)
            if karar == "AL":
                if acik or not fiyat:
                    continue
                # Deterministik stop / kademeli hedef (stop_hedef motoru). Veri yoksa
                # AI'nin metinsel stop_loss/hedef_fiyat alanlarina geri dus.
                from src.ai import stop_hedef
                sh = stop_hedef.hesapla(sig, r.get("risk_ai"))
                if sh:
                    stop, hedef, hedef2 = sh["stop"], sh["hedef1"], sh["hedef2"]
                else:
                    stop = parse_first_price(r.get("stop_loss"))
                    hedef = parse_first_price(r.get("hedef_fiyat"))
                    hedef2 = None
                rr = None
                if stop is not None and hedef is not None and (fiyat - stop) != 0:
                    rr = round((hedef - fiyat) / (fiyat - stop), 2)
                db.open_trade(ticker, karar, fiyat, stop_fiyat=stop, hedef_fiyat=hedef,
                              hedef2_fiyat=hedef2, para_birimi=para_birimi, rr_oran=rr,
                              acilis_tarihi=bugun, strategy_version=STRATEGY_VERSION,
                              veri_guveni=r.get("veri_guveni"),
                              eksik_veriler=r.get("eksik_veriler"))
                acilan += 1
                if verbose:
                    if sh:
                        print(f"  [trade] AL {ticker} @ {fiyat} stop/hedef: deterministik "
                              f"(vol %{sig.get('volatilite_%')} -> stop -%{sh['stop_pct']}, "
                              f"hedef1 +%{sh['hedef1_pct']}) hedef2 +%{sh['hedef2_pct']} RR={rr}")
                    else:
                        print(f"  [trade] AL {ticker} @ {fiyat} stop={stop} hedef={hedef} RR={rr}")
            elif karar in ("AZALT", "UZAK_DUR", "SAT", "GUCLU_SAT"):
                if not acik or not fiyat:
                    continue
                entry = acik.get("entry_fiyat") or 0.0
                pnl_y = round((fiyat - entry) / entry * 100, 2) if entry else None
                hold = _gun_farki(acik.get("acilis_tarihi"), bugun)
                db.close_trade(acik["id"], fiyat, kapanis_sebep=f"karar:{karar}",
                               pnl_yuzde=pnl_y, holding_days=hold, tarih=bugun)
                kapanan += 1
                if verbose:
                    print(f"  [trade] {karar} {ticker} @ {fiyat} (giris {entry}) -> %{pnl_y}")
            elif karar == "BEKLE" and acik:
                # BEKLE + acik pozisyon: KAPATMA. 'yeniden degerlendir' isaretle; stop
                # entry'nin %5'ten fazla altindaysa entry x0.97'ye sikilastir.
                db.set_yeniden_degerlendir(acik["id"], 1)
                entry = acik.get("entry_fiyat") or 0.0
                cur_stop = acik.get("stop_fiyat")
                sikilasti = False
                if entry and cur_stop is not None and cur_stop < entry * 0.95:
                    yeni_stop = round(entry * 0.97, 2)
                    if yeni_stop > cur_stop:
                        db.update_trade_stop(acik["id"], yeni_stop)
                        sikilasti = True
                if sikilasti:
                    _trade_telegram(f"🟡 {ticker}: karar BEKLE'ye döndü, açık pozisyonun "
                                    f"stopu sıkılaştırıldı.", acik.get("kullanici_id"))
                else:
                    _trade_telegram(f"🟡 {ticker}: karar BEKLE'ye döndü, açık pozisyonu "
                                    f"gözden geçir.", acik.get("kullanici_id"))
                if verbose:
                    print(f"  [trade] BEKLE {ticker}: yeniden_degerlendir=1"
                          f"{' + stop sikilastirildi' if sikilasti else ''}")
        except Exception as e:
            if verbose:
                print(f"  [{ticker}] trade kaydi yazilamadi: {type(e).__name__}")
    return {"acilan": acilan, "kapanan": kapanan}


def _gun_farki(baslangic, bitis) -> int | None:
    """Iki ISO tarih (YYYY-MM-DD) arasi gun farki; hata olursa None."""
    try:
        a = datetime.fromisoformat(str(baslangic)[:10]).date()
        b = datetime.fromisoformat(str(bitis)[:10]).date()
        return (b - a).days
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Karar ust-filtreleri (persist ONCESI): sektor tavani + tekrarli sinyal.
# Sabah brifinginde ayni sektore yigilan ve yeni bilgisiz tekrar eden AL'lari
# BEKLE'ye dusurur. Hem run_batch hem run, persiste _persist uzerinden girer.
# ---------------------------------------------------------------------------
# Tavan uygulanan 6 sektor: _sektor_of (internal) adi -> gerekce/kullanici adi.
_TAVAN_SEKTORLER = {
    "Bankacılık": "Bankacılık",
    "Havacılık": "Havacılık",
    "Enerji/Rafineri": "Enerji",
    "Savunma": "Savunma",
    "Telekom": "Telekom",
    "Gayrimenkul": "GYO",
}
_SEKTOR_AL_TAVANI = 2          # ayni sektorde gunde en fazla bu kadar AL

# AL icin minimum puan esigi (piyasaya gore): BIST daha secici (8), ABD (7).
_AL_PUAN_ESIGI = {"bist": 8, "abd": 7}


def _al_puan_esigi_ad(r) -> int:
    """Kaydin piyasasina gore AL minimum puan esigi (BIST 8, ABD 7)."""
    mk = (r.get("market") or "bist").lower()
    return _AL_PUAN_ESIGI["abd"] if mk in ("us", "abd") else _AL_PUAN_ESIGI["bist"]


def _aktif_al(r) -> bool:
    """Kayit gercek (uygulanabilir) bir AL mi? skip/kill/veto haric."""
    return bool(r) and not r.get("skipped") and not r.get("kill_switch") \
        and not r.get("vetoed") and (r.get("final_decision") == "AL")


def _yeni_bilgi_var(r) -> bool:
    """Kayitta taze (YENI/GUNCEL = son 3 gun) haber/KAP var mi? Tekrarli-sinyal
    filtresi 'yeni sinyal yok' kararini buna gore verir. Tazelik bilinmiyorsa
    haber var sayilir (guvenli taraf: AL'i dusurme)."""
    haberler = r.get("haberler") or []
    if not haberler:
        return False
    bilinen = [(h.get("tazelik") or "").upper() for h in haberler
               if isinstance(h, dict) and h.get("tazelik")]
    if not bilinen:
        return True
    return any(t in ("YENI", "GUNCEL") for t in bilinen)


def _al_to_bekle(r, not_metni: str, verbose: bool = False) -> None:
    """Bir AL kaydini YERINDE BEKLE'ye dusurur. AL'a ozgu alanlar temizlenir
    (trade acilmaz, UI'da stale AL kalmaz); gerekce + sade_yorum'a not eklenir."""
    r["karar"] = "BEKLE"
    r["final_decision"] = "BEKLE"
    r["final_label"] = _LABEL["BEKLE"]
    onceki = (r.get("gerekce") or "").strip()
    r["gerekce"] = (onceki + " " if onceki else "") + not_metni
    sade = (r.get("sade_yorum") or "").strip()
    r["sade_yorum"] = (sade + " " if sade else "") + not_metni
    r["aksiyon"] = "Koşullar netleşince tekrar değerlendir"
    for k in ("giris_seviyesi", "stop_loss", "hedef_fiyat", "cikis_stratejisi"):
        if k in r:
            r[k] = ""
    r["position_size_oneri"] = None
    r["position_size_tl"] = None
    r["expected_value"] = None
    r["entry_quality"] = None
    r.setdefault("gozlemler", []).append(not_metni)
    r["karar_filtresi"] = not_metni             # bayrak (UI/raporlama)
    if verbose:
        print(f"  [filtre] {r.get('ticker')}: AL -> BEKLE ({not_metni})")


def _apply_karar_filtreleri(results, verbose: bool = False):
    """AL kararlarina iki ust-kurali SIRAYLA uygular (persist oncesi):
      1) Tekrarli sinyal: son 3 gunde ayni hisseye AL verilmis ve yeni bilgi
         (taze haber/KAP) yoksa -> BEKLE.
      2) Sektor tavani: izlenen 6 sektorde gunde max 2 AL; en dusuk puanli
         fazlalik -> BEKLE.
    Tekrarli sinyal ONCE uygulanir ki bayat AL'lar sektor kotasini doldurmasin.
    results yerinde degistirilir (cagiranin gosterdigi kayitlar da guncellenir)."""
    from src.db import database as db
    from src.ai.learning import _sektor_of
    from src.news import bilanco_takvimi
    bugun = datetime.now(_TZ).date()

    # 0a) AL PUAN ESIGI: BIST'te puan<8, ABD'de puan<7 olan AL -> BEKLE (daha secici).
    for r in results or []:
        if not _aktif_al(r):
            continue
        esik = _al_puan_esigi_ad(r)
        mk = "BIST" if esik == _AL_PUAN_ESIGI["bist"] else "ABD"
        _dn_r(r, "AL puan eşiği", deger=r.get("score"), esik=f"≥{esik} ({mk})",
              sonuc=("takildi" if (r.get("score") or 0) < esik else "gecti"),
              aciklama=f"{mk} pazarında AL için minimum puan {esik}.")
        if (r.get("score") or 0) < esik:
            _al_to_bekle(
                r, f"{mk} AL eşiği puan {esik}; puan {r.get('score')} yetersiz.",
                verbose=verbose)

    # 0b) MARKET BREADTH: izleme listesi zayifsa (<%30 SMA20 uzeri) yeni BIST AL -> BEKLE.
    try:
        from src.ai.presignal import market_breadth
        breadth = market_breadth()
    except Exception:
        breadth = None
    for r in results or []:
        if not _aktif_al(r):
            continue
        abd = (r.get("market") or "bist").lower() in ("us", "abd")
        zayif = bool(breadth and breadth.get("durum") == "zayıf")
        _dn_r(r, "Market breadth",
              deger=(f"%{breadth.get('oran')} ({breadth.get('durum')})"
                     if breadth else None),
              esik="SMA20 üstü oranı ≥%30",
              sonuc=("uygulanmadi" if (abd or not breadth)
                     else ("takildi" if zayif else "gecti")),
              aciklama=("Breadth BIST metriği; ABD kararlarına uygulanmaz." if abd
                        else ("Breadth hesaplanamadı." if not breadth
                              else "İzleme listesi geneli.")))
        if zayif and not abd:
            _al_to_bekle(
                r, f"Piyasa geneli zayıf (breadth %{breadth.get('oran')}); "
                   "yeni AL BEKLE'ye çekildi.", verbose=verbose)

    # 0c) ENTRY QUALITY (giris kalitesi) filtresi: AL kararinda EQ skoru hesaplanmissa
    #   - skor < 60        -> AL'i BEKLE'ye cek (daha iyi giris noktasi bekle)
    #   - 60 <= skor <= 75 -> yarim pozisyon onerisiyle devam et
    # Sektor tavanindan ONCE calisir ki dusuk kaliteli AL kotayi doldurmasin.
    for r in results or []:
        if not _aktif_al(r):
            continue
        eq_skor = (r.get("entry_quality") or {}).get("skor")
        _dn_r(r, "Giriş kalitesi (EQ)", deger=eq_skor, esik=f"≥{EQ_ESIK}",
              sonuc=("takildi" if (eq_skor is None or eq_skor < EQ_ESIK) else "gecti"),
              aciklama=("Hesaplanamadı — güvenli tarafta BEKLE (fail-safe)."
                        if eq_skor is None else
                        ("Daha iyi giriş noktası bekleniyor." if eq_skor < EQ_ESIK
                         else ("Yarım pozisyon bandı (60-75)." if eq_skor <= 75
                               else "Tam pozisyon bandı (>75)."))))
        if eq_skor is None:
            # FAIL-SAFE (6a): giris kalitesi hesaplanamadi -> filtreyi atlayip AL'i
            # geciralamak yerine guvenli tarafta BEKLE'ye cek.
            _al_to_bekle(
                r, "Giriş kalitesi hesaplanamadı — güvenli tarafta BEKLE",
                verbose=verbose)
            continue
        if eq_skor < EQ_ESIK:
            # GOLGE KAYDI (shadow mode, 15 Tem 2026): 55 <= EQ < 60 bandi "kil
            # payi elenenler". Karari DEGISTIRMEZ — asagidaki _al_to_bekle yine
            # calisir. 2 hafta sonra bu golgelerin gercekten yukselip yukselmedigi
            # olculur; yukseldiyse esik 60 fazla yuksek demektir (bkz.
            # src/ops/eq_golge.py). Kayit hatasi canli karari BOZMASIN diye
            # try/except ile sarili.
            if EQ_GOLGE_ALT <= eq_skor < EQ_ESIK:
                try:
                    from src.db import database as db
                    db.eq_golge_kaydet(
                        r.get("ticker"), datetime.now(_TZ).date().isoformat(),
                        eq_skor, (r.get("kullanilan_on_sinyal") or {}).get("son_kapanis"),
                        market=r.get("market") or "bist",
                        strateji=STRATEGY_VERSION)
                    if verbose:
                        print(f"  [golge] {r.get('ticker')}: EQ {eq_skor} "
                              f"(55-60 bandi) kaydedildi — karar degismedi")
                except Exception as e:
                    print(f"  [golge] {r.get('ticker')} kaydedilemedi: "
                          f"{type(e).__name__}")
            _al_to_bekle(
                r, "Giriş kalitesi düşük (EQ < 60), daha iyi giriş noktası bekle",
                verbose=verbose)
        elif eq_skor <= 75:
            r["position_size_oneri"] = "Yarım pozisyon — portföyün %2-3'ü"
            if verbose:
                print(f"  [filtre] {r.get('ticker')}: EQ {eq_skor} -> yarım pozisyon")

    # 0d) BILANCO FRENI: hissenin bilanco aciklama tarihi 2 ISLEM GUNU icindeyse yeni AL
    #   -> BEKLE. Sonuc oncesi surpriz/volatilite riski; aciklama sonrasi degerlendir.
    #   Bilanco tarihi bilinmiyorsa (gun_farki None) filtre atlanir.
    for r in results or []:
        if not _aktif_al(r):
            continue
        ticker = (r.get("ticker") or "").upper().replace(".IS", "")
        if not ticker:
            continue
        try:
            gun = bilanco_takvimi.gun_farki(ticker, bugun)
        except Exception:
            gun = None
        if gun is None:
            _dn_r(r, "Bilanço freni", deger=None, esik=">2 işlem günü",
                  sonuc="uygulanmadi",
                  aciklama="Bilanço açıklama tarihi bilinmiyor — filtre atlandı.")
            continue                              # bilinmiyor -> filtre atla
        try:
            import numpy as _np
            _ed = bugun + timedelta(days=gun)     # bilanco aciklama tarihi
            islem_gun = int(_np.busday_count(bugun.isoformat(), _ed.isoformat()))
        except Exception:
            _dn_r(r, "Bilanço freni", deger=f"{gun} takvim günü",
                  esik=">2 işlem günü", sonuc="uygulanmadi",
                  aciklama="İşlem günü farkı hesaplanamadı — filtre atlandı.")
            continue
        _dn_r(r, "Bilanço freni", deger=f"{islem_gun} işlem günü",
              esik=">2 işlem günü",
              sonuc=("takildi" if islem_gun <= 2 else "gecti"),
              aciklama=("Açıklama öncesi sürpriz/volatilite riski." if islem_gun <= 2
                        else "Bilanço yakın değil."))
        if islem_gun <= 2:
            kala = "bugün" if gun == 0 else f"{gun} gün"
            _al_to_bekle(
                r, f"Bilanço açıklamasına {kala} var — sonuç belirsizliği, "
                   "açıklama sonrası değerlendir.", verbose=verbose)

    # 1) Tekrarli sinyal filtresi (hisse bazli, bagimsiz)
    for r in results or []:
        if not _aktif_al(r):
            continue
        ticker = (r.get("ticker") or "").upper().replace(".IS", "")
        if not ticker:
            continue
        try:
            gecmis = db.list_decisions_for(ticker, limit=5)
        except Exception:
            gecmis = []
        son_al_fark = None
        for g in gecmis:
            if (g.get("karar") or "").upper() != "AL":
                continue
            try:
                gd = datetime.fromisoformat(str(g.get("tarih") or "")[:10]).date()
            except Exception:
                continue
            fark = (bugun - gd).days
            if 1 <= fark <= 3:                  # bugun haric, son 3 gun
                son_al_fark = fark
                break
        yeni_bilgi = _yeni_bilgi_var(r)
        dusur = (son_al_fark is not None and not yeni_bilgi)
        _dn_r(r, "Tekrarlı sinyal",
              deger=(f"{son_al_fark} gün önce AL var" if son_al_fark is not None
                     else "son 3 günde AL yok"),
              esik="son 1-3 günde AL + yeni bilgi yok",
              sonuc=("takildi" if dusur else "gecti"),
              aciklama=("Taze haber/KAP yok — aynı sinyal tekrarlanıyor." if dusur
                        else ("Taze haber/KAP var." if son_al_fark is not None
                              else "Yakın geçmişte AL verilmemiş.")))
        if dusur:
            _al_to_bekle(
                r, f"{ticker} için {son_al_fark} gün önce AL verilmişti, yeni sinyal yok.",
                verbose=verbose)

    # 2) Sektor tavani (tekrarli sinyalden sonra kalan AL'lar uzerinde)
    sektor_al = {}
    for r in results or []:
        if not _aktif_al(r):
            continue
        sek = _sektor_of(r.get("ticker"))
        if sek in _TAVAN_SEKTORLER:
            sektor_al.setdefault(sek, []).append(r)
    for sek, kayitlar in sektor_al.items():
        ad = _TAVAN_SEKTORLER[sek]
        # En guclu 2'yi koru: puan desc, esitlikte risk asc
        kayitlar.sort(key=lambda r: (-(r.get("score") or 0),
                                     (r.get("risk") or {}).get("score") or 0))
        for sira, r in enumerate(kayitlar):
            asti = sira >= _SEKTOR_AL_TAVANI
            _dn_r(r, "Sektör tavanı",
                  deger=f"{ad}: {len(kayitlar)} AL, bu hisse {sira + 1}. sırada",
                  esik=f"sektör başına ≤{_SEKTOR_AL_TAVANI} AL",
                  sonuc=("takildi" if asti else "gecti"),
                  aciklama=("Puan/risk sıralamasında tavanın dışında kaldı." if asti
                            else "Sektör kotası içinde."))
            if asti:
                _al_to_bekle(
                    r, f"{ad} sektöründe bugün zaten {_SEKTOR_AL_TAVANI} AL var.",
                    verbose=verbose)
    return results


def _denetim_yaz(r, tarih, verbose: bool = False) -> None:
    """Bir kaydin motor izini db.karar_denetim'e yazar (KILL_SWITCH dahil)."""
    from src.db import database as db
    if r.get("kill_switch"):
        iz = [{"motor": "KILL_SWITCH", "deger": r.get("reason"), "esik": None,
               "sonuc": "takildi",
               "aciklama": r.get("mesaj") or "Veri bütünlüğü kill-switch'i devrede."}]
        db.karar_denetim_kaydet(
            ticker=r["ticker"], tarih=tarih, karar_ham=None,
            karar_final="KILL_SWITCH", motorlar=iz, degistiren="KILL_SWITCH",
            strategy_version=STRATEGY_VERSION)
        return
    iz = _denetim_tamamla(r)
    # Karari fiilen degistiren motor: ize 'takildi' yazan SON motor.
    degistiren = None
    for k in iz:
        if k.get("sonuc") == "takildi":
            degistiren = k.get("motor")
    db.karar_denetim_kaydet(
        ticker=r["ticker"], tarih=tarih,
        karar_ham=r.get("karar_ham"), karar_final=r.get("final_decision"),
        motorlar=iz, degistiren=degistiren,
        strategy_version=STRATEGY_VERSION)
    if verbose and degistiren and r.get("karar_ham") != r.get("final_decision"):
        print(f"  [denetim] {r.get('ticker')}: {r.get('karar_ham')} -> "
              f"{r.get('final_decision')} ({degistiren})")


def _persist(results, save: bool, verbose: bool):
    """Sonuclari decisions tablosuna yazar + ai_commentary.json'a kaydeder."""
    from src.db import database as db
    _apply_karar_filtreleri(results, verbose=verbose)   # sektor tavani + tekrar filtresi
    today = datetime.now(_TZ).date().isoformat()
    for r in results:
        try:
            if r.get("kill_switch"):
                db.record_decision(
                    ticker=r["ticker"], karar="KILL_SWITCH", puan=None, risk=None,
                    eminlik=None, gerekce=r.get("mesaj"), tarih=today,
                    strategy_version=STRATEGY_VERSION)
            elif not r.get("skipped"):
                db.record_decision(
                    ticker=r["ticker"], karar=r["final_decision"],
                    puan=r.get("score"), risk=(r.get("risk") or {}).get("score"),
                    eminlik=r.get("eminlik"), gerekce=r.get("gerekce"), tarih=today,
                    tahmini_sure=(r.get("tahmini_sure") or None),
                    strategy_version=STRATEGY_VERSION,
                    veri_guveni=r.get("veri_guveni"),
                    eksik_veriler=r.get("eksik_veriler"))
        except Exception as e:
            if verbose:
                print(f"  [{r.get('ticker')}] karar kaydi yazilamadi: {type(e).__name__}")
        # KARAR SEFFAFLIGI: motor izini ayri denetim tablosuna yaz. HER karar icin
        # (AL olsun olmasin). Denetim yazimi CANLI KARARI BOZMASIN diye ayri
        # try/except — hata yalniz loglanir.
        if not r.get("skipped"):
            try:
                _denetim_yaz(r, today, verbose=verbose)
            except Exception as e:
                if verbose:
                    print(f"  [{r.get('ticker')}] denetim izi yazilamadi: "
                          f"{type(e).__name__}")
    _record_trades(results, verbose=verbose, tarih=today)
    if save:
        _save_results(results, verbose=verbose)


def _verbose_satir(t, r):
    if r.get("kill_switch"):
        return f"  {t:7} KILL_SWITCH ({r.get('reason')})"
    if r.get("skipped"):
        return f"  {t:7} ATLANDI ({r.get('reason')})"
    return (f"  {t:7} {r['final_decision']:5} puan {r['score']}/10 "
            f"risk {r['risk']['score']}/10 {r['eminlik']} haber={r['haber_sayisi']}")


def run_batch(tickers: list[str], save: bool = True, verbose: bool = True,
              overview=None, learning=None, poll_interval: int = 30,
              max_wait: int = 1800, extra_context=None) -> list[dict]:
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

    # KREDI FRENI: bakiye bittiyse batch bile acma (veri hazirligi + batch
    # olusturma bosuna). Fren gun boyu; ertesi gun kendiliginden duser.
    if kredi_freni_aktif_mi():
        if verbose:
            print("  [kredi] AI kredisi bitti (gunluk fren aktif) -> batch "
                  "acilmayacak, hicbir AI cagrisi yapilmayacak.")
        results = [{"ticker": str(t).partition(":")[0].strip().upper(),
                    "skipped": True, "ai_hata": True,
                    "reason": "Kredi bitti (AI cagrisi atlandi)"}
                   for t in (tickers or [])]
        _atlama_alarmi(results, tickers, verbose=verbose)
        return results

    news_src, is_sample = get_news_source(verbose=verbose)
    rss_src = RSSNewsSource()
    context = market_context(rss_src=rss_src, overview=overview)
    if extra_context:                       # ABD brifingi: abd_gundemi vb. ek baglam
        context.update(extra_context)
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
        # Batch custom_id yalniz [A-Za-z0-9_-] olabilir; ticker'daki '.' (orn.
        # GMSTR.F) gecersiz -> tum batch 400 verirdi. Gecersiz karakterleri temizle.
        safe = "".join(c if (c.isalnum() or c in "_-") else "_"
                       for c in t.upper().replace(".IS", ""))
        cid = f"{i}-{safe}"
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
                model=MODEL, max_tokens=MAX_TOKENS, system=_SYSTEM_CACHED,
                messages=[{"role": "user", "content": _user_prompt(ctx["ticker"], payload)}],
                output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
            )))

    # 2) Batch gonder + polling
    if requests:
        client = anthropic.Anthropic()
        # SOGUK CACHE ISITMA: batch istekleri paralel firlar; SYSTEM (+ ortak
        # piyasa_baglami) cache'i daha yazilmadan cok sayida istek onu yeniden yazar
        # (pahali cache_write). Batch'ten hemen ONCE tek kucuk istekle ayni onekleri
        # (SYSTEM + piyasa_baglami) bir kez yaz -> batch istekleri ucuz cache-read'e
        # duser. Prefix'in birebir eslesmesi icin batch ile AYNI model/system/output_config
        # ve ayni baglam blogu kullanilir. Best-effort; hata batch'i dusurmez.
        try:
            client.messages.create(
                model=MODEL, max_tokens=16, system=_SYSTEM_CACHED,
                messages=[{"role": "user", "content": [
                    _baglam_blok(context),
                    {"type": "text", "text": "Hazir."}]}],
                output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}})
            if verbose:
                print("  [batch] cache ısıtıldı (SYSTEM + piyasa_baglami)")
        except Exception as e:
            if verbose:
                print(f"  [batch] cache ısıtma atlandı: {type(e).__name__}")
            # Isitma "best-effort" ama KREDI hatasi burada gorunur (15 Tem 2026:
            # sessizce yutuldu, ardindan batch 400 verdi). Freni hemen koy ki
            # fallback run() tek bir cagri bile denemesin.
            if kredi_bitti_mi(e):
                kredi_freni_koy(f"{type(e).__name__}: {str(e)[:120]}")
                results = [{"ticker": ctxs.get(c, {}).get("ticker",
                                                          str(t).partition(":")[0].upper()),
                            "skipped": True, "ai_hata": True,
                            "reason": "Kredi bitti (AI cagrisi atlandi)"}
                           for c, t in order]
                _persist(results, save=save, verbose=verbose)
                _atlama_alarmi(results, tickers, verbose=verbose)
                return results
        try:
            batch = client.messages.batches.create(requests=requests)
        except Exception as e:
            if kredi_bitti_mi(e):
                kredi_freni_koy(f"{type(e).__name__}: {str(e)[:120]}")
            raise
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
        toplam_input = toplam_output = 0
        toplam_cache_read = toplam_cache_write = 0
        if status == "ended":
            for res in client.messages.batches.results(batch.id):
                cid = res.custom_id
                ctx = ctxs.get(cid)
                if ctx is None:
                    continue
                if res.result.type == "succeeded":
                    try:
                        msg = res.result.message
                        usage = getattr(msg, "usage", None)
                        if usage is not None:        # her hisse icin token topla
                            toplam_input += getattr(usage, "input_tokens", 0) or 0
                            toplam_output += getattr(usage, "output_tokens", 0) or 0
                            toplam_cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
                            toplam_cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
                        text = next((b.text for b in msg.content if b.type == "text"), "")
                        v = Verdict(**json.loads(text))
                        final[cid] = _finalize_record(ctx, v)
                    except Exception as e:
                        final[cid] = {"ticker": ctx["ticker"], "skipped": True,
                                      "ai_hata": True,
                                      "reason": f"Batch parse: {type(e).__name__}"}
                else:
                    final[cid] = {"ticker": ctx["ticker"], "skipped": True,
                                  "ai_hata": True,
                                  "reason": f"Batch {res.result.type}"}
                    try:                         # gunluk AI hata sayaci (health_monitor okur)
                        from src.db import database as _db
                        _db.ai_hata_inc()
                    except Exception:
                        pass

        # Tum batch bitti -> token/maliyet ozeti (log dosyasina dusen stdout).
        # TR brifingi -> briefing.log, US brifingi -> briefing_us.log (cron yonlendirmesi).
        # input = cache'siz tam ucretli; cache_read = %90 ucuz okuma (hit);
        # cache_write = ilk yazim (1.25x). Cache sayesinde SYSTEM bir kez yazilir.
        # BATCH yolu -> batch fiyati (%50). Ortak yardimcidan (tek kaynak).
        maliyet.logla({"input": toplam_input, "output": toplam_output,
                       "cache_write": toplam_cache_write, "cache_read": toplam_cache_read},
                      MODEL, batch=True,
                      tarih=datetime.now(_TZ).strftime("%Y-%m-%d %H:%M"))

    # Hala sonuc gelmeyenler (timeout vb.) -> skipped
    for cid, ctx in ctxs.items():
        if cid not in final:
            final[cid] = {"ticker": ctx["ticker"], "skipped": True,
                          "ai_hata": True,
                          "reason": "Batch sonuc gelmedi (timeout)"}

    # 4) Orijinal sirada birlestir + kaydet
    results = [final[cid] for cid, _ in order if cid in final]
    if verbose:
        for cid, t in order:
            r = final.get(cid)
            if r:
                print(_verbose_satir(t, r))
    _persist(results, save=save, verbose=verbose)

    # Sessiz basarisizlik alarmi (run() ile ORTAK yardimci; esikler orada).
    _atlama_alarmi(results, tickers, verbose=verbose)

    return results
def run(tickers: list[str], save: bool = True, verbose: bool = True,
        overview=None, learning=None, extra_context=None) -> list[dict]:
    from src.news.service import get_news_source
    import anthropic

    _load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY yok - AI yorumu uretilemez.")

    from src.news.rss_source import RSSNewsSource

    news_src, is_sample = get_news_source(verbose=verbose)
    rss_src = RSSNewsSource()                       # Bloomberg HT + Investing + Mynet
    # genel piyasa baglami (1 kez); brifing onceden hesaplamissa onu kullan
    context = market_context(rss_src=rss_src, overview=overview)
    if extra_context:                       # ABD brifingi: abd_gundemi vb. ek baglam
        context.update(extra_context)
    learning = learning or {}
    if verbose:
        gp = context.get("genel_piyasa") or {}
        print(f"  [rss] 24s haber: {rss_src.recent_count()} | "
              f"makro: {context['makro'].get('available')} | "
              f"piyasa: {gp.get('yon')} (BIST %{gp.get('bist100_gunluk_%')})")
    client = anthropic.Anthropic()

    usage_acc = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    results = []
    # KREDI FRENI: bakiye bittiyse tek bir cagri bile yapma (15 Tem 2026: 92
    # hisse icin 92 bosuna cagri yapildi). Fren gun boyu; ertesi gun duser.
    kredi_frenli = kredi_freni_aktif_mi()
    if kredi_frenli and verbose:
        print("  [kredi] AI kredisi bitti (gunluk fren aktif) -> "
              "hicbir AI cagrisi yapilmayacak.")
    for raw in tickers:
        # "TICKER" (bist) veya "TICKER:us"/"TICKER:abd" formatini destekle
        t, _, mk = str(raw).partition(":")
        t = t.strip()
        market = (mk.strip().lower() or "bist")
        if kredi_frenli:                  # cagri YOK -> token/para harcanmaz
            results.append({"ticker": t.upper(), "skipped": True, "ai_hata": True,
                            "reason": "Kredi bitti (AI cagrisi atlandi)"})
            continue
        try:
            r = analyze_stock(t, news_src=news_src, rss_src=rss_src,
                              client=client, context=context, market=market,
                              learning_note=learning.get(t.upper().replace(".IS", "")),
                              usage_acc=usage_acc)
        except Exception as e:
            if verbose:
                print(f"  [{t}] HATA: {type(e).__name__}: {str(e)[:100]}")
            r = {"ticker": t.upper(), "skipped": True,
                 "reason": f"Hata: {type(e).__name__}"}
            if _ai_cagri_hatasi_mi(e):
                r["ai_hata"] = True
            # Kredi bitti -> bayragi koy, KALAN hisseler icin cagri yapma.
            if kredi_bitti_mi(e):
                kredi_freni_koy(f"{type(e).__name__}: {str(e)[:120]}")
                kredi_frenli = True
                if verbose:
                    print("  [kredi] Bakiye bitti -> kalan hisseler icin AI "
                          "cagrisi YAPILMAYACAK (gun sonuna kadar fren).")
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
    # Persist (sektor tavani + tekrarli sinyal filtresi _persist icinde uygulanir,
    # ardindan decisions + trades + ai_commentary.json yazilir). run_batch ile ayni yol.
    _persist(results, save=save, verbose=verbose)

    # Sessiz basarisizlik alarmi — run_batch ile ORTAK. 15 Tem 2026'ya kadar bu
    # alarm YALNIZ run_batch icindeydi; batch kredi hatasiyla dusup buraya
    # (fallback) gelince kimse uyarilmadi. Hatalar tam da bu yola dusuyor.
    _atlama_alarmi(results, tickers, verbose=verbose)

    # En az bir cagri basardiysa kredi freni gereksiz -> kaldir (bakiye yuklenmis).
    if any(not r.get("skipped") for r in results if isinstance(r, dict)):
        kredi_freni_kaldir()

    # TOKEN OZET — run() SENKRON tek-tek yol (batch fallback). Bu yol STANDART
    # fiyata calisir (batch %50 indirimi YOK) -> batch=False ile logla. Boylece
    # batch'e dusen gunler artik gercek (~2x) maliyetle loglanir, log eksik
    # gostermez. (Iyilestirme 3c.)
    maliyet.logla(usage_acc, MODEL, batch=False,
                  tarih=datetime.now(_TZ).strftime("%Y-%m-%d %H:%M"))
    return results


def main():
    tickers = sys.argv[1:] or ["THYAO", "GARAN", "ASELS", "KCHOL", "TUPRS"]
    print(f"Tam analiz zinciri: {tickers}\n")
    run(tickers)


if __name__ == "__main__":
    main()
