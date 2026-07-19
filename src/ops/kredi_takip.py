"""API kredi takibi + erken uyari (15 Tem 2026).

NEDEN ELLE YUKLEME KAYDI: Anthropic API'de kalan bakiyeyi donen bir uc YOKTUR.
Admin API (/v1/organizations/...) yalnizca uye/workspace/anahtar yonetimi ile
kullanim-maliyet raporu sunar; "kac dolar kaldi" sorusunu cevaplayan endpoint
yok — ustelik Admin API bireysel hesaplara kapali. Bu yuzden bakiye TAHMIN
edilir:

    kalan   = kaydedilen yukleme - yuklemeden sonraki harcama
    harcama = logs/briefing.log icindeki "TOKEN OZET ... tahmini_maliyet=$X"
              satirlarinin toplami (brifingin kendi olctugu gercek maliyet)
    gun     = kalan / son gunlerin ortalama gunluk maliyeti

Yukleme kaydedilmemisse tahmin YAPILMAZ (kayitli=False) — uydurma bir sayi
uretmek yerine durum acikca "bilinmiyor" doner.

Kullanim (kredi yukledikten sonra):
    python -m src.ops.kredi_takip yukle 50        # bugun 50$ yuklendi
    python -m src.ops.kredi_takip yukle 50 2026-07-15
    python -m src.ops.kredi_takip durum
"""
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Istanbul")
# TUM log'lar taranir, yalnizca briefing.log DEGIL: ABD brifingi briefing_us.log'a,
# alarm/haber cagrilari alerts.log'a yazar (15 Tem 2026: yalniz briefing.log
# okununca gunluk maliyet ~$0.29 eksik cikiyordu -> "daha cok gun var" yalani,
# yani tam olarak engellemeye calistigimiz yon). Glob sayesinde ileride eklenen
# log'lar da kendiliginden sayilir.
LOG_DIZIN = Path(__file__).resolve().parents[2] / "logs"

ORTALAMA_GUN = 7        # ortalama kac TAM gunden hesaplanir
PENCERE_GUN = 30        # kac gunluk gecmise bakilir (eski/ucuz donem sarkmasin)
TAM_GUN_ORANI = 0.50    # penceredeki en pahali gunun bu orani ustu = "tam gun"
UYARI_GUN = 7           # bu kadar veya daha az is gunu kaldiysa uyar

_BAKIYE_ANAHTAR = "ai_kredi_bakiye"
_TARIH_ANAHTAR = "ai_kredi_tarih"

_SATIR = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2})[^\]]*\]\s*TOKEN OZET:.*?tahmini_maliyet=\$([0-9.]+)"
)


def _bugun() -> str:
    return datetime.now(_TZ).date().isoformat()


def maliyet_gecmisi() -> dict:
    """logs/*.log -> {gun_iso: o gunun toplam $ maliyeti}.

    Bir gunde birden fazla kosu olabilir (BIST brifingi + ABD brifingi + haber/
    alarm cagrilari + elle tetikleme) ve bunlar AYRI log dosyalarina yazar —
    hepsi toplanir, yoksa maliyet oldugundan dusuk cikar.
    """
    gecmis: dict = {}
    try:
        dosyalar = sorted(LOG_DIZIN.glob("*.log"))
    except OSError:
        return {}
    for yol in dosyalar:
        try:
            with yol.open("r", encoding="utf-8", errors="replace") as f:
                for satir in f:
                    m = _SATIR.match(satir)
                    if not m:
                        continue
                    gun, tutar = m.group(1), m.group(2)
                    try:
                        gecmis[gun] = gecmis.get(gun, 0.0) + float(tutar)
                    except ValueError:
                        continue
        except OSError:
            continue
    return gecmis


def gunluk_ortalama(gun: int = ORTALAMA_GUN, haric: str = None) -> float:
    """Son `gun` TAM calisma gununun ortalama maliyeti. `haric` verilirse o gun
    (or. bugun) ortalamadan DISLANIR — anomali tespitinde bugunku sicramanin
    kendi normal-esigini sismesini onler.

    'Tam gun' = penceredeki en pahali gunun >=%50'si. Sabit bir dolar esigi ise
    yaramaz: maliyet profili zamanla degisti (Haziran ~$0.30/gun, Temmuz
    ~$1.80/gun) ve bot bozukken (8-12 Tem kadans arizasi) gunler $0.003-0.24
    arasi kaldi. Bu yarim/bozuk gunleri ortalamaya katmak yanma hizini dusuk
    gosterir -> "daha cok gun var" iyimser yalani; yani tam da engellemeye
    calistigimiz yon. Esigi veriden turetmek profil degisimine kendiliginden
    uyum saglar ve tahmini guvenli (temkinli) tarafta tutar.
    """
    gecmis = maliyet_gecmisi()
    if haric:
        gecmis = {g: v for g, v in gecmis.items() if g != haric}
    if not gecmis:
        return 0.0
    pencere = sorted(gecmis.items())[-PENCERE_GUN:]
    en_yuksek = max((v for _, v in pencere), default=0.0)
    if en_yuksek <= 0:
        return 0.0
    tam = [v for _, v in pencere if v >= en_yuksek * TAM_GUN_ORANI]
    if not tam:
        return 0.0
    son = tam[-gun:]
    return sum(son) / len(son)


def harcama(baslangic: str) -> float:
    """`baslangic` gunu DAHIL bugune kadarki toplam maliyet."""
    return sum(v for g, v in maliyet_gecmisi().items() if g >= baslangic)


# ---------------------------------------------------------------------------
# GERÇEK HARCAMA TAKİBİ (loglardan, KESİN — tahmin DEĞİL) — 19 Tem 2026
# ---------------------------------------------------------------------------
# "Kac dolar kaldi" bir TAHMINDI (Anthropic bakiye ucu vermez) ve otomatik
# yenileme ($5->$20) devrede oldugu icin hem YANLIS hem GEREKSIZ. Onun yerine
# loglardan KESIN gunluk + aylik harcama raporlanir (her cagrinin gercek
# maliyeti "TOKEN OZET ... tahmini_maliyet=$X" satirlarinda zaten var).
ANOMALI_KAT = 3.0        # gunluk maliyet normal ortalamanin bu katini asarsa uyari
ANOMALI_MIN = 3.0        # ...ve en az bu kadar $ ise (dusuk-hacim gunde yanlis alarm olmasin)


def _ay(tarih: str = None) -> str:
    return (tarih or _bugun())[:7]


def bugun_maliyet(tarih: str = None) -> float:
    """Bugunku (veya verilen gunun) GERCEK $ maliyeti — loglardan."""
    return round(maliyet_gecmisi().get(tarih or _bugun(), 0.0), 4)


def ay_maliyet(ay: str = None) -> float:
    """Bu ayin (YYYY-MM) GERCEK toplam $ maliyeti — loglardan."""
    ay = ay or _ay()
    return round(sum(v for g, v in maliyet_gecmisi().items() if g.startswith(ay)), 4)


def maliyet_anormal_mi(tarih: str = None):
    """Bugunku harcama normalin COK ustune cikti mi (kacak/dongu harcamasi)?
    Doner: None (normal) veya (bugun$, normal_ort$, esik$). Esik = normal
    ortalamanin ANOMALI_KAT kati VE en az ANOMALI_MIN — boylece hem oransal
    sicrama hem mutlak taban aranir (dusuk-hacim gunde yanlis alarm olmaz)."""
    tarih = tarih or _bugun()
    bugun = maliyet_gecmisi().get(tarih, 0.0)
    normal = gunluk_ortalama(haric=tarih)     # bugun haric -> kendi esigini sisirmesin
    if normal <= 0:
        return None
    esik = max(ANOMALI_KAT * normal, ANOMALI_MIN)
    if bugun > esik:
        return (round(bugun, 2), round(normal, 2), round(esik, 2))
    return None


def harcama_ozeti(tarih: str = None) -> dict:
    """GERCEK harcama ozeti (loglardan). 'kac dolar kaldi' TAHMINI DEGIL.
    otomatik_yenileme: $5->$20 Anthropic tarafinda aktif (bot dogrudan goremez;
    asil koruma kredi-bitti alarmidir — bkz. health_monitor._kontrol_kredi)."""
    tarih = tarih or _bugun()
    anomali = maliyet_anormal_mi(tarih)
    return {
        "bugun": bugun_maliyet(tarih),
        "ay": ay_maliyet(_ay(tarih)),
        "ay_etiket": _ay(tarih),
        "normal_gunluk": round(gunluk_ortalama(haric=tarih), 2),
        "anomali": bool(anomali),
        "anomali_detay": anomali,         # (bugun, normal, esik) veya None
        "otomatik_yenileme": True,
    }


def harcama_satir(d: dict = None) -> str:
    """Panel/karne icin tek satir GERCEK harcama."""
    d = d if d is not None else harcama_ozeti()
    s = f"bugün ${d['bugun']:.2f} · bu ay ${d['ay']:.2f}"
    if d.get("anomali"):
        s += f"  ⚠️ beklenmedik yüksek (normal ~${d['normal_gunluk']:.2f}/gün)"
    return s


def yukle(tutar: float, tarih: str = None) -> dict:
    """Kredi yuklemesini kaydeder. Bu andan sonraki harcama bu tutardan dusulur."""
    from src.db import database as db
    tarih = tarih or _bugun()
    db.set_setting(_BAKIYE_ANAHTAR, str(float(tutar)))
    db.set_setting(_TARIH_ANAHTAR, tarih)
    return durum()


def durum() -> dict:
    """Kredi tahmini. kayitli=False ise yukleme kaydi yok -> tahmin uretilmez."""
    from src.db import database as db
    try:
        ham = db.get_setting(_BAKIYE_ANAHTAR)
        tarih = db.get_setting(_TARIH_ANAHTAR)
    except Exception:
        ham, tarih = None, None

    ort = gunluk_ortalama()
    if not ham or not tarih:
        return {"kayitli": False, "gunluk_ort": ort, "kalan": None,
                "gun_kaldi": None, "bakiye": None, "tarih": None,
                "harcanan": None, "uyari": False}

    try:
        bakiye = float(ham)
    except ValueError:
        return {"kayitli": False, "gunluk_ort": ort, "kalan": None,
                "gun_kaldi": None, "bakiye": None, "tarih": None,
                "harcanan": None, "uyari": False}

    harcanan = harcama(tarih)
    kalan = bakiye - harcanan
    gun_kaldi = (kalan / ort) if ort > 0 else None
    return {
        "kayitli": True,
        "bakiye": bakiye,
        "tarih": tarih,
        "harcanan": harcanan,
        "kalan": kalan,
        "gunluk_ort": ort,
        "gun_kaldi": gun_kaldi,
        "uyari": gun_kaldi is not None and gun_kaldi <= UYARI_GUN,
    }


def ozet_satir(d: dict = None) -> str:
    """Panel/karne icin tek satir."""
    d = d if d is not None else durum()
    if not d["kayitli"]:
        return "takip kurulu degil (python -m src.ops.kredi_takip yukle <tutar>)"
    if d["gun_kaldi"] is None:
        return f"~${d['kalan']:.2f} kaldi (gunluk maliyet bilinmiyor)"
    return (f"~${d['kalan']:.2f} kaldi — ~{d['gun_kaldi']:.0f} is gunu "
            f"(gunluk ~${d['gunluk_ort']:.2f})")


def main(argv) -> int:
    komut = argv[1] if len(argv) > 1 else "durum"
    if komut == "yukle":
        if len(argv) < 3:
            print("kullanim: python -m src.ops.kredi_takip yukle <tutar> [YYYY-MM-DD]")
            return 1
        tarih = argv[3] if len(argv) > 3 else None
        d = yukle(float(argv[2]), tarih)
        print(f"Kaydedildi: ${d['bakiye']:.2f} @ {d['tarih']}")
    elif komut == "harcama":                 # GERCEK harcama (loglardan)
        h = harcama_ozeti()
        print(f"AI harcaması: {harcama_satir(h)}")
        print(f"  (normal ~${h['normal_gunluk']:.2f}/gün · otomatik yenileme aktif)")
        return 0
    else:
        d = durum()
    print(f"AI harcaması (gerçek): {harcama_satir()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
