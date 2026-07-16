"""GÖLGE haber→hisse→etki katmanı (17 Tem 2026).

BOTUN VARLIK SEBEBI: haberi önden yakalayıp aksiyona çevirmek. Ama yanlış
kurulursa tehlikeli (her "savaş" kelimesine panik AL). Bu yüzden bu katman
GÖLGE MODDA çalışır:

  * CANLI KARARA ETKI ETMEZ. decisions tablosuna hiçbir şey yazmaz, sabah
    brifingini/karar akışını değiştirmez, v2.1 test dönemini bozmaz.
  * Yalnız `haber_sinyal` tablosuna kaydeder ve panelde "Bugünün Haber
    Sinyalleri" olarak gösterir; kullanıcı doğru/yanlış kendisi değerlendirir.

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

Çalıştırma (gölge — canlıyı etkilemez):
    python -m src.news.haber_sinyal tara          # bugünün haberlerini işle
    python -m src.news.haber_sinyal goster        # bugünün sinyallerini yaz
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

_ETKI_SCHEMA = {
    "type": "object",
    "properties": {
        "yon": {"type": "string", "enum": ["yukari", "asagi", "belirsiz"]},
        "guc": {"type": "string", "enum": ["zayif", "orta", "guclu"]},
        "fiyatlanmis": {"type": "string", "enum": ["evet", "kismen", "hayir"]},
        "gerekce": {"type": "string"},
    },
    "required": ["yon", "guc", "fiyatlanmis", "gerekce"],
    "additionalProperties": False,
}


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


def _fiyat_hareket(ticker: str) -> float | None:
    """Hissenin gunluk % hareketi (fiyat_cache) — fiyatlanmis teyidi icin.
    Ag cagrisi yok; cache yoksa None."""
    try:
        with _CACHE.open(encoding="utf-8") as f:
            d = json.load(f)
        kayit = d.get(ticker) or d.get(ticker + ".IS")
        if kayit and isinstance(kayit.get("gunluk"), (int, float)):
            return float(kayit["gunluk"])
    except Exception:
        pass
    return None


def _haber_hash(baslik: str) -> str:
    import hashlib
    return hashlib.sha256(_norm(baslik).encode("utf-8")).hexdigest()[:16]


def _golge_karar(yon: str, guc: str, fiyatlanmis: str) -> str:
    """Gölge karar (yalniz kayit — CANLI KARARA ETKI ETMEZ).
    Panik AL'i onlemek icin muhafazakar: yon yukari VE guc>=orta VE tam
    fiyatlanmamis olmali; aksi halde BEKLE."""
    if yon == "yukari" and guc in ("orta", "guclu") and fiyatlanmis != "evet":
        return "AL"
    return "BEKLE"


def _ai_etki(ticker: str, baslik: str, ozet: str, konu: str,
             hareket: float | None, client=None) -> dict | None:
    """Bir haberin bu hisseye etkisini AI ile etiketle. Hata -> None."""
    try:
        import anthropic
        client = client or anthropic.Anthropic()
    except Exception:
        return None
    hareket_txt = (f"Hissenin bugünkü fiyat hareketi: %{hareket:+.1f}. "
                   "Haber bu hareketle zaten fiyatlanmış olabilir mi değerlendir."
                   if hareket is not None else
                   "Güncel fiyat hareketi verisi yok.")
    sys_p = (
        "Sen bir finans-haberi etki analistisin. Verilen haberin BELİRTİLEN HİSSE "
        "üzerindeki olası etkisini değerlendir. Abartma; dolaylı/zayıf ilişkide "
        "'zayif' ve 'belirsiz' kullan. FIYATLANMIS: haber çıktığında hisse o yönde "
        "çoktan hareket ettiyse 'evet' (geç kalınmış), kısmen hareket ettiyse "
        "'kismen', daha hareket etmediyse 'hayir'. gerekce TEK KISA cümle olsun.")
    kullanici = (f"Hisse: {ticker}\nKonu: {konu}\nHaber başlığı: {baslik}\n"
                 f"Özet: {(ozet or '')[:400]}\n{hareket_txt}")
    try:
        resp = client.messages.create(
            model=_MODEL, max_tokens=250, system=sys_p,
            messages=[{"role": "user", "content": kullanici}],
            output_config={"format": {"type": "json_schema",
                                      "schema": _ETKI_SCHEMA}})
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text)
    except Exception as e:
        print(f"  [haber_sinyal] {ticker}: AI etki alinamadi "
              f"({type(e).__name__}: {str(e)[:120]})")
        return None


def _kayit_var(c, tarih: str, ticker: str, h: str) -> bool:
    r = c.execute("SELECT 1 FROM haber_sinyal WHERE tarih=? AND ticker=? "
                  "AND haber_hash=?", (tarih, ticker, h)).fetchone()
    return r is not None


def tara(rss=None, verbose: bool = True, limit_haber: int = 40) -> dict:
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

    # (hisse, haber) ciftlerini topla (dedup: ayni gun ayni haber+hisse bir kez).
    gorevler = []
    seen = set()
    for e in entries[:200]:
        text = f"{e.get('baslik','')} {e.get('ozet','')}"
        konular = _konular(text)
        if not konular:
            continue
        h = _haber_hash(e.get("baslik", ""))
        for kural in konular:
            for tic in kural["hisseler"]:
                if watch and tic not in watch:
                    continue
                anahtar = (tic, h)
                if anahtar in seen:
                    continue
                seen.add(anahtar)
                gorevler.append({"ticker": tic, "konu": kural["konu"],
                                 "baslik": e.get("baslik", ""),
                                 "ozet": e.get("ozet", ""),
                                 "link": e.get("link"), "hash": h})
        if len(gorevler) >= limit_haber:
            break

    yeni = 0
    islenen = 0
    with db.get_conn() as c:
        for g in gorevler:
            if _kayit_var(c, tarih, g["ticker"], g["hash"]):
                continue                     # bugun zaten islendi
            islenen += 1
            hareket = _fiyat_hareket(g["ticker"])
            etki = _ai_etki(g["ticker"], g["baslik"], g["ozet"], g["konu"], hareket)
            if not etki:
                continue
            karar = _golge_karar(etki["yon"], etki["guc"], etki["fiyatlanmis"])
            c.execute(
                "INSERT OR IGNORE INTO haber_sinyal "
                "(tarih,ticker,konu,baslik,link,haber_hash,yon,guc,fiyatlanmis,"
                " golge_karar,gerekce,fiyat_hareket,sonuc,olusturma) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tarih, g["ticker"], g["konu"], g["baslik"][:300], g["link"],
                 g["hash"], etki["yon"], etki["guc"], etki["fiyatlanmis"],
                 karar, etki["gerekce"][:300], hareket, None,
                 datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")))
            yeni += 1
    if verbose:
        print(f"[haber_sinyal] GÖLGE tarama: {len(gorevler)} eslesme, "
              f"{islenen} yeni islendi, {yeni} sinyal yazildi (tarih={tarih})")
    return {"yeni": yeni, "islenen": islenen, "eslesme": len(gorevler),
            "tarih": tarih}


def bugun_sinyaller(tarih: str = None) -> list[dict]:
    """Panel/rapor icin: bir gunun gölge haber sinyalleri."""
    from src.db import database as db
    tarih = tarih or _bugun()
    try:
        with db.get_conn() as c:
            rows = c.execute(
                "SELECT ticker,konu,baslik,link,yon,guc,fiyatlanmis,golge_karar,"
                "gerekce,fiyat_hareket,sonuc FROM haber_sinyal WHERE tarih=? "
                "ORDER BY CASE golge_karar WHEN 'AL' THEN 0 ELSE 1 END, ticker",
                (tarih,)).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


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
    else:
        tara()
        _goster()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
