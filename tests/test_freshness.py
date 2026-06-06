"""Freshness kontrolunu gercek veriyle dener: guncel veri ve yapay eski veri."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd
from src.data.factory import get_data_source
from src.markets.bist import BIST
from src.data.freshness import check_freshness, Freshness


def main():
    src = get_data_source("yfinance")
    bist = BIST()

    print("=== 1) GUNCEL VERI (son ~10 gun, bugune kadar) ===")
    for ticker in ["THYAO", "GARAN", "ASELS", "KCHOL", "TUPRS"]:
        symbol = bist.to_symbol(ticker)
        df = src.get_history(symbol, start=str(pd.Timestamp.today().date() - pd.Timedelta(days=10)))
        rep = check_freshness(df, tz=bist.timezone)
        flag = "OK " if rep.is_ok else "!! "
        print(f"{flag}{symbol:10s} [{rep.status.value:6s}] {rep.message}")

    print("\n=== 2) YAPAY ESKI VERI (Ocak 2024 kesiti) -> STALE bekleniyor ===")
    df_old = src.get_history(bist.to_symbol("THYAO"), start="2024-01-01", end="2024-01-15")
    rep = check_freshness(df_old, tz=bist.timezone)
    assert rep.status == Freshness.STALE, f"STALE bekleniyordu, gelen: {rep.status}"
    print(f"OK THYAO.IS [{rep.status.value}] {rep.message}")
    print(f"   takvim yasi: {rep.calendar_age} gun, islem yasi: ~{rep.trading_age} is gunu")

    print("\nTum freshness kontrolleri gecti.")


if __name__ == "__main__":
    main()
