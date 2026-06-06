"""Portfoy takibi motoru: pozisyon basina gunluk TUT/SAT sinyali + kar/zarar.

Sinyal DETERMINISTIK uretilir (token harcamaz): on-sinyal -> kural-tabanli puan
+ risk ajani. SAT esigi: puan <= 3 VEYA risk vetosu; aksi halde TUT.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..data.factory import get_data_source
from ..data.freshness import check_freshness
from ..markets.bist import BIST
from ..ai.presignal import build_presignal
from ..ai.risk import assess_risk
from ..backtest.scorer import deterministic_score
from ..db import database as db

_TZ = ZoneInfo("Europe/Istanbul")


def _recent_stock(ticker, src, market):
    symbol = market.to_symbol(ticker)
    today = datetime.now(_TZ).date()
    df = src.get_history(symbol, start=(today - timedelta(days=40)).isoformat())
    if not df.empty:
        df = df[df["Volume"] > 0]
    if df.empty or len(df) < 3:
        return None, symbol
    status = check_freshness(df, tz=market.timezone).status.value
    bars = []
    for ix, row in df.tail(15).iterrows():
        bars.append({"date": ix.date().isoformat(),
                     "open": float(row["Open"]), "high": float(row["High"]),
                     "low": float(row["Low"]), "close": float(row["Close"]),
                     "volume": int(row["Volume"])})
    return {"ticker": ticker, "symbol": symbol,
            "freshness": {"status": status}, "bars": bars}, symbol


def position_signal(ticker, src=None, market=None) -> dict:
    src = src or get_data_source()
    market = market or BIST()
    stock, symbol = _recent_stock(ticker, src, market)
    if not stock:
        return {"ticker": ticker, "symbol": symbol, "signal": "VERI_YOK",
                "last_close": None, "score": None, "risk": None, "veto": None}

    ps = build_presignal(stock)
    score = deterministic_score(ps)
    risk = assess_risk(stock)
    last_close = stock["bars"][-1]["close"]
    signal = "SAT" if (score <= 3 or risk.veto) else "TUT"
    return {"ticker": ticker, "symbol": symbol, "signal": signal,
            "last_close": round(last_close, 2), "score": score,
            "risk": risk.score, "veto": risk.veto,
            "freshness": stock["freshness"]["status"]}


def portfolio_report(kullanici_id) -> dict:
    positions = db.list_portfolio(kullanici_id)
    rows = []
    tot_cost = tot_val = 0.0
    cache = {}
    for p in positions:
        t = p["ticker"]
        if t not in cache:
            cache[t] = position_signal(t)
        sig = cache[t]
        lc = sig["last_close"]
        cost = p["adet"] * p["alim_fiyati"]
        val = p["adet"] * lc if lc is not None else None
        pnl = (val - cost) if val is not None else None
        pnl_pct = ((lc - p["alim_fiyati"]) / p["alim_fiyati"] * 100) if lc else None
        tot_cost += cost
        tot_val += val or 0.0
        rows.append({
            "ticker": t, "adet": p["adet"], "alim_fiyati": p["alim_fiyati"],
            "last_close": lc, "maliyet": round(cost, 2),
            "deger": round(val, 2) if val is not None else None,
            "kar_zarar": round(pnl, 2) if pnl is not None else None,
            "kar_zarar_yuzde": round(pnl_pct, 2) if pnl_pct is not None else None,
            "signal": sig["signal"], "score": sig["score"], "risk": sig["risk"],
        })
    return {"rows": rows, "toplam_maliyet": round(tot_cost, 2),
            "toplam_deger": round(tot_val, 2),
            "toplam_kar_zarar": round(tot_val - tot_cost, 2),
            "toplam_kar_zarar_yuzde": round((tot_val / tot_cost - 1) * 100, 2) if tot_cost else None}
