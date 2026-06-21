"""Sicak uyari kosusu (cron: hafta ici 10:00-18:00, her 30 dk).

Watchlist'teki her hisseyi kontrol eder; ACIL/IZLE seviyesindeki YENI uyarilari
Telegram'a gonderir. Spam onleme: ayni hisseye gun icinde ayni/daha dusuk
seviyede tekrar gondermez. Bayat/tatil verisinde (bugun bari yoksa) uyarmaz.
"""
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

from src.watchlist import load_watchlist
from src.alerts.engine import intraday_change, classify, level_rank
from src.notify import telegram
from src.db import database as db

_EMOJI = {"ACIL": "\U0001F6A8", "IZLE": "\U0001F440", "HABER": "\U0001F4F0",
          "HACIM": "\U0001F4CA"}


def unpriced_fresh_news(ticker, news_src=None):
    """Hisseye ait TAZE ve henuz FIYATLANMAMIS bir KAP haberi varsa dondurur."""
    from src.news.service import filtered_news
    try:
        for h in filtered_news(ticker, source=news_src):   # eski olmayanlar
            if h.get("fiyatlanma") == "FIYATLANMADI":
                return h
    except Exception:
        return None
    return None


def scan_kap_unpriced(now=None, window_min=30, move_limit=1.0):
    """GUN ICI KAP TARAMASI (cron: hafta ici 10-18 her 15 dk).

    Tum watchlist hisselerinin KAP bildirimlerini tarar. Son `window_min` dakika
    icinde YENI bir bildirim cikmis VE fiyat henuz %`move_limit`'ten az oynamissa
    'FIYATLANMAMIS HABER' olarak aninda Telegram'a gonderir (firsat penceresi).

    Spam onleme: her bildirim (disclosure_id) gun icinde bir kez gonderilir.
    """
    now = now or datetime.now(_TZ)
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yapilandirilmamis - KAP taramasi atlandi.")
        return 0

    from src.news.service import get_news_source
    news_src, is_sample = get_news_source(verbose=False)
    if is_sample:
        # Canli KAP yoksa zaman damgalari anlamli degil; yanlis 'taze' uyari uretmeyiz.
        print(f"[{now:%Y-%m-%d %H:%M}] KAP canli degil (ornek kaynak) - tarama atlandi.")
        return 0

    today = now.date().isoformat()
    hits = []
    checked = 0
    for ticker in load_watchlist():
        try:
            items = news_src.get_news(ticker, limit=20)
        except Exception:
            continue
        checked += 1
        # son window_min dakika icindeki bildirimler
        taze = []
        for it in items:
            pub = it.published_at
            if pub is None:
                continue
            yas_dk = (now - pub).total_seconds() / 60.0
            if 0 <= yas_dk <= window_min:
                taze.append(it)
        if not taze:
            continue

        # fiyat (o anki son kapanis) - haber_etki kaydi + fiyatlanma kontrolu icin
        info = intraday_change(ticker, today=now.date())
        fiyat_ani = info["last_close"] if info else None

        # HABER ETKISI: her yeni KAP bildirimi icin o anki fiyati kaydet (dedup: haber_id)
        if fiyat_ani is not None:
            from src.ops.update_haber_etki import kategori_of
            for it in taze:
                did = it.disclosure_id or (it.title or "")[:60]
                db.record_haber_etki(
                    ticker, haber_id=did,
                    haber_tarihi=it.published_at.isoformat(),
                    fiyat_haber_ani=fiyat_ani,
                    haber_kategori=kategori_of(it.title),
                    baslik=it.title)

        # fiyat henuz oynamamis mi? (bugun islemde + |degisim| < move_limit)
        if not info or not info["is_today"]:
            continue
        if abs(info["change"]) >= move_limit:
            continue   # haber zaten fiyatlanmaya baslamis

        gonderilmis = set(db.alert_levels_today(ticker, today))
        for it in taze:
            did = it.disclosure_id or (it.title or "")[:40]
            tok = f"KAPHIZLI:{did}"
            if tok in gonderilmis:
                continue
            db.record_alert(ticker, today, tok, info["change"])
            gonderilmis.add(tok)
            hits.append({"ticker": ticker, "change": info["change"], "item": it})

    if not hits:
        print(f"[{now:%Y-%m-%d %H:%M}] Fiyatlanmamis yeni KAP haberi yok "
              f"({checked} hisse tarandi).")
        return 0

    lines = [f"<b>\U0001F4F0 FIYATLANMAMIS HABER</b> — {now:%Y-%m-%d %H:%M}",
             "<i>Son 30 dk'da KAP bildirimi cikti, fiyat henuz oynamadi (firsat penceresi)</i>",
             ""]
    for h in hits:
        it = h["item"]
        url = getattr(it, "url", None)
        baslik = it.title or "(baslik yok)"
        if url:
            baslik = f'<a href="{url}">{baslik}</a>'
        lines.append(f"⚡ <b>{h['ticker']}</b> ({h['change']:+}%): {baslik} "
                     f"<i>[{it.published_at:%H:%M}]</i>")
    sonuc = telegram.broadcast("\n".join(lines))
    ok = [c for c, s in sonuc.items() if s == "ok"]
    print(f"[{now:%Y-%m-%d %H:%M}] {len(hits)} fiyatlanmamis KAP haberi -> "
          f"{len(ok)}/{len(sonuc)} aliciya gonderildi.")
    return 0


def build_message(price_alerts, news_alerts, vol_alerts, now):
    acil = [a for a in price_alerts if a["seviye"] == "ACIL"]
    izle = [a for a in price_alerts if a["seviye"] == "IZLE"]
    lines = [f"<b>\U0001F525 Sicak Uyari</b> — {now:%Y-%m-%d %H:%M}", ""]

    def fmt(a):
        arrow = "\U0001F4C8" if a["change"] > 0 else "\U0001F4C9"
        sign = "+" if a["change"] > 0 else ""
        return (f"{arrow} <b>{a['ticker']}</b>  {sign}{a['change']}%  "
                f"({a['prev_close']} → {a['last_close']} TL)")

    if acil:
        lines.append(f"{_EMOJI['ACIL']} <b>ACIL</b>")
        lines += ["  " + fmt(a) for a in acil]
        lines.append("")
    if izle:
        lines.append(f"{_EMOJI['IZLE']} <b>IZLE</b>")
        lines += ["  " + fmt(a) for a in izle]
        lines.append("")
    if vol_alerts:
        # son 5 gun ortalamasinin 3 katindan fazla -> COK YUKSEK hacim
        lines.append(f"{_EMOJI['HACIM']} <b>COK YUKSEK HACIM</b> "
                     "<i>(5 gun ortalamasinin 3x+ usti)</i>")
        for a in vol_alerts:
            sign = "+" if a.get("change", 0) > 0 else ""
            lines.append(f"  ⚡ <b>{a['ticker']}</b> hacim {a['kat']}x "
                         f"(fiyat {sign}{a['change']}%)")
        lines.append("")
    if news_alerts:
        # KAP haberi var ama fiyat henuz oynamamis -> firsat penceresi
        lines.append(f"{_EMOJI['HABER']} <b>ACIL · FIYATLANMAMIS HABER</b> "
                     "<i>(fiyat henuz oynamadi)</i>")
        for a in news_alerts:
            h = a["haber"]
            lines.append(f"  ⚡ <b>{a['ticker']}</b> ({a['change']:+}%): "
                         f"{h.get('baslik')} <i>[{h.get('tarih')}]</i>")
    return "\n".join(lines).rstrip()


def main():
    now = datetime.now(_TZ)
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yapilandirilmamis - uyari atlandi.")
        return 0

    from src.news.service import get_news_source
    from src.news.fundamental_source import get_volume_anomaly
    news_src, _ = get_news_source(verbose=False)

    today = now.date().isoformat()
    price_alerts, news_alerts, vol_alerts = [], [], []
    checked = 0
    for ticker in load_watchlist():
        info = intraday_change(ticker, today=now.date())
        if not info or not info["is_today"]:
            continue
        checked += 1
        level = classify(info["change"])
        if level:
            # FIYAT uyarisi (%2+ IZLE / %5+ ACIL) — spam onleme
            sent = max((level_rank(l) for l in db.alert_levels_today(ticker, today)),
                       default=0)
            if level_rank(level) > sent:
                db.record_alert(ticker, today, level, info["change"])
                price_alerts.append({"ticker": ticker, "seviye": level, **info})
        else:
            # Fiyat oynamamis -> KAP'ta taze fiyatlanmamis haber var mi? -> ACIL
            haber = unpriced_fresh_news(ticker, news_src)
            if haber and "HABER" not in db.alert_levels_today(ticker, today):
                db.record_alert(ticker, today, "HABER", info["change"])
                news_alerts.append({"ticker": ticker, "change": info["change"],
                                    "haber": haber})

        # HACIM anomalisi: COK YUKSEK (5g ort. 3x+) -> uyari (gunde bir kez)
        try:
            va = get_volume_anomaly(ticker)
        except Exception:
            va = {}
        if va.get("seviye") == "COK YUKSEK" and \
                "HACIM" not in db.alert_levels_today(ticker, today):
            db.record_alert(ticker, today, "HACIM", va.get("kat") or 0)
            vol_alerts.append({"ticker": ticker, "kat": va.get("kat"),
                               "change": info["change"]})

    if not price_alerts and not news_alerts and not vol_alerts:
        print(f"[{now:%Y-%m-%d %H:%M}] Yeni uyari yok ({checked} hisse bugun islemde).")
        return 0

    sonuc = telegram.broadcast(build_message(price_alerts, news_alerts, vol_alerts, now))
    ok = [c for c, s in sonuc.items() if s == "ok"]
    print(f"[{now:%Y-%m-%d %H:%M}] {len(price_alerts)} fiyat + {len(news_alerts)} haber + "
          f"{len(vol_alerts)} hacim uyarisi -> {len(ok)}/{len(sonuc)} aliciya gonderildi.")
    return 0


if __name__ == "__main__":
    # 'kap' argumani: gun ici fiyatlanmamis KAP haberi taramasi (15 dk'da bir)
    if len(sys.argv) > 1 and sys.argv[1].lower() == "kap":
        sys.exit(scan_kap_unpriced())
    sys.exit(main())
