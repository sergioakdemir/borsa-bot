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


_TR_AYLAR = ["", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
             "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]


def _tr_tarih(d):
    """date -> 'DD Ay YYYY' (Turkce ay adi)."""
    return f"{d.day} {_TR_AYLAR[d.month]} {d.year}"


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


def _portfolio_tickers():
    """Tum portfoylerdeki benzersiz hisse kodlari (BIST + ABD), normalize."""
    from src.db import database as db
    try:
        rows = db.list_portfolio()
        return {(r.get("ticker") or "").upper().replace(".IS", "")
                for r in rows if r.get("ticker")}
    except Exception:
        return set()


def select_targets(market="bist"):
    """AI brifingi icin hedef hisseleri sec.

    market='bist' -> TUM bist_endeks watchlist + kisisel (09:00 brifingi).
    market='us'   -> yalnizca portfoydeki ABD hisseleri (':us' etiketli, 15:30).
    """
    from src.watchlist import load_mover_threshold

    if market in ("us", "abd"):
        us = _us_portfolio_tickers()
        targets = [f"{t}:us" for t in us]
        return {"targets": targets, "personal": [], "movers": [], "us": us,
                "changes": {}, "threshold": load_mover_threshold(),
                "taranan": len(us), "portfolio": _portfolio_tickers(),
                "market": "us"}

    from src.watchlist import load_index, load_personal
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

    # TUM bist_endeks hisseleri analiz edilir (ABD ayri brifingde)
    targets = list(index)
    for t in personal:                 # kisisel listede index disinda hisse olabilir
        if t not in targets:
            targets.append(t)

    return {"targets": targets, "personal": personal, "movers": movers, "us": [],
            "changes": changes, "threshold": threshold, "taranan": len(index),
            "portfolio": _portfolio_tickers(), "market": "bist"}


def evaluate_all(targets, overview=None, learning=None):
    """Her hedef hisse icin TAM analiz zincirini calistirir (commentary.py).

    Zincir: yfinance + KAP(30g) + haber(7g) -> Claude -> karar/puan/risk/...
    ai_commentary.json'a yazar ve her karari decisions tablosuna kaydeder.
    overview/learning: brifingden gecirilen genel piyasa baglami + karar ogrenimi.
    """
    from src.ai import commentary
    if not targets:
        return []
    # Batch API: %50 daha ucuz; sabah brifinginde gecikme kabul edilebilir.
    # Batch basarisiz olursa tek-tek calistirmaya geri don.
    try:
        return commentary.run_batch(targets, save=True, verbose=True,
                                    overview=overview, learning=learning)
    except Exception as e:
        print(f"  [batch] basarisiz ({type(e).__name__}: {str(e)[:80]}); "
              "tek-tek calistiriliyor")
        return commentary.run(targets, save=True, verbose=True,
                              overview=overview, learning=learning)


def build_message(results, sel, now, overview=None):
    """Kisa ozet + en iyi firsat + hisse basina tek satir."""
    is_us = sel.get("market") in ("us", "abd")
    valid = [r for r in results if not r.get("skipped")]
    al = [r for r in valid if r["final_decision"] == "AL"]
    tut = [r for r in valid if r["final_decision"] == "TUT"]
    sat = [r for r in valid if r["final_decision"] in ("SAT", "GUCLU_SAT")]
    veto = [r for r in valid if r["final_decision"] == "VETO"]

    baslik = "\U0001F1FA\U0001F1F8 ABD Piyasası Açılıyor" if is_us else "\U0001F305 Sabah Brifingi"
    lines = [f"<b>{baslik}</b> — {now:%Y-%m-%d %H:%M}"]
    if not results:
        bos = ("Analiz edilecek ABD hissesi yok." if is_us
               else "Bugun kisisel liste bos ve belirgin hareket yok. Yorum uretilmedi.")
        lines.append(f"\n{bos}")
        return "\n".join(lines)

    ozet = f"{len(al)} AL · {len(tut)} TUT · {len(sat)} SAT"
    if veto:
        ozet += f" · {len(veto)} VETO"
    lines.append(f"<b>Ozet:</b> {ozet}")
    if is_us:
        lines.append(f"<i>{sel['taranan']} ABD hissesi tarandı</i>")
    else:
        lines.append(f"<i>Kisisel {len(sel['personal'])} · Hareketli {len(sel['movers'])} "
                     f"(≥%{sel['threshold']:g}) · taranan {sel['taranan']}</i>")

    # Genel piyasa yonu (BIST-100 / breadth / USD-TRY) - yalniz BIST brifingi
    if not is_us and overview and overview.get("available"):
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

    # Yabanci yatirimci akisi (varsa) - yalniz BIST brifingi
    if not is_us:
        try:
            from src.news.foreign_investor import briefing_line
            yb = briefing_line()
            if yb:
                lines.append(yb)
        except Exception:
            pass

    # Makro: BIST -> TCMB politika faizi + USD/TRY; ABD -> yalniz USD/TRY
    # get_macro cache'li (ek maliyet yok)
    try:
        from src.news.macro import get_macro
        mk = get_macro()
        usd = mk.get("usdtry")
        if is_us:
            if usd is not None:
                lines.append(f"💵 <b>USD/TRY:</b> {usd:g}")
        else:
            pf = mk.get("politika_faizi")
            if pf is not None:
                satir = f"🏦 <b>TCMB Politika Faizi:</b> %{pf:g}"
                if usd is not None:
                    satir += f" · USD/TRY {usd:g}"
                lines.append(satir)
    except Exception:
        pass

    # PPK (Para Politikasi Kurulu) toplanti takvimi - yalniz BIST brifingi
    if not is_us:
        try:
            from src.news.macro import sonraki_ppk, bugun_ppk_mi
            bugun = now.date()
            ppk_bugun = bugun_ppk_mi(bugun)
            if ppk_bugun:
                lines.append("⚠️ <b>Bugün PPK var!</b> Faiz kararı 14:00'te açıklanacak.")
            nxt = sonraki_ppk(bugun, dahil=not ppk_bugun)
            if nxt:
                kalan = (nxt - bugun).days
                lines.append(f"📅 <b>Sonraki PPK:</b> {_tr_tarih(nxt)} ({kalan} gün kaldı)")
        except Exception:
            pass

    # En iyi firsat: VETO haric en yuksek puanli (AL'lar oncelikli)
    cand = [r for r in valid if r["final_decision"] != "VETO"]
    cand.sort(key=lambda r: (r["final_decision"] == "AL", r.get("score") or 0), reverse=True)
    if cand:
        b = cand[0]
        lines.append("")
        lines.append(f"⭐ <b>En iyi firsat: {_esc(b['ticker'])}</b> "
                     f"({b['score']}/10 · risk {b['risk']['score']} · {_esc(b['final_label'])})")
        lines.append(f"<i>{_esc((b.get('gerekce') or '')[:220])}</i>")

    # Kategoriler: Portfoy (her zaman) / Firsat-Radar (portfoy disi AL) /
    # Bildirim (portfoy disi SAT-AZALT-VETO). Web Radar/Bildirimler de
    # ai_commentary.json'dan ayni ayrimi otomatik turetir.
    portfolio = sel.get("portfolio") or set()

    def _in_pf(r):
        return (r.get("ticker") or "").upper() in portfolio

    def _satir(r, varsayilan="⚪"):
        emoji = _EMOJI.get(r["final_decision"], varsayilan)
        return (f"{emoji} <b>{_esc(r.get('ticker') or r.get('symbol'))}</b> "
                f"{r['final_label']} · {r['score']}/10 · risk {r['risk']['score']}")

    pf_rows = [r for r in valid if _in_pf(r)]
    firsat = [r for r in valid if not _in_pf(r) and r["final_decision"] == "AL"]
    uyari = [r for r in valid if not _in_pf(r)
             and r["final_decision"] in ("SAT", "GUCLU_SAT", "AZALT", "VETO")]

    # Portfoyum: karar ne olursa olsun her zaman goster
    if pf_rows:
        lines.append("")
        lines.append("<b>💼 Portföyüm</b>")
        for r in pf_rows:
            lines.append(_satir(r))

    # Firsat / Radar: portfoy disi AL sinyalleri (web Radar'a da duser)
    if firsat:
        firsat.sort(key=lambda r: r.get("score") or 0, reverse=True)
        lines.append("")
        lines.append(f"<b>🟢 Fırsat / Radar ({len(firsat)})</b>")
        for r in firsat[:6]:
            lines.append(_satir(r, "🟢"))
        if len(firsat) > 6:
            lines.append(f"<i>+{len(firsat) - 6} hisse daha — web Radar'da</i>")

    # Bildirim: portfoy disi SAT/risk sinyalleri (web Bildirimler'e de duser)
    if uyari:
        uyari.sort(key=lambda r: r.get("score") or 0)
        lines.append("")
        lines.append(f"<b>🔴 Bildirim ({len(uyari)})</b>")
        for r in uyari[:6]:
            lines.append(_satir(r, "🔴"))
        if len(uyari) > 6:
            lines.append(f"<i>+{len(uyari) - 6} hisse daha — web Bildirimler'de</i>")

    if tut:
        lines.append(f"\n⚪ TUT ({len(tut)}): " + ", ".join(
            _esc(r.get("ticker")) for r in tut[:14]))
    msg = "\n".join(lines)
    if len(msg) > 2800:                       # Telegram guvenli ust sinir (4096 limit)
        msg = msg[:2780].rsplit("\n", 1)[0] + "\n…"
    return msg


def _record_briefing_memory(results):
    """Sabah brifingindeki dikkat ceken kararlari (AL/SAT/VETO) her kullanicinin
    hafizasina 'karar' tipiyle yazar (kime gonderildigi)."""
    from src.db import database as db
    notable = [r for r in (results or [])
               if not r.get("skipped")
               and r.get("final_decision") in ("AL", "SAT", "GUCLU_SAT", "VETO")]
    if not notable:
        return
    users = [u for u in db.list_users()]
    bugun = datetime.now(_TZ).date().isoformat()
    for u in users:
        for r in notable:
            tkr = (r.get("ticker") or "").upper()
            db.add_memory(
                u["id"], "karar",
                {"karar": r.get("final_decision"), "puan": r.get("score"),
                 "risk": (r.get("risk") or {}).get("score"),
                 "ozet": f"{tkr} {r.get('final_label') or r.get('final_decision')} "
                         f"({r.get('score')}/10)",
                 "gerekce": (r.get("gerekce") or "")[:240]},
                ticker=tkr, tarih=bugun)


def main(market="bist"):
    is_us = market in ("us", "abd")
    etiket = "ABD brifingi" if is_us else "Sabah brifingi"
    now = datetime.now(_TZ)
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yapilandirilmamis. {etiket} atlandi, token harcanmadi.")
        return 0
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"[{now:%Y-%m-%d %H:%M}] ANTHROPIC_API_KEY yok. {etiket} atlandi.")
        return 1

    # 1) Karar sonuclarini doldur (ogrenme) - brifingden ONCE
    try:
        from src.ops import update_decisions
        guncellenen = update_decisions.run(verbose=False)
        print(f"[{now:%Y-%m-%d %H:%M}] Karar ogrenimi: {guncellenen} sonuc guncellendi.")
    except Exception as e:
        print(f"[{now:%Y-%m-%d %H:%M}] Karar ogrenimi atlandi: {type(e).__name__}: {str(e)[:80]}")

    print(f"[{now:%Y-%m-%d %H:%M}] {etiket} - hedef secimi ({market})...")
    sel = select_targets(market=market)
    print(f"  taranan={sel['taranan']} -> AI hedefi: {sel['targets']}")

    # 2) Genel piyasa baglami - yalniz BIST (ABD brifingi BIST breadth'i kullanmaz)
    overview = None
    if not is_us:
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

    # 4) Paper trading: AL -> sanal alim ac, SAT -> kapat
    try:
        from src.portfolio import paper
        pt = paper.record_from_results(results, verbose=True)
        print(f"  paper trading: {pt['acilan']} acildi, {pt['kapanan']} kapandi")
    except Exception as e:
        print(f"  paper trading atlandi: {type(e).__name__}: {str(e)[:80]}")

    # 5) Model portfoy (100K): AL -> 50K alim, SAT -> kapat
    try:
        from src.portfolio import model
        mp = model.record_from_results(results, verbose=True)
        print(f"  model portfoy: {mp['acilan']} acildi, {mp['kapanan']} kapandi")
    except Exception as e:
        print(f"  model portfoy atlandi: {type(e).__name__}: {str(e)[:80]}")

    # 6) Kararlari her kullanicinin hafizasina yaz (kim aldi)
    try:
        _record_briefing_memory(results)
    except Exception as e:
        print(f"  hafiza kaydi atlandi: {type(e).__name__}: {str(e)[:80]}")

    msg = build_message(results, sel, now, overview=overview)
    sonuc = telegram.broadcast(msg)        # tum alicilara (Serhat + Yigit ...)
    ok = [c for c, s in sonuc.items() if s == "ok"]
    print(f"[{now:%Y-%m-%d %H:%M}] Telegram broadcast ({etiket}): {len(ok)}/{len(sonuc)} alici "
          f"({len(results)} hisse). Sonuc: {sonuc}")
    return 0 if ok else 1


if __name__ == "__main__":
    _market = "us" if (len(sys.argv) > 1 and sys.argv[1].lower() in ("us", "abd")) else "bist"
    sys.exit(main(_market))
