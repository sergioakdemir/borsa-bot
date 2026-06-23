"""Backtest motoru: tarihsel veride sistemin kararlarini simule eder, basari
oranini ve BIST100 karsilastirmasini hesaplar.

Basitlestirmeler (seffaflik): islem maliyeti/slipaj yok; sadece long/flat;
ileriye donuk getiri ufku sabit; karar puani deterministik skorlayicidan
(canli Claude degil). Getiriler NOMINAL (TRY).
"""
from collections import Counter

from ..data.factory import get_data_source
from ..ai.presignal import build_presignal
from ..ai.decision import decision_from_score
from ..ai.risk import assess_risk
from .scorer import deterministic_score

AL_KARARLARI = ("AL", "AL_TEMKINLI")
SAT_KARARLARI = ("SAT", "GUCLU_SAT", "AZALT", "UZAK_DUR")


def _bars_from_df(df, require_volume=True):
    if require_volume and not df.empty:
        df = df[df["Volume"] > 0]
    bars = []
    for ix, row in df.iterrows():
        bars.append({
            "date": ix.date().isoformat(),
            "open": float(row["Open"]), "high": float(row["High"]),
            "low": float(row["Low"]), "close": float(row["Close"]),
            "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
        })
    return bars


def backtest_stock(symbol, src, start, end, window=10, horizon=5):
    df = src.get_history(symbol, start=start, end=end)
    bars = _bars_from_df(df)
    closes = [b["close"] for b in bars]
    n = len(bars)
    if n < window + horizon + 2:
        return None

    decisions = [None] * n
    signals = []
    for t in range(window - 1, n - 1):
        win = bars[t - window + 1:t + 1]
        stock = {"symbol": symbol, "freshness": {"status": "RECENT"}, "bars": win}
        ps = build_presignal(stock)
        score = deterministic_score(ps)
        dcode, _ = decision_from_score(score)
        decision = "VETO" if assess_risk(stock).veto else dcode
        decisions[t] = decision
        if t + horizon < n:
            fwd = (closes[t + horizon] - closes[t]) / closes[t] * 100
            signals.append({"date": bars[t]["date"], "score": score,
                            "decision": decision, "fwd_return": fwd})

    # --- Basari orani: yonlu sinyaller ---
    actionable = [s for s in signals if s["decision"] in AL_KARARLARI + SAT_KARARLARI]
    hits = sum(
        1 for s in actionable
        if (s["decision"] in AL_KARARLARI and s["fwd_return"] > 0)
        or (s["decision"] in SAT_KARARLARI and s["fwd_return"] < 0)
    )
    hit_rate = round(hits / len(actionable) * 100, 1) if actionable else None

    # --- Strateji getirisi (long/flat) vs hisse al-tut ---
    strat = 1.0
    for t in range(window - 1, n - 1):
        ret = closes[t + 1] / closes[t] - 1
        pos = 1 if decisions[t] in AL_KARARLARI else 0
        strat *= (1 + pos * ret)
    buyhold = closes[-1] / closes[window - 1]

    return {
        "symbol": symbol, "bar": n,
        "period": [bars[window - 1]["date"], bars[-1]["date"]],
        "sinyal": len(signals), "yonlu_sinyal": len(actionable),
        "isabet": hits, "basari_orani_%": hit_rate,
        "strateji_getiri_%": round((strat - 1) * 100, 2),
        "al_tut_getiri_%": round((buyhold - 1) * 100, 2),
        "karar_dagilimi": dict(Counter(d for d in decisions if d)),
    }


def run_backtest(symbols, start="2024-01-01", end="2026-06-06",
                 window=10, horizon=5, benchmark="XU100.IS"):
    src = get_data_source()
    per = [r for r in (backtest_stock(s, src, start, end, window, horizon)
                       for s in symbols) if r]

    total_actionable = sum(p["yonlu_sinyal"] for p in per)
    total_hits = sum(p["isabet"] for p in per)
    agg_hit = round(total_hits / total_actionable * 100, 1) if total_actionable else None
    port_strat = round(sum(p["strateji_getiri_%"] for p in per) / len(per), 2)
    port_bh = round(sum(p["al_tut_getiri_%"] for p in per) / len(per), 2)

    # --- BIST100 al-tut (ayni aktif donem) ---
    bdf = src.get_history(benchmark, start=start, end=end)
    bbars = _bars_from_df(bdf, require_volume=False)
    bclose = [b["close"] for b in bbars]
    bench_bh = round((bclose[-1] / bclose[window - 1] - 1) * 100, 2) if len(bclose) > window else None

    return {
        "ayar": {"start": start, "end": end, "window": window, "horizon": horizon,
                 "benchmark": benchmark},
        "hisseler": per,
        "ozet": {
            "toplam_yonlu_sinyal": total_actionable,
            "toplam_isabet": total_hits,
            "agirlikli_basari_orani_%": agg_hit,
            "portfoy_strateji_getiri_%": port_strat,
            "portfoy_al_tut_getiri_%": port_bh,
            "bist100_al_tut_getiri_%": bench_bh,
            "strateji_vs_bist100_fark_%": (round(port_strat - bench_bh, 2)
                                           if bench_bh is not None else None),
        },
    }
