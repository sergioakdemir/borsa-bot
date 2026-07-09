"""Gerçek performans metrikleri (trades tablosundaki KAPALI işlemlerden).

trades tablosu commentary.py tarafından doldurulur (AL -> açılır, stop/hedef veya
SAT kararı -> kapanır). Bu modül kapanmış işlemlerin pnl_yuzde değerlerinden
profesyonel metrikleri hesaplar (hit rate, profit factor, expectancy, ...).

Trades tablosu birikene kadar değerler None/0 döner; karne sayfası bunu
"birikmekte" olarak gösterir.
"""


def get_performance_metrics(kullanici_id=None) -> dict:
    """Kapalı trade'lerden performans metriklerini hesaplar.

    kullanici_id verilirse o kullanıcının trade'leri; None ise tümü (kullanici_id=0
    sistem geneli dahil). Döner:
      kazanan_ort, kaybeden_ort, profit_factor, expectancy, max_drawdown,
      hit_rate, islem_sayisi, kazanan_sayisi, kaybeden_sayisi.
    """
    from src.db import database as db

    trades = db.list_trades(durum="kapali", kullanici_id=kullanici_id)
    pnls = [t["pnl_yuzde"] for t in trades
            if t.get("pnl_yuzde") is not None]

    acik_versiyon = acik_versiyon_ozet(kullanici_id)   # açık kitap v1/v2 kırılımı

    bos = {
        "islem_sayisi": len(trades),
        "kapali_sayisi": len(pnls),
        "kazanan_sayisi": 0,
        "kaybeden_sayisi": 0,
        "hit_rate": None,
        "kazanan_ort": None,
        "kaybeden_ort": None,
        "profit_factor": None,
        "expectancy": None,
        "max_drawdown": None,
        "acik_versiyon": acik_versiyon,
        "yeterli_veri": False,
    }
    if not pnls:
        return bos

    kazananlar = [p for p in pnls if p > 0]
    kaybedenler = [p for p in pnls if p < 0]

    kazanan_ort = round(sum(kazananlar) / len(kazananlar), 2) if kazananlar else 0.0
    kaybeden_ort = round(sum(kaybedenler) / len(kaybedenler), 2) if kaybedenler else 0.0

    toplam_kazanc = sum(kazananlar)
    toplam_kayip = abs(sum(kaybedenler))
    if toplam_kayip > 0:
        profit_factor = round(toplam_kazanc / toplam_kayip, 2)
    else:
        profit_factor = None        # hiç kayıp yoksa tanımsız (∞)

    hit_rate = round(len(kazananlar) / len(pnls), 4)
    expectancy = round(hit_rate * kazanan_ort - (1 - hit_rate) * abs(kaybeden_ort), 2)

    # max_drawdown: kümülatif getiri eğrisindeki en büyük tepe-dip farkı (gerçek
    # drawdown). İşlemleri kapanış sırasına diz, kümülatif PnL hesapla, zirveden en
    # derin geri çekilmeyi bul. (Eski hatalı hesap min(pnls) yalnız tek en kötü
    # işlemi gösteriyordu; ardışık zararların birikimini kaçırıyordu.)
    sirali = sorted(
        (t for t in trades if t.get("pnl_yuzde") is not None),
        key=lambda t: (str(t.get("kapanis_tarihi") or ""), t.get("id") or 0),
    )
    kumulatif = zirve = 0.0
    max_dd = 0.0
    for t in sirali:
        kumulatif += t["pnl_yuzde"]
        zirve = max(zirve, kumulatif)
        max_dd = min(max_dd, kumulatif - zirve)   # zirveden düşüş (<= 0)
    max_drawdown = round(max_dd, 2)         # kümülatif tepe-dip (yüzde puan)

    return {
        "islem_sayisi": len(trades),
        "kapali_sayisi": len(pnls),
        "kazanan_sayisi": len(kazananlar),
        "kaybeden_sayisi": len(kaybedenler),
        "hit_rate": hit_rate,
        "kazanan_ort": kazanan_ort,
        "kaybeden_ort": kaybeden_ort,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "max_drawdown": max_drawdown,
        "acik_versiyon": acik_versiyon,
        "yeterli_veri": True,
    }


def acik_versiyon_ozet(kullanici_id=None) -> dict:
    """AÇIK pozisyonları strateji sürümüne göre (v2/v1) kırar.

    Her sürüm için: açık pozisyon sayısı ve ortalama güncel K/Z (pnl_yuzde). Açık
    pozisyonların pnl_yuzde'si her gece update_trades tarafından güncel getiriyle
    yazılır; None olan pozisyon ortalamaya katılmaz ama sayıya girer.

    Döner: {"v2": {"sayi": X, "ort": Y|None}, "v1": {"sayi": Z, "ort": W|None}}.
    'v2' = 7 Temmuz 2026 paketiyle açılan yeni sistem; 'v1' = önceki (eski yük).
    """
    from src.db import database as db

    trades = db.list_trades(durum="acik", kullanici_id=kullanici_id)
    ozet = {}
    for v in ("v2", "v1"):
        grup = [t for t in trades if (t.get("strategy_version") or "v1") == v]
        pnls = [t["pnl_yuzde"] for t in grup
                if isinstance(t.get("pnl_yuzde"), (int, float))]
        ort = round(sum(pnls) / len(pnls), 2) if pnls else None
        ozet[v] = {"sayi": len(grup), "ort": ort}
    return ozet


def versiyon_ozet_satiri(kullanici_id=None) -> str | None:
    """acik_versiyon_ozet'ten tek satırlık karşılaştırma metni üretir (brifing/karne):
    'Yeni sistem (v2): X pozisyon, ort %Y | Eski yük (v1): Z pozisyon, ort %W'.
    Hiç açık pozisyon yoksa None."""
    ozet = acik_versiyon_ozet(kullanici_id)
    v2, v1 = ozet["v2"], ozet["v1"]
    if not v2["sayi"] and not v1["sayi"]:
        return None

    def _p(g):
        ort_s = f"ort %{g['ort']:+.1f}" if g["ort"] is not None else "ort —"
        return f"{g['sayi']} pozisyon, {ort_s}"

    return f"Yeni sistem (v2): {_p(v2)} | Eski yük (v1): {_p(v1)}"


if __name__ == "__main__":
    import json
    print(json.dumps(get_performance_metrics(), ensure_ascii=False, indent=2))
    print(json.dumps(acik_versiyon_ozet(), ensure_ascii=False, indent=2))
