"""AI yorumcu katmani.

Akis: Claude SADECE puan (1-10) + eminlik + gerekce uretir (rakam uydurmaz).
Karar PUANDAN turetilir (esik tablosu). Risk ajani 1-10 risk verir; 8+ ise VETO
nihai karari ezer.

KILL SWITCH: freshness=STALE veya yetersiz veride Claude cagrilmaz; hisse atlanir.
"""
import json
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .metrics import compute_metrics
from .decision import decision_from_score
from .risk import assess_risk

MODEL = "claude-opus-4-8"

SYSTEM = """Sen bir borsa verisi yorumcususun. Gorevin SADECE sana verilen sayisal veriyi yorumlamak.

KESIN KURALLAR:
- Sana verilmeyen HICBIR sayi, fiyat, oran, tarih veya hacim UYDURMA. Yalnizca girdideki degerleri kullan.
- Haber, sirket beklentisi, sektor bilgisi, makro yorum veya disaridan HICBIR bilgi ekleme.
- Yalnizca verilen OHLCV barlarini ve onceden hesaplanmis metrikleri yorumla.
- Sayisal hesabi sen yapma; metrikler hazir verildi, sen yorumla.

CIKTI:
- 'score': 1-10 arasi puan. 10 = veriye gore en olumlu teknik gorunum, 1 = en olumsuz.
  (AL/SAT kararini SEN verme; karar puandan otomatik turetilir, sen sadece dogru puanla.)
- 'eminlik': DUSUK / ORTA / YUKSEK. Veri ne kadar net ve yeterliyse o kadar yuksek;
  az bar, celiskili sinyal veya zayif hacimde DUSUK ver.
- 'gerekce': kullandigin her sayi girdide birebir mevcut olmali.
- 'gozlemler': veriden cikarilan teknik gozlemler.

Bu teknik bir veri yorumudur, yatirim tavsiyesi DEGILDIR.
"""


class StockVerdict(BaseModel):
    score: int = Field(description="1-10 arasi puan; 10 en olumlu teknik gorunum")
    eminlik: Literal["DUSUK", "ORTA", "YUKSEK"] = Field(
        description="Yorumun eminlik seviyesi; veri netligi/yeterliligine gore")
    gerekce: str = Field(description="Sadece girdideki sayilara dayanan kisa gerekce")
    gozlemler: list[str] = Field(description="Verilen veriden cikarilan teknik gozlemler")


def evaluate_stock(stock: dict, client: anthropic.Anthropic | None = None) -> dict:
    ticker = stock.get("ticker")
    symbol = stock.get("symbol")
    status = stock.get("freshness", {}).get("status")

    # --- KILL SWITCH: bayat veride yorum yapma ---
    if status == "STALE":
        return {"ticker": ticker, "symbol": symbol, "freshness": status,
                "skipped": True, "reason": "STALE veri - kill switch, yorum yapilmadi.",
                "score": None, "eminlik": None, "decision": None, "final_decision": None}

    metrics = compute_metrics(stock)
    if "error" in metrics:
        return {"ticker": ticker, "symbol": symbol, "freshness": status,
                "skipped": True, "reason": f"Yetersiz veri - {metrics['error']}",
                "score": None, "eminlik": None, "decision": None, "final_decision": None}

    payload = {"ticker": ticker, "symbol": symbol, "freshness": status,
               "hesaplanmis_metrikler": metrics, "ham_barlar": stock.get("bars", [])}

    client = client or anthropic.Anthropic()
    resp = client.messages.parse(
        model=MODEL, max_tokens=4000, thinking={"type": "adaptive"}, system=SYSTEM,
        messages=[{"role": "user", "content": (
            "Asagidaki hisse verisini yorumla; 1-10 puan ve eminlik ver. "
            "Sadece bu veriyi kullan:\n\n" + json.dumps(payload, ensure_ascii=False, indent=2))}],
        output_format=StockVerdict,
    )
    v = resp.parsed_output

    # --- Karar puandan turetilir ---
    dcode, dlabel = decision_from_score(v.score)

    # --- Risk ajani + veto ---
    risk = assess_risk(stock, metrics)
    if risk.veto:
        final_code, final_label = "VETO", f"VETO (risk {risk.score}/10) -> islem yok"
    else:
        final_code, final_label = dcode, dlabel

    return {
        "ticker": ticker, "symbol": symbol, "freshness": status, "skipped": False,
        "score": v.score, "eminlik": v.eminlik,
        "decision": dcode, "decision_label": dlabel,
        "risk": {"score": risk.score, "veto": risk.veto,
                 "factors": risk.factors, "message": risk.message},
        "vetoed": risk.veto,
        "final_decision": final_code, "final_label": final_label,
        "gerekce": v.gerekce, "gozlemler": v.gozlemler,
        "kullanilan_metrikler": metrics,
    }
