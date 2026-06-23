"""Gün sonu özeti (cron: hafta içi 18:20, borsa kapanışından sonra).

Format:
  GÜN SONU
  [Bugün genel ne oldu? 1-2 cümle]

  PORTFÖY
  [Her hisse: KARAR + kısa yorum + günün değişimi]

  YARIN BAKILACAKLAR
  [2-3 madde: bekleyen şartlı senaryolar, yaklaşan PPK vb.]

Sadece 5 karar kelimesi (AL/TUT/BEKLE/AZALT/UZAK DUR), izinli emojiler
(🟢🟡🔴⚡📰), teknik oran yok, kısa. Kararlar sabah brifinginde üretilen
ai_commentary.json'dan okunur (ek AI maliyeti yok).
"""
import html
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")
ROOT = Path(__file__).resolve().parents[2]
_COMMENTARY_PATH = ROOT / "data" / "ai_commentary.json"


def _load_dotenv():
    env_path = ROOT / ".env"
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
from src.ai.decision import karar_kelime, karar_emoji


def _esc(s):
    return html.escape(str(s or ""))


def _kisa(metin, limit=160):
    g = " ".join((metin or "").split())
    if len(g) > limit:
        g = g[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return g


def _karar_map():
    """ai_commentary.json -> {TICKER: rec} (sabah brifingi kararları)."""
    try:
        import json
        data = json.loads(_COMMENTARY_PATH.read_text(encoding="utf-8"))
        out = {}
        for rec in (data if isinstance(data, list) else []):
            t = (rec.get("ticker") or "").upper()
            if t:
                out.setdefault(t, rec)
        return out
    except Exception:
        return {}


def _genel_ozet(overview):
    """Bugün genel ne oldu — 1-2 sade cümle (teknik oran yok)."""
    if overview and overview.get("available"):
        notu = (overview.get("brifing_notu") or "").strip()
        if notu:
            return notu
        yon = (overview.get("yon") or "").upper()
        return {"YUKSELIYOR": "Borsa günü yükselişle kapattı.",
                "DUSUYOR": "Borsa günü düşüşle kapattı.",
                "YATAY": "Borsa günü yatay kapattı."}.get(yon, "Borsa günü karışık kapattı.")
    return "Borsa günü karışık kapattı."


def _yarin_bakilacaklar(now):
    """YARIN BAKILACAKLAR: bekleyen senaryolar + yaklaşan PPK (max 3 madde)."""
    maddeler = []
    try:
        from src.ai import senaryo
        for s in (senaryo.yukle().get("senaryolar") or []):
            if s.get("durum") == "bekliyor" and s.get("metin"):
                maddeler.append(s["metin"])
    except Exception:
        pass
    try:
        from src.news.macro import sonraki_ppk
        nxt = sonraki_ppk(now.date())
        if nxt:
            kalan = (nxt - now.date()).days
            if 0 <= kalan <= 5:
                maddeler.append(f"PPK faiz kararı {kalan} gün sonra.")
    except Exception:
        pass
    if not maddeler:
        maddeler.append("Önemli bir takvim/gelişme görünmüyor; piyasayı izlemeye devam.")
    return maddeler[:3]


def _gun_degisim(ticker):
    """Hissenin bugünkü yüzde değişimi (yoksa None)."""
    try:
        from src.alerts.engine import intraday_change
        info = intraday_change(ticker)
        return info["change"] if info else None
    except Exception:
        return None


def _hisse_satiri(rec, ticker):
    fd = rec.get("final_decision") if rec else None
    kelime = karar_kelime(fd) or "TUT"
    emoji = karar_emoji(fd)
    chg = _gun_degisim(ticker)
    yon = f" %{chg:+.1f}" if chg is not None else ""
    satir = f"{emoji} <b>{_esc(ticker)} — {kelime}</b>{yon}"
    yorum = _kisa((rec or {}).get("sade_yorum") or (rec or {}).get("gerekce")) if rec else ""
    if yorum:
        satir += f"\n<i>{_esc(yorum)}</i>"
    return satir


def build_message(portfolio, kmap, overview, yarin, now, kullanici_ad=None):
    ad = f" · {str(kullanici_ad).capitalize()}" if kullanici_ad else ""
    lines = [f"<b>GÜN SONU</b>{ad} — {now:%d.%m %H:%M}", _esc(_genel_ozet(overview))]
    lines += ["", "<b>PORTFÖY</b>"]
    pf = sorted(portfolio)
    if pf:
        for tkr in pf:
            lines.append(_hisse_satiri(kmap.get(tkr), tkr))
    else:
        lines.append("Takip ettiğin portföy hissesi yok.")
    lines += ["", "<b>YARIN BAKILACAKLAR</b>"]
    for m in yarin:
        lines.append(f"• {_esc(m)}")
    msg = "\n".join(lines)
    if len(msg) > 3500:
        msg = msg[:3480].rsplit("\n", 1)[0] + "\n…"
    return msg


def run():
    now = datetime.now(_TZ)
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yapilandirilmamis - gun sonu atlandi.")
        return 0

    from src.db import database as db
    db.init_db()
    kmap = _karar_map()
    overview = None
    try:
        from src.news.market_overview import get_market_overview
        overview = get_market_overview()
    except Exception as e:
        print(f"  piyasa ozeti alinamadi: {type(e).__name__}")
    yarin = _yarin_bakilacaklar(now)

    sonuc = {}
    gonderilen = set()
    try:
        kullanicilar = db.list_users()
    except Exception:
        kullanicilar = []
    for u in kullanicilar:
        tg = u.get("telegram_id")
        if not tg:
            continue
        try:
            pf = {(p.get("ticker") or "").upper().replace(".IS", "")
                  for p in db.list_portfolio(u["id"]) if p.get("ticker")}
        except Exception:
            pf = set()
        msg = build_message(pf, kmap, overview, yarin, now, kullanici_ad=u.get("ad"))
        try:
            telegram.send_message(msg, chat_id=tg)
            sonuc[str(tg)] = "ok"
        except Exception as e:
            sonuc[str(tg)] = f"hata:{type(e).__name__}"
        gonderilen.add(str(tg))

    # DB dışı env alıcıları -> tüm portföyler birleşik
    try:
        birlesik = {(r.get("ticker") or "").upper().replace(".IS", "")
                    for r in db.list_portfolio() if r.get("ticker")}
    except Exception:
        birlesik = set()
    genel = build_message(birlesik, kmap, overview, yarin, now)
    for cid in telegram.recipient_ids():
        if str(cid) in gonderilen:
            continue
        try:
            telegram.send_message(genel, chat_id=cid)
            sonuc[str(cid)] = "ok"
        except Exception as e:
            sonuc[str(cid)] = f"hata:{type(e).__name__}"

    ok = [c for c, s in sonuc.items() if s == "ok"]
    print(f"[{now:%Y-%m-%d %H:%M}] Gun sonu gonderim: {len(ok)}/{len(sonuc)} alici. Sonuc: {sonuc}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
