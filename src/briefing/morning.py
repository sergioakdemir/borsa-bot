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
    """Her hedef hisse icin TAM analiz zincirini calistirir (commentary.py).

    Zincir: yfinance + KAP(30g) + haber(7g) -> Claude -> karar/puan/risk/...
    ai_commentary.json'a yazar ve her karari decisions tablosuna kaydeder.
    """
    from src.ai import commentary
    if not targets:
        return []
    return commentary.run(targets, save=True, verbose=True)


def build_message(results, sel, now):
    """Kisa ozet + en iyi firsat + hisse basina tek satir."""
    personal = set(sel["personal"])
    valid = [r for r in results if not r.get("skipped")]
    al = [r for r in valid if r["final_decision"] == "AL"]
    tut = [r for r in valid if r["final_decision"] == "TUT"]
    sat = [r for r in valid if r["final_decision"] in ("SAT", "GUCLU_SAT")]
    veto = [r for r in valid if r["final_decision"] == "VETO"]

    lines = [f"<b>\U0001F305 Sabah Brifingi</b> — {now:%Y-%m-%d %H:%M}"]
    if not results:
        lines.append("\nBugun kisisel liste bos ve belirgin hareket yok. Yorum uretilmedi.")
        return "\n".join(lines)

    ozet = f"{len(al)} AL · {len(tut)} TUT · {len(sat)} SAT"
    if veto:
        ozet += f" · {len(veto)} VETO"
    lines.append(f"<b>Ozet:</b> {ozet}")
    lines.append(f"<i>Kisisel {len(sel['personal'])} · Hareketli {len(sel['movers'])} "
                 f"(≥%{sel['threshold']:g}) · taranan {sel['taranan']}</i>")

    # En iyi firsat: VETO haric en yuksek puanli (AL'lar oncelikli)
    cand = [r for r in valid if r["final_decision"] != "VETO"]
    cand.sort(key=lambda r: (r["final_decision"] == "AL", r.get("score") or 0), reverse=True)
    if cand:
        b = cand[0]
        lines.append("")
        lines.append(f"⭐ <b>En iyi firsat: {_esc(b['ticker'])}</b> "
                     f"({b['score']}/10 · risk {b['risk']['score']} · {_esc(b['final_label'])})")
        lines.append(f"<i>{_esc((b.get('gerekce') or '')[:220])}</i>")

    # Kisa tut (Telegram ~1000-1200 karakter): yalniz dikkat ceken kararlar.
    # TUT'lar tek satirda ozetlenir.
    notable = [r for r in valid if r["final_decision"] != "TUT"]
    if notable:
        lines.append("")
        for r in notable:
            sym = _esc(r.get("ticker") or r.get("symbol"))
            emoji = _EMOJI.get(r["final_decision"], "⚪")
            lines.append(f"{emoji} <b>{sym}</b> {r['final_label']} · "
                         f"{r['score']}/10 · risk {r['risk']['score']}")
    if tut:
        lines.append(f"\n⚪ TUT ({len(tut)}): " + ", ".join(
            _esc(r.get("ticker")) for r in tut[:12]))
    msg = "\n".join(lines)
    if len(msg) > 1200:                       # guvenli ust sinir
        msg = msg[:1180].rsplit("\n", 1)[0] + "\n…"
    return msg


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
    sonuc = telegram.broadcast(msg)        # tum alicilara (Serhat + Yigit ...)
    ok = [c for c, s in sonuc.items() if s == "ok"]
    print(f"[{now:%Y-%m-%d %H:%M}] Telegram broadcast: {len(ok)}/{len(sonuc)} alici "
          f"({len(results)} hisse). Sonuc: {sonuc}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
