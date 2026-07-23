"""Sicak uyari kosusu (cron: hafta ici 10:00-18:00, her 30 dk).

Watchlist'teki her hisseyi kontrol eder; ACIL/IZLE seviyesindeki YENI uyarilari
Telegram'a gonderir. Spam onleme: ayni hisseye gun icinde ayni/daha dusuk
seviyede tekrar gondermez. Bayat/tatil verisinde (bugun bari yoksa) uyarmaz.
"""
import hashlib
import os
import sys
from datetime import datetime, timedelta
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

# --- Token/maliyet ozeti (briefing loglariyla ayni "TOKEN OZET" formati) ---
# Tum AI cagrilari Haiku 4.5 (gercek-zamanli). MTok basina $: cache_write=1.25x
# input, cache_read=0.10x input.
_FIYAT_INPUT = 1.00 / 1_000_000
_FIYAT_OUTPUT = 5.00 / 1_000_000
_FIYAT_CACHE_WRITE = _FIYAT_INPUT * 1.25
_FIYAT_CACHE_READ = _FIYAT_INPUT * 0.10
_TOKEN_ACC = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def _ai_create(**kwargs):
    """Anthropic mesaj cagrisi + token kullanimini _TOKEN_ACC'ye toplar.
    Tum run_alerts AI cagrilari bunun uzerinden gecer ki kosu sonunda
    TOKEN OZET (alerts.log) basilabilsin."""
    import anthropic
    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(**kwargs)
    except Exception:
        try:                                    # gunluk AI hata sayaci (health_monitor okur)
            from src.db import database as db
            db.ai_hata_inc()
        except Exception:
            pass
        raise
    try:
        u = resp.usage
        _TOKEN_ACC["input"] += getattr(u, "input_tokens", 0) or 0
        _TOKEN_ACC["output"] += getattr(u, "output_tokens", 0) or 0
        _TOKEN_ACC["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        _TOKEN_ACC["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
    except Exception:
        pass
    return resp


def _token_ozet():
    """Kosu sonunda toplam token/maliyet ozetini basar (briefing'lerle ayni format).
    Hic AI cagrisi olmadiysa (cogu */30 sektor kosusunda) sessizce atlar ki
    alerts.log sifir satirlariyla dolmasin."""
    a = _TOKEN_ACC
    if not (a["input"] or a["output"] or a["cache_read"] or a["cache_write"]):
        return
    maliyet = (a["input"] * _FIYAT_INPUT + a["output"] * _FIYAT_OUTPUT
               + a["cache_write"] * _FIYAT_CACHE_WRITE
               + a["cache_read"] * _FIYAT_CACHE_READ)
    tarih = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")
    print(f"[{tarih}] TOKEN OZET: input={a['input']}, output={a['output']}, "
          f"cache_hit={a['cache_read']}, cache_write={a['cache_write']}, "
          f"tahmini_maliyet=${maliyet:.4f}")


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


def _karar_kararsiz(karar) -> bool:
    """Karar KARARSIZ/nötr mü? (BEKLE veya TUT). Böyle kararlarda düşük seviyeli
    gün içi hareket bildirimi bastırılır; yalnız AL/SAT/AZALT/UZAK_DUR'da gönderilir.
    İstisnalar (çağıran tarafta): %5+ ani hareket ve taze KAP haberi her zaman geçer."""
    k = (karar or "").upper()
    return "BEKLE" in k or "TUT" in k


_STOP_MAP = None


def _stop_loss_map():
    """ai_commentary.json'dan {TICKER: stop_seviyesi(float)} (bir kez yükle).

    Once deterministik sayisal 'stop_loss_seviyesi' alanini kullanir (varsa, en
    guvenilir); yoksa AI'nin serbest metin 'stop_loss' alanindan regex ile sayi
    cikarir ('Y TL altina duserse cik')."""
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
                seviye = _stop_seviye(rec)
                if seviye is not None:
                    _STOP_MAP.setdefault(t, seviye)
        except Exception:
            pass
    return _STOP_MAP


def _stop_seviye(rec):
    """Bir ai_commentary kaydindan stop seviyesini cozer: ONCE sayisal
    'stop_loss_seviyesi' alani, YOKSA 'stop_loss' metninden parse. None olabilir."""
    sv = rec.get("stop_loss_seviyesi")
    if isinstance(sv, (int, float)) and sv > 0:
        return float(sv)
    return _parse_stop_level(rec.get("stop_loss"))


def _sayi_to_float(s):
    """'1.234,56'/'270,5'/'270' gibi Turkce/ABD bicimli sayi metnini float'a cevirir."""
    s = str(s).strip().rstrip(".,")
    if "," in s and "." in s:            # 1.234,56 -> 1234.56 (nokta binlik)
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:                        # 270,5 -> 270.5 (virgul ondalik)
        s = s.replace(",", ".")
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


def _parse_stop_level(metin):
    """Serbest metinden stop seviyesini (float) cikarir; bulunamazsa None.

    Yil/yuzde gibi alakasiz sayilara takilmamak icin ONCE para birimi ('TL','$',
    '₺') veya 'alt(ı/ına)'/'below'/'under' anahtarina KOMSU sayiyi tercih eder;
    bulamazsa metindeki ilk sayiya duser.
    Ornek: '270 TL altina duserse cik' -> 270.0, "295'in alti" -> 295.0,
           '$270 below' -> 270.0, '270,5 TL' -> 270.5."""
    import re
    if not metin:
        return None
    s = str(metin)
    desenler = (
        r"\$\s*([\d.,]+)",                                   # $270
        r"([\d.,]+)\s*(?:TL|₺|\$|usd|dolar|tl)\b",           # 270 TL / 270₺
        r"([\d.,]+)\s*(?:'\w+)?\s*alt",                      # 295'in alti / 270 altina
        r"(?:below|under)\s*\$?\s*([\d.,]+)",                # below 270
    )
    for d in desenler:
        m = re.search(d, s, re.IGNORECASE)
        if m:
            v = _sayi_to_float(m.group(1))
            if v is not None:
                return v
    m = re.search(r"\d[\d.,]*", s)                            # yedek: ilk sayi
    return _sayi_to_float(m.group(0)) if m else None


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
            sym, usd = _sembol_usd(p)
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


_HEDEF_MAP = None


def _hedef_seviye(rec):
    """Bir ai_commentary kaydindan hedef fiyat seviyesini cozer: ONCE sayisal
    'hedef_fiyat_seviyesi', YOKSA serbest metin 'hedef_fiyat'tan ilk fiyat. None olabilir."""
    hv = rec.get("hedef_fiyat_seviyesi")
    if isinstance(hv, (int, float)) and hv > 0:
        return float(hv)
    metin = rec.get("hedef_fiyat")
    if not metin:
        return None
    try:
        from src.ai.commentary import parse_first_price
        v = parse_first_price(metin)
        return v if isinstance(v, (int, float)) and v > 0 else None
    except Exception:
        return None


def _hedef_fiyat_map():
    """ai_commentary.json'dan {TICKER: hedef_seviye(float)} (bir kez yükle).
    Yalniz hedef fiyati TANIMLI (genelde AL) hisseler haritaya girer."""
    global _HEDEF_MAP
    if _HEDEF_MAP is None:
        _HEDEF_MAP = {}
        try:
            import json
            data = json.loads(_COMMENTARY_PATH.read_text(encoding="utf-8"))
            for rec in (data if isinstance(data, list) else []):
                t = (rec.get("ticker") or "").upper()
                if not t:
                    continue
                seviye = _hedef_seviye(rec)
                if seviye is not None:
                    _HEDEF_MAP.setdefault(t, seviye)
        except Exception:
            pass
    return _HEDEF_MAP


def _pozisyon_us(p, tkr):
    """Pozisyon ABD hissesi mi? para_birimi=USD ya da enstruman ana tablosu isareti."""
    if (p.get("para_birimi") or "TL").upper() == "USD":
        return True
    try:
        return db.is_us_instrument(tkr)
    except Exception:
        return False


def check_hedef_fiyat(now=None):
    """PORTFOY HEDEF-FIYAT kontrolu (30 dk'lik taramada calisir).

    Her kullanicinin portfoyundeki hisseler icin ai_commentary.json'daki hedef
    fiyat seviyesini okur; guncel fiyat bu seviyeye ULASTI/GECTIYSE o KULLANICIYA
    ozel Telegram bildirimi gonderir. Ayni hisse-kullanici icin gunde bir kez (spam onleme).
    """
    now = now or datetime.now(_TZ)
    if not telegram.is_configured():
        return 0
    hmap = _hedef_fiyat_map()
    if not hmap:
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
            hedef = hmap.get(tkr)
            if hedef is None:
                continue
            sym, usd = _sembol_usd(p)
            fiyat = _alarm_price(sym)
            if fiyat is None or fiyat < hedef:      # henuz hedefe ulasmadi
                continue
            anahtar = f"HEDEF:{uid}"
            if anahtar in db.alert_levels_today(tkr, today):
                continue
            db.record_alert(tkr, today, anahtar, fiyat)
            birim = "$" if usd else "TL"
            msg = (f"🎯 <b>HEDEF ULAŞILDI: {tkr}</b> {hedef:g} {birim} hedefine ulaştı! "
                   "Kâr realize etmeyi değerlendirin.")
            if _notify_alarm(uid, msg):
                tetik += 1
                print(f"[{now:%Y-%m-%d %H:%M}] [hedef-fiyat] {tkr} {fiyat} >= "
                      f"{hedef} -> kullanici {uid} bildirildi.")
    return tetik


def _sembol_usd(rec: dict):
    """Bir pozisyon/trade kaydi icin (yfinance_sembolu, usd_mu).

    ABD tespiti ve sembol uretimi (.IS ekleme/cikarma) enstruman ana tablosundan
    (instruments) okunur; tabloda yoksa para_birimi=USD ise duz sembol, degilse
    BIST ('.IS'). Tum hedef/stop/takip bildirimleri ayni mantigi paylasir."""
    tkr = (rec.get("ticker") or "").upper().replace(".IS", "")
    usd = (rec.get("para_birimi") or "TL").upper() == "USD"
    try:
        usd = usd or db.is_us_instrument(tkr)
        if db.get_instrument(tkr) is not None:
            return db.instrument_symbol(tkr), usd
    except Exception:
        pass
    return (tkr if usd else f"{tkr}.IS"), usd


# Geriye donuk uyum: eski cagri adi
_takip_sembol = _sembol_usd


def check_pozisyon_takip(now=None) -> int:
    """AL POZİSYONU TAKİP bildirimi (30 dk'lik taramada çalışır).

    `trades` tablosundaki her açık pozisyon için giriş/hedef/stop seviyelerine
    göre kademeli bildirim gönderir (her kademe gün içinde bir kez, spam önleme):
      - hedef ilerlemesi %50 / %80 / %100 (giriş→hedef arası)
      - stop yastığı %50'nin altına inince (giriş→stop arası kalan mesafe)
    Pozisyonun sahibine (kullanici_id) özel; sahibi yoksa broadcast'e düşer.
    """
    now = now or datetime.now(_TZ)
    if not telegram.is_configured():
        return 0
    try:
        acik = db.list_trades(durum="acik")
    except Exception as e:
        print(f"[pozisyon-takip] DB hatasi: {type(e).__name__}")
        return 0
    today = now.date().isoformat()
    tetik = 0
    for t in acik:
        tkr = (t.get("ticker") or "").upper().replace(".IS", "")
        entry = t.get("entry_fiyat")
        if not entry:
            continue
        sym, usd = _takip_sembol(t)
        fiyat = _alarm_price(sym)
        if fiyat is None:
            continue
        uid = t.get("kullanici_id") or 0
        birim = "$" if usd else "TL"
        gonderilen = db.alert_levels_today(tkr, today)

        def _gonder(key: str, msg: str) -> bool:
            nonlocal tetik
            if key in gonderilen:
                return False
            db.record_alert(tkr, today, key, fiyat)
            if _notify_alarm(uid, msg):
                tetik += 1
                print(f"[{now:%Y-%m-%d %H:%M}] [pozisyon-takip] {tkr} {key} "
                      f"@ {fiyat:g} -> kullanici {uid}")
                return True
            return False

        # Hedef ilerlemesi (giriş -> hedef). En yüksek YENİ kademeyi gönder.
        hedef = t.get("hedef_fiyat")
        if hedef and hedef > entry:
            ilerleme = (fiyat - entry) / (hedef - entry)
            if ilerleme >= 1.0:
                _gonder(f"TAKIP100:{uid}",
                        f"🎯 <b>{tkr}</b> hedefe ulaştı! Kâr realize etmeyi değerlendir.")
            elif ilerleme >= 0.80:
                _gonder(f"TAKIP80:{uid}",
                        f"⚠️ <b>{tkr}</b> hedefin %80'ine ulaştı. Stop'u giriş fiyatının "
                        f"üzerine çekmeyi değerlendir. Giriş: {entry:g} | Şu an: {fiyat:g} "
                        f"| Hedef: {hedef:g}")
            elif ilerleme >= 0.50:
                _gonder(f"TAKIP50:{uid}",
                        f"📈 <b>{tkr}</b> hedefin yarısına ulaştı! Giriş: {entry:g} | "
                        f"Şu an: {fiyat:g} | Hedef: {hedef:g}")

        # Stop yastığı (giriş -> stop arası kalan mesafe %50'nin altına inince).
        stop = t.get("stop_fiyat")
        if stop and entry > stop:
            kalan = (fiyat - stop) / (entry - stop)   # 1.0 girişte, 0 stopta
            if kalan < 0.50:
                _gonder(f"TAKIPSTOP:{uid}",
                        f"⚠️ <b>{tkr}</b> stop'a yaklaşıyor! Giriş: {entry:g} | "
                        f"Şu an: {fiyat:g} | Stop: {stop:g}")
    return tetik


# ---------------------------------------------------------------------------
# DİNAMİK ÇIKIŞ KONTROLLERİ (main() ile 30 dk'da bir, seans içinde)
# Açık AL pozisyonları (trades, durum='acik', karar 'AL'*) için erken çıkış sinyali:
#   1) trend bozuldu (son 3 günde -%5+)  2) haber değişti (olumsuz taze KAP)
#   3) makro rejim Risk-On -> Risk-Off geçişi.
# Hepsi günde bir kez (uyari_kayit ile spam önleme).
# ---------------------------------------------------------------------------
def _acik_al_pozisyonlari():
    """trades tablosundaki açık AL pozisyonları (durum='acik', karar 'AL' ile başlar)."""
    try:
        acik = db.list_trades(durum="acik")
    except Exception as e:
        print(f"[dinamik-cikis] DB hatasi: {type(e).__name__}")
        return []
    return [t for t in acik if (t.get("karar") or "").upper().startswith("AL")]


def _uc_gun_getiri(sym):
    """Son 3 işlem günü % değişimi (yfinance 7d penceresi). Veri yoksa None."""
    try:
        import yfinance as yf
        h = yf.Ticker(sym).history(period="7d")
        if h is None or h.empty:
            return None
        c = h["Close"].dropna()
        if len(c) < 4:
            return None
        onceki, son = float(c.iloc[-4]), float(c.iloc[-1])   # 3 işlem günü önce -> son
        if onceki <= 0:
            return None
        return round((son - onceki) / onceki * 100, 2)
    except Exception:
        return None


def check_trend_break(now=None) -> int:
    """TREND BOZULDU: açık AL pozisyonu son 3 günde -%5'ten fazla düştüyse uyar."""
    now = now or datetime.now(_TZ)
    if not telegram.is_configured():
        return 0
    today = now.date().isoformat()
    tetik = 0
    for t in _acik_al_pozisyonlari():
        tkr = (t.get("ticker") or "").upper().replace(".IS", "")
        uid = t.get("kullanici_id") or 0
        key = f"TRENDBROKE:{uid}"
        if key in db.alert_levels_today(tkr, today):
            continue
        sym, _usd = _sembol_usd(t)
        g3 = _uc_gun_getiri(sym)
        if g3 is None or g3 > -5.0:
            continue
        db.record_alert(tkr, today, key, g3)
        if _notify_alarm(uid, f"⚠️ <b>{tkr}</b>'da trend bozuldu — son 3 günde "
                              f"%{g3:.1f}. Pozisyonda çıkışı değerlendir."):
            tetik += 1
            print(f"[{now:%Y-%m-%d %H:%M}] [trend-break] {tkr} {g3:.1f}% -> kullanici {uid}")
    return tetik


def check_news_flip(now=None) -> int:
    """HABER DEĞİŞTİ: açık AL pozisyonuna TAZE + FİYATLANMAMIŞ bir KAP bildirimi
    gelip OLUMSUZ yorumlanıyorsa uyar (long'tayken negatif haber). Bildirim başına
    günde bir kez."""
    now = now or datetime.now(_TZ)
    if not telegram.is_configured():
        return 0
    try:
        from src.news.service import get_news_source
        news_src, _ = get_news_source(verbose=False)
    except Exception:
        news_src = None
    today = now.date().isoformat()
    tetik = 0
    for t in _acik_al_pozisyonlari():
        tkr = (t.get("ticker") or "").upper().replace(".IS", "")
        uid = t.get("kullanici_id") or 0
        haber = unpriced_fresh_news(tkr, news_src)
        if not haber:
            continue
        key = f"NEWSFLIP:{uid}:{_kap_key(haber.get('disclosure_id'), haber.get('baslik'))}"
        if key in db.alert_levels_today(tkr, today):
            continue
        yorum = _kap_yorum(tkr, haber)
        if not yorum or yorum.get("yon") != "olumsuz":
            continue                          # yalnız açıkça OLUMSUZ KAP'ta uyar (notr/olumlu atla)
        db.record_alert(tkr, today, key, 0)
        baslik = (haber.get("baslik") or "").strip()[:120]
        _cumle = yorum.get("cumle") or "Negatif KAP bildirimi."
        if _notify_alarm(uid, f"📰 <b>{tkr}</b> için haber değişti — negatif KAP:\n"
                              f"“{baslik}”\n{_cumle}\n"
                              f"Hedefi/pozisyonu gözden geçir."):
            tetik += 1
            print(f"[{now:%Y-%m-%d %H:%M}] [news-flip] {tkr} -> kullanici {uid}")
    return tetik


def check_regime_flip(now=None) -> int:
    """MAKRO REJİM DEĞİŞTİ: piyasa rejimi Risk-On'dan Risk-Off'a geçtiyse ve açık AL
    pozisyonu varsa, sahiplerine tek uyarı gönderir. Önceki rejim ayar tablosunda
    saklanır; yalnız On->Off kenarında tetiklenir (günde bir kez)."""
    now = now or datetime.now(_TZ)
    if not telegram.is_configured():
        return 0
    try:
        from src.ai.kombinasyon import guncel_rejim
        rej = (guncel_rejim() or {}).get("rejim")
    except Exception:
        return 0
    if not rej:
        return 0
    onceki = db.get_setting("son_makro_rejim")
    db.set_setting("son_makro_rejim", rej)
    if onceki != "Risk-On" or rej != "Risk-Off":
        return 0                              # yalnız Risk-On -> Risk-Off geçişi
    al_pozlar = _acik_al_pozisyonlari()
    if not al_pozlar:
        return 0
    today = now.date().isoformat()
    if "REGIMEFLIP" in db.alert_levels_today("_MARKET_", today):
        return 0
    db.record_alert("_MARKET_", today, "REGIMEFLIP", 0)
    msg = ("🌡️ <b>Makro rejim değişti: Risk-On → Risk-Off.</b> Portföydeki AL "
           "pozisyonlarını gözden geçir; yeni risk almada temkinli ol.")
    tetik = 0
    for uid in {t.get("kullanici_id") or 0 for t in al_pozlar}:
        if _notify_alarm(uid, msg):
            tetik += 1
    print(f"[{now:%Y-%m-%d %H:%M}] [regime-flip] Risk-On->Risk-Off, {tetik} kullanici")
    return tetik


def _islem_gunu_gecti(baslangic_iso, bugun_date) -> int | None:
    """baslangic (dahil) -> bugun (haric) arasi gecen ISLEM GUNU (Pzt-Cum) sayisi.
    Hafta sonlarini eler (resmi tatilleri saymaz; yaklasik). Parse hatasinda None."""
    try:
        import numpy as np
        a = datetime.fromisoformat(str(baslangic_iso)[:10]).date()
        return int(np.busday_count(a.isoformat(), bugun_date.isoformat()))
    except Exception:
        return None


def check_early_loss(now=None) -> int:
    """ERKEN UYARI (ilk 2 gun): acik AL pozisyonu acilistan itibaren >=2 islem gunu
    gecmis, hic kara girmemis (max_profit<=0) ve su an -%2'den fazla zararda ise
    sahibine gunde bir kez Telegram uyarisi gonderir."""
    now = now or datetime.now(_TZ)
    if not telegram.is_configured():
        return 0
    today = now.date().isoformat()
    tetik = 0
    for t in _acik_al_pozisyonlari():
        tkr = (t.get("ticker") or "").upper().replace(".IS", "")
        entry = t.get("entry_fiyat")
        if not entry:
            continue
        gun = _islem_gunu_gecti(t.get("acilis_tarihi"), now.date())
        if gun is None or gun < 2:
            continue
        if (t.get("max_profit") or 0) > 0:        # bir kez kara girmis -> erken uyari degil
            continue
        uid = t.get("kullanici_id") or 0
        key = f"EARLYLOSS:{uid}"
        if key in db.alert_levels_today(tkr, today):
            continue
        sym, _usd = _sembol_usd(t)
        fiyat = _alarm_price(sym)
        if fiyat is None:
            continue
        pnl = (fiyat - entry) / entry * 100
        if pnl >= -2.0:                            # yeterli zarar yok
            continue
        db.record_alert(tkr, today, key, round(pnl, 2))
        if _notify_alarm(uid, f"⚠️ <b>ERKEN UYARI:</b> {tkr} 2 gündür hiç kâra girmedi, "
                              f"-%{abs(pnl):.1f} zararda. Pozisyonu gözden geçir."):
            tetik += 1
            print(f"[{now:%Y-%m-%d %H:%M}] [early-loss] {tkr} {pnl:.1f}% -> kullanici {uid}")
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


def _kap_baslik_norm(baslik: str) -> str:
    """KAP basligini dedup icin normalize eder (yildiz/bosluk/kucuk-buyuk farkini siler)."""
    s = (baslik or "").lower().replace("*", " ")
    return " ".join(s.split())[:80]


def _mcp_kap_ekle(ticker, items, limit=10):
    """Borsa MCP'den KAP haberi cekip mevcut (scraping) listesine EK olarak katar.

    Ayni bildirimi iki kez islemesin diye normalize edilmis basliga gore tekrar
    edenler elenir. MCP erisilemezse liste DEGISMEDEN doner (sessizce)."""
    try:
        from src.news.borsa_mcp import get_kap_news
        mcp_haberler = get_kap_news(ticker, limit=limit)
    except Exception:
        mcp_haberler = None
    if not mcp_haberler:
        return items
    from src.news.base import NewsItem
    gorulen = {_kap_baslik_norm(getattr(it, "title", "")) for it in items}
    gorulen_id = {getattr(it, "disclosure_id", None) for it in items}
    birlesik = list(items)
    eklenen = 0
    for h in mcp_haberler:
        pub = h.get("published_at")
        if pub is None:                       # tarihsiz bildirimi 'taze' sayamayiz
            continue
        norm = _kap_baslik_norm(h.get("baslik"))
        if norm in gorulen or (h.get("id") and h.get("id") in gorulen_id):
            continue                          # ayni bildirim scraping'den de gelmis
        gorulen.add(norm)
        birlesik.append(NewsItem(
            ticker=ticker, symbol=ticker, title=h.get("baslik"),
            published_at=pub, source=h.get("kaynak") or "KAP-MCP",
            url=h.get("url"), disclosure_id=h.get("id")))
        eklenen += 1
    if eklenen:
        print(f"[KAP-MCP] {ticker}: Borsa MCP'den {eklenen} ek KAP bildirimi katildi.")
    return birlesik


# Bildirimler sekmesinin okudugu KAP yorum deposu (web app.py get_alerts)
_KAP_YORUM_PATH = Path(__file__).resolve().parents[2] / "data" / "kap_yorumlar.json"


def _sirket_tanimi(ticker) -> str:
    """AI promptlarina konacak sirket kimligi: '{TICKER} — {sektor} sektoru ({aciklama})'.
    instruments tablosundaki GERCEK sektor/aciklama alanlarindan uretilir; AI'nin
    sektoru uydurmasini onler (or. ASELS'e 'elektrik dagitim' dedirtmez). Veri yoksa
    sade ticker doner."""
    tkr = (ticker or "").upper().replace(".IS", "")
    try:
        from src.db import database as db
        row = db.get_instrument(tkr) or {}
    except Exception:
        row = {}
    sektor = (row.get("sektor") or "").strip()
    aciklama = (row.get("aciklama") or "").strip()
    if sektor and aciklama:
        return f"{tkr} — {sektor} sektörü ({aciklama})"
    if aciklama:
        return f"{tkr} — {aciklama}"
    if sektor:
        return f"{tkr} — {sektor} sektörü"
    return tkr


def _kap_yorum(ticker, haber):
    """Bu KAP bildiriminin bu hisse icin YONUNU + kisa yorumunu YAPILI dondurur:
    {"cumle": <1-2 cumle>, "yon": "olumlu"|"olumsuz"|"notr"}. Anahtar yoksa/hata/parse
    basarisizsa None.

    yon='notr': rutin/proseudurel bildirim ya da etkisi net degerlendirilemez. Cagiran
    NOTR KAP'lari Telegram'a GONDERMEZ (haber havuzunda kalir); yalniz net olumlu/olumsuz
    bildirilir."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    baslik = (haber.get("baslik") or "").strip()
    if not baslik:
        return None
    try:
        resp = _ai_create(
            model="claude-haiku-4-5", max_tokens=160,
            system=("Sen Max'sin: 40 yasinda, 25 yillik tecrubeli bir Turk borsa uzmani. "
                    "Direkt ve net. Verilen KAP bildiriminin bu hisse icin yonunu belirle "
                    "ve SADECE su JSON'u dondur (baska metin yok):\n"
                    '{"yon":"olumlu|olumsuz|notr","cumle":"<1-2 kisa cumle degerlendirme>"}\n'
                    "yon=notr: rutin/proseudurel bildirim VEYA etkisi net degerlendirilemez "
                    "(yatirim yonu belirsiz). yon=olumlu/olumsuz: net degerde bir etki var. "
                    "Hissenin GERCEK sektorunu/faaliyet alanini kullan (promptta verildi); "
                    "sektor veya faaliyet alani UYDURMA. Sade Turkce, jargon/markdown yok, "
                    "kesin al/sat tavsiyesi verme, veri uydurma, SADECE gecerli JSON."),
            messages=[{"role": "user", "content":
                       f"Hisse: {_sirket_tanimi(ticker)}\nKAP bildirimi: {baslik}\n"
                       "Bu bildirim bu hisse icin ne anlama geliyor?"}],
        )
        t = "".join(getattr(b, "text", "") for b in resp.content
                    if getattr(b, "type", "") == "text").strip()
        import json, re
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            return None
        d = json.loads(m.group(0))
        yon = (d.get("yon") or "notr").strip().lower()
        if yon not in ("olumlu", "olumsuz", "notr"):
            yon = "notr"
        return {"cumle": (d.get("cumle") or "").strip() or None, "yon": yon}
    except Exception:
        return None


def _kap_onemli_mi(ticker, baslik) -> bool:
    """KAP bildirimi YATIRIM KARARINI etkiler mi? Haiku ile EVET/HAYIR.

    Rutin/teknik bildirimleri (borclanma araci ihrac/itfa, kupon/faiz odemesi,
    varant/sertifika itfasi, fon pay degeri vb.) eler. Anahtar yoksa/hata olursa
    True doner (fail-open: suphede sustur ma, bildir)."""
    baslik = (baslik or "").strip()
    if not baslik:
        return False
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        resp = _ai_create(
            model="claude-haiku-4-5", max_tokens=5,
            system=("Bir KAP bildiriminin hisse YATIRIM KARARINI etkileyip "
                    "etkilemedigini degerlendir. RUTIN/TEKNIK bildirimler yatirim "
                    "kararini ETKILEMEZ: borclanma araci/tahvil ihrac veya itfasi, "
                    "kupon/faiz/ana para odemesi, varant veya sertifika itfasi, fon "
                    "pay/birim deger duyurusu, ek getiri odemesi, rutin kayitli sermaye "
                    "islemleri, rutin pay geri alim adimlari. ONEMLI olanlar yatirim "
                    "kararini ETKILER: bilanco/kar-zarar, temettu karari, yeni buyuk "
                    "sozlesme/ihale, yatirim/satin alma/birlesme, ortaklik veya yonetim "
                    "degisikligi, sorusturma/ceza/dava, uretim-kapasite, hedef/beklenti "
                    "revizyonu. SADECE tek kelime cevap ver: EVET veya HAYIR."),
            messages=[{"role": "user", "content":
                       f"Hisse: {_sirket_tanimi(ticker)}\nKAP bildirimi: {baslik}\n"
                       "Bu bildirim yatirim kararini etkiler mi? EVET/HAYIR."}],
        )
        t = "".join(getattr(b, "text", "") for b in resp.content
                    if getattr(b, "type", "") == "text").strip().upper()
        return not t.startswith("HAYIR")
    except Exception:
        return True


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


def _brief_kaydi(ticker):
    """ai_commentary.json'dan bu hisse icin son (skipped olmayan) brifing kaydini
    doner (dict) veya None. Sabah motorunun teshisini (gerekce/neden_simdi) ve
    gordugu KAP haberlerini ani-gelisme sebebine tasimak icin kullanilir."""
    try:
        import json
        data = json.loads(_COMMENTARY_PATH.read_text(encoding="utf-8"))
        t = (ticker or "").upper().replace(".IS", "")
        for rec in (data if isinstance(data, list) else []):
            if (rec.get("ticker") or "").upper().replace(".IS", "") == t \
                    and not rec.get("skipped"):
                return rec
    except Exception:
        pass
    return None


def _son_gunler_kap(haberler, now, gun=5):
    """haberler icinden son `gun` gunun (tarih, baslik) listesi, en yeni once.
    _hareket_sebebi baglamini bugunden son birkac gune genisletmek icin (dunku
    'tipe donusum' gibi bildirimler de gorulsun)."""
    esik = (now.date() - timedelta(days=gun)).isoformat()
    out = []
    for h in (haberler or []):
        tarih = str(h.get("tarih", ""))[:16]
        if tarih[:10] >= esik:
            baslik = (h.get("baslik") or h.get("ozet") or "").strip()
            if baslik:
                out.append((tarih, baslik))
    return out


def _hareket_sebebi(ticker, change, haberler, now=None):
    """Fiyat hareketinin nedeni (1 cumle) — YALNIZ dogrulanmis baglamdan.

    Baglam uc kaynaktan toplanir: (1) son 5 gunun KAP bildirimleri (yalniz bugun
    degil), (2) sabah brifinginin bu hisse icin yazdigi teshis/gerekce ve gordugu
    KAP haberleri (ai_commentary — bot kendi bildigini kullanir). Somut baglam
    YOKSA spekulasyon (muhtemelen X) URETMEZ; 'sebep teyit edilemedi' der.
    Anahtar yoksa/hata olursa None (sessiz)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    now = now or datetime.now(_TZ)
    yon = "yukseldi" if change > 0 else "dustu"

    # 1) BAGLAM TOPLA: son 5 gun KAP + brifing gerekcesi + brifingin gordugu KAP
    kap = _son_gunler_kap(haberler, now, gun=5)
    brief = _brief_kaydi(ticker)
    brief_gerekce = ""
    if brief:
        brief_gerekce = (brief.get("gerekce") or brief.get("neden_simdi")
                         or brief.get("sade_yorum") or "").strip()
        for h in (brief.get("haberler") or [])[:5]:
            baslik = (h.get("baslik") or h.get("ozet") or "").strip()
            if baslik:
                kap.append((str(h.get("tarih", ""))[:16], baslik))
    # tekrar eden basliklari ele, en yeni once
    gorulen, kap_satir = set(), []
    for tarih, baslik in sorted(kap, reverse=True):
        anahtar = baslik.lower()
        if anahtar not in gorulen:
            gorulen.add(anahtar)
            kap_satir.append(f"- {tarih} {baslik}")

    # 2) SOMUT BAGLAM YOK -> uydurma yapma, sabit durust cumle (AI cagirma)
    if not kap_satir and not brief_gerekce:
        return ("Sebep teyit edilemedi — son günlerde bu hisseye dair KAP "
                "bildirimi veya bot teşhisi bulunamadı.")

    # 3) SOMUT BAGLAM VAR -> AI yalniz verilen gercekleri tek cumlede ozetler
    parcalar = []
    if brief_gerekce:
        parcalar.append(f"Botun sabah teşhisi: {brief_gerekce}")
    if kap_satir:
        parcalar.append("Son 5 günün KAP bildirimleri:\n" + "\n".join(kap_satir[:6]))
    baglam = "\n\n".join(parcalar)
    try:
        resp = _ai_create(
            model="claude-haiku-4-5", max_tokens=140,
            system=("Sen deneyimli bir Türk borsa analistisin. Sana bir hissenin gün "
                    "içi fiyat hareketi ve o hisseye dair DOĞRULANMIŞ bağlam (bot "
                    "teşhisi + son günlerin KAP bildirimleri) verilecek. Hareketin "
                    "nedenini SADECE bu bağlama dayanarak TEK kısa cümlede açıkla. "
                    "KESİN KURALLAR: (1) Yalnız verilen bağlamı kullan; bağlamda "
                    "OLMAYAN hiçbir sebep (bilanço beklentisi, sektör dedikodusu vb.) "
                    "UYDURMA. (2) 'muhtemelen', 'olasılıkla', 'büyük ihtimalle', "
                    "'sanırım' gibi spekülatif kalıplar YASAK. (3) Bağlam hareketi net "
                    "açıklamıyorsa 'Sebep tam teyit edilemedi' de ve yalnız bilinen "
                    "gerçekleri (ör. SPK süreci, KAP bildirimi başlığı) say. (4) Sade "
                    "Türkçe, markdown yok, tek cümle."),
            messages=[{"role": "user", "content":
                       f"Hisse: {_sirket_tanimi(ticker)}\nBugünkü hareket: %{change:+g} "
                       f"({yon})\n\nDOĞRULANMIŞ BAĞLAM:\n{baglam}\n\n"
                       "Bu hareketin nedeni ne? Yalnız yukarıdaki bağlamı kullan, tek cümle."}],
        )
        t = "".join(getattr(b, "text", "") for b in resp.content
                    if getattr(b, "type", "") == "text").strip()
        if not t:
            raise ValueError("bos yanit")
        # GUVENLIK AGI: model kurallara ragmen spekulasyon kalibi kullandiysa,
        # uydurmayi yaymaktansa elimizdeki gercege don.
        if "muhtemelen" in t.lower() or "olasılıkla" in t.lower():
            raise ValueError("spekulatif kalip")
        return t
    except Exception:
        # AI patlarsa/spekulasyona kacarsa: elde ne varsa YALNIZ gercegi sun
        if brief_gerekce:
            return "Sebep (bot teşhisi): " + brief_gerekce[:220]
        if kap_satir:
            return ("Sebep teyit edilemedi — son günlerde şu KAP bildirimleri mevcut: "
                    + "; ".join(s[2:] for s in kap_satir[:3]))
        return None


def _fiyatlanma_notu(item):
    """Haberin GERCEK fiyat tepkisini olcup (src.news.priced_in.check_priced_in)
    bildirim icin NITEL etiket doner:
      FIYATLANDI   -> "⚠️ Not: Fiyat bu habere muhtemelen tepki verdi bile
                       (%X hareket) — geç kalmış olabilirsin."
      FIYATLANMADI -> "Haber henüz fiyatlanmamış görünüyor."
      VERI_YOK/hata -> None (etiket eklenmez).
    Bildirimi ENGELLEMEZ; yalniz etiketler. 'item' .ticker/.symbol/.published_at
    tasimalidir (KAP haber nesnesi veya sektor icin SimpleNamespace shim)."""
    try:
        from src.news.priced_in import check_priced_in
        rap = check_priced_in(item)
    except Exception:
        return None
    if rap.status == "FIYATLANDI":
        hareket = (f" (%{abs(rap.day_return_pct):.1f} hareket)"
                   if isinstance(rap.day_return_pct, (int, float)) else "")
        return (f"⚠️ Not: Fiyat bu habere muhtemelen tepki verdi bile{hareket} — "
                "geç kalmış olabilirsin.")
    if rap.status == "FIYATLANMADI":
        return "Haber henüz fiyatlanmamış görünüyor."
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
        # EK KAYNAK: Borsa MCP'den de KAP bildirimlerini cek, mevcutla birlestir
        # (tekrar edenler elenir). MCP yoksa items aynen kalir.
        items = _mcp_kap_ekle(ticker, items)
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
            # ONEM FILTRESI: rutin/notr bildirimleri (borclanma, kupon, varant itfa
            # vb.) gondermeden once Haiku ile ele; etkisizse logla ve gec.
            if not _kap_onemli_mi(ticker, it.title):
                print(f"[{now:%Y-%m-%d %H:%M}] [KAP-filtre] {ticker} rutin/notr bildirim "
                      f"gonderilmedi: {(it.title or '')[:70]}")
                continue
            haber = {"baslik": it.title, "url": it.url}
            yorum = _kap_yorum(ticker, haber)          # AI: {cumle, yon} veya None
            # NÖTR KAP FİLTRESİ: bot analizi 'notr' (proseudurel/etkisi belirsiz) ise
            # Telegram'a GÖNDERME; haber havuzunda kalır, sabah brifingi isterse özetler.
            # Yalnız net olumlu/olumsuz bildirilir. (yorum None -> AI yok/başarısız:
            # fail-open, gönder — önem filtresinden zaten geçti.)
            if yorum and yorum.get("yon") == "notr":
                print(f"[{now:%Y-%m-%d %H:%M}] [KAP-notr] {ticker} nötr yorum, "
                      f"gönderilmedi: {(it.title or '')[:70]}")
                continue
            cumle = (yorum or {}).get("cumle")
            _kaydet_kap_yorum(ticker, haber, cumle, today)
            hits.append({"ticker": ticker, "change": info["change"],
                         "item": it, "yorum": cumle})

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
        fnot = _fiyatlanma_notu(it)             # fiyatlanma kontrolu (etiket; engellemez)
        if fnot:
            blok.append(fnot)
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


# ---------------------------------------------------------------------------
# [EMEKLI — 20 Tem 2026] PROAKTIF SEKTOR HABER TARAMASI
#
# BU SISTEM ARTIK KULLANICIYA MESAJ GONDERMEZ. Yerini src/news/haber_sinyal.py
# aldi (bkz. haber_sinyal.bildir). Emekli etme gerekcesi — bu katman asagidaki
# duzeltmelerin HICBIRINI icermiyordu:
#   * SEKTOR_HABER_KURALLARI yanlis/eksik esleme yapiyordu ("namlu" -> petrol;
#     Hurmuz/altin/havacilik/celik konulari yok; ROKET/STFA gecersiz kodlar).
#   * ANA OYUNCU sektor-alakasi yok -> ASELS savunma haberinde "dolayli/zayif"
#     damgalanabiliyordu (27 Haz 2026 hatali bildirimi).
#   * Deterministik fiyatlanmislik olcumu yok (yalniz AI'in oznel yorumu).
#   * Adversarial haber temizleme/karantina yok (supheli kaynak suzulmuyordu).
#   * Gece susturmasinin portfoy istisnasi siziyordu (03:30'da bildirim).
#
# KOD ARSIV OLARAK DURUYOR (kural tablosu + esleme mantigi referans); GONDERIM
# OLDU. _sektor_haber_tarama() basindaki emeklilik kapisi her cagriyi durdurur
# ve __main__ 'sektor' kolu artik bu fonksiyonu hic cagirmaz.
# ---------------------------------------------------------------------------

# Emeklilik kapisi: True kaldigi surece eski sektor-haber gonderimi calismaz.
# (Testlerin eski davranisi dogrulayabilmesi icin degisken; uretimde ELLENMEZ.)
SEKTOR_HABER_EMEKLI = True

# konu -> tetik anahtar kelimeler + etkilenen hisseler. Kelimeler NORMALIZE yazilir
# (kucuk + tr->ascii) ve metinde KELIME BASINA cengellenir (bkz. _haber_konulari):
# 'petrol fiyat' -> 'petrol fiyatlari'ni da yakalar. Cingil cok genel kelimelerden
# (or. yalniz 'dolar' -> '295 milyon dolar') kacinmak icin baglam iceren ifadeler.
SEKTOR_HABER_KURALLARI = [
    {"konu": "Savunma / KAAN",
     "kelimeler": ["kaan", "milli muharip", "insansiz hava", "siha", "jet motoru",
                   "savunma sanayi", "savunma bakanlig", "msb", "ssb", "roketsan",
                   "savunma ihrac", "savunma ihale", "savunma sozlesme"],
     "hisseler": ["ASELS", "ROKET", "STFA"]},
    {"konu": "Petrol / Brent",
     "kelimeler": ["brent", "ham petrol", "petrol fiyat", "petrol varil", "opec",
                   "namlu", "petrol uretim"],
     "hisseler": ["TUPRS", "PETKM"]},
    {"konu": "Faiz / TCMB",
     "kelimeler": ["tcmb", "merkez bankas", "politika faiz", "faiz indir",
                   "faiz artir", "ppk", "faiz karar"],
     "hisseler": ["GARAN", "AKBNK", "YKBNK", "ISCTR", "HALKB", "VAKBN"]},
    {"konu": "Döviz / Dolar",
     "kelimeler": ["dolar kuru", "dolar/tl", "doviz kuru", "kurda", "kur rekor",
                   "dolar rekor", "devaluasyon", "tl deger kayb", "tl deger kayip"],
     "hisseler": ["ASELS", "FROTO", "TOASO", "EREGL", "TUPRS", "KRDMD"]},
]

_GECE_HABER_PATH = Path(__file__).resolve().parents[2] / "data" / "gece_haberleri.json"


def _borsa_acik(now) -> bool:
    """BIST o an acik mi? (hafta ici 10:00-18:00 Istanbul + resmi tatil).

    Mantik src.piyasa_takvim'de TEK kaynakta. Tatil takvimi 20 Tem 2026'da
    baglandi; oncesinde yalniz hafta sonu bakiliyordu."""
    from src.piyasa_takvim import borsa_acik
    return borsa_acik(now, "bist")


def _haber_konulari(text: str) -> list:
    """Bir haber metnindeki (baslik+ozet) sektor kurallarini dondurur (eslesen).

    Eslesme KELIME BASINA cengellidir (sona degil): '\\bpetrol fiyat' Turkce ekli
    'petrol fiyatlari/fiyati'ni da yakalar. Boylece dilin ekleri recall'u kirmaz;
    cok genel tek kelimelerden (or. 'dolar') kurallarda kacinilir."""
    import re
    from src.news.rss_source import _norm
    n = _norm(text or "")
    out = []
    for kural in SEKTOR_HABER_KURALLARI:
        for kw in kural["kelimeler"]:
            if re.search(r"\b" + re.escape(_norm(kw)), n):
                out.append(kural)
                break
    return out


def _haber_etki_notu(hisse, baslik, konu):
    """Bu haberin hisseye olasi etkisini YAPILI dondurur:
    {cumle, yon(olumlu/olumsuz/notr), etki(yuksek/orta/dusuk/etkisiz), tema}.
    Anahtar yok / hata / JSON parse basarisizsa None. Haiku."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        resp = _ai_create(
            model="claude-haiku-4-5", max_tokens=160,
            system=("Sen Max'sin: 25 yillik tecrubeli bir Turk borsa uzmani. Verilen "
                    "sektor baglami ve haber icin SADECE su JSON'u dondur (baska metin yok):\n"
                    '{"cumle":"<haberin bu SEKTORE/temaya olasi etkisi, tek kisa cumle>",'
                    '"yon":"olumlu|olumsuz|notr",'
                    '"etki":"yuksek|orta|dusuk|etkisiz",'
                    '"tema":"<haberin ana temasi tek kelime snake_case, or. faiz_beklentisi>"}\n'
                    "ONEMLI: 'cumle' bir SEKTOR/tema yorumudur ve ayni haber birden cok "
                    "sirketi ilgilendirebilir; bu yuzden cumle'de HICBIR sirket/hisse ADI "
                    "GECIRME (or. 'Petkim', 'Tupras', 'THY' yazma) — etkiyi sektor/faaliyet "
                    "duzeyinde anlat (or. 'rafineri marjlarini baski altina alabilir'). "
                    "Verilen GERCEK sektoru kullan, sektor uydurma. Kesin al/sat tavsiyesi "
                    "verme, veri/rakam uydurma, sade Turkce, markdown/backtick yok, "
                    "SADECE gecerli JSON."),
            messages=[{"role": "user", "content":
                       f"Sektör bağlamı: {_sirket_tanimi(hisse)}\nKonu: {konu}\n"
                       f"Haber: {baslik}"}],
        )
        t = "".join(getattr(b, "text", "") for b in resp.content
                    if getattr(b, "type", "") == "text").strip()
        import json, re
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            return None
        d = json.loads(m.group(0))
        tema = re.sub(r"[^a-z0-9_]", "",
                      (d.get("tema") or "").strip().lower().replace(" ", "_")) or "genel"
        return {
            "cumle": (d.get("cumle") or "").strip() or None,
            "yon": (d.get("yon") or "notr").strip().lower(),
            "etki": (d.get("etki") or "etkisiz").strip().lower(),
            "tema": tema,
        }
    except Exception:
        return None


def _haber_net_yonlu(etki) -> bool:
    """Haber net yonlu mu? Yalniz (olumlu|olumsuz) yon + (orta|yuksek) etki bildirilir.
    notr/etkisiz/dusuk -> False (etkisiz haber; Telegram'a gitmez)."""
    if not etki:
        return False
    return (etki.get("yon") in ("olumlu", "olumsuz")
            and etki.get("etki") in ("orta", "yuksek"))


def _haber_hash(baslik) -> str:
    """Bir haber basligi icin kararli kisa dedup anahtari."""
    base = " ".join((baslik or "").lower().split())
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:12]


def _gece_haber_ekle(hisseler, baslik, link, konu, etki, tarih_iso):
    """Gece (borsa kapali) gelen sektor haberini sabah brifingi icin biriktirir.
    hisseler: bu haberden etkilenen (izlenen) hisse kodlari listesi."""
    try:
        import json
        d = {"haberler": []}
        if _GECE_HABER_PATH.exists():
            d = json.loads(_GECE_HABER_PATH.read_text(encoding="utf-8")) or {"haberler": []}
        haberler = d.get("haberler") or []
        h = _haber_hash(baslik)
        if any(x.get("hash") == h for x in haberler):
            return                                 # ayni haber zaten biriktirildi
        haberler.append({"hisseler": sorted(hisseler), "baslik": baslik, "link": link,
                         "konu": konu, "etki": etki, "tarih": tarih_iso, "hash": h})
        d["haberler"] = haberler[-50:]             # son 50 ile sinirla
        _GECE_HABER_PATH.parent.mkdir(exist_ok=True)
        _GECE_HABER_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    except Exception as e:
        print(f"[gece-haber] yazma hatasi: {type(e).__name__}")


def _sektor_haber_tarama(now=None, within_hours: float = 2.0):
    """RSS kaynaklarini tarar; sektor haberini etkilenen hisselere baglar.

    Etkilenen hisse bir kullanicinin portfoyunde VEYA genel watchlist'te ise o
    haberi (AI etki notuyla) Telegram'a bildirir. Spam onleme: ayni (kullanici,
    hisse, haber) gun icinde bir kez. Borsa kapaliyken gelen haberler ayrica
    sabah brifingine girsin diye data/gece_haberleri.json'a yazilir.
    """
    now = now or datetime.now(_TZ)
    # EMEKLILIK KAPISI (20 Tem 2026): bu katman kullaniciya mesaj GONDERMEZ.
    # Yerini src/news/haber_sinyal.bildir aldi. Hicbir cagri yolu -- cron, main(),
    # elle calistirma -- buradan Telegram'a ulasamaz.
    if SEKTOR_HABER_EMEKLI:
        print(f"[{now:%Y-%m-%d %H:%M}] [sektor-haber] EMEKLI — gonderim yok "
              f"(yerini haber_sinyal.bildir aldi).")
        return 0
    today = now.date().isoformat()
    gece = not _borsa_acik(now)

    # 1) RSS girdilerini topla
    try:
        from src.news.rss_source import RSSNewsSource
        entries = RSSNewsSource(within_hours=int(max(1, within_hours)))._all_entries()
    except Exception as e:
        print(f"[{now:%Y-%m-%d %H:%M}] [sektor-haber] RSS alinamadi: {type(e).__name__}")
        return 0
    if not entries:
        print(f"[{now:%Y-%m-%d %H:%M}] [sektor-haber] taze haber yok.")
        return 0

    # 2) HABER-MERKEZLI esleme (taze, son within_hours saat): bir haber birden cok
    # hisseyi etkileyebilir (or. faiz haberi -> 6 banka). Hisse basina ayri blok
    # yerine TEK haber blogunda etkilenen hisseler listelenir (spam + AI maliyeti az).
    cutoff = now - timedelta(hours=within_hours)
    haberler = {}    # hash -> {baslik, link, konu, tarih, hisseler:set}
    for e in entries:
        tarih = e.get("tarih")
        if tarih is not None and tarih < cutoff:
            continue
        metin = f"{e.get('baslik', '')} {e.get('ozet', '')}"
        kurallar = _haber_konulari(metin)
        if not kurallar:
            continue
        h = _haber_hash(e.get("baslik"))
        rec = haberler.setdefault(h, {
            "baslik": (e.get("baslik") or "").strip(), "link": e.get("link"),
            "konu": kurallar[0]["konu"],
            "tarih": tarih.isoformat() if tarih is not None else today,
            "hisseler": set()})
        for kural in kurallar:
            rec["hisseler"].update(kural["hisseler"])
    if not haberler:
        print(f"[{now:%Y-%m-%d %H:%M}] [sektor-haber] eslesen sektor haberi yok "
              f"({len(entries)} haber tarandi).")
        return 0

    # 3) Kim izliyor? Genel watchlist + her kullanicinin portfoyu
    try:
        watch = {(t or "").upper().replace(".IS", "") for t in load_watchlist()}
    except Exception:
        watch = set()
    try:
        users = db.list_users()
    except Exception:
        users = []
    pf_of = {}
    for u in users:
        try:
            pf_of[u["id"]] = {(p.get("ticker") or "").upper().replace(".IS", "")
                              for p in db.list_portfolio(u["id"]) if p.get("ticker")}
        except Exception:
            pf_of[u["id"]] = set()
    izlenen_tum = set(watch)
    for pf in pf_of.values():
        izlenen_tum |= pf

    etki_cache = {}     # hash -> etki notu (ayni haber icin AI'yi bir kez cagir)

    def _etki(h, info):
        if h not in etki_cache:
            ornek = sorted(info["hisseler"] & izlenen_tum) or sorted(info["hisseler"])
            etki_cache[h] = _haber_etki_notu(
                ornek[0] if ornek else "", info["baslik"], info["konu"])
        return etki_cache[h]

    # 4) Gece (borsa kapali) gelen haberleri sabah brifingi icin biriktir (bir kez)
    if gece:
        for h, info in haberler.items():
            etkilenen = info["hisseler"] & izlenen_tum
            if not etkilenen:
                continue
            anahtar = sorted(etkilenen)[0]                  # dedup icin sabit ticker
            tok = f"SEKTORHB:GECE:{h}"
            if tok in db.alert_levels_today(anahtar, today):
                continue
            _gece_haber_ekle(etkilenen, info["baslik"], info["link"],
                             info["konu"], (_etki(h, info) or {}).get("cumle"),
                             info["tarih"])
            db.record_alert(anahtar, today, tok, 0)

    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] [sektor-haber] Telegram yok; "
              f"{'gece haberi biriktirildi' if gece else 'bildirim atlandi'}.")
        return 0

    # 5) Kullaniciya ozel bildirim: izledigi hisse(ler) etkilendiyse tek blokta gonder
    gonderim = 0
    for u in users:
        tg = u.get("telegram_id")
        if not tg:
            continue
        uid = u["id"]
        from src.notify import filtre
        pf_uid = pf_of.get(uid, set())
        izlenen = pf_uid | watch
        bloklar = []
        for h, info in haberler.items():
            etkilenen = sorted(info["hisseler"] & izlenen)
            # BİLDİRİM GEÇİT: genel haber portföy dışı hisseler için susturulur (kural).
            etkilenen = [t for t in etkilenen
                         if filtre.should_notify(t, uid, "haber", portfoy=pf_uid)]
            if not etkilenen:
                continue
            tok = f"SEKTORHB:{uid}:{h}"
            if tok in db.alert_levels_today(etkilenen[0], today):
                continue
            etki = _etki(h, info)
            # DEDUP SIZINTISI FIX: 'tok' kaydini AI cagrisindan (_etki) HEMEN SONRA yaz —
            # haber sonucta gonderilsin ya da (net-yonlu degil / gece / tema-tekrari
            # nedeniyle) gonderilmesin, ayni haber sonraki 30dk kosularinda 2 saatlik RSS
            # penceresi boyunca YENIDEN Haiku'ya sorulmaz. (Onceden yalniz gonderimde
            # yazildigi icin gonderilmeyen haberler her kosuda tekrar etiketleniyordu.)
            db.record_alert(etkilenen[0], today, tok, 0)
            # Degisiklik 4: yalniz net yonlu (olumlu/olumsuz + orta/yuksek etki) haber
            # gonder; notr/etkisiz/dusuk etkili haberi bildirme.
            if not _haber_net_yonlu(etki):
                continue
            # Degisiklik 2: borsa kapaliyken sektor haberi Telegram'a GITMEZ. Istisna:
            # portfoydeki hisseyi etkileyen onemli haber. (Haber zaten gece havuzunda,
            # sabah brifingi toplar.)
            if gece and not (set(etkilenen) & pf_uid):
                continue
            # PORTFÖY İSTİSNASI DEDUP (kapalı piyasa): portföy hissesi gece istisnadan
            # geçse bile susturma/tema kuralına tabidir. Kapalı piyasada bir portföy
            # hissesi için gece EN FAZLA 1 mesaj gönderilir (ilk geliş).
            gece_pf_stock = None
            if gece:
                _pf_hit = [t for t in etkilenen if t in pf_uid]
                if _pf_hit:
                    gece_pf_stock = _pf_hit[0]
                    if (f"SEKTORHB:GECE_PF:{uid}:{gece_pf_stock}"
                            in db.alert_levels_today(gece_pf_stock, today)):
                        continue        # bu portföy hissesi için gece zaten mesaj gitti
            # TEMA-TEKRAR ENGELİ: aynı gün aynı TEMA (stabil 'konu') tekrar gönderilmez.
            # NOT: önceden değişken AI 'tema' alanı kullanılıyordu (tema:konu) -> aynı
            # konudaki farklı haberler farklı tema üretip dedup'ı atlıyordu (gece petrol
            # akını, 4 mesaj). Stabil info['konu'] ile konu başına günde 1 gönderim;
            # portföy istisnası da buna tabi.
            tema_anahtar = f"_TEMA_{uid}"
            tema_key = info["konu"]
            if tema_key in db.alert_levels_today(tema_anahtar, today):
                continue
            db.record_alert(tema_anahtar, today, tema_key, 0)   # tema (konu) dedup
            if gece_pf_stock:                                   # gece portföy tek-mesaj kaydı
                db.record_alert(gece_pf_stock, today,
                                f"SEKTORHB:GECE_PF:{uid}:{gece_pf_stock}", 0)
            baslik = info["baslik"]
            if info.get("link"):
                baslik = f'<a href="{info["link"]}">{baslik}</a>'
            blok = f"📰 <b>{', '.join(etkilenen)}</b> için önemli haber: {baslik}"
            if etki.get("cumle"):
                blok += f"\n{etki['cumle']}"
            # Fiyatlanma kontrolu (etiket; engellemez). info dict -> check_priced_in'in
            # bekledigi .ticker/.symbol/.published_at icin hafif shim kur.
            try:
                from types import SimpleNamespace
                try:
                    _pub = datetime.fromisoformat(str(info.get("tarih")))
                except Exception:
                    _pub = now
                fnot = _fiyatlanma_notu(SimpleNamespace(
                    ticker=etkilenen[0], symbol=None, published_at=_pub))
            except Exception:
                fnot = None
            if fnot:
                blok += f"\n{fnot}"
            bloklar.append(blok)
            if len(bloklar) >= 8:                           # mesaji sismekten koru
                break
        if not bloklar:
            continue
        bas = (f"<b>SEKTÖR HABERİ</b> — {now:%H:%M}"
               + ("\n<i>Borsa kapalı; sabah brifinginde de göreceksin.</i>" if gece else ""))
        try:
            telegram.send_message(bas + "\n\n" + "\n\n".join(bloklar), chat_id=str(tg))
            gonderim += 1
        except Exception as e:
            print(f"[sektor-haber] gonderim hatasi ({tg}): {type(e).__name__}")
    print(f"[{now:%Y-%m-%d %H:%M}] [sektor-haber] {len(haberler)} sektor haberi -> "
          f"{gonderim} kullaniciya bildirildi{' (gece)' if gece else ''}.")
    return gonderim


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
                # Hareket nedeni icin o gun cikan KAP haberlerini cek (taze KAP istisnasi
                # de bu listeden hesaplanir).
                try:
                    haberler = filtered_news(ticker, source=news_src)
                except Exception:
                    haberler = []
                taze_kap = any(str(h.get("tarih", "")).startswith(today)
                               for h in (haberler or []))
                karar = _son_karar(ticker)
                # KARARSIZ FILTRESI: AI karari BEKLE/TUT ise dusuk seviyeli (ACIL olmayan)
                # hareket bildirimini bastir. Istisna: %5+ ani hareket (ACIL) veya bugun
                # taze KAP haberi -> karar ne olursa olsun gonder.
                if level != "ACIL" and not taze_kap and _karar_kararsiz(karar):
                    print(f"[{now:%Y-%m-%d %H:%M}] [karar-filtre] {ticker} {karar} "
                          f"(%{info['change']:+g}) dikkat hareketi bildirilmedi.")
                else:
                    # SEBEP: neden dustu/yukseldi? Varsa o gun cikan KAP haberiyle iliskilendir.
                    sebep = _hareket_sebebi(ticker, info["change"], haberler, now=now)
                    price_alerts.append({"ticker": ticker, "seviye": level,
                                         "sebep": sebep, "portfoyde": portfoyde,
                                         "karar": karar, **info})
        else:
            # Fiyat oynamamis -> KAP'ta taze fiyatlanmamis haber var mi? -> ACIL
            haber = unpriced_fresh_news(ticker, news_src)
            # Dedup: hizli KAP taramasiyla AYNI anahtar -> ayni bildirim tekrar gitmez
            tok = _kap_key(haber.get("disclosure_id"), haber.get("baslik")) if haber else None
            if haber and tok not in db.alert_levels_today(ticker, today):
                db.record_alert(ticker, today, tok, info["change"])
                # ONEM FILTRESI: rutin/notr KAP bildirimini gondermeden once ele.
                if not _kap_onemli_mi(ticker, haber.get("baslik")):
                    print(f"[{now:%Y-%m-%d %H:%M}] [KAP-filtre] {ticker} rutin/notr "
                          f"bildirim gonderilmedi: {(haber.get('baslik') or '')[:70]}")
                else:
                    yorum = _kap_yorum(ticker, haber)      # AI: {cumle, yon} veya None
                    # NÖTR KAP FİLTRESİ: bot analizi 'notr' ise Telegram'a gönderme
                    # (haber havuzunda kalır); yalnız net olumlu/olumsuz bildirilir.
                    if yorum and yorum.get("yon") == "notr":
                        print(f"[{now:%Y-%m-%d %H:%M}] [KAP-notr] {ticker} nötr yorum, "
                              f"gönderilmedi: {(haber.get('baslik') or '')[:70]}")
                    else:
                        cumle = (yorum or {}).get("cumle")
                        _kaydet_kap_yorum(ticker, haber, cumle, today)
                        news_alerts.append({"ticker": ticker, "change": info["change"],
                                            "haber": haber, "yorum": cumle})

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

    # Portföy hedef-fiyat kontrolü (kullanıcıya özel, günde bir kez)
    try:
        check_hedef_fiyat(now)
    except Exception as e:
        print(f"[hedef-fiyat] kontrol hatasi: {type(e).__name__}")

    # AL pozisyonu kademeli takip (hedef %50/%80/%100, stop yaklaşması)
    try:
        check_pozisyon_takip(now)
    except Exception as e:
        print(f"[pozisyon-takip] kontrol hatasi: {type(e).__name__}")

    # Dinamik çıkış kontrolleri (açık AL pozisyonları için erken çıkış sinyalleri)
    try:
        check_trend_break(now)
    except Exception as e:
        print(f"[trend-break] kontrol hatasi: {type(e).__name__}")
    try:
        check_news_flip(now)
    except Exception as e:
        print(f"[news-flip] kontrol hatasi: {type(e).__name__}")
    try:
        check_regime_flip(now)
    except Exception as e:
        print(f"[regime-flip] kontrol hatasi: {type(e).__name__}")
    try:
        check_early_loss(now)
    except Exception as e:
        print(f"[early-loss] kontrol hatasi: {type(e).__name__}")

    # BİLDİRİM GEÇİT FİLTRESİ — portföy dışı hisseler için gürültüyü kes:
    #   portföydeki hisse -> her uyarı geçer
    #   portföy dışı      -> fiyat/hacim yalnızca |günlük %|>=5; KAP haberi (zorunlu) geçer
    # Broadcast market geneli olduğu için user_id=None ("herhangi bir portföyde mi").
    from src.notify import filtre
    _once_f, _once_h = len(price_alerts), len(vol_alerts)
    price_alerts = [a for a in price_alerts
                    if filtre.should_notify(a["ticker"], None, "fiyat", portfoy=portfolio,
                                            gunluk_degisim=a.get("change"),
                                            karar=a.get("karar"))]
    vol_alerts = [a for a in vol_alerts
                  if filtre.should_notify(a["ticker"], None, "hacim", portfoy=portfolio,
                                          gunluk_degisim=a.get("change"))]
    news_alerts = [a for a in news_alerts
                   if filtre.should_notify(a["ticker"], None, "kap", portfoy=portfolio,
                                           kap_zorunlu=True)]
    if _once_f - len(price_alerts) or _once_h - len(vol_alerts):
        print(f"[{now:%Y-%m-%d %H:%M}] [bildirim-filtre] portföy dışı susturuldu: "
              f"{_once_f - len(price_alerts)} fiyat + {_once_h - len(vol_alerts)} hacim.")

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
        tkr = (a.get("ticker") or "").upper().replace(".IS", "")
        sym, usd = _sembol_usd(a)
        fiyat = _alarm_price(sym)
        if fiyat is None:
            continue
        hedef, yon = a["hedef_fiyat"], a["yon"]
        vurdu = (yon == "yukari" and fiyat >= hedef) or \
                (yon == "asagi" and fiyat <= hedef)
        if not vurdu:
            continue
        # KARARSIZ FILTRESI: AI karari BEKLE/TUT ise bildirimi bastir (alarm AKTIF
        # kalir; karar AL/SAT/AZALT/UZAK_DUR'a donunce tetiklenir). Hedefe %5+ asma
        # kritik sayilir -> karar ne olursa olsun gonder.
        asim = abs(fiyat - hedef) / hedef * 100 if hedef else 0
        if asim < ANI_ESIK and _karar_kararsiz(_son_karar(tkr)):
            print(f"[{now:%Y-%m-%d %H:%M}] [karar-filtre] {tkr} fiyat alarmi "
                  f"{_son_karar(tkr)} karari nedeniyle bekletildi (alarm aktif).")
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
    arg = sys.argv[1].lower() if len(sys.argv) > 1 else ""
    if arg == "sektor":
        # [20 Tem 2026] Eski sektor-haber gonderimi EMEKLI — bu kolda artik
        # _sektor_haber_tarama() CAGRILMIYOR. Haber bildirimi
        # `python -m src.news.haber_sinyal bildir` ile gidiyor.
        # Bu */30 kosusu YALNIZ AL pozisyonu kademeli takibi icin duruyor
        # (gece/ABD seansi dahil; gunde-bir-kez spam korumasi yineleme yapmaz).
        rc = 0
        try:
            check_pozisyon_takip()
        except Exception as e:
            print(f"[pozisyon-takip] kontrol hatasi: {type(e).__name__}")
    elif arg == "kap":
        # gun ici fiyatlanmamis KAP haberi taramasi (15 dk'da bir)
        rc = scan_kap_unpriced()
        try:
            check_price_alarms()          # ayni 15 dk'lik kosuda fiyat alarmlari
        except Exception as e:
            print(f"[alarm] hata: {type(e).__name__}")
    else:
        rc = main()
    _token_ozet()                          # her modda token/maliyet ozeti (varsa)
    sys.exit(rc)
