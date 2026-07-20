"""GÖLGE kural backtest (21 Tem 2026) — Is 2 yeni-katalizor isabet olcumu.

CANLI DEGIL: golge_kurallar.golge_karar_v2'yi gecmis haber_sinyal kayitlarina
uygular, v1 AL demedigi ama v2'nin AL/AL_KISMI dedigi 'fliplar'i bulur ve bu
sinyallerin GERCEKLESEN getirisini (getiri_yuzde) olcer. Amac: yeni kural para
kazandirir mi yoksa v1 momentum-kovalama hatasini mi tekrarlar (isabet).

Kullanim:  venv/bin/python -m scripts.golge_backtest [BASLANGIC_TARIHI]
"""
import sys
from src.db import database as db
from src.news import golge_kurallar as gk


def calistir(baslangic: str = "2026-07-07") -> None:
    with db.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT ticker,tarih,konu,baslik,yon,guc,golge_karar,"
            "fiyatlanmislik_sayisal,fiyat_hareket_yuzde,getiri_yuzde "
            "FROM haber_sinyal WHERE tarih>=? ORDER BY tarih", (baslangic,))]
    print(f"toplam sinyal (>= {baslangic}): {len(rows)}")

    flips = []
    for d in rows:
        v2, ger = gk.golge_karar_v2(d["yon"], d["guc"], d["fiyatlanmislik_sayisal"],
                                    d["fiyat_hareket_yuzde"], d["baslik"])
        if v2 in ("AL", "AL_KISMI") and d["golge_karar"] != "AL":
            flips.append((d, v2))
    print(f"v1 AL-DEGIL iken v2 AL/AL_KISMI (yeni-katalizor kazanimi): {len(flips)}")

    olgun = [f for f in flips if f[0].get("getiri_yuzde") is not None]
    if olgun:
        g = [f[0]["getiri_yuzde"] for f in olgun]
        poz = sum(1 for x in g if x > 0)
        print(f"  olgunlasmis: {len(olgun)} | ORT getiri {sum(g)/len(g):+.2f}% | "
              f"pozitif {poz}/{len(olgun)} ({100*poz//len(olgun)}%)")
    for d, v2 in flips[:15]:
        gg = d.get("getiri_yuzde")
        gs = f"{gg:+.1f}%" if gg is not None else "olgunlasmadi"
        print(f"    {d['tarih']} {d['ticker']:7} {(d['konu'] or '')[:14]:14} "
              f"3g={d['fiyat_hareket_yuzde']} -> {v2:8} | getiri: {gs}")

    # KONTROL: v2 mevcut v1 AL'lari bozmamali
    v1al = [d for d in rows if d["golge_karar"] == "AL"]
    bozulan = sum(1 for d in v1al if gk.golge_karar_v2(
        d["yon"], d["guc"], d["fiyatlanmislik_sayisal"],
        d["fiyat_hareket_yuzde"], d["baslik"])[0] == "BEKLE")
    print(f"  KONTROL: v1 AL ({len(v1al)}) icinde v2 BEKLE'e dusen: {bozulan} (0 beklenir)")


if __name__ == "__main__":
    calistir(sys.argv[1] if len(sys.argv) > 1 else "2026-07-07")
