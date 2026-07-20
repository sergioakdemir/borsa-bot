"""Haber→hisse→etki katmanı (17 Tem 2026 gölge · 20 Tem 2026 canlı bildirim).

BOTUN VARLIK SEBEBI: haberi önden yakalayıp aksiyona çevirmek. Ama yanlış
kurulursa tehlikeli (her "savaş" kelimesine panik AL). Bu yüzden katman önce
GÖLGE modda çalıştı; korumaları doğrulandıktan sonra (20 Tem 2026) eski
`run_alerts._sektor_haber_tarama` sisteminin YERİNE kullanıcıya bildirim
göndermeye başladı.

  * KARAR MOTORUNA HÂLÂ ETKI ETMEZ. decisions tablosuna hiçbir şey yazmaz,
    sabah brifingini/karar akışını değiştirmez, v2.1 karar motorunu bozmaz.
    Değişen tek şey ÇIKTI: artık panelin yanında Telegram'a da gidiyor.
  * `haber_sinyal` tablosuna kaydeder, panelde "Bugünün Haber Sinyalleri"
    olarak gösterir ve `bildir()` ile seans saatlerinde Telegram'a yollar.

NASIL ÇALIŞIR (kural tablosu + AI etki):
  1. RSS havuzu (24s) taranır.
  2. KONU_KURALLARI ile her haber KONU/SEKTÖR/EMTIA bazlı hisselere bağlanır
     (isim-bazlı `mentions`'ın kaçırdığı petrol/Hürmüz/altın haberleri buradan
     yakalanır). Eşleşen hisseler watchlist ile kesiştirilir.
  3. Eşleşen (hisse, haber) için AI'a sorulur: yön (yukarı/aşağı/belirsiz),
     güç (zayıf/orta/güçlü), FIYATLANMIS mı (evet/kısmen/hayır — haber çıkınca
     hisse çoktan hareket ettiyse geç kalınmış). Fiyatlanma teyidi için hissenin
     günlük % hareketi (fiyat_cache) AI'a verilir.
  4. Gölge karar deterministik türetilir: yön=yukarı + güç>=orta + fiyatlanmamış
     -> gölge AL; aksi halde BEKLE.

Çalıştırma:
    python -m src.news.haber_sinyal tara          # bugünün haberlerini işle
    python -m src.news.haber_sinyal goster        # bugünün sinyallerini yaz
    python -m src.news.haber_sinyal bildir        # CANLI: Telegram'a gönder
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_TZ = ZoneInfo("Europe/Istanbul")
_CACHE = ROOT / "data" / "fiyat_cache.json"

# Ucuz + hizli etki analizi (commentary.HABER_MODEL ile ayni tercih).
_MODEL = "claude-haiku-4-5"

# KONU/SEKTOR/EMTIA -> tetik kelimeler + etkilenen hisseler.
# run_alerts.SEKTOR_HABER_KURALLARI'nin GENISLETILMIS ve DUZELTILMIS surumu:
#  - "namlu" (silah namlusu, petrolle alakasiz) cikarildi.
#  - Hurmuz/Hormuz, akaryakit, dogalgaz petrol konusuna eklendi.
#  - Altin/Madencilik, Havacilik, Celik konulari eklendi (IS 2 recall boslugu).
# Kelimeler NORMALIZE (kucuk + tr->ascii) yazilir; eslesme KELIME BASINA cengelli
# (bkz. _konular): '\bpetrol fiyat' -> 'petrol fiyatlari'ni da yakalar.
KONU_KURALLARI = [
    {"konu": "Petrol / Brent",
     "kelimeler": ["brent", "ham petrol", "petrol fiyat", "petrol varil", "opec",
                   "hurmuz", "hormuz", "petrol uretim", "akaryakit", "dogalgaz",
                   "petrol arz", "petrol talep", "varil"],
     "hisseler": ["TUPRS", "PETKM", "AYGAZ"]},
    {"konu": "Savunma",
     "kelimeler": ["kaan", "milli muharip", "insansiz hava", "siha", "jet motoru",
                   "savunma sanayi", "savunma bakanlig", "msb", "ssb", "roketsan",
                   "savunma ihrac", "savunma ihale", "savunma sozlesme", "nato"],
     "hisseler": ["ASELS", "OTKAR"]},
    {"konu": "Faiz / TCMB",
     "kelimeler": ["tcmb", "merkez bankas", "politika faiz", "faiz indir",
                   "faiz artir", "ppk", "faiz karar", "enflasyon"],
     "hisseler": ["GARAN", "AKBNK", "YKBNK", "ISCTR", "HALKB", "VAKBN",
                  "SKBNK", "QNBTR"]},
    {"konu": "Döviz / Dolar",
     "kelimeler": ["dolar kuru", "dolar/tl", "doviz kuru", "kurda", "kur rekor",
                   "dolar rekor", "devaluasyon", "tl deger kayb", "tl deger kayip"],
     "hisseler": ["ASELS", "FROTO", "TOASO", "EREGL", "TUPRS", "KRDMD"]},
    {"konu": "Altın / Madencilik",
     "kelimeler": ["altin fiyat", "altin rekor", "ons altin", "gram altin",
                   "altin ons", "kiymetli metal", "altin uretim"],
     "hisseler": ["KOZAL", "KOZAA"]},
    {"konu": "Havacılık / Yolcu",
     "kelimeler": ["hava yolu", "havayolu", "yolcu sayis", "ucak trafik",
                   "havalimani yolcu", "jet yakit", "ucus trafik"],
     "hisseler": ["THYAO", "PGSUS", "TAVHL", "CLEBI"]},
    {"konu": "Çelik / Emtia",
     "kelimeler": ["celik fiyat", "demir cevheri", "celik uretim", "celik ihrac",
                   "demir celik"],
     "hisseler": ["EREGL", "KRDMD"]},
]

# ANA OYUNCU sektor-alaka (kalibrasyon 3): bir hisse KENDI ana sektorunun
# haberinde "etkisi sinirli/dolayli" diye zayiflatilamaz. Ana oyuncuysa alaka
# GUCLU'dur. (17 Tem 2026: AI, ASELS'i (savunma ana oyuncusu) IHA ihalesinde
# "dolayli, guc=zayif" damgaladi -> yanlis.)
ANA_OYUNCULAR = {
    "ASELS": "Türkiye'nin ANA savunma elektroniği ve İHA/SİHA sistemleri üreticisi",
    "OTKAR": "ana askeri kara aracı üreticisi",
    "TUPRS": "Türkiye'nin ANA petrol rafinericisi",
    "PETKM": "ana petrokimya üreticisi",
    "AYGAZ": "ana LPG dağıtıcısı",
    "GARAN": "büyük ölçekli özel mevduat bankası",
    "AKBNK": "büyük ölçekli özel mevduat bankası",
    "YKBNK": "büyük ölçekli özel mevduat bankası",
    "ISCTR": "büyük ölçekli özel mevduat bankası",
    "HALKB": "büyük kamu mevduat bankası",
    "VAKBN": "büyük kamu mevduat bankası",
    "QNBTR": "büyük ölçekli mevduat bankası",
    "SKBNK": "orta ölçekli mevduat bankası",
    "EREGL": "Türkiye'nin ANA yassı çelik üreticisi",
    "KRDMD": "ana uzun çelik üreticisi",
    "KOZAL": "ana altın madencisi",
    "KOZAA": "ana madencilik şirketi",
    "THYAO": "Türkiye'nin ANA havayolu taşıyıcısı",
    "PGSUS": "ana düşük maliyetli havayolu",
    "TAVHL": "ana havalimanı işletmecisi",
    "FROTO": "ana otomotiv üreticisi (ihracat ağırlıklı)",
    "TOASO": "ana otomotiv üreticisi (ihracat ağırlıklı)",
}


# Sektor-bazli etki: TEK AI cagrisiyla hem SEKTOR yonu hem her hisse degerlendirilir
# (kalibrasyon 2: ayni haber ayni sektoru keyfi bolmesin; kalibrasyon 3: ana
# oyuncu zayiflatilmasin; kalibrasyon 1: TUPRS gibi cift-etkili hisselerde baskin
# mekanizma sorulur + yon-gerekce tutarli olsun).
_SEKTOR_ETKI_SCHEMA = {
    "type": "object",
    "properties": {
        "sektor_yon": {"type": "string", "enum": ["yukari", "asagi", "karisik"]},
        "sektor_gerekce": {"type": "string"},
        "hisseler": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker": {"type": "string"},
                    "yon": {"type": "string", "enum": ["yukari", "asagi", "belirsiz"]},
                    "guc": {"type": "string", "enum": ["zayif", "orta", "guclu"]},
                    "fiyatlanmis": {"type": "string",
                                    "enum": ["evet", "kismen", "hayir"]},
                    "baskin_mekanizma": {"type": "string"},
                    "gerekce": {"type": "string"},
                },
                "required": ["ticker", "yon", "guc", "fiyatlanmis", "gerekce"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["sektor_yon", "sektor_gerekce", "hisseler"],
    "additionalProperties": False,
}

# Yon-gerekce tutarlilik kontrolu (kalibrasyon 1): gerekce baskin olumsuz dil
# tasirken yon=yukari (veya tersi) ise -> CELISKILI, dusuk guven.
_OLUMSUZ_IZ = ("sikis", "daral", "baski", "maliyet artir", "maliyet yuksel",
               "olumsuz", "negatif", "zarar", "dusur", "gerile", "asind",
               "yuk artir", "borc yuk", "marj dus", "kar dus", "karlilik dus",
               "kar azal", "aleyhine", "baskila")
_OLUMLU_IZ = ("artar", "iyiles", "olumlu", "pozitif", "destekl", "kazanc",
              "yukselt", "kar artir", "karlilik artir", "marj iyiles",
              "fayda", "guclen", "lehine", "prim")


def _load_dotenv():
    """ANTHROPIC_API_KEY vb. .env'den yukle (standalone/cron kosusu icin;
    systemd zaten yukluyor ama setdefault ile cakismaz)."""
    import os
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _bugun() -> str:
    return datetime.now(_TZ).date().isoformat()


def _norm(s: str) -> str:
    # rss_source._norm ile ayni (bagimsiz kopya — import donguselligini onler).
    s = s or ""
    for a, b in (("İ", "i"), ("I", "ı")):
        s = s.replace(a, b)
    s = s.lower()
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"),
                 ("ü", "u"), ("ö", "o"), ("ç", "c"), ("â", "a")):
        s = s.replace(a, b)
    return s


def _konular(text: str) -> list:
    """Metindeki eslesen konu kurallari (kelime basina \\b cengelli)."""
    import re
    n = _norm(text)
    out = []
    for kural in KONU_KURALLARI:
        for kw in kural["kelimeler"]:
            if re.search(r"\b" + re.escape(_norm(kw)), n):
                out.append(kural)
                break
    return out


def _watchlist() -> set:
    try:
        from src.watchlist import load_index
        return {t.upper().replace(".IS", "") for t in load_index()}
    except Exception:
        return set()


def _fiyat_bilgi(ticker: str) -> tuple:
    """(gunluk_%, mutlak_fiyat) fiyat_cache'ten. Ag cagrisi yok; yoksa (None,None).
    gunluk: fiyatlanmis teyidi icin; mutlak: golge isabet takibi (IS 4) icin."""
    try:
        with _CACHE.open(encoding="utf-8") as f:
            d = json.load(f)
        kayit = d.get(ticker) or d.get(ticker + ".IS")
        if kayit:
            g = kayit.get("gunluk")
            fi = kayit.get("fiyat")
            return (float(g) if isinstance(g, (int, float)) else None,
                    float(fi) if isinstance(fi, (int, float)) else None)
    except Exception:
        pass
    return (None, None)


def _haber_hash(baslik: str) -> str:
    import hashlib
    return hashlib.sha256(_norm(baslik).encode("utf-8")).hexdigest()[:16]


def _golge_karar(yon: str, guc: str, fiyatlanmis: str, guven: str = "normal") -> str:
    """Gölge karar (yalniz kayit — CANLI KARARA ETKI ETMEZ).
    Panik AL'i onlemek icin muhafazakar: yon yukari VE guc>=orta VE tam
    fiyatlanmamis VE guven dusuk degil. Aksi halde BEKLE. Celiskili (yon-gerekce
    tutarsiz) sinyal AL veremez."""
    if guven == "dusuk":
        return "BEKLE"
    if yon == "yukari" and guc in ("orta", "guclu") and fiyatlanmis != "evet":
        return "AL"
    return "BEKLE"


# ---------------------------------------------------------------------------
# İŞ 2: DETERMİNİSTİK FİYATLANMIŞLIK ÖLÇÜMÜ
# ---------------------------------------------------------------------------
# "Fiyatlanmis mi" artik yalniz AI'in oznel yorumu degil; hissenin son 1-3
# gundeki fiyat hareketi + hacmi SAYISAL olculur ve AI'i DESTEKLER (ezmez):
#   - Haberle AYNI yonde zaten BUYUK hareket olduysa -> "fiyatlanmis"
#     (gec kalindi) -> guven dusurulur.
#   - Hisse AZ LIKIT ve hareket henuz KUCUK ise -> "erken_drift"
#     (drift potansiyeli, fiyatlanmamis say) -> guven dokunulmaz.
#   - Aksi halde "notr".
# Arastirma dayanagi: piyasa haberi onceden fiyatlar AMA kucuk/likit-olmayan
# hisselerde drift (haber yonunde suren hareket) gunlerce surebilir.
_FIYATLANMIS_ESIK = 6.0      # ayni-yon 1-3g kumulatif hareket bu %'yi astiysa fiyatlanmis
_DRIFT_ESIK = 3.0            # az-likit + |hareket| bu %'nin altindaysa erken_drift
_LIKIDITE_ESIK_TL = 50_000_000   # gunluk ort. TL ciro < bu -> "az_likit"
_OLCUM_GUN = 3              # kumulatif hareket penceresi (takvim degil, islem bari)


def _index_seti() -> set:
    """Ana BIST endeks hisseleri (likit kabul edilir)."""
    try:
        from src.watchlist import load_index
        return {t.upper().replace(".IS", "") for t in load_index()}
    except Exception:
        return set()


def _fiyat_hareket_ham(ticker: str) -> dict:
    """yfinance ~20 islem gunu ile ham olcum: 1-3g kumulatif %, hacim kati,
    likidite. Ag cagrisi; hata/veri yoksa alanlar None. (tara() ici memolanir.)"""
    out = {"hareket": None, "hacim_kati": None, "likidite": None}
    try:
        from datetime import timedelta as _td
        from src.markets.bist import BIST
        from src.data.factory import get_data_source
        sym = BIST().to_symbol(ticker)
        start = (datetime.now(_TZ).date() - _td(days=20)).isoformat()
        df = get_data_source().get_history(sym, start=start)
        if df is None or getattr(df, "empty", True) or len(df) < 3:
            return out
        closes = [float(x) for x in df["Close"].tolist() if x == x and x]
        vols = [float(x) for x in df["Volume"].tolist() if x == x]
        if len(closes) < 3:
            return out
        n = min(_OLCUM_GUN, len(closes) - 1)
        baz = closes[-1 - n]
        if baz:
            out["hareket"] = round((closes[-1] - baz) / baz * 100, 2)
        onceki = vols[:-1] or vols
        avg_vol = (sum(onceki) / len(onceki)) if onceki else None
        if avg_vol and vols:
            out["hacim_kati"] = round(vols[-1] / avg_vol, 2)
        ciro = (avg_vol * closes[-1]) if avg_vol else None
        likit = (ticker in _index_seti()) or (ciro is not None and ciro >= _LIKIDITE_ESIK_TL)
        out["likidite"] = "likit" if likit else "az_likit"
    except Exception:
        return out
    return out


def _fiyatlanmislik_etiket(ham: dict, yon: str) -> str:
    """Ham olcumu + haber yonunu birlestirip deterministik etiket uretir.
      fiyatlanmis  : haberle AYNI yonde zaten >=_FIYATLANMIS_ESIK hareket (gec kalindi)
      erken_drift  : az-likit + |hareket| < _DRIFT_ESIK (drift potansiyeli, erken)
      notr         : arada
      veri_yok     : olcum alinamadi"""
    hareket = ham.get("hareket")
    if hareket is None:
        return "veri_yok"
    ayni_yon = (yon == "yukari" and hareket > 0) or (yon == "asagi" and hareket < 0)
    if ayni_yon and abs(hareket) >= _FIYATLANMIS_ESIK:
        return "fiyatlanmis"
    if ham.get("likidite") == "az_likit" and abs(hareket) < _DRIFT_ESIK:
        return "erken_drift"
    return "notr"


def _celiski_mi(yon: str, gerekce: str) -> bool:
    """Yon ile gerekce metni celisiyor mu? (kalibrasyon 1 emniyet agi)
    gerekce baskin OLUMSUZ dil tasirken yon=yukari, veya baskin OLUMLU dil
    tasirken yon=asagi -> celiskili (dusuk guven damgasi)."""
    n = _norm(gerekce or "")
    olumsuz = sum(1 for k in _OLUMSUZ_IZ if k in n)
    olumlu = sum(1 for k in _OLUMLU_IZ if k in n)
    if yon == "yukari" and olumsuz > olumlu:
        return True
    if yon == "asagi" and olumlu > olumsuz:
        return True
    return False


def _ai_sektor_etki(konu: str, baslik: str, ozet: str, hisseler: list,
                    hareketler: dict, client=None) -> dict | None:
    """TEK AI cagrisiyla bir haberin bir SEKTORDEKI hisselere etkisi.

    Kalibrasyon:
      2) Once SEKTOR yonu belirlenir; ayni sektordeki hisseler keyfi bolunmez
         (hisse-ozel fark ancak SOMUT sebeple).
      3) Ana oyuncu hisse KENDI sektorunun haberinde zayiflatilamaz (alaka guclu).
      1) Rafineri gibi CIFT-ETKILI hisselerde baskin mekanizma sorulur ve yon
         gerekce ile tutarli olur.
    """
    hisse_satir = []
    for t in hisseler:
        rol = ANA_OYUNCULAR.get(t, "sektör oyuncusu")
        hrk = hareketler.get(t)
        hrk_txt = f"bugün %{hrk:+.1f}" if hrk is not None else "hareket verisi yok"
        hisse_satir.append(f"  - {t}: {rol} ({hrk_txt})")
    petrol_not = ""
    if konu.startswith("Petrol"):
        petrol_not = (
            "\nÖNEMLI — RAFINERI ÇIFT ETKISI: Petrol fiyatı yükselişi rafineri/dağıtıcı "
            "(TUPRS, AYGAZ) için ÇIFT yönlüdür: (a) jeopolitik/fiyat primi ve stok değer "
            "artışı YUKARI iter, (b) ham madde maliyeti/marj sıkışması AŞAĞI iter. Her "
            "rafineri hissesi için HANGISININ BASKIN olduğunu 'baskin_mekanizma'da belirt "
            "ve 'yon'u ona göre ver. gerekce ile yon ÇELİŞMESİN.")
    sys_p = (
        "Sen bir finans-haberi etki analistisin. Bir haber ve etkilediği SEKTÖRdeki "
        "hisseler verilir.\n"
        "1) Önce haberin bu SEKTÖR üzerindeki BASKIN yönünü belirle (sektor_yon: "
        "yukari/asagi/karisik) ve tek cümle gerekçele.\n"
        "2) Sonra her hisse için yon/guc/fiyatlanmis/gerekce ver. Net bir haberse "
        "aynı sektördeki hisseler AYNI yönde olmalı; bir hisseyi farklı yöne koyacaksan "
        "gerekçede SOMUT sebebini yaz (yoksa sektör yönünü uygula). Yönü hissenin "
        "günlük fiyat hareketine göre DEĞİL, haberin mekanizmasına göre belirle.\n"
        "3) ANA OYUNCU olarak tanımlanan hisse KENDI sektörünün haberinde 'etkisi "
        "sınırlı/dolaylı' diye zayıflatılamaz — ana oyuncuysa alaka GÜÇLÜdür.\n"
        "FIYATLANMIS: haberdeki hareket zaten olduysa 'evet', kısmen 'kismen', "
        "olmadıysa 'hayir'. Her gerekce TEK KISA cümle." + petrol_not)
    kullanici = (f"Haber başlığı: {baslik}\nÖzet: {(ozet or '')[:400]}\n"
                 f"Sektör/konu: {konu}\nEtkilenen hisseler ve rolleri:\n"
                 + "\n".join(hisse_satir))
    # Çift-sağlayıcı yönlendirici: ÖNCE Anthropic; Anthropic hata verirse ve NVIDIA
    # anahtarı varsa NVIDIA yedeğine düşer (is_tipi="golge" -> yedeğe UYGUN). Ana
    # AL/SAT kararı bu yoldan GEÇMEZ (o commentary._ai_verdict'te, saf Anthropic).
    from src.ai import saglayici
    return saglayici.json_cagir(
        sys_p, kullanici, _SEKTOR_ETKI_SCHEMA, max_tokens=1500,
        is_tipi="golge", anthropic_model=_MODEL, client=client)


def _kayit_var(c, tarih: str, ticker: str, h: str) -> bool:
    r = c.execute("SELECT 1 FROM haber_sinyal WHERE tarih=? AND ticker=? "
                  "AND haber_hash=?", (tarih, ticker, h)).fetchone()
    return r is not None


# Hisse+konu basina GUNLUK sinyal cap'i. Ayni banka icin 5 ayri "faiz" haberi ayni
# temayi tekrarlar -> gurultu; ayni hisse+konu icin gunde en fazla bu kadar sinyal
# tutulur. Boylece banka faiz tekrari, petrol/Hurmuz/savunma gibi DIGER konularin
# sinyal butcesini yemez (17 Tem 2026: 40'lik global cap banka faiziyle dolup
# Hurmuz+Iran petrol haberlerini dusuruyordu).
_KONU_BASINA_MAX = 3


def tara(rss=None, verbose: bool = True, limit_haber: int = 120) -> dict:
    """GÖLGE tarama: RSS havuzu -> konu eslestirme -> AI etki -> haber_sinyal.
    CANLI KARARA DOKUNMAZ. Yeni yazilan sinyal sayisini + ozet doner."""
    from src.db import database as db
    _load_dotenv()
    db.init_db()
    tarih = _bugun()
    watch = _watchlist()

    if rss is None:
        from src.news.rss_source import RSSNewsSource
        rss = RSSNewsSource()
    try:
        entries = rss._all_entries()
    except Exception as e:
        if verbose:
            print(f"[haber_sinyal] RSS havuzu alinamadi: {type(e).__name__}")
        return {"yeni": 0, "islenen": 0, "hata": str(e)}

    # (hisse, haber) ciftlerini topla. Dedup: (1) ayni gun ayni haber+hisse bir kez,
    # (2) hisse+konu basina gunluk _KONU_BASINA_MAX cap (banka faiz tekrari digerlerini
    # ezmesin). Cap'e MEVCUT DB kayitlari da katilir -> gun ici tekrar kosu sismesin.
    from src.db import database as _dbm
    konu_say = {}                            # (ticker, konu) -> bugunku sinyal sayisi
    try:
        with _dbm.get_conn() as _c:
            for r in _c.execute("SELECT ticker,konu,COUNT(*) n FROM haber_sinyal "
                                "WHERE tarih=? GROUP BY ticker,konu", (tarih,)):
                konu_say[(r["ticker"], r["konu"])] = r["n"]
    except Exception:
        pass
    # (haber, konu) grupla: ayni haber+sektordeki TUM hisseler TEK AI cagrisina
    # girsin -> sektor yon tutarliligi (kalibrasyon 2). Cap yine hisse+konu bazinda.
    from src.news.temizle import haber_temizle, karantina_logla
    gruplar = {}                             # (hash, konu) -> {baslik,ozet,link,tickerlar}
    seen = set()
    toplam_cift = 0
    karantina_say = 0
    for e in entries[:200]:
        # İŞ 1: adversarial temizleme — haber golge sinyale girmeden ONCE.
        # (rss_source girişte zaten temizliyor; burada da savunma-derinligi icin
        # yeniden dogrulanir, boylece kaynak degisse de katman izole korunur.)
        tr = haber_temizle(e.get("baslik", ""), e.get("ozet", ""), e.get("kaynak"))
        if tr["karantina"]:
            karantina_logla(e.get("kaynak"), tr["nedenler"], e.get("baslik", ""))
            karantina_say += 1
            continue                         # supheli haber GOLGE SINYALE GIRMEZ
        baslik_t, ozet_t = tr["baslik"], tr["ozet"]
        text = f"{baslik_t} {ozet_t}"
        konular = _konular(text)
        if not konular:
            continue
        h = _haber_hash(baslik_t)
        for kural in konular:
            for tic in kural["hisseler"]:
                if watch and tic not in watch:
                    continue
                anahtar = (tic, h)
                if anahtar in seen:
                    continue
                ck = (tic, kural["konu"])
                if konu_say.get(ck, 0) >= _KONU_BASINA_MAX:
                    continue                 # hisse+konu gunluk cap doldu
                seen.add(anahtar)
                konu_say[ck] = konu_say.get(ck, 0) + 1
                g = gruplar.setdefault((h, kural["konu"]), {
                    "baslik": baslik_t, "ozet": ozet_t,
                    "link": e.get("link"), "tickerlar": []})
                g["tickerlar"].append(tic)
                toplam_cift += 1
        if toplam_cift >= limit_haber:
            break

    yeni = 0
    islenen = 0
    olcum_cache = {}                         # İŞ 2: ticker -> ham fiyat olcumu (memo)
    with db.get_conn() as c:
        for (h, konu), g in gruplar.items():
            kalan = [t for t in g["tickerlar"] if not _kayit_var(c, tarih, t, h)]
            if not kalan:
                continue                     # bu haber+sektor bugun zaten islendi
            hareketler, fiyatlar = {}, {}
            for t in kalan:
                hareketler[t], fiyatlar[t] = _fiyat_bilgi(t)
            islenen += len(kalan)
            sonuc = _ai_sektor_etki(konu, g["baslik"], g["ozet"], kalan, hareketler)
            if not sonuc:
                continue
            per = {d.get("ticker"): d for d in sonuc.get("hisseler", [])}
            zaman = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
            for t in kalan:
                d = per.get(t)
                if not d:
                    continue                 # AI bu hisseyi dondurmedi
                yon, guc, fy = d["yon"], d["guc"], d["fiyatlanmis"]
                gerekce = d.get("gerekce", "")
                # İŞ 2: deterministik fiyatlanmislik olcumu (AI'i DESTEKLER, ezmez).
                ham = olcum_cache.get(t)
                if ham is None:
                    ham = _fiyat_hareket_ham(t)
                    olcum_cache[t] = ham
                fs = _fiyatlanmislik_etiket(ham, yon)
                guven = "dusuk" if _celiski_mi(yon, gerekce) else "normal"
                if fs == "fiyatlanmis":
                    guven = "dusuk"          # buyuk olcude fiyatlanmis -> guveni dusur
                karar = _golge_karar(yon, guc, fy, guven)
                c.execute(
                    "INSERT OR IGNORE INTO haber_sinyal "
                    "(tarih,ticker,konu,baslik,link,haber_hash,yon,guc,fiyatlanmis,"
                    " golge_karar,gerekce,fiyat_hareket,fiyat_sinyal,guven,"
                    " baskin_mekanizma,fiyat_hareket_yuzde,hacim_kati,"
                    " fiyatlanmislik_sayisal,sonuc,getiri_yuzde,olusturma) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (tarih, t, konu, g["baslik"][:300], g["link"], h,
                     yon, guc, fy, karar, gerekce[:300], hareketler.get(t),
                     fiyatlar.get(t), guven, (d.get("baskin_mekanizma") or "")[:200],
                     ham.get("hareket"), ham.get("hacim_kati"), fs,
                     None, None, zaman))
                yeni += 1
    # IS 4a: gunluk icerik denetimi — havuz/eslesme snapshot (ayar tablosuna).
    try:
        _denetim_kaydet(entries, tarih, karantina_say)
    except Exception:
        pass
    if verbose:
        kt = f", {karantina_say} karantina" if karantina_say else ""
        print(f"[haber_sinyal] GÖLGE tarama: {toplam_cift} eslesme ({len(gruplar)} "
              f"haber-sektör grubu), {islenen} yeni islendi, {yeni} sinyal yazildi"
              f"{kt} (tarih={tarih})")
    return {"yeni": yeni, "islenen": islenen, "eslesme": toplam_cift,
            "gruplar": len(gruplar), "karantina": karantina_say, "tarih": tarih}


# ---------------------------------------------------------------------------
# IS 4a: GUNLUK ICERIK DENETIMI — "kac haber eslesti / kac cope gitti"
# ---------------------------------------------------------------------------
def _denetim_kaydet(entries, tarih, karantina=0):
    """Gunun eslesme snapshot'ini ayar tablosuna yazar: havuz, isim-bazli eslesen,
    konu-bazli eslesen, karantina (İŞ 1 supheli haber). health_monitor bunu
    okuyup deger kaybini alarma cevirir."""
    from src.db import database as db
    from src.news.rss_source import mentions
    watch = _watchlist()
    havuz = len(entries)
    konu_esles = isim_esles = 0
    for e in entries:
        text = f"{e.get('baslik','')} {e.get('ozet','')}"
        if _konular(text):
            konu_esles += 1
        if watch and any(mentions(text, t) for t in watch):
            isim_esles += 1
    snap = {"havuz": havuz, "konu_esles": konu_esles, "isim_esles": isim_esles,
            "karantina": karantina}
    db.set_setting(f"haber_denetim:{tarih}", json.dumps(snap))


def denetim_ozeti(tarih: str = None) -> dict:
    """Gunun eslesme snapshot'i (panel/karne icin). Yoksa canli hesaplar."""
    from src.db import database as db
    tarih = tarih or _bugun()
    try:
        ham = db.get_setting(f"haber_denetim:{tarih}")
        if ham:
            return json.loads(ham)
    except Exception:
        pass
    return {"havuz": None, "konu_esles": None, "isim_esles": None}


# ---------------------------------------------------------------------------
# IS 4b: GOLGE ISABET TAKIBI — sinyaller sonradan dogru mu cikti?
# ---------------------------------------------------------------------------
_ISABET_ESIK = 1.5     # AL icin: bu %'den fazla YUKARI -> isabet; asagi -> iskalama
_KACIRMA_ESIK = 3.0    # BEKLE icin: bu %'den fazla YUKARI kacirdiysa -> iskalama


def _gun_farki(t1: str, t2: str) -> int:
    from datetime import date
    try:
        a = date.fromisoformat(t1); b = date.fromisoformat(t2)
        return (a - b).days
    except Exception:
        return 0


def sonuclandir(min_gun: int = 1, verbose: bool = True) -> dict:
    """`min_gun` (takvim gunu) once uretilmis, henuz sonuclanmamis golge sinyalleri
    guncel fiyatla degerlendirir. getiri = (guncel-sinyal_ani)/sinyal_ani.
      AL   : getiri >= +%1.5 -> isabet | <= -%1.5 -> iskalama | arasi notr
      BEKLE: getiri <= +%1.5 -> isabet (dogru bekledi) | >= +%3 -> iskalama (kacirdi)
    CANLI KARARA ETKI ETMEZ — yalniz golge tablosunu doldurur."""
    from src.db import database as db
    bugun = _bugun()
    guncel_fiyat = {}
    n = 0
    with db.get_conn() as c:
        rows = list(c.execute(
            "SELECT id,ticker,tarih,golge_karar,fiyat_sinyal FROM haber_sinyal "
            "WHERE sonuc IS NULL AND fiyat_sinyal IS NOT NULL"))
        for r in rows:
            if _gun_farki(bugun, r["tarih"]) < min_gun:
                continue                     # henuz olgunlasmadi
            tic = r["ticker"]
            if tic not in guncel_fiyat:
                _, guncel_fiyat[tic] = _fiyat_bilgi(tic)
            gf = guncel_fiyat[tic]
            if not gf or not r["fiyat_sinyal"]:
                continue
            getiri = (gf - r["fiyat_sinyal"]) / r["fiyat_sinyal"] * 100
            if r["golge_karar"] == "AL":
                sonuc = ("isabet" if getiri >= _ISABET_ESIK else
                         ("iskalama" if getiri <= -_ISABET_ESIK else "notr"))
            else:   # BEKLE
                sonuc = ("iskalama" if getiri >= _KACIRMA_ESIK else "isabet")
            c.execute("UPDATE haber_sinyal SET sonuc=?, getiri_yuzde=? WHERE id=?",
                      (sonuc, round(getiri, 2), r["id"]))
            n += 1
    if verbose:
        print(f"[haber_sinyal] {n} golge sinyal sonuclandirildi (>= {min_gun} gun)")
    return {"sonuclanan": n}


def isabet_ozeti() -> dict:
    """Sonuclanmis golge sinyallerin isabet karnesi (golge katman canliya deger mi?).
    AL sinyalleri asil olcu — 'haber-AL dedi, sonra yukseldi mi?'."""
    from src.db import database as db
    out = {"al": {}, "bekle": {}, "toplam_sonuclanan": 0}
    try:
        with db.get_conn() as c:
            rows = list(c.execute(
                "SELECT golge_karar,sonuc,getiri_yuzde FROM haber_sinyal "
                "WHERE sonuc IS NOT NULL"))
    except Exception:
        return out
    for grup, karar in (("al", "AL"), ("bekle", "BEKLE")):
        g = [r for r in rows if r["golge_karar"] == karar]
        isabet = sum(1 for r in g if r["sonuc"] == "isabet")
        iskalama = sum(1 for r in g if r["sonuc"] == "iskalama")
        notr = sum(1 for r in g if r["sonuc"] == "notr")
        degerli = isabet + iskalama       # notr disi (yon netlesen)
        ort_getiri = (sum(r["getiri_yuzde"] or 0 for r in g) / len(g)) if g else None
        out[grup] = {
            "toplam": len(g), "isabet": isabet, "iskalama": iskalama, "notr": notr,
            "isabet_oran": (isabet / degerli * 100) if degerli else None,
            "ort_getiri": round(ort_getiri, 2) if ort_getiri is not None else None,
        }
    out["toplam_sonuclanan"] = len(rows)
    return out


def ticker_sinyalleri(ticker: str, tarih: str = None) -> list[dict]:
    """CANLI (Is 1, 21 Tem 2026): ANA karar motoruna beslemek icin bir hissenin
    BUGUNku gölge haber sinyalleri (jeopolitik/makro/sektor). Amac: ana motorun
    petrol/Iran gibi haberleri GORMESI (korluk duzeltmesi). KARARI ZORLAMAZ —
    yalniz baglam; AI kendi degerlendirir. Konu basina en yeni kayit, max 4."""
    from src.db import database as db
    tarih = tarih or _bugun()
    tk = (ticker or "").upper().replace(".IS", "")
    try:
        with db.get_conn() as c:
            rows = c.execute(
                "SELECT konu,baslik,yon,guc,fiyatlanmis,golge_karar,gerekce,"
                "fiyatlanmislik_sayisal,fiyat_hareket_yuzde "
                "FROM haber_sinyal WHERE tarih=? AND ticker=? ORDER BY id DESC",
                (tarih, tk)).fetchall()
    except Exception:
        return []
    out, seen = [], set()
    for r in rows:
        d = dict(r)
        if d["konu"] in seen:
            continue
        seen.add(d["konu"])
        out.append({
            "konu": d["konu"], "baslik": (d["baslik"] or "")[:140],
            "yon": d["yon"], "guc": d["guc"],
            "haberde_fiyatlanmis": d["fiyatlanmis"],
            "fiyatlanmislik_olcum": d["fiyatlanmislik_sayisal"],
            "son3g_hareket_%": d["fiyat_hareket_yuzde"],
            "ozet": (d["gerekce"] or "")[:200],
        })
        if len(out) >= 4:
            break
    return out


def bugun_sinyaller(tarih: str = None) -> list[dict]:
    """Panel/rapor icin: bir gunun gölge haber sinyalleri."""
    from src.db import database as db
    tarih = tarih or _bugun()
    try:
        with db.get_conn() as c:
            rows = c.execute(
                "SELECT ticker,konu,baslik,link,yon,guc,fiyatlanmis,golge_karar,"
                "gerekce,fiyat_hareket,guven,baskin_mekanizma,"
                "fiyat_hareket_yuzde,hacim_kati,fiyatlanmislik_sayisal,sonuc "
                "FROM haber_sinyal WHERE tarih=? "
                "ORDER BY CASE golge_karar WHEN 'AL' THEN 0 ELSE 1 END, ticker",
                (tarih,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# CANLI BILDIRIM (20 Tem 2026) — golge katman kullaniciya cikti veriyor
# ---------------------------------------------------------------------------
# Bu katman 17 Tem'e kadar GOLGE'ydi (yalniz DB + panel). Eski canli sistem
# (run_alerts._sektor_haber_tarama) duzeltilmemis oldugu icin EMEKLI edildi ve
# yerini burasi aldi. Golgede dogrulanmis korumalar bildirimde de gecerli:
#   * karantina  : supheli haber zaten tara()'da elenir, sinyale hic girmez.
#   * guven      : celiskili (yon-gerekce tutarsiz) VEYA buyuk olcude
#                  fiyatlanmis sinyaller "dusuk" damgalidir -> GONDERILMEZ.
#   * ana oyuncu : ASELS gibi sektor ana oyuncusu kendi haberinde zayiflatilmaz.
#   * gece       : borsa kapaliyken (hafta sonu/resmi tatil/seans disi) HIC
#                  bildirim gitmez — PORTFOY ISTISNASI YOK. Eski sistemin
#                  portfoy istisnasi 03:30'da mesaj siziyordu.
# Kritik sistem alarmlari (kredi/cokme) bu yoldan GECMEZ; onlar
# telegram.notify_admins uzerinden gece de gidebilir.

# Yalniz bu guven seviyeleri kullaniciya gider ("dusuk" ve NULL disarida).
BILDIRIM_GUVEN = ("normal", "yuksek")

# Tek mesajda en fazla kac sinyal blogu (mesaj sismesin).
_BILDIRIM_MAX_BLOK = 8

_YON_IKON = {"yukari": "🟢", "asagi": "🔴", "belirsiz": "⚪"}


def _bildirim_blogu(s: dict) -> str:
    """Bir sinyal kaydini Telegram HTML blogu haline getirir."""
    baslik = s["baslik"] or ""
    if s.get("link"):
        baslik = f'<a href="{s["link"]}">{baslik}</a>'
    ikon = _YON_IKON.get(s.get("yon"), "⚪")
    blok = (f"{ikon} <b>{s['ticker']}</b> — {s['konu']}\n{baslik}")
    if s.get("gerekce"):
        blok += f"\n{s['gerekce']}"
    # Fiyatlanmislik: deterministik olcum (AI'in oznel yorumu degil).
    fs = s.get("fiyatlanmislik_sayisal")
    hrk = s.get("fiyat_hareket_yuzde")
    if fs == "erken_drift":
        blok += f"\n<i>Hareket henüz küçük (%{hrk:+.1f}) — erken.</i>" if hrk is not None \
            else "\n<i>Hareket henüz küçük — erken.</i>"
    elif fs == "notr" and hrk is not None:
        blok += f"\n<i>Son 3 gün: %{hrk:+.1f}</i>"
    return blok


def bildir(now=None, verbose: bool = True) -> int:
    """Bugunun haber sinyallerini Telegram'a gonderir. Kac kullaniciya gitti doner.

    Gonderim kapilari (sirayla): borsa acik mi -> Telegram yapili mi ->
    guven normal/yuksek mi -> kullanici bu hisseyi izliyor mu (filtre.should_notify)
    -> bu sinyal bu kullaniciya bugun gonderildi mi (dedup).
    """
    now = now or datetime.now(_TZ)
    _load_dotenv()
    from src.piyasa_takvim import borsa_acik

    # KAPI 1 — GECE SUSTURMA: borsa kapaliysa (hafta sonu, resmi tatil, seans
    # disi saat) hicbir haber bildirimi gitmez. Istisna YOK.
    if not borsa_acik(now, "bist"):
        if verbose:
            print(f"[{now:%Y-%m-%d %H:%M}] [haber-bildir] borsa kapali — "
                  f"bildirim yok (sinyaller kayitli, panelde gorunur).")
        return 0

    from src.notify import telegram
    if not telegram.is_configured():
        if verbose:
            print(f"[{now:%Y-%m-%d %H:%M}] [haber-bildir] Telegram yapilandirilmamis.")
        return 0

    from src.db import database as db
    from src.notify import filtre
    tarih = _bugun()

    # KAPI 2 — GUVEN: celiskili/fiyatlanmis (guven=dusuk) sinyaller elenir.
    try:
        with db.get_conn() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT id,ticker,konu,baslik,link,yon,guc,gerekce,guven,"
                "       golge_karar,fiyat_hareket_yuzde,fiyatlanmislik_sayisal "
                "FROM haber_sinyal WHERE tarih=? "
                "ORDER BY CASE golge_karar WHEN 'AL' THEN 0 ELSE 1 END, ticker",
                (tarih,))]
    except Exception as e:
        print(f"[haber-bildir] DB okuma hatasi: {type(e).__name__}")
        return 0
    sinyaller = [s for s in rows if (s.get("guven") or "") in BILDIRIM_GUVEN]
    elenen = len(rows) - len(sinyaller)
    if not sinyaller:
        if verbose:
            print(f"[{now:%Y-%m-%d %H:%M}] [haber-bildir] gonderilecek sinyal yok "
                  f"({len(rows)} kayit, {elenen} dusuk-guven elendi).")
        return 0

    try:
        from src.watchlist import load_watchlist
        watch = {(t or "").upper().replace(".IS", "") for t in load_watchlist()}
    except Exception:
        watch = set()
    try:
        users = db.list_users()
    except Exception:
        users = []

    gonderim = 0
    for u in users:
        tg = u.get("telegram_id")
        if not tg:
            continue
        uid = u["id"]
        try:
            pf = {(p.get("ticker") or "").upper().replace(".IS", "")
                  for p in db.list_portfolio(uid) if p.get("ticker")}
        except Exception:
            pf = set()
        izlenen = pf | watch
        bloklar, kayitlar = [], []
        for s in sinyaller:
            tic = s["ticker"]
            if tic not in izlenen:
                continue
            # KAPI 3 — BILDIRIM GECIDI: portfoy disi hisseler icin genel kural.
            if not filtre.should_notify(tic, uid, "haber", portfoy=pf):
                continue
            # KAPI 4 — DEDUP: ayni sinyal ayni kullaniciya gun icinde bir kez.
            tok = f"HABERSINYAL:{uid}:{s['id']}"
            if tok in db.alert_levels_today(tic, tarih):
                continue
            bloklar.append(_bildirim_blogu(s))
            kayitlar.append((tic, tok))
            if len(bloklar) >= _BILDIRIM_MAX_BLOK:
                break
        if not bloklar:
            continue
        bas = f"<b>HABER SİNYALİ</b> — {now:%H:%M}"
        try:
            telegram.send_message(bas + "\n\n" + "\n\n".join(bloklar), chat_id=str(tg))
        except Exception as e:
            print(f"[haber-bildir] gonderim hatasi ({tg}): {type(e).__name__}")
            continue          # dedup YAZILMAZ -> sonraki kosuda yeniden denenir
        for tic, tok in kayitlar:
            db.record_alert(tic, tarih, tok, 0)
        gonderim += 1

    if verbose:
        print(f"[{now:%Y-%m-%d %H:%M}] [haber-bildir] {len(sinyaller)} uygun sinyal "
              f"({elenen} dusuk-guven elendi) -> {gonderim} kullaniciya bildirildi.")
    return gonderim


def _goster(tarih: str = None) -> None:
    sinyaller = bugun_sinyaller(tarih)
    if not sinyaller:
        print(f"[haber_sinyal] {tarih or _bugun()}: sinyal yok")
        return
    print(f"=== Bugünün Haber Sinyalleri ({tarih or _bugun()}) — GÖLGE ===")
    for s in sinyaller:
        fy = f" | fiyatlanmis: {s['fiyatlanmis']}" if s['fiyatlanmis'] else ""
        print(f"  {s['ticker']:6} ← {s['konu']:18} | gölge: {s['golge_karar']:5} "
              f"({s['yon']}/{s['guc']}{fy})")
        print(f"         {s['baslik'][:80]}")
        print(f"         gerekçe: {s['gerekce']}")


def main(argv) -> int:
    komut = argv[1] if len(argv) > 1 else "tara"
    if komut == "goster":
        _goster()
    elif komut == "bildir":            # CANLI: bugunun sinyallerini Telegram'a gonder
        bildir()
    elif komut == "sonuclandir":       # IS 4b: olgunlasmis sinyalleri degerlendir
        sonuclandir()
        import json as _j
        print("Isabet karnesi:", _j.dumps(isabet_ozeti(), ensure_ascii=False))
    elif komut == "isabet":
        import json as _j
        print(_j.dumps(isabet_ozeti(), ensure_ascii=False, indent=2))
    elif komut == "denetim":
        import json as _j
        print(_j.dumps(denetim_ozeti(), ensure_ascii=False, indent=2))
    else:
        tara()
        _goster()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
