"""Sicak uyari kosusu (cron: hafta ici 10:00-18:00, her 30 dk).

Watchlist'teki her hisseyi kontrol eder; ACIL/IZLE seviyesindeki YENI uyarilari
Telegram'a gonderir. Spam onleme: ayni hisseye gun icinde ayni/daha dusuk
seviyede tekrar gondermez. Bayat/tatil verisinde (bugun bari yoksa) uyarmaz.
"""
import hashlib
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


def _kap_key(disclosure_id, baslik) -> str:
    """Bir KAP bildirimi icin KARARLI dedup anahtari.

    disclosure_id (KAP disclosureIndex) varsa onu, yoksa baslik hash'ini kullanir.
    Hem gunluk uyari (main) hem hizli KAP taramasi (scan_kap_unpriced) AYNI anahtari
    uretir; boylece ayni bildirim iki yoldan/iki taramadan tekrar gonderilmez.
    """
    if disclosure_id:
        base = f"id:{disclosure_id}"
    else:
        base = "t:" + " ".join((baslik or "").lower().split())
    return "KAP:" + hashlib.md5(base.encode("utf-8")).hexdigest()[:12]


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


# Bildirimler sekmesinin okudugu KAP yorum deposu (web app.py get_alerts)
_KAP_YORUM_PATH = Path(__file__).resolve().parents[2] / "data" / "kap_yorumlar.json"


def _kap_yorum(ticker, haber):
    """Bu KAP bildirimi bu hisse icin olumlu mu olumsuz mu? 1-2 cumle AI yorumu.
    Anahtar yoksa/hata olursa None (sessiz)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    baslik = (haber.get("baslik") or "").strip()
    if not baslik:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5", max_tokens=120,
            system=("Sen Max'sin: 40 yasinda, 25 yillik tecrubeli bir Turk borsa uzmani. "
                    "Direkt ve net, gereksiz yumusatmazsin. Verilen KAP bildiriminin "
                    "bu hisse icin OLUMLU mu OLUMSUZ mu yoksa NOTR mu oldugunu 1-2 kisa "
                    "cumlede degerlendir; etkinin yonunu ve nedenini soyle. Sade Turkce, "
                    "jargon yok, markdown yok. Kesin al/sat tavsiyesi verme, veri uydurma."),
            messages=[{"role": "user", "content":
                       f"Hisse: {ticker}\nKAP bildirimi: {baslik}\n"
                       "Bu bildirim bu hisse icin ne anlama geliyor?"}],
        )
        t = "".join(getattr(b, "text", "") for b in resp.content
                    if getattr(b, "type", "") == "text").strip()
        return t or None
    except Exception:
        return None


def _kaydet_kap_yorum(ticker, haber, yorum, tarih):
    """KAP yorumunu data/kap_yorumlar.json'a yazar (hisse basina son kayit)."""
    if not yorum:
        return
    try:
        import json
        d = {}
        if _KAP_YORUM_PATH.exists():
            d = json.loads(_KAP_YORUM_PATH.read_text(encoding="utf-8"))
        d[ticker.upper()] = {"tarih": tarih, "baslik": haber.get("baslik"),
                             "url": haber.get("url"), "yorum": yorum}
        _KAP_YORUM_PATH.parent.mkdir(exist_ok=True)
        _KAP_YORUM_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    except Exception:
        pass


def _hareket_sebebi(ticker, change, haberler, now=None):
    """Fiyat hareketinin OLASI nedeni (1 cumle). Varsa o gun cikan KAP haberiyle
    iliskilendirir. Anahtar yoksa/hata olursa None (sessiz)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    now = now or datetime.now(_TZ)
    bugun = now.date().isoformat()
    taze = [h for h in (haberler or []) if str(h.get("tarih", "")).startswith(bugun)]
    if taze:
        haber_txt = "\n".join(f"- {h.get('baslik')} (fiyatlanma: {h.get('fiyatlanma')})"
                              for h in taze[:5])
    else:
        haber_txt = "(bugun bu hisseye dair KAP haberi yok)"
    yon = "yukseldi" if change > 0 else "dustu"
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5", max_tokens=110,
            system=("Sen Max'sin: 25 yillik tecrubeli bir Turk borsa uzmani. Bir hissenin "
                    "gun ici fiyat hareketinin OLASI nedenini TEK kisa cumlede acikla. "
                    "Asagida bugunku KAP haberleri varsa hareketi DOGRUDAN onlarla "
                    "iliskilendir; haber yoksa 'belirgin KAP haberi yok, muhtemelen "
                    "piyasa/sektor kaynakli' de. Veri veya haber UYDURMA, kesin neden "
                    "iddia etme, sade Turkce, markdown yok, tek cumle."),
            messages=[{"role": "user", "content":
                       f"Hisse: {ticker}\nBugunku hareket: %{change:+} ({yon})\n"
                       f"Bugunku KAP haberleri:\n{haber_txt}\n"
                       "Bu hareket neden olmus olabilir? Tek cumle."}],
        )
        t = "".join(getattr(b, "text", "") for b in resp.content
                    if getattr(b, "type", "") == "text").strip()
        return t or None
    except Exception:
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
            tok = _kap_key(it.disclosure_id, it.title)
            if tok in gonderilmis:
                continue   # ayni bildirim bugun zaten gonderildi (main veya onceki tarama)
            db.record_alert(ticker, today, tok, info["change"])
            gonderilmis.add(tok)
            haber = {"baslik": it.title, "url": it.url}
            yorum = _kap_yorum(ticker, haber)          # AI: olumlu/olumsuz/notr yorum
            _kaydet_kap_yorum(ticker, haber, yorum, today)
            hits.append({"ticker": ticker, "change": info["change"],
                         "item": it, "yorum": yorum})

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
        if h.get("yorum"):
            lines.append(f"     💡 {h['yorum']}")
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
        line = (f"{arrow} <b>{a['ticker']}</b>  {sign}{a['change']}%  "
                f"({a['prev_close']} → {a['last_close']} TL)")
        if a.get("sebep"):
            line += f"\n     💡 {a['sebep']}"
        return line

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
            if a.get("yorum"):
                lines.append(f"     💡 {a['yorum']}")
    return "\n".join(lines).rstrip()


def main():
    now = datetime.now(_TZ)
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yapilandirilmamis - uyari atlandi.")
        return 0

    from src.news.service import get_news_source, filtered_news
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
                # SEBEP: neden dustu/yukseldi? Varsa o gun cikan KAP haberiyle iliskilendir.
                try:
                    haberler = filtered_news(ticker, source=news_src)
                except Exception:
                    haberler = []
                sebep = _hareket_sebebi(ticker, info["change"], haberler, now=now)
                price_alerts.append({"ticker": ticker, "seviye": level,
                                     "sebep": sebep, **info})
        else:
            # Fiyat oynamamis -> KAP'ta taze fiyatlanmamis haber var mi? -> ACIL
            haber = unpriced_fresh_news(ticker, news_src)
            # Dedup: hizli KAP taramasiyla AYNI anahtar -> ayni bildirim tekrar gitmez
            tok = _kap_key(haber.get("disclosure_id"), haber.get("baslik")) if haber else None
            if haber and tok not in db.alert_levels_today(ticker, today):
                db.record_alert(ticker, today, tok, info["change"])
                yorum = _kap_yorum(ticker, haber)          # AI: olumlu/olumsuz yorum
                _kaydet_kap_yorum(ticker, haber, yorum, today)
                news_alerts.append({"ticker": ticker, "change": info["change"],
                                    "haber": haber, "yorum": yorum})

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


def _alarm_price(sym: str):
    """Alarm kontrolu icin tek sembolun guncel fiyati (yfinance). None olabilir."""
    try:
        import yfinance as yf
        t = yf.Ticker(sym)
        h = t.history(period="1d")
        if h is not None and not h.empty:
            c = h["Close"].dropna()
            if len(c):
                return round(float(c.iloc[-1]), 4)
        fi = t.fast_info
        lp = fi.get("last_price") if hasattr(fi, "get") else None
        return round(float(lp), 4) if lp else None
    except Exception:
        return None


def _notify_alarm(kullanici_id, msg: str) -> bool:
    """Alarmi ilgili kullaniciya (telegram_id varsa) gonderir; yoksa broadcast."""
    if not telegram.is_configured():
        return False
    try:
        tid = None
        for u in db.list_users():
            if u.get("id") == kullanici_id:
                tid = u.get("telegram_id")
                break
        if tid:
            telegram.send_message(msg, chat_id=str(tid))
            return True
        res = telegram.broadcast(msg)
        return any(v == "ok" for v in res.values())
    except Exception as e:
        print(f"[alarm] gonderim hatasi: {type(e).__name__}")
        return False


def check_price_alarms(now=None) -> int:
    """Aktif fiyat alarmlarini kontrol eder; hedef gecilmise Telegram'a bildirir ve
    alarmi pasif yapar. (KAP taramasiyla birlikte 15 dk'da bir calisir.)"""
    now = now or datetime.now(_TZ)
    try:
        alarms = db.list_price_alarms(aktif=True)
    except Exception as e:
        print(f"[alarm] DB hatasi: {type(e).__name__}")
        return 0
    if not alarms:
        return 0
    tetik = 0
    for a in alarms:
        tkr = (a.get("ticker") or "").upper()
        usd = (a.get("para_birimi") or "TL").upper() == "USD"
        sym = tkr if usd else f"{tkr}.IS"
        fiyat = _alarm_price(sym)
        if fiyat is None:
            continue
        hedef, yon = a["hedef_fiyat"], a["yon"]
        vurdu = (yon == "yukari" and fiyat >= hedef) or \
                (yon == "asagi" and fiyat <= hedef)
        if not vurdu:
            continue
        birim = "$" if usd else "TL"
        ok = ">=" if yon == "yukari" else "<="
        yon_tr = "yükseldi" if yon == "yukari" else "düştü"
        msg = (f"\U0001F514 <b>Fiyat Alarmı</b>\n{tkr} {yon_tr}: "
               f"<b>{fiyat:g} {birim}</b> ({ok} {hedef:g} {birim}).")
        if _notify_alarm(a["kullanici_id"], msg):
            db.deactivate_price_alarm(a["id"], tetik=True)
            tetik += 1
            print(f"[{now:%Y-%m-%d %H:%M}] [alarm] {tkr} {fiyat} {yon} {hedef} -> bildirildi.")
    return tetik


if __name__ == "__main__":
    # 'kap' argumani: gun ici fiyatlanmamis KAP haberi taramasi (15 dk'da bir)
    if len(sys.argv) > 1 and sys.argv[1].lower() == "kap":
        rc = scan_kap_unpriced()
        try:
            check_price_alarms()          # ayni 15 dk'lik kosuda fiyat alarmlari
        except Exception as e:
            print(f"[alarm] hata: {type(e).__name__}")
        sys.exit(rc)
    sys.exit(main())
