"""5 BIST hissesinin guncel verisini cekip her biri icin AI yorumu uretir.

Calistirmak icin ANTHROPIC_API_KEY ortam degiskeni gereklidir.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import anthropic
from src.export_json import build_snapshot
from src.ai.commentator import evaluate_stock


def main():
    tickers = ["THYAO", "GARAN", "ASELS", "KCHOL", "TUPRS"]
    print("Veri cekiliyor (yfinance)...")
    snapshot = build_snapshot(tickers)

    client = anthropic.Anthropic()
    results = []
    for stock in snapshot["stocks"]:
        print(f"  {stock['symbol']} yorumlaniyor...", flush=True)
        results.append(evaluate_stock(stock, client=client))

    out_dir = Path(__file__).resolve().parents[2] / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "ai_commentary.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nKaydedildi: {out_path}\n")
    print(f"{'HISSE':10s} {'PUAN':>5s}  {'KARAR':5s}  GEREKCE")
    print("-" * 80)
    for r in results:
        print(f"{r['symbol']:10s} {r['score']:>4d}/10  {r['decision']:5s}  {r['gerekce'][:55]}")


if __name__ == "__main__":
    main()
