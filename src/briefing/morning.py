"""Sabah brifingi: 5 BIST hissesini yorumlar ve sonucu Telegram'a gonderir.

Cron ile her hafta ici sabah (BIST acilisindan once) calistirilmak uzere.
GUVENLIK: Telegram kimlik bilgileri yoksa AI'a HIC gitmeden cikar (token harcanmaz).
"""
import html
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")


def _load_dotenv():
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

from src.notify import telegram
from src.notify.telegram import TelegramNotConfigured

_EMOJI = {"AL": "\U0001F7E2", "AL_TEMKINLI": "\U0001F7E1", "TUT": "⚪",
          "SAT": "\U0001F534", "GUCLU_SAT": "\U0001F534", "VETO": "⛔",
          "SKIP": "⏭"}


def _esc(s):
    return html.escape(str(s or ""))


def build_message(results, now):
    lines = [f"<b>\U0001F4CA Sabah Brifingi</b> — {now:%Y-%m-%d %H:%M}",
             "<i>BIST acilisindan once · 5 hisse</i>", ""]
    evaluated = skipped = total_news = 0
    for r in results:
        sym = _esc(r["symbol"])
        if r.get("skipped"):
            skipped += 1
            lines.append(f"{_EMOJI['SKIP']} <b>{sym}</b> — ATLANDI ({_esc(r.get('reason',''))[:40]})")
            continue
        evaluated += 1
        total_news += r.get("haber_sayisi", 0)
        emoji = _EMOJI.get(r["final_decision"], "⚪")
        risk = r["risk"]["score"]
        head = f"{emoji} <b>{sym}</b>  {r['score']}/10 · risk {risk} · {_esc(r['eminlik'])}"
        lines.append(head)
        lines.append(f"   → {_esc(r['final_label'])}")
        lines.append(f"   <i>{_esc(r['gerekce'])[:120]}</i>")
        if r.get("haber_sayisi"):
            lines.append(f"   \U0001F4F0 {r['haber_sayisi']} filtreli haber")
        lines.append("")
    lines.append(f"<i>Ozet: {evaluated} yorumlandi, {skipped} atlandi · {total_news} haber</i>")
    return "\n".join(lines)


def evaluate_all():
    import anthropic
    from src.export_json import build_snapshot
    from src.ai.commentator import evaluate_stock
    from src.ai import audit
    from src.db import database as db
    from src.news.service import get_news_source, filtered_news
    from src.watchlist import load_watchlist

    db.seed_default_sources()
    tickers = load_watchlist()
    snapshot = build_snapshot(tickers)
    news_src, is_sample = get_news_source(verbose=False)
    db.update_status("KAP", "ERISILEMEZ" if is_sample else "AKTIF",
                     "Sabah brifingi.")
    db.update_status("yfinance", "AKTIF", "Sabah brifingi.")

    audit.log_run_start(len(snapshot["stocks"]))
    client = anthropic.Anthropic()
    results = []
    ev = sk = 0
    for stock in snapshot["stocks"]:
        news = filtered_news(stock["ticker"], source=news_src)
        r = evaluate_stock(stock, news=news, client=client)
        results.append(r)
        status = stock.get("freshness", {}).get("status")
        if r.get("skipped"):
            sk += 1
            audit.log_decision(stock["symbol"], status, "SKIPPED_STALE", note=r.get("reason", ""))
        else:
            ev += 1
            note = f"eminlik={r['eminlik']} risk={r['risk']['score']} veto={r['vetoed']} haber={r['haber_sayisi']}"
            audit.log_decision(stock["symbol"], status, "EVALUATED",
                               decision=r["final_decision"], score=r["score"], note=note)
    audit.log_run_end(ev, sk)
    return results


def main():
    now = datetime.now(_TZ)

    # --- GUVENLIK: Telegram yoksa AI'a gitmeden cik (token harcama) ---
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yapilandirilmamis "
              "(TELEGRAM_BOT_TOKEN/CHAT_ID). Brifing atlandi, token harcanmadi.")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"[{now:%Y-%m-%d %H:%M}] ANTHROPIC_API_KEY yok. Brifing atlandi.")
        return 1

    print(f"[{now:%Y-%m-%d %H:%M}] Sabah brifingi basliyor...")
    results = evaluate_all()
    msg = build_message(results, now)
    try:
        telegram.send_message(msg)
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram'a gonderildi ({len(results)} hisse).")
    except (TelegramNotConfigured, RuntimeError) as e:
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram gonderim HATASI: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
