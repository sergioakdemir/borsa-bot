"""Sabah brifingi: SADECE kisisel liste + hareketli hisseler icin AI yorumu.

09:00 acilistan once calistigi icin -hareketli- = onceki seansin belirgin
hareket edenleri (|gunluk degisim| >= hareketli_esik). Tum BIST-30 ucuzca
taranir; AI yalnizca kisisel + hareketli alt kume icin calisir (token kontrolu).

GUVENLIK: Telegram kimlik bilgileri yoksa AI cagrilmadan cikilir.
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


def select_targets():
    """AI brifingi icin hedef hisseleri sec: kisisel + hareketli (onceki seans)."""
    from src.watchlist import load_index, load_personal, load_mover_threshold
    from src.alerts.engine import intraday_change

    personal = load_personal()
    index = load_index()
    threshold = load_mover_threshold()

    changes = {}
    for t in index:
        info = intraday_change(t)
        if info:
            changes[t] = info["change"]   # son seansin degisimi

    movers = [t for t in index if abs(changes.get(t, 0.0)) >= threshold]

    targets = []
    for t in personal + movers:
        if t not in targets:
            targets.append(t)

    # Fallback: kisisel bos ve hareketli yoksa en hareketli 3 hisse
    if not targets and changes:
        targets = sorted(changes, key=lambda k: abs(changes[k]), reverse=True)[:3]
        movers = list(targets)

    return {"targets": targets, "personal": personal, "movers": movers,
            "changes": changes, "threshold": threshold, "taranan": len(index)}


def evaluate_all(targets):
    import anthropic
    from src.export_json import build_snapshot
    from src.ai.commentator import evaluate_stock
    from src.ai import audit
    from src.db import database as db
    from src.news.service import get_news_source, filtered_news

    if not targets:
        return []
    db.seed_default_sources()
    snapshot = build_snapshot(targets)
    news_src, is_sample = get_news_source(verbose=False)
    db.update_status("yfinance", "AKTIF", "Sabah brifingi.")

    audit.log_run_start(len(snapshot["stocks"]))
    client = anthropic.Anthropic()
    results, ev, sk = [], 0, 0
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
            # Gercek karar gunlugu: her AL/TUT/SAT karari decisions tablosuna yazilir
            # (sonuc=None; ileride fiyat takibiyle doldurulacak).
            try:
                db.record_decision(
                    ticker=r["ticker"],
                    karar=r["final_decision"],
                    puan=r.get("score"),
                    risk=(r.get("risk") or {}).get("score"),
                    eminlik=r.get("eminlik"),
                    gerekce=r.get("gerekce"),
                )
            except Exception as e:
                print(f"  [karar-kaydi] {r['ticker']} yazilamadi: {type(e).__name__}: {e}")
    audit.log_run_end(ev, sk)
    return results


def build_message(results, sel, now):
    personal = set(sel["personal"])
    lines = [f"<b>\U0001F4CA Sabah Brifingi</b> — {now:%Y-%m-%d %H:%M}",
             f"<i>Kisisel: {len(sel['personal'])} · Hareketli: {len(sel['movers'])} "
             f"(≥%{sel['threshold']:g}) · taranan: {sel['taranan']}</i>", ""]
    if not results:
        lines.append("Bugun kisisel liste bos ve belirgin hareket yok. Yorum uretilmedi.")
        return "\n".join(lines)
    for r in results:
        sym = _esc(r["symbol"])
        tag = "K" if r["ticker"] in personal else "H"   # Kisisel / Hareketli
        if r.get("skipped"):
            lines.append(f"{_EMOJI['SKIP']} <b>{sym}</b> [{tag}] — ATLANDI")
            continue
        emoji = _EMOJI.get(r["final_decision"], "⚪")
        lines.append(f"{emoji} <b>{sym}</b> [{tag}]  {r['score']}/10 · risk {r['risk']['score']} · {_esc(r['eminlik'])}")
        lines.append(f"   → {_esc(r['final_label'])}")
        lines.append(f"   <i>{_esc(r['gerekce'])[:120]}</i>")
        lines.append("")
    lines.append("<i>K=Kisisel · H=Hareketli</i>")
    return "\n".join(lines)


def main():
    now = datetime.now(_TZ)
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yapilandirilmamis. Brifing atlandi, token harcanmadi.")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"[{now:%Y-%m-%d %H:%M}] ANTHROPIC_API_KEY yok. Brifing atlandi.")
        return 1

    print(f"[{now:%Y-%m-%d %H:%M}] Hedef secimi (kisisel + hareketli)...")
    sel = select_targets()
    print(f"  taranan={sel['taranan']} kisisel={len(sel['personal'])} "
          f"hareketli={len(sel['movers'])} -> AI hedefi: {sel['targets']}")

    results = evaluate_all(sel["targets"])
    msg = build_message(results, sel, now)
    try:
        telegram.send_message(msg)
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram'a gonderildi ({len(results)} hisse AI yorumu).")
    except (TelegramNotConfigured, RuntimeError) as e:
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram gonderim HATASI: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
