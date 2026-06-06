"""AI yorumcu katmani.

GIRDI artik KOMPAKT: ham OHLCV barlari yerine onceden hesaplanmis on-sinyal +
filtreden gecmis haberler gonderilir (token tasarrufu).

Akis: Claude puan(1-10) + eminlik + gerekce uretir. Karar PUANDAN turetilir
(esik tablosu). Risk ajani 8+ ise VETO. STALE veride kill switch.
"""
import json
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .metrics import compute_metrics
from .presignal import build_presignal
from .decision import decision_from_score
from .risk import assess_risk

MODEL = "claude-opus-4-8"

SYSTEM = """Sen bir borsa verisi yorumcususun. Gorevin SADECE sana verilen ozet veriyi yorumlamak.

GIRDI:
- 'on_sinyal': onceden hesaplanmis kompakt teknik sinyaller (trend, degisim %, fiyat konumu, hacim sinyali, volatilite). Ham fiyat barlari verilmez. Bunlari yorumla.
- 'haberler': haber filtresinden gecmis (eski olmayan) bildirim basliklari; her birinde tazelik ve fiyatlanma (FIYATLANDI/FIYATLANMADI) durumu var. NITEL baglam olarak degerlendir. Liste bos olabilir.

KESIN KURALLAR:
- Sana verilmeyen HICBIR sayi, fiyat, oran veya tarih UYDURMA. Yalnizca girdideki degerleri kullan.
- Haber basliklari disinda detay, beklenti veya sayisal etki UYDURMA. 'FIYATLANDI' olan haber zaten fiyata yansimistir, puani sismeye birakma.
- Disaridan (internet, sektor, makro) HICBIR bilgi ekleme.

CIKTI:
- 'score': 1-10 puan. 10 = en olumlu teknik gorunum, 1 = en olumsuz. (Karari SEN verme; puandan otomatik turetilir.)
- 'eminlik': DUSUK / ORTA / YUKSEK. Veri ve sinyaller ne kadar net/yeterliyse o kadar yuksek; zayif hacim, az bar veya celiskili sinyalde DUSUK.
- 'gerekce': kullandigin her sayi/haber girdide mevcut olmali.
- 'gozlemler': sinyallerden ve (varsa) haberlerden cikarilan teknik gozlemler.

Bu teknik bir veri yorumudur, yatirim tavsiyesi DEGILDIR.
"""


class StockVerdict(BaseModel):
    score: int = Field(description="1-10 arasi puan; 10 en olumlu teknik gorunum")
    eminlik: Literal["DUSUK", "ORTA", "YUKSEK"] = Field(
        description="Yorumun eminlik seviyesi; sinyal/veri netligine gore")
    gerekce: str = Field(description="Sadece girdideki sayi/haberlere dayanan kisa gerekce")
    gozlemler: list[str] = Field(description="Sinyal ve haberlerden cikarilan gozlemler")


def evaluate_stock(stock: dict, news: list | None = None,
                   client: anthropic.Anthropic | None = None) -> dict:
    ticker = stock.get("ticker")
    symbol = stock.get("symbol")
    status = stock.get("freshness", {}).get("status")
    news = news or []

    # --- KILL SWITCH ---
    if status == "STALE":
        return {"ticker": ticker, "symbol": symbol, "freshness": status,
                "skipped": True, "reason": "STALE veri - kill switch, yorum yapilmadi.",
                "score": None, "eminlik": None, "decision": None, "final_decision": None,
                "haber_sayisi": len(news)}

    metrics = compute_metrics(stock)
    if "error" in metrics:
        return {"ticker": ticker, "symbol": symbol, "freshness": status,
                "skipped": True, "reason": f"Yetersiz veri - {metrics['error']}",
                "score": None, "eminlik": None, "decision": None, "final_decision": None,
                "haber_sayisi": len(news)}

    # --- KOMPAKT GIRDI: on-sinyal + haberler (ham bar YOK) ---
    payload = {"on_sinyal": build_presignal(stock), "haberler": news}

    client = client or anthropic.Anthropic()
    resp = client.messages.parse(
        model=MODEL, max_tokens=4000, thinking={"type": "adaptive"}, system=SYSTEM,
        messages=[{"role": "user", "content": (
            "Asagidaki ozet veriyi yorumla; 1-10 puan ve eminlik ver. "
            "Sadece bu veriyi kullan:\n\n" + json.dumps(payload, ensure_ascii=False, indent=2))}],
        output_format=StockVerdict,
    )
    v = resp.parsed_output

    dcode, dlabel = decision_from_score(v.score)
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
        "haber_sayisi": len(news), "haberler": news,
        "kullanilan_on_sinyal": payload["on_sinyal"],
    }
