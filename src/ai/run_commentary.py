"""5 BIST hissesinin guncel verisini cekip her biri icin AI yorumu uretir.

Calistirmak icin ANTHROPIC_API_KEY gereklidir. Proje kokundeki .env dosyasi
varsa otomatik yuklenir (ekstra bagimlilik gerekmez).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _load_dotenv():
    """Proje kokundeki .env dosyasini os.environ'a yukler (basit parser)."""
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)  # mevcut env degiskenini ezme


_load_dotenv()

import anthropic
from src.export_json import build_snapshot
from src.ai.commentator import evaluate_stock


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("HATA: ANTHROPIC_API_KEY bulunamadi. .env dosyasina ekleyin "
                 "veya 'export ANTHROPIC_API_KEY=...' yapin.")

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
