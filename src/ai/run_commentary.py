"""5 BIST hissesini yorumlar: puan + eminlik + risk(veto) + nihai karar.

Karar puandan turetilir, risk ajani 8+ ise veto eder. STALE veri atlanir (kill
switch). Her calistirmada audit log yazilir ve kaynak sicili guncellenir.

Calistirmak icin ANTHROPIC_API_KEY gereklidir (.env otomatik yuklenir).
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _load_dotenv():
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

import anthropic
from src.export_json import build_snapshot
from src.ai.commentator import evaluate_stock
from src.ai import audit
from src.db import database as db


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("HATA: ANTHROPIC_API_KEY bulunamadi. .env dosyasina ekleyin.")

    db.seed_default_sources()
    db.update_status("yfinance", "AKTIF", "Yorumlama calistirildi.")

    tickers = ["THYAO", "GARAN", "ASELS", "KCHOL", "TUPRS"]
    print("Veri cekiliyor (yfinance)...")
    snapshot = build_snapshot(tickers)

    audit.log_run_start(len(snapshot["stocks"]))
    client = anthropic.Anthropic()
    results = []
    evaluated = skipped = 0

    for stock in snapshot["stocks"]:
        symbol = stock["symbol"]
        status = stock.get("freshness", {}).get("status")
        if status == "STALE":
            print(f"  {symbol}: STALE -> ATLANDI (kill switch)", flush=True)
        else:
            print(f"  {symbol} yorumlaniyor...", flush=True)

        r = evaluate_stock(stock, client=client)
        results.append(r)

        if r.get("skipped"):
            skipped += 1
            audit.log_decision(symbol, status, "SKIPPED_STALE", note=r.get("reason", ""))
        else:
            evaluated += 1
            note = (f"eminlik={r['eminlik']} risk={r['risk']['score']} "
                    f"veto={r['vetoed']} puan_karari={r['decision']}")
            audit.log_decision(symbol, status, "EVALUATED",
                               decision=r["final_decision"], score=r["score"], note=note)

    audit.log_run_end(evaluated, skipped)

    out_dir = Path(__file__).resolve().parents[2] / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "ai_commentary.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nKaydedildi: {out_path}")
    print(f"Audit log : {audit.AUDIT_LOG}\n")
    print(f"{'HISSE':10s} {'PUAN':>4s} {'EMINLIK':8s} {'RISK':>4s}  {'NIHAI KARAR':22s} GEREKCE")
    print("-" * 100)
    for r in results:
        if r.get("skipped"):
            print(f"{r['symbol']:10s} {'-':>4s} {'-':8s} {'-':>4s}  "
                  f"{'ATLANDI (STALE)':22s} {r.get('reason','')[:35]}")
        else:
            risk = r["risk"]["score"]
            print(f"{r['symbol']:10s} {r['score']:>3d}/10 {r['eminlik']:8s} {risk:>3d}/10  "
                  f"{r['final_label']:22s} {r['gerekce'][:35]}")
    print(f"\nOzet: {evaluated} yorumlandi, {skipped} atlandi (STALE).")


if __name__ == "__main__":
    main()
