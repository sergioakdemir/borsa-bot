"""Risk ajani: bir hisseye 1-10 risk skoru verir. 8+ ise VETO (islemi engeller).

Deterministiktir: gercek metriklerden (volatilite, gunluk bant, tazelik, veri
yeterliligi) hesaplanir; rakam uydurmaz. Ileride AI-destekli yapilabilir.
"""
import statistics
from dataclasses import dataclass, field

from .metrics import compute_metrics

VETO_THRESHOLD = 8


@dataclass
class RiskAssessment:
    score: int
    veto: bool
    factors: list[str] = field(default_factory=list)
    message: str = ""


def assess_risk(stock: dict, metrics: dict | None = None) -> RiskAssessment:
    metrics = metrics or compute_metrics(stock)
    bars = [b for b in stock.get("bars", []) if b.get("volume")]

    if "error" in metrics or len(bars) < 2:
        return RiskAssessment(10, True, ["Yetersiz veri"],
                              "Risk 10/10: veri yetersiz -> VETO.")

    closes = [b["close"] for b in bars]
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] * 100
            for i in range(1, len(closes)) if closes[i - 1]]
    vol = statistics.pstdev(rets) if len(rets) >= 2 else 0.0
    avg_range = sum((b["high"] - b["low"]) / b["close"] * 100
                    for b in bars if b["close"]) / len(bars)

    score = 1.0
    factors = []

    vp = min(vol * 1.5, 7.0)
    score += vp
    factors.append(f"Volatilite (gunluk getiri std) %{vol:.2f} -> +{vp:.1f}")

    rp = min(avg_range / 2.0, 2.0)
    score += rp
    factors.append(f"Ortalama gunluk bant %{avg_range:.2f} -> +{rp:.1f}")

    status = stock.get("freshness", {}).get("status")
    if status == "STALE":
        score += 3
        factors.append("Veri bayat (STALE) -> +3")
    if len(bars) < 5:
        score += 1
        factors.append(f"Az islem bari ({len(bars)}) -> +1")

    score = max(1, min(10, round(score)))
    veto = score >= VETO_THRESHOLD
    msg = f"Risk {score}/10." + (f" VETO (>= {VETO_THRESHOLD}): islem engellendi." if veto else "")
    return RiskAssessment(score, veto, factors, msg)
