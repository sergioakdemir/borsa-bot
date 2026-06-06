"""THYAO icin haber filtresi testi: tazelik + fiyatlanma.

Once canli KAP denenir; bu sunucudan engelliyse otomatik ornek kaynaga duser.
Fiyatlanma analizi her durumda GERCEK yfinance verisiyle yapilir.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from datetime import datetime
from zoneinfo import ZoneInfo

from src.news.kap_source import KAPSource
from src.news.sample_source import SampleNewsSource
from src.news.base import NewsSourceUnavailable
from src.news.analyze import analyze_news

_TZ = ZoneInfo("Europe/Istanbul")


def get_source():
    try:
        kap = KAPSource(timeout=15)
        kap.get_news("THYAO", limit=1)
        print("KAP CANLI kaynak kullaniliyor.\n")
        return kap
    except NewsSourceUnavailable as e:
        print(f"[UYARI] KAP canli kaynaga ulasilamadi -> ORNEK kaynaga dusuldu.")
        print(f"        Sebep: {e}\n")
        return SampleNewsSource()


def main():
    src = get_source()
    items = src.get_news("THYAO", limit=10)
    if getattr(src, "IS_SAMPLE", False):
        print("NOT: Haber basliklari/tarihleri ORNEKTIR; fiyatlanma analizi GERCEK fiyatladir.\n")

    now = datetime.now(_TZ)
    results = analyze_news(items, now=now)

    print(f"THYAO - {len(results)} haber | referans an: {now:%Y-%m-%d %H:%M %Z}")
    print("=" * 78)
    for r in results:
        it, fr, pi = r["item"], r["freshness"], r["priced_in"]
        if fr.status.value == "ESKI":
            verdict = "ATLA (eski haber - yeni sanilmasin)"
        elif pi.status == "FIYATLANDI":
            verdict = "BILGI ZATEN FIYATLANMIS"
        elif pi.status == "FIYATLANMADI":
            verdict = "TAKIP ET (taze + henuz fiyatlanmamis)"
        else:
            verdict = "VERI YOK"
        print(f"\n* {it.published_at:%Y-%m-%d %H:%M} | {it.title}")
        print(f"    tazelik    : {fr.status.value:6s} | {fr.message}")
        print(f"    fiyatlanma : {pi.status:12s} | {pi.message}")
        print(f"    FILTRE     : {verdict}")


if __name__ == "__main__":
    main()
