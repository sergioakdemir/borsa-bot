"""BIST hisselerinin verisini structured JSON olarak kaydeder.

Her hisse icin: sembol, ticker, freshness durumu ve tarihli OHLCV barlari.
Veri kaynagi ve piyasa soyutlama katmani uzerinden calisir (degistirilebilir).
"""
import json
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .data.factory import get_data_source
from .data.freshness import check_freshness
from .markets.bist import BIST


def _num(v):
    """NaN/inf degerleri JSON-guvenli sekilde None'a cevirir."""
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return None
    return v


def _bars_to_records(df: pd.DataFrame) -> list[dict]:
    records = []
    for ts, row in df.iterrows():
        records.append({
            "date": pd.Timestamp(ts).date().isoformat(),
            "open": _num(round(float(row["Open"]), 4)),
            "high": _num(round(float(row["High"]), 4)),
            "low": _num(round(float(row["Low"]), 4)),
            "close": _num(round(float(row["Close"]), 4)),
            "volume": _num(int(row["Volume"])) if pd.notna(row["Volume"]) else None,
        })
    return records


def build_snapshot(tickers: list[str], days: int = 10, source: str = "yfinance") -> dict:
    src = get_data_source(source)
    market = BIST()
    start = (pd.Timestamp.today().date() - pd.Timedelta(days=days)).isoformat()

    stocks = []
    for ticker in tickers:
        symbol = market.to_symbol(ticker)
        df = src.get_history(symbol, start=start)
        rep = check_freshness(df, tz=market.timezone)
        stocks.append({
            "ticker": ticker,
            "symbol": symbol,
            "freshness": {
                "status": rep.status.value,
                "is_ok": rep.is_ok,
                "last_bar": rep.last_bar.isoformat() if rep.last_bar else None,
                "expected_trading_day": rep.expected.isoformat() if rep.expected else None,
                "today": rep.today.isoformat(),
                "calendar_age_days": rep.calendar_age,
                "trading_age_days": rep.trading_age,
                "message": rep.message,
            },
            "bar_count": len(df),
            "bars": _bars_to_records(df),
        })

    return {
        "generated_at": datetime.now(ZoneInfo(market.timezone)).isoformat(),
        "market": market.name,
        "currency": market.currency,
        "timezone": market.timezone,
        "source": source,
        "stock_count": len(stocks),
        "stocks": stocks,
    }


def main():
    tickers = ["THYAO", "GARAN", "ASELS", "KCHOL", "TUPRS"]
    snapshot = build_snapshot(tickers)

    out_dir = Path(__file__).resolve().parents[1] / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "bist_snapshot.json"
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Kaydedildi: {out_path}")
    print(f"  piyasa: {snapshot['market']} | kaynak: {snapshot['source']} | hisse: {snapshot['stock_count']}")
    for s in snapshot["stocks"]:
        print(f"  {s['symbol']:10s} [{s['freshness']['status']:6s}] "
              f"{s['bar_count']} bar, son: {s['freshness']['last_bar']}")


if __name__ == "__main__":
    main()
