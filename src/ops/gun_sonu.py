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
ai_commentary.json'dan okunur; hisse yorumları gün sonunda TEK Haiku çağrısıyla
sade/jargonsuz, en fazla 2 cümle + net aksiyon olacak şekilde yeniden yazılır
(cümleler asla yarım kesilmez). Anahtar yoksa ham gerekçe tam cümlelerle gösterilir.
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


def _tam_cumleler(metin, max_len=240):
    """Metni TAM CUMLELERE gore kirpar; cumleyi asla yarim birakmaz.

    max_len'i asarsa, son tam cumle sinirina (.!?) kadar olan kismi dondurur;
    hic cumle siniri yoksa metni oldugu gibi birakir (yarim '…' eklemez)."""
    g = " ".join((metin or "").split())
    if len(g) <= max_len:
        return g
    import re
    parcalar = re.split(r"(?<=[.!?])\s+", g)
    out = ""
    for p in parcalar:
        if out and len(out) + 1 + len(p) > max_len:
            break
        out = (out + " " + p).strip()
    return out or g


def _temiz_yorumlar(tickers, kmap):
    """Portfoy hisseleri icin TEK Haiku cagrisiyla temiz, jargonsuz yorum uretir.

    Her hisse: gunluk dilde, teknik terim icermeyen, EN FAZLA 2 cumle + net aksiyon.
    BEKLE/TUT'ta neden + ne zaman tekrar bakilacagini soyler. {TICKER: yorum}
    doner; anahtar yoksa/hata olursa {} (cagiran taraf ham metne duser)."""
    secili = [t for t in tickers if kmap.get(t) and not kmap.get(t, {}).get("skipped")]
    if not secili or not os.environ.get("ANTHROPIC_API_KEY"):
        return {}
    import json as _json
    girdi = []
    for t in secili:
        r = kmap.get(t) or {}
        girdi.append({
            "hisse": t,
            "karar": r.get("final_decision") or r.get("karar") or "TUT",
            "gerekce": (r.get("sade_yorum") or r.get("gerekce") or "")[:300],
            "tekrar_bak": (r.get("tekrar_bak_kosulu") or r.get("aksiyon") or "")[:200],
        })
    try:
        import anthropic
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5", max_tokens=1000,
            system=("Sen Max'sin: 25 yillik tecrubeli bir Turk borsa uzmani. Sana "
                    "hisse listesi + karar + ham gerekce verilir. Her hisse icin "
                    "KULLANICIYA donuk, GUNLUK dilde, kisa bir yorum yaz. KURALLAR:\n"
                    "- TEKNIK TERIM KULLANMA. Sunlari YAZMA: MA10, MA50, hareketli "
                    "ortalama, 52 hafta zirvesi/dibi, RSI, MACD, destek, direnc, "
                    "fibonacci, formasyon. Bunlari gunluk dile cevir (or. 'son "
                    "aylarin tepesinden uzak', 'henuz toparlanma sinyali yok').\n"
                    "- Her hisse icin MAKSIMUM 2 cumle, sonunda NET aksiyon.\n"
                    "- Cumleleri ASLA yarim birakma; her yorum tam ve bagimsiz olsun.\n"
                    "- Karar BEKLE veya TUT ise NEDEN beklenmesi gerektigini ve NE "
                    "ZAMAN / hangi kosulda tekrar bakilacagini sade dille soyle.\n"
                    "- Veri/rakam UYDURMA. Markdown/yildiz yok.\n"
                    "SADECE su JSON'u don (baska metin yok): "
                    "{\"TICKER\": \"yorum\", ...}"),
            messages=[{"role": "user", "content":
                       "Hisseler:\n" + _json.dumps(girdi, ensure_ascii=False)}],
        )
        metin = "".join(getattr(b, "text", "") for b in resp.content
                        if getattr(b, "type", "") == "text").strip()
        import re
        m = re.search(r"\{.*\}", metin, re.S)
        data = _json.loads(m.group(0)) if m else {}
        return {(k or "").upper(): " ".join(str(v).split())
                for k, v in data.items() if v}
    except Exception:
        return {}


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


def _gun_degisim(ticker, birim="TL"):
    """Hissenin bugünkü yüzde değişimi + son fiyat. USD hisseler .IS eki OLMADAN
    ABD piyasasından çekilir. Döner: (degisim_%, son_fiyat) | (None, None)."""
    try:
        from src.alerts.engine import intraday_change
        if (birim or "TL").upper() == "USD":
            from src.markets.us import US
            info = intraday_change(ticker, market=US())
        else:
            info = intraday_change(ticker)
        if not info:
            return None, None
        return info["change"], info["last_close"]
    except Exception:
        return None, None


def _hisse_satiri(rec, ticker, birim="TL", yorum_map=None):
    fd = rec.get("final_decision") if rec else None
    kelime = karar_kelime(fd) or "TUT"
    emoji = karar_emoji(fd)
    usd = (birim or "TL").upper() == "USD"
    chg, fiyat = _gun_degisim(ticker, birim)
    if chg is not None:
        parca = f" %{chg:+.1f}"
        if usd and fiyat is not None:        # ABD hissesi: fiyatı USD olarak göster
            parca += f" · {fiyat:g}$"
    else:
        parca = " (USD)" if usd else ""
    satir = f"{emoji} <b>{_esc(ticker)} — {kelime}</b>{parca}"
    # Once temiz AI yorumu (jargonsuz, tam cumle); yoksa ham metni TAM CUMLELERE
    # gore (yarim kesmeden) goster.
    yorum = (yorum_map or {}).get((ticker or "").upper())
    if not yorum and rec:
        yorum = _tam_cumleler((rec or {}).get("sade_yorum") or (rec or {}).get("gerekce"))
    if yorum:
        satir += f"\n<i>{_esc(yorum)}</i>"
    return satir


def build_message(portfolio, kmap, overview, yarin, now, kullanici_ad=None, yorum_map=None):
    ad = f" · {str(kullanici_ad).capitalize()}" if kullanici_ad else ""
    lines = [f"<b>GÜN SONU</b>{ad} — {now:%d.%m %H:%M}", _esc(_genel_ozet(overview))]
    lines += ["", "<b>PORTFÖY</b>"]
    # portfolio: {ticker: para_birimi} (TL/USD)
    pf = sorted(portfolio)
    if pf:
        for tkr in pf:
            lines.append(_hisse_satiri(kmap.get(tkr), tkr, portfolio.get(tkr, "TL"),
                                       yorum_map=yorum_map))
    else:
        lines.append("Takip ettiğin portföy hissesi yok.")
    lines += ["", "<b>YARIN BAKILACAKLAR</b>"]
    for m in yarin:
        lines.append(f"• {_esc(m)}")
    return "\n".join(lines)


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

    # Tum portfoylerdeki benzersiz hisseler -> TEK Haiku cagrisiyla temiz yorumlar
    # (tum mesajlarda paylasilir; her kullanici icin tekrar AI cagrilmaz).
    try:
        tum_tickerlar = sorted({(r.get("ticker") or "").upper().replace(".IS", "")
                                for r in db.list_portfolio() if r.get("ticker")})
    except Exception:
        tum_tickerlar = []
    yorum_map = _temiz_yorumlar(tum_tickerlar, kmap)
    print(f"[{now:%Y-%m-%d %H:%M}] gun sonu temiz yorum: "
          f"{len(yorum_map)}/{len(tum_tickerlar)} hisse")

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
            pf = {(p.get("ticker") or "").upper().replace(".IS", ""):
                  (p.get("para_birimi") or "TL").upper()
                  for p in db.list_portfolio(u["id"]) if p.get("ticker")}
        except Exception:
            pf = {}
        msg = build_message(pf, kmap, overview, yarin, now, kullanici_ad=u.get("ad"),
                            yorum_map=yorum_map)
        try:
            telegram.send_message(msg, chat_id=tg)
            sonuc[str(tg)] = "ok"
        except Exception as e:
            sonuc[str(tg)] = f"hata:{type(e).__name__}"
        gonderilen.add(str(tg))

    # DB dışı env alıcıları -> tüm portföyler birleşik
    try:
        birlesik = {}
        for r in db.list_portfolio():
            t = (r.get("ticker") or "").upper().replace(".IS", "")
            if t:
                birlesik.setdefault(t, (r.get("para_birimi") or "TL").upper())
    except Exception:
        birlesik = {}
    genel = build_message(birlesik, kmap, overview, yarin, now, yorum_map=yorum_map)
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
