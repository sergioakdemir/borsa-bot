"""5 BIST hissesini yorumlar: on-sinyal + haber + puan + eminlik + risk(veto).

Her hisse icin: kompakt on-sinyal (ham bar yok) + haber filtresinden gecen
haberler AI'a verilir. Karar puandan turetilir, risk 8+ veto eder, STALE atlanir.
Audit log + kaynak sicili guncellenir.

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
from src.news.service import get_news_source, filtered_news
from src.watchlist import load_watchlist


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("HATA: ANTHROPIC_API_KEY bulunamadi. .env dosyasina ekleyin.")

    db.seed_default_sources()
    db.update_status("yfinance", "AKTIF", "Yorumlama calistirildi.")

    tickers = load_watchlist()
    print("Veri cekiliyor (yfinance)...")
    snapshot = build_snapshot(tickers)

    # haber kaynagi (canli KAP -> ornek fallback)
    news_src, is_sample = get_news_source(verbose=True)
    db.update_status("KAP", "ERISILEMEZ" if is_sample else "AKTIF",
                     "Ornek kaynak (KAP engelli)." if is_sample else "Canli KAP.")

    audit.log_run_start(len(snapshot["stocks"]))
    client = anthropic.Anthropic()
    results = []
    evaluated = skipped = 0

    for stock in snapshot["stocks"]:
        symbol = stock["symbol"]
        status = stock.get("freshness", {}).get("status")
        ticker = stock["ticker"]

        news = filtered_news(ticker, source=news_src)
        if status == "STALE":
            print(f"  {symbol}: STALE -> ATLANDI (kill switch)", flush=True)
        else:
            print(f"  {symbol} yorumlaniyor... (filtreli haber: {len(news)})", flush=True)

        r = evaluate_stock(stock, news=news, client=client)
        results.append(r)

        if r.get("skipped"):
            skipped += 1
            audit.log_decision(symbol, status, "SKIPPED_STALE", note=r.get("reason", ""))
        else:
            evaluated += 1
            note = (f"eminlik={r['eminlik']} risk={r['risk']['score']} "
                    f"veto={r['vetoed']} puan_karari={r['decision']} haber={r['haber_sayisi']}")
            audit.log_decision(symbol, status, "EVALUATED",
                               decision=r["final_decision"], score=r["score"], note=note)

    audit.log_run_end(evaluated, skipped)

    out_dir = Path(__file__).resolve().parents[2] / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "ai_commentary.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    if is_sample:
        print("\nNOT: Haberler ORNEK kaynaktandir (KAP engelli); fiyatlanma analizi gercek fiyatladir.")
    print(f"\nKaydedildi: {out_path}")
    print(f"Audit log : {audit.AUDIT_LOG}\n")
    print(f"{'HISSE':10s} {'PUAN':>4s} {'EMINLIK':8s} {'RISK':>4s} {'HBR':>3s}  {'NIHAI KARAR':22s} GEREKCE")
    print("-" * 105)
    for r in results:
        if r.get("skipped"):
            print(f"{r['symbol']:10s} {'-':>4s} {'-':8s} {'-':>4s} {r.get('haber_sayisi',0):>3d}  "
                  f"{'ATLANDI (STALE)':22s} {r.get('reason','')[:30]}")
        else:
            print(f"{r['symbol']:10s} {r['score']:>3d}/10 {r['eminlik']:8s} {r['risk']['score']:>3d}/10 "
                  f"{r['haber_sayisi']:>3d}  {r['final_label']:22s} {r['gerekce'][:30]}")
    print(f"\nOzet: {evaluated} yorumlandi, {skipped} atlandi (STALE).")


if __name__ == "__main__":
    main()
