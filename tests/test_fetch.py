"""BIST hisseleri icin veri cekmenin calistigini dogrular."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.factory import get_data_source
from src.markets.bist import BIST


def main():
    src = get_data_source("yfinance")
    bist = BIST()

    for ticker in ["THYAO", "GARAN", "ASELS", "KCHOL", "TUPRS"]:
        symbol = bist.to_symbol(ticker)
        df = src.get_history(symbol, start="2024-01-01", end="2024-01-15")
        print(f"\n=== {symbol} ({len(df)} satir) ===")
        if df.empty:
            print("  UYARI: veri bos dondu!")
        else:
            print(df[["Open", "High", "Low", "Close", "Volume"]].tail(3).to_string())


if __name__ == "__main__":
    main()
