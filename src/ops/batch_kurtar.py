"""Zaman asimina ugrayan Anthropic batch'inin sonucunu geri kurtarir.

NEDEN (30 Tem 2026)
  ABD brifingi 15:30'da 17 istegi batch'e gonderdi. Batch normalde 120-210 sn'de
  biter; o gun Anthropic tarafinda ~4 saat 50 dk surdu. Kod 1800 sn (30 dk)
  bekleyip vazgecti, 17 hissenin hepsini "Batch sonuc gelmedi (timeout)" diye
  isaretledi ve brifingi IPTAL etti. Oysa batch daha sonra SORUNSUZ tamamlandi:
      succeeded=17, errored=0, ended_at=2026-07-30 17:20:52 UTC
  Yani is yapildi ve FATURALANDI; sonuc yalnizca hic alinmadigi icin cope gitti.
  Ustelik TOKEN OZET $0.0000 yazdi -> gunun maliyeti oldugundan az gorundu.

NE YAPAR
  run_batch zaman asimina ugradiginda batch kimligini + her istegin ctx'ini
  data/bekleyen_batch.json'a yazar (bkz. kaydet). Bu modul saat basi cron'la
  kosar, bekleyen batch'i sorar; bitmisse sonuclari cekip kararlari normal
  yoldan (_finalize_record + _persist) kaydeder ve gercek maliyeti loglar.

SINIRLAR (bilerek)
  - Brifing MESAJI yeniden gonderilmez. Saatler sonra "sabah brifingi" yollamak
    yaniltici olur; kurtarilan sey KARAR VERISIDIR (decisions + ai_commentary),
    boylece ogrenme/karne/portfoy zincirleri bosluk gormez. Admin bilgilendirilir.
  - Yalniz AYNI GUN gonderilen batch kurtarilir. Ertesi gunun brifingi zaten
    yeni kayit yazmis olur; eski sonucu ustune yazmak veriyi BOZAR -> atilir.
    (Anthropic batch'leri 24 saatte suresi doldugu icin pencere zaten bu.)

Calistir:  python -m src.ops.batch_kurtar [--kuru]      (--kuru: yazma, sadece raporla)
"""
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_TZ = ZoneInfo("Europe/Istanbul")
BEKLEYEN_PATH = ROOT / "data" / "bekleyen_batch.json"


# --- kayit / okuma --------------------------------------------------------

def _risk_det_ser(rd):
    """RiskAssessment dataclass'i -> dict (ctx icindeki tek JSON-disi nesne)."""
    if rd is None:
        return None
    try:
        import dataclasses
        return dataclasses.asdict(rd)
    except Exception:
        return None


def _risk_det_deser(d):
    """dict -> RiskAssessment. Bozuksa None (cagiran 'HESAPLANAMADI' yazar)."""
    if not isinstance(d, dict):
        return None
    try:
        from src.ai.risk import RiskAssessment
        return RiskAssessment(**d)
    except Exception:
        return None


def _yukle() -> list:
    try:
        veri = json.loads(BEKLEYEN_PATH.read_text(encoding="utf-8"))
        return veri if isinstance(veri, list) else []
    except Exception:
        return []


def _yaz(kayitlar: list) -> None:
    try:
        BEKLEYEN_PATH.parent.mkdir(exist_ok=True)
        BEKLEYEN_PATH.write_text(json.dumps(kayitlar, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    except Exception as e:
        print(f"  [kurtar] bekleyen_batch.json yazilamadi: {type(e).__name__}: {e}")


def kaydet(batch_id: str, ctxs: dict, order: list, etiket: str = "",
           model: str = "") -> None:
    """Zaman asimina ugrayan batch'i 'bekleyen' listesine ekler (run_batch cagirir).

    ctxs: {custom_id: ctx} — kurtarmada _finalize_record'a AYNEN geri verilir.
    order: [(custom_id, ticker)] — sonuclarin orijinal sirasi."""
    kayit = {
        "batch_id": batch_id,
        "etiket": etiket,
        "model": model,
        "gonderim": datetime.now(_TZ).isoformat(timespec="seconds"),
        "order": [list(x) for x in order],
        "ctxs": {},
    }
    for cid, ctx in (ctxs or {}).items():
        c = dict(ctx)
        c["risk_det"] = _risk_det_ser(c.get("risk_det"))
        kayit["ctxs"][cid] = c
    kayitlar = [k for k in _yukle() if k.get("batch_id") != batch_id]
    kayitlar.append(kayit)
    _yaz(kayitlar)
    print(f"  [batch] kurtarma icin saklandi: {batch_id} "
          f"({len(kayit['ctxs'])} istek) -> {BEKLEYEN_PATH.name}")


# --- kurtarma -------------------------------------------------------------

def _bugun_mu(iso: str) -> bool:
    try:
        return datetime.fromisoformat(iso).date() == datetime.now(_TZ).date()
    except Exception:
        return False


def _kurtar_tek(kayit: dict, client, yaz: bool = True) -> dict:
    """Tek bekleyen batch'i dener. Doner: {'durum':..., 'kurtarilan':n, 'mesaj':str}.
    durum: 'kurtarildi' | 'bekliyor' | 'dusuruldu' | 'hata'"""
    from src.ai import commentary as C
    from src.ai import maliyet

    bid = kayit.get("batch_id")
    if not _bugun_mu(kayit.get("gonderim", "")):
        return {"durum": "dusuruldu", "kurtarilan": 0,
                "mesaj": f"{bid}: gonderim gunu gecmis ({kayit.get('gonderim')}) — "
                         "yeni kayitlarin ustune yazmamak icin atildi."}
    try:
        durum = client.messages.batches.retrieve(bid).processing_status
    except Exception as e:
        return {"durum": "hata", "kurtarilan": 0,
                "mesaj": f"{bid}: durum sorulamadi ({type(e).__name__})"}
    if durum != "ended":
        return {"durum": "bekliyor", "kurtarilan": 0,
                "mesaj": f"{bid}: hala {durum} — sonraki kosuda tekrar denenecek."}

    ctxs = kayit.get("ctxs") or {}
    final, acc = {}, maliyet.bos_acc()
    hata = 0
    for res in client.messages.batches.results(bid):
        ctx = ctxs.get(res.custom_id)
        if ctx is None:
            continue
        ctx = dict(ctx)
        ctx["risk_det"] = _risk_det_deser(ctx.get("risk_det"))
        if res.result.type != "succeeded":
            hata += 1
            continue
        try:
            msg = res.result.message
            maliyet.ekle(acc, getattr(msg, "usage", None))
            text = next((b.text for b in msg.content if b.type == "text"), "")
            final[res.custom_id] = C._finalize_record(ctx, C.Verdict(**json.loads(text)))
        except Exception as e:
            hata += 1
            print(f"  [kurtar] {ctx.get('ticker')}: cozumlenemedi "
                  f"({type(e).__name__}: {str(e)[:60]})")

    if not final:
        return {"durum": "dusuruldu", "kurtarilan": 0,
                "mesaj": f"{bid}: batch bitti ama kurtarilabilir sonuc yok "
                         f"({hata} hatali)."}

    results = [final[cid] for cid, _t in kayit.get("order", []) if cid in final]
    if yaz:
        C._persist(results, save=True, verbose=True)
        # GERCEK maliyet — ETIKETLI loglanir. kredi_takip yalnizca etiketsiz
        # "TOKEN OZET:" satirlarini toplar; zaman asiminda zaten TAHMINI maliyet
        # etiketsiz yazildi. Etiketli yazmak CIFT SAYIMI onler, kayit ise gorunur
        # kalir (tahmin ile gercek arasindaki fark buradan denetlenir).
        maliyet.logla(acc, kayit.get("model") or C.MODEL,
                      etiket=f"batch kurtarma{(' ' + kayit['etiket']) if kayit.get('etiket') else ''}",
                      batch=True,
                      tarih=datetime.now(_TZ).strftime("%Y-%m-%d %H:%M"))
    return {"durum": "kurtarildi", "kurtarilan": len(results),
            "mesaj": f"{bid}: {len(results)} karar kurtarildi"
                     f"{f', {hata} hatali' if hata else ''}."}


def kurtar(yaz: bool = True, verbose: bool = True) -> dict:
    """Bekleyen tum batch'leri dener. Doner: {'kurtarilan': n, 'bekleyen': n}."""
    kayitlar = _yukle()
    if not kayitlar:
        if verbose:
            print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] bekleyen batch yok.")
        return {"kurtarilan": 0, "bekleyen": 0}

    from src.news.haber_sinyal import _load_dotenv
    _load_dotenv()
    import anthropic
    client = anthropic.Anthropic()

    kalan, toplam, mesajlar = [], 0, []
    for kayit in kayitlar:
        sonuc = _kurtar_tek(kayit, client, yaz=yaz)
        if verbose:
            print(f"  [kurtar] {sonuc['mesaj']}")
        mesajlar.append(sonuc["mesaj"])
        if sonuc["durum"] == "bekliyor":
            kalan.append(kayit)
        toplam += sonuc["kurtarilan"]
    if yaz:
        _yaz(kalan)

    # Admin'e yalniz DEGISIKLIK varsa haber ver (bekliyor -> sessiz, spam olmasin).
    if yaz and toplam:
        try:
            from src.notify import telegram
            telegram.notify_admins(
                f"Zaman asimina ugrayan batch kurtarildi: {toplam} karar geri "
                f"alindi ve kaydedildi (faturasi zaten odenmisti). Brifing mesaji "
                f"YENIDEN GONDERILMEZ; kararlar veri tabanina islendi.\n"
                + "\n".join(f"· {m}" for m in mesajlar), prefix="♻️")
        except Exception as e:
            print(f"  [kurtar] admin bildirimi gonderilemedi: {type(e).__name__}")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] kurtarma bitti: "
              f"{toplam} karar, {len(kalan)} batch hala bekliyor.")
    return {"kurtarilan": toplam, "bekleyen": len(kalan)}


if __name__ == "__main__":
    kurtar(yaz="--kuru" not in sys.argv)
