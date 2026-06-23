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
from src.alerts.engine import intraday_change, level_rank
from src.notify import telegram
from src.db import database as db
from src.ai.decision import karar_kelime, karar_emoji, aksiyon_metni

# Gün içi tekrar-bildirim eşikleri (mutlak yüzde hareket)
PORTF_ESIK = 2.5     # portföy hissesi
RADAR_ESIK = 3.0     # radar (portföy dışı) hissesi
ANI_ESIK = 5.0       # ani büyük gelişme (kendi mesaj tipi)

_COMMENTARY_PATH = Path(__file__).resolve().parents[2] / "data" / "ai_commentary.json"
_KARAR_MAP = None


def _portfolio_set():
    """Tüm portföylerdeki benzersiz hisse kodları (normalize)."""
    try:
        return {(r.get("ticker") or "").upper().replace(".IS", "")
                for r in db.list_portfolio() if r.get("ticker")}
    except Exception:
        return set()


def _karar_map():
    """ai_commentary.json'dan {TICKER: final_decision} (bir kez yükle)."""
    global _KARAR_MAP
    if _KARAR_MAP is None:
        _KARAR_MAP = {}
        try:
            import json
            data = json.loads(_COMMENTARY_PATH.read_text(encoding="utf-8"))
            for rec in (data if isinstance(data, list) else []):
                t = (rec.get("ticker") or "").upper()
                if t:
                    _KARAR_MAP.setdefault(t, rec.get("final_decision"))
        except Exception:
            pass
    return _KARAR_MAP


def _son_karar(ticker):
    """Hissenin son AI kararı (final_decision) — gün içi 'KARAR' satırı için."""
    return _karar_map().get((ticker or "").upper())


_STOP_MAP = None


def _stop_loss_map():
    """ai_commentary.json'dan {TICKER: stop_seviyesi(float)} (bir kez yükle).

    Once AI'nin 'stop_loss' metnindeki sayiyi parse eder ('Y TL altina duserse cik');
    metin yoksa deterministik 'stop_loss_seviyesi' (TUT'ta dolan sayi) yedek alinir."""
    global _STOP_MAP
    if _STOP_MAP is None:
        _STOP_MAP = {}
        try:
            import json
            data = json.loads(_COMMENTARY_PATH.read_text(encoding="utf-8"))
            for rec in (data if isinstance(data, list) else []):
                t = (rec.get("ticker") or "").upper()
                if not t:
                    continue
                seviye = _parse_stop_level(rec.get("stop_loss"))
                if seviye is None:
                    sv = rec.get("stop_loss_seviyesi")
                    seviye = float(sv) if isinstance(sv, (int, float)) and sv else None
                if seviye is not None:
                    _STOP_MAP.setdefault(t, seviye)
        except Exception:
            pass
    return _STOP_MAP


def _parse_stop_level(metin):
    """'88 TL altina duserse cik' / '270,5 TL' / '$88 altina' -> 88.0 (float) | None.
    Metindeki ILK sayiyi (binlik nokta + ondalik virgul destekli) ceker."""
    import re
    if not metin:
        return None
    m = re.search(r"\d[\d.,]*", str(metin))
    if not m:
        return None
    s = m.group(0).rstrip(".,")
    if "," in s and "." in s:           # 1.234,56 -> 1234.56
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:                       # 270,5 -> 270.5
        s = s.replace(",", ".")
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def check_stop_loss(now=None):
    """PORTFOY STOP-LOSS kontrolu (30 dk'lik taramada calisir).

    Her kullanicinin portfoyundeki hisseler icin ai_commentary.json'daki stop_loss
    seviyesini okur; guncel fiyat bu seviyenin ALTINA dustuyse o KULLANICIYA ozel
    Telegram bildirimi gonderir. Ayni hisse-kullanici icin gunde bir kez (spam onleme).
    """
    now = now or datetime.now(_TZ)
    if not telegram.is_configured():
        return 0
    smap = _stop_loss_map()
    if not smap:
        return 0
    today = now.date().isoformat()
    tetik = 0
    try:
        users = db.list_users()
    except Exception:
        return 0
    for u in users:
        uid = u.get("id")
        try:
            pozisyonlar = db.list_portfolio(uid)
        except Exception:
            continue
        for p in pozisyonlar:
            tkr = (p.get("ticker") or "").upper().replace(".IS", "")
            seviye = smap.get(tkr)
            if seviye is None:
                continue
            usd = (p.get("para_birimi") or "TL").upper() == "USD"
            sym = tkr if usd else f"{tkr}.IS"
            fiyat = _alarm_price(sym)
            if fiyat is None or fiyat >= seviye:
                continue
            # Spam onleme: bu hisse-kullanici icin bugun zaten gonderildi mi?
            anahtar = f"STOPLOSS:{uid}"
            if anahtar in db.alert_levels_today(tkr, today):
                continue
            db.record_alert(tkr, today, anahtar, fiyat)
            birim = "$" if usd else "TL"
            msg = (f"🔴 <b>STOP-LOSS TETİKLENDİ: {tkr}</b> {fiyat:g} {birim} — "
                   f"Bot hedefi {seviye:g} {birim} altına düştü. "
                   "Pozisyonu gözden geçir.")
            if _notify_alarm(uid, msg):
                tetik += 1
                print(f"[{now:%Y-%m-%d %H:%M}] [stop-loss] {tkr} {fiyat} < "
                      f"{seviye} -> kullanici {uid} bildirildi.")
    return tetik


def _seviye(change_abs, portfoyde):
    """Hareketi içsel uyarı seviyesine indirger: 'ACIL' (ani), 'IZLE' (dikkat) veya None.
    Eşik listeye göre: portföy %2.5, radar %3; %5+ ani."""
    if change_abs >= ANI_ESIK:
        return "ACIL"
    if change_abs >= (PORTF_ESIK if portfoyde else RADAR_ESIK):
        return "IZLE"
    return None


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

    # Taze KAP başlıkları -> şartlı senaryo kontrolü (haber tetikleyici)
    _senaryo_kontrol_ve_bildir(now, basliklar=[h["item"].title or "" for h in hits])

    if not hits:
        print(f"[{now:%Y-%m-%d %H:%M}] Fiyatlanmamis yeni KAP haberi yok "
              f"({checked} hisse tarandi).")
        return 0

    bloklar = []
    for h in hits:
        it = h["item"]
        url = getattr(it, "url", None)
        baslik = it.title or "(başlık yok)"
        if url:
            baslik = f'<a href="{url}">{baslik}</a>'
        blok = [f"📰 <b>{h['ticker']}</b> — yeni KAP bildirimi <i>[{it.published_at:%H:%M}]</i>",
                baslik]
        if h.get("yorum"):
            blok.append(h["yorum"])
        blok.append("Aksiyon: Haber fiyatlanmadan değerlendir.")
        bloklar.append("\n".join(blok))
    bas = (f"<b>GÜN İÇİ DİKKAT</b> — {now:%H:%M}\n"
           "<i>KAP bildirimi çıktı, fiyat henüz oynamadı.</i>")
    sonuc = telegram.broadcast(bas + "\n\n" + "\n\n".join(bloklar))
    ok = [c for c, s in sonuc.items() if s == "ok"]
    print(f"[{now:%Y-%m-%d %H:%M}] {len(hits)} fiyatlanmamis KAP haberi -> "
          f"{len(ok)}/{len(sonuc)} aliciya gonderildi.")
    return 0


def _yon_kelime(change):
    return "yükseldi" if change > 0 else "düştü"


def build_ani_message(ani_alerts, now):
    """ANİ BÜYÜK GELİŞME mesajı (her biri tek blok): ⚡ ANİ GELİŞME / ne oldu /
    etkilenen hisse / Aksiyon: hisse — karar. Boşsa '' döner."""
    if not ani_alerts:
        return ""
    bloklar = []
    for a in ani_alerts:
        kelime = karar_kelime(a.get("karar")) or "BEKLE"
        blok = ["⚡ <b>ANİ GELİŞME</b>",
                f"{a['ticker']} %{abs(a['change']):.1f} {_yon_kelime(a['change'])} "
                f"({a['prev_close']}→{a['last_close']} TL)."]
        if a.get("sebep"):
            blok.append(a["sebep"])
        blok.append(f"Aksiyon: {a['ticker']} — {kelime}")
        bloklar.append("\n".join(blok))
    return "\n\n".join(bloklar)


def build_message(price_alerts, news_alerts, vol_alerts, now):
    """GÜN İÇİ DİKKAT mesajı (yalnızca yeni bilgi). Hisse formatı:
    [emoji] HİSSE — KARAR / gelişme / Aksiyon. Boşsa '' döner."""
    dikkat = [a for a in price_alerts if a["seviye"] == "IZLE"]
    bloklar = []

    for a in dikkat:
        karar = a.get("karar")
        kelime = karar_kelime(karar) or "BEKLE"
        emoji = karar_emoji(karar)
        blok = [f"{emoji} <b>{a['ticker']} — {kelime}</b>",
                f"%{abs(a['change']):.1f} {_yon_kelime(a['change'])} "
                f"({a['prev_close']}→{a['last_close']} TL)."]
        if a.get("sebep"):
            blok.append(a["sebep"])
        blok.append(f"Aksiyon: {aksiyon_metni(karar, a.get('portfoyde'))}")
        bloklar.append("\n".join(blok))

    # Fiyatlanmamış KAP haberi (📰) — fırsat penceresi
    for a in news_alerts:
        h = a["haber"]
        blok = [f"📰 <b>{a['ticker']}</b> — yeni KAP bildirimi",
                f"{h.get('baslik')}"]
        if a.get("yorum"):
            blok.append(a["yorum"])
        blok.append("Aksiyon: Haber fiyatlanmadan değerlendir.")
        bloklar.append("\n".join(blok))

    # Yüksek hacim (kısa, 🟡)
    for a in vol_alerts:
        sign = "+" if a.get("change", 0) > 0 else ""
        bloklar.append(f"🟡 <b>{a['ticker']}</b> — yüksek hacim "
                       f"({a['kat']}x, fiyat {sign}{a['change']}%)")

    if not bloklar:
        return ""
    return f"<b>GÜN İÇİ DİKKAT</b> — {now:%H:%M}\n\n" + "\n\n".join(bloklar)


def _senaryo_kontrol_ve_bildir(now, basliklar=None):
    """Bekleyen şartlı senaryoları kontrol eder; gerçekleşeni ⚡ ile bildirir.
    Güncel usdtry (makro) + bist100 günlük (piyasa) değerlerini geçer; haber
    tipi senaryolar için taze başlıklarda anahtar kelime arar."""
    try:
        from src.ai import senaryo
        usd = None
        try:
            from src.news.macro import get_macro
            usd = get_macro().get("usdtry")
        except Exception:
            usd = None
        bist_gunluk = None
        try:                                   # bist100 makro senaryoları için günlük %
            from src.news.market_overview import get_market_overview
            bist_gunluk = (get_market_overview() or {}).get("bist100_gunluk_%")
        except Exception:
            bist_gunluk = None
        tetik = senaryo.kontrol_et(basliklar=basliklar or [], guncel_usdtry=usd,
                                   guncel_bist_gunluk=bist_gunluk)
        if tetik:
            telegram.broadcast("\n\n".join(s["bildirim"] for s in tetik))
            print(f"[{now:%Y-%m-%d %H:%M}] {len(tetik)} senaryo gerçekleşti -> bildirildi.")
        return len(tetik)
    except Exception as e:
        print(f"[senaryo] kontrol hatasi: {type(e).__name__}")
        return 0


def main():
    now = datetime.now(_TZ)
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yapilandirilmamis - uyari atlandi.")
        return 0

    from src.news.service import get_news_source, filtered_news
    from src.news.fundamental_source import get_volume_anomaly
    news_src, _ = get_news_source(verbose=False)

    today = now.date().isoformat()
    portfolio = _portfolio_set()
    price_alerts, news_alerts, vol_alerts = [], [], []
    checked = 0
    for ticker in load_watchlist():
        info = intraday_change(ticker, today=now.date())
        if not info or not info["is_today"]:
            continue
        checked += 1
        portfoyde = (ticker or "").upper().replace(".IS", "") in portfolio
        # Eşik listeye göre: portföy %2.5, radar %3; %5+ ani gelişme.
        level = _seviye(abs(info["change"]), portfoyde)
        if level:
            # Spam onleme: ayni/daha dusuk seviyede tekrar gonderme
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
                                     "sebep": sebep, "portfoyde": portfoyde,
                                     "karar": _son_karar(ticker), **info})
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

    # Şartlı senaryo kontrolü (makro: usdtry; gün içi her taramada)
    _senaryo_kontrol_ve_bildir(now)

    # Portföy stop-loss kontrolü (kullanıcıya özel, günde bir kez)
    try:
        check_stop_loss(now)
    except Exception as e:
        print(f"[stop-loss] kontrol hatasi: {type(e).__name__}")

    if not price_alerts and not news_alerts and not vol_alerts:
        print(f"[{now:%Y-%m-%d %H:%M}] Yeni uyari yok ({checked} hisse bugun islemde).")
        return 0

    gonderilen = 0
    # 1) ANİ BÜYÜK GELİŞME (%5+) — kendi mesaj tipi
    ani = [a for a in price_alerts if a["seviye"] == "ACIL"]
    ani_msg = build_ani_message(ani, now)
    if ani_msg:
        gonderilen += sum(1 for s in telegram.broadcast(ani_msg).values() if s == "ok")
    # 2) GÜN İÇİ DİKKAT (dikkat + fiyatlanmamış haber + hacim)
    dikkat_msg = build_message(price_alerts, news_alerts, vol_alerts, now)
    if dikkat_msg:
        gonderilen += sum(1 for s in telegram.broadcast(dikkat_msg).values() if s == "ok")

    print(f"[{now:%Y-%m-%d %H:%M}] {len(ani)} ani + {len(price_alerts) - len(ani)} dikkat + "
          f"{len(news_alerts)} haber + {len(vol_alerts)} hacim uyarisi gonderildi.")
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
