"""AI yorumcu katmani: hazir metrikleri Claude'a yorumlatip 1-10 puan ve
AL/TUT/SAT karari uretir. Model SADECE verilen sayilari kullanir; rakam uydurmaz.

KILL SWITCH: freshness=STALE ise Claude'a hic gidilmez; hisse atlanir.
"""
import json
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .metrics import compute_metrics

MODEL = "claude-opus-4-8"

SYSTEM = """Sen bir borsa verisi yorumcususun. Gorevin SADECE sana verilen sayisal veriyi yorumlamak.

KESIN KURALLAR:
- Sana verilmeyen HICBIR sayi, fiyat, oran, tarih veya hacim UYDURMA. Yalnizca girdideki degerleri kullan.
- Haber, sirket beklentisi, sektor bilgisi, makro yorum veya disaridan HICBIR bilgi ekleme.
- Yalnizca verilen OHLCV barlarini ve onceden hesaplanmis metrikleri (degisim %, donem yuksek/dusuk, hacim) yorumla.
- Sayisal hesabi sen yapma; metrikler zaten hesaplanmis halde verildi, sen yorumla.
- 1-10 arasi puan ver: 10 = veriye gore en olumlu teknik gorunum, 1 = en olumsuz.
- Karar uret: AL, TUT veya SAT.
- 'gerekce' alaninda kullandigin her sayi girdide birebir mevcut olmali.
- Bu teknik bir veri yorumudur, yatirim tavsiyesi DEGILDIR.
"""


class StockVerdict(BaseModel):
    score: int = Field(description="1-10 arasi puan; 10 en olumlu teknik gorunum")
    decision: Literal["AL", "TUT", "SAT"] = Field(description="Veriye dayali karar")
    gerekce: str = Field(description="Karari destekleyen kisa gerekce; sadece girdideki sayilara dayanir")
    gozlemler: list[str] = Field(description="Verilen veriden cikarilan teknik gozlemler")


def evaluate_stock(stock: dict, client: anthropic.Anthropic | None = None) -> dict:
    """Bir hisseyi yorumlar. STALE veride KILL SWITCH devreye girer:
    Claude'a hic gidilmez, 'skipped' isaretli sonuc doner."""
    ticker = stock.get("ticker")
    symbol = stock.get("symbol")
    status = stock.get("freshness", {}).get("status")

    # --- KILL SWITCH: bayat veride yorum yapma ---
    if status == "STALE":
        return {
            "ticker": ticker, "symbol": symbol, "freshness": status,
            "skipped": True,
            "reason": "STALE veri - kill switch devrede, yorum yapilmadi.",
            "score": None, "decision": None,
        }

    metrics = compute_metrics(stock)
    # yeterli bar yoksa da yorumlama
    if "error" in metrics:
        return {
            "ticker": ticker, "symbol": symbol, "freshness": status,
            "skipped": True,
            "reason": f"Yetersiz veri - {metrics['error']}",
            "score": None, "decision": None,
        }

    payload = {
        "ticker": ticker, "symbol": symbol, "freshness": status,
        "hesaplanmis_metrikler": metrics,
        "ham_barlar": stock.get("bars", []),
    }

    client = client or anthropic.Anthropic()
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=4000,
        thinking={"type": "adaptive"},
        system=SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                "Asagidaki hisse verisini yorumla, 1-10 puan ve AL/TUT/SAT karari ver. "
                "Sadece bu veriyi kullan, disaridan bilgi ekleme:\n\n"
                + json.dumps(payload, ensure_ascii=False, indent=2)
            ),
        }],
        output_format=StockVerdict,
    )

    v = resp.parsed_output
    return {
        "ticker": ticker, "symbol": symbol, "freshness": status,
        "skipped": False,
        "score": v.score, "decision": v.decision,
        "gerekce": v.gerekce, "gozlemler": v.gozlemler,
        "kullanilan_metrikler": metrics,
    }
