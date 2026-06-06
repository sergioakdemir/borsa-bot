"""Haberin fiyatlanip fiyatlanmadigini olcer: haber tarihindeki fiyat/hacim
tepkisini GERCEK fiyat verisiyle (yfinance) degerlendirir."""
from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from ..data.factory import get_data_source
from ..markets.bist import BIST


@dataclass
class PricedInReport:
    status: str                       # FIYATLANDI | FIYATLANMADI | VERI_YOK
    reaction_date: str | None
    day_return_pct: float | None
    volume_vs_avg_pct: float | None
    message: str


def check_priced_in(item, source=None, market=None, lookback_days=20,
                    return_threshold=3.0, volume_factor=1.5):
    src = source or get_data_source()
    market = market or BIST()
    symbol = item.symbol or market.to_symbol(item.ticker)
    news_date = item.published_at.date()

    start = (news_date - timedelta(days=lookback_days + 12)).isoformat()
    end = (news_date + timedelta(days=8)).isoformat()
    df = src.get_history(symbol, start=start, end=end)
    if not df.empty:
        df = df[df["Volume"] > 0]
    if df.empty or len(df) < 5:
        return PricedInReport("VERI_YOK", None, None, None,
                              "Haber tarihi etrafinda yeterli fiyat verisi yok.")

    dates = [pd.Timestamp(ix).date() for ix in df.index]
    idx = next((i for i, d in enumerate(dates) if d >= news_date), None)
    if idx is None or idx == 0:
        return PricedInReport("VERI_YOK", None, None, None,
                              "Haber gunune denk islem bari bulunamadi.")

    row, prev = df.iloc[idx], df.iloc[idx - 1]
    day_ret = (row["Close"] - prev["Close"]) / prev["Close"] * 100

    base = df.iloc[max(0, idx - lookback_days):idx]
    avg_vol = base["Volume"].mean()
    vol_ratio = (row["Volume"] / avg_vol - 1) * 100 if avg_vol else 0.0

    reacted = abs(day_ret) >= return_threshold or (avg_vol and row["Volume"] >= volume_factor * avg_vol)
    status = "FIYATLANDI" if reacted else "FIYATLANMADI"
    msg = (f"{status}: {dates[idx]} gunu getiri %{day_ret:.2f}, "
           f"hacim ortalamaya gore %{vol_ratio:.0f}.")
    return PricedInReport(status, dates[idx].isoformat(), round(day_ret, 2),
                          round(vol_ratio, 1), msg)
