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


def _us_portfolio_tickers():
    """Portfoylerdeki ABD (USD) hisselerinin benzersiz kodlari."""
    from src.db import database as db
    try:
        with db.get_conn() as c:
            rows = c.execute(
                "SELECT DISTINCT ticker FROM portfoy WHERE UPPER(para_birimi)='USD'")
            return [(r[0] or "").upper().replace(".IS", "") for r in rows if r[0]]
    except Exception:
        return []


def select_targets():
    """AI brifingi icin hedef hisseleri sec: kisisel + hareketli (onceki seans)
    + portfoydeki ABD hisseleri (':us' etiketli)."""
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

    # Portfoydeki ABD hisselerini ':us' etiketiyle ekle (KAP/analist atlanir)
    us = _us_portfolio_tickers()
    for t in us:
        if t not in targets and f"{t}:us" not in targets:
            targets.append(f"{t}:us")

    return {"targets": targets, "personal": personal, "movers": movers, "us": us,
            "changes": changes, "threshold": threshold, "taranan": len(index)}


def evaluate_all(targets, overview=None, learning=None):
    """Her hedef hisse icin TAM analiz zincirini calistirir (commentary.py).

    Zincir: yfinance + KAP(30g) + haber(7g) -> Claude -> karar/puan/risk/...
    ai_commentary.json'a yazar ve her karari decisions tablosuna kaydeder.
    overview/learning: brifingden gecirilen genel piyasa baglami + karar ogrenimi.
    """
    from src.ai import commentary
    if not targets:
        return []
    return commentary.run(targets, save=True, verbose=True,
                          overview=overview, learning=learning)


def build_message(results, sel, now, overview=None):
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

    # Genel piyasa yonu (BIST-100 / breadth / USD-TRY)
    if overview and overview.get("available"):
        yon_emoji = {"YUKSELIYOR": "\U0001F4C8", "DUSUYOR": "\U0001F4C9",
                     "YATAY": "➡️"}.get(overview.get("yon"), "📊")
        g = overview.get("bist100_gunluk_%")
        h = overview.get("bist100_haftalik_%")
        detay = []
        if g is not None:
            detay.append(f"bugün %{g:+g}")
        if h is not None:
            detay.append(f"hafta %{h:+g}")
        detay.append(f"{overview.get('yukselen')}↑/{overview.get('dusen')}↓")
        lines.append("")
        lines.append(f"{yon_emoji} <b>Piyasa: {_esc(overview.get('yon'))}</b> "
                     f"<i>({' · '.join(detay)})</i>")
        lines.append(f"<i>{_esc(overview.get('brifing_notu'))}</i>")

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

    # 1) Karar sonuclarini doldur (ogrenme) - brifingden ONCE
    try:
        from src.ops import update_decisions
        guncellenen = update_decisions.run(verbose=False)
        print(f"[{now:%Y-%m-%d %H:%M}] Karar ogrenimi: {guncellenen} sonuc guncellendi.")
    except Exception as e:
        print(f"[{now:%Y-%m-%d %H:%M}] Karar ogrenimi atlandi: {type(e).__name__}: {str(e)[:80]}")

    print(f"[{now:%Y-%m-%d %H:%M}] Hedef secimi (kisisel + hareketli)...")
    sel = select_targets()
    print(f"  taranan={sel['taranan']} kisisel={len(sel['personal'])} "
          f"hareketli={len(sel['movers'])} -> AI hedefi: {sel['targets']}")

    # 2) Genel piyasa baglami (breadth icin sel['changes'] tekrar kullanilir)
    try:
        from src.news.market_overview import get_market_overview
        overview = get_market_overview(changes=sel.get("changes"))
        print(f"  piyasa yonu: {overview.get('yon')} | BIST gunluk "
              f"%{overview.get('bist100_gunluk_%')} haftalik %{overview.get('bist100_haftalik_%')}")
    except Exception as e:
        print(f"  piyasa baglami alinamadi: {type(e).__name__}: {str(e)[:80]}")
        overview = None

    # 3) Karar gecmisi ogrenimi (hedef hisseler icin)
    try:
        from src.ai.learning import build_learning_notes
        learning = build_learning_notes(sel["targets"])
        if learning:
            print(f"  karar gecmisi notu: {list(learning.keys())}")
    except Exception as e:
        print(f"  karar gecmisi notu alinamadi: {type(e).__name__}")
        learning = {}

    results = evaluate_all(sel["targets"], overview=overview, learning=learning)
    msg = build_message(results, sel, now, overview=overview)
    sonuc = telegram.broadcast(msg)        # tum alicilara (Serhat + Yigit ...)
    ok = [c for c, s in sonuc.items() if s == "ok"]
    print(f"[{now:%Y-%m-%d %H:%M}] Telegram broadcast: {len(ok)}/{len(sonuc)} alici "
          f"({len(results)} hisse). Sonuc: {sonuc}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
