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
_GECE_HABER_PATH = Path(__file__).resolve().parents[2] / "data" / "gece_haberleri.json"


def _gece_haber_blok(filtre=None) -> str:
    """Gece (borsa kapaliyken) gelen sektor haberlerini brifing blogu olarak dondurur.

    run_alerts._sektor_haber_tarama gece haberlerini data/gece_haberleri.json'a
    yazar. filtre verilirse (kullanicinin portfoy/watchlist hisseleri) yalniz o
    hisseler gosterilir. Haber yoksa '' doner."""
    try:
        import json
        if not _GECE_HABER_PATH.exists():
            return ""
        haberler = (json.loads(_GECE_HABER_PATH.read_text(encoding="utf-8"))
                    or {}).get("haberler") or []
    except Exception:
        return ""
    if not haberler:
        return ""
    satirlar = []
    for h in haberler:
        hisseler = [str(x).upper() for x in (h.get("hisseler")
                                             or ([h["hisse"]] if h.get("hisse") else []))]
        if filtre is not None:
            hisseler = [x for x in hisseler if x in filtre]
        if not hisseler:
            continue
        baslik = (h.get("baslik") or "").strip()
        if not baslik:
            continue
        link = h.get("link")
        bs = f'<a href="{link}">{baslik}</a>' if link else baslik
        satir = f"📰 <b>{', '.join(hisseler)}</b>: {bs}"
        if h.get("etki"):
            satir += f"\n{h['etki']}"
        satirlar.append(satir)
    if not satirlar:
        return ""
    return "🌙 <b>GECE GELEN HABERLER</b>\n" + "\n\n".join(satirlar)


def _gece_haber_temizle() -> None:
    """Brifinge eklenen gece haberlerini temizler (bir sonraki geceye hazir)."""
    try:
        import json
        _GECE_HABER_PATH.write_text(json.dumps({"haberler": []}, ensure_ascii=False),
                                    encoding="utf-8")
    except Exception:
        pass


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
from src.ai.decision import karar_kelime, karar_emoji, aksiyon_metni


def _esc(s):
    return html.escape(str(s or ""))


def _kisa_gerekce(r, limit=100):
    """Karar gerekcesini tek satira indirir, kelime sinirinda max `limit` karakter."""
    g = " ".join((r.get("gerekce") or "").split())
    if not g:
        return ""
    if len(g) > limit:
        g = g[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return g


def _zarar_uyarilari(uid, results):
    """Kullanicinin portfoyundeki zarardaki pozisyonlar icin uyari listesi.

    -%5'i gecen -> ('DIKKAT', hisse, %), -%10'u gecen -> ('KRITIK', hisse, %).
    Guncel fiyat once brifingde analiz edilen recs'ten (son_kapanis), yoksa
    BIST icin intraday_change'ten alinir. USD pozisyonlarda yalniz recs kullanilir
    (alis ile ayni para birimi olmasi icin)."""
    from src.db import database as db
    from src.alerts.engine import intraday_change

    fiyat = {}
    for r in (results or []):
        if r.get("skipped"):
            continue
        t = (r.get("ticker") or "").upper()
        sk = (r.get("kullanilan_on_sinyal") or {}).get("son_kapanis")
        if t and sk:
            fiyat[t] = sk

    uyarilar = []
    try:
        pozisyonlar = db.list_portfolio(uid)
    except Exception:
        return uyarilar
    for p in pozisyonlar:
        tkr = (p.get("ticker") or "").upper().replace(".IS", "")
        birim = (p.get("para_birimi") or "TL").upper()
        alis = p.get("alim_fiyati") or 0.0
        guncel = fiyat.get(tkr)
        if guncel is None and birim != "USD":
            try:
                info = intraday_change(tkr)
                guncel = info["last_close"] if info else None
            except Exception:
                guncel = None
        if not alis or guncel is None:
            continue
        kz_y = (guncel - alis) / alis * 100
        if kz_y <= -10:
            uyarilar.append(("KRITIK", tkr, kz_y))
        elif kz_y <= -5:
            uyarilar.append(("DIKKAT", tkr, kz_y))
    uyarilar.sort(key=lambda u: u[2])      # en cok zararda olan once
    return uyarilar


def _zarar_satirlari(uyarilar):
    """Zarar uyarilarini Telegram satirlarina cevirir."""
    lines = []
    for seviye, tkr, kz_y in uyarilar:
        if seviye == "KRITIK":
            lines.append(f"🔴 <b>KRİTİK: {_esc(tkr)} -%{abs(kz_y):.1f} zararda</b> — "
                         "pozisyonu gözden geçir.")
        else:
            lines.append(f"🟡 <b>DİKKAT: {_esc(tkr)} -%{abs(kz_y):.1f} zararda.</b>")
    return lines


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


def _us_watchlist_tickers():
    """Watchlist'teki ABD hisseleri (kisisel_diger, market=abd). Ufuk'un izledigi
    NVDA/AMD/TSM/IONQ... gibi semboller. Akademik haberleri bunlarla iliskilendiririz."""
    try:
        from src.watchlist import _data
        wl = _data()
        return {(d.get("ticker") or "").upper().replace(".IS", "")
                for d in wl.get("kisisel_diger", [])
                if d.get("market") == "abd" and d.get("ticker")}
    except Exception:
        return set()


def _akademik_ozet_baglanti(akademik_gundem, izlenen_us, client=None, limit=3):
    """ABD brifingi akademik/kurum haberlerini Haiku ile Turkce ozetler ve izlenen
    ABD hisseleriyle iliskilendirir.

    Doner: [{"ozet": TR ozet, "etkilenen": [ticker...], "yorum": kisa yorum}].
    Anahtar yoksa/hata olursa [] -> cagiran ham (Ingilizce) listeye duser."""
    if not akademik_gundem or not os.environ.get("ANTHROPIC_API_KEY"):
        return []
    izl = {(t or "").upper() for t in (izlenen_us or set()) if t}
    haber_satir = []
    for i, h in enumerate(akademik_gundem[:8]):
        b = (h.get("baslik") if isinstance(h, dict) else str(h)) or ""
        k = h.get("kaynak") if isinstance(h, dict) else ""
        oz = h.get("ozet") if isinstance(h, dict) else ""
        if oz in (None, "None"):
            oz = ""
        if b:
            haber_satir.append(f"{i + 1}. [{k}] {b}"
                               + (f" — {oz}" if oz and oz != b else ""))
    if not haber_satir:
        return []
    import json as _json
    try:
        import anthropic
        client = client or anthropic.Anthropic()
        resp = client.messages.create(
            model="claude-haiku-4-5", max_tokens=700,
            system=("Sen bir ABD piyasalari analistisin. Sana akademik/kurum/teknoloji "
                    "haber basliklari ve bir yatirimcinin izledigi ABD hisseleri verilir. "
                    f"EN ONEMLI {limit} haberi sec; her birini KISA tek Turkce cumleyle "
                    "ozetle (basligi kopyalama, gercekten cevir/ozetle), izlenen "
                    "hisselerden hangilerini etkileyebilecegini SADECE verilen listeden "
                    "sec (etkilemiyorsa bos birak), tek cumlelik kisa yorum/aksiyon ver "
                    "(or. 'yari iletken trendi, uzun vadeli AL firsati' / 'kisa vadede "
                    "notr'). Veri veya hisse UYDURMA, listede olmayan hisse yazma. "
                    "SADECE su JSON dizisini dondur, baska hicbir metin yok:\n"
                    '[{"ozet":"...","etkilenen":["NVDA","AMD"],"yorum":"..."}]'),
            messages=[{"role": "user", "content":
                       "Izlenen ABD hisseleri: " + ", ".join(sorted(izl))
                       + "\n\nHaberler:\n" + "\n".join(haber_satir)}],
        )
        txt = "".join(getattr(b, "text", "") for b in resp.content
                      if getattr(b, "type", "") == "text").strip()
        a, z = txt.find("["), txt.rfind("]")
        data = _json.loads(txt[a:z + 1]) if a >= 0 and z > a else []
    except Exception as e:
        print(f"  akademik ozet (Haiku) atlandi: {type(e).__name__}: {str(e)[:60]}")
        return []
    out = []
    for it in (data if isinstance(data, list) else []):
        if not isinstance(it, dict):
            continue
        ozet = (it.get("ozet") or "").strip()
        if not ozet:
            continue
        etk = [str(x).upper() for x in (it.get("etkilenen") or [])
               if str(x).upper() in izl]
        out.append({"ozet": ozet, "etkilenen": etk,
                    "yorum": (it.get("yorum") or "").strip()})
        if len(out) >= limit:
            break
    return out


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


def evaluate_all(targets, overview=None, learning=None, extra_context=None):
    """Her hedef hisse icin TAM analiz zincirini calistirir (commentary.py).

    Zincir: yfinance + KAP(30g) + haber(7g) -> Claude -> karar/puan/risk/...
    ai_commentary.json'a yazar ve her karari decisions tablosuna kaydeder.
    overview/learning: brifingden gecirilen genel piyasa baglami + karar ogrenimi.
    extra_context: AI baglamina eklenecek ek alanlar (ABD brifingi: abd_gundemi).
    """
    from src.ai import commentary
    if not targets:
        return []
    # Batch API: %50 daha ucuz; sabah brifinginde gecikme kabul edilebilir.
    # Batch basarisiz olursa tek-tek calistirmaya geri don.
    try:
        return commentary.run_batch(targets, save=True, verbose=True,
                                    overview=overview, learning=learning,
                                    extra_context=extra_context)
    except Exception as e:
        print(f"  [batch] basarisiz ({type(e).__name__}: {str(e)[:300]}); "
              "tek-tek calistiriliyor")
        return commentary.run(targets, save=True, verbose=True,
                              overview=overview, learning=learning,
                              extra_context=extra_context)


def _yorum_cumle(r, limit=180):
    """Hisse icin sade 1-2 cumlelik yorum. Once AI'nin teknik-oran icermeyen
    'sade_yorum' alanini kullanir; yoksa (eski kayit) gerekceye duser."""
    sade = " ".join((r.get("sade_yorum") or "").split())
    if sade:
        if len(sade) > limit:
            sade = sade[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"
        return sade
    return _kisa_gerekce(r, limit=limit)


def _risk_kelime(r):
    """Risk skorunu sade kelimeye cevirir (sayi gostermez)."""
    s = (r.get("risk") or {}).get("score")
    if s is None:
        return None
    if s >= 7:
        return "yüksek risk"
    if s >= 4:
        return "orta risk"
    return "düşük risk"


def _karar_motoru_satirlari(r):
    """Karar motoru alanlari: AL'da Giris/Hedef/Stop; TUT'ta Stop; hepsinde Tetikleyici."""
    fd = (r.get("final_decision") or "").upper()
    giris = (r.get("giris_seviyesi") or "").strip()
    hedef = (r.get("hedef_fiyat") or "").strip()
    stop = (r.get("stop_loss") or "").strip()
    tetik = (r.get("tetikleyici_kosul") or "").strip()
    cikis = (r.get("cikis_stratejisi") or "").strip()
    pozisyon = (r.get("position_size_oneri") or "").strip()
    out = []
    if fd == "AL":
        eq = r.get("entry_quality") or {}
        if eq.get("skor") is not None:
            out.append(f"Giriş kalitesi: {eq.get('yildiz', '')} ({eq['skor']}/100)"
                       + (f" — {eq['oneri']}" if eq.get("oneri") else ""))
        p = []
        if giris: p.append(f"Giriş: {giris}")
        if hedef: p.append(f"Hedef: {hedef}")
        if stop: p.append(f"Stop: {stop}")
        if p:
            out.append(" | ".join(p))
        if pozisyon:                     # pozisyon buyuklugu onerisi (yalniz AL)
            out.append(f"Pozisyon: {pozisyon}")
    elif fd == "TUT" and stop:
        out.append(f"Stop: {stop}")
    if cikis and fd in ("AL", "TUT"):    # cikis stratejisi (AL/TUT)
        out.append(f"Çıkış: {cikis}")
    if tetik:                            # tetikleyici TUM kararlarda
        out.append(f"Tetikleyici: {tetik}")
    return out


def _firsat_siralamasi(valid):
    """AL kararlarini expected_value'ya gore siralar (en iyi 3 firsat).

    expected_value = (hedef_fiyat - guncel_fiyat) * entry_quality_skoru / 100
    Yalniz hedef fiyati + giris kalitesi skoru olan AL'ler degerlendirilir.
    Doner: [{ticker, yildiz, hedef_pct, ev}, ...] (en fazla 3, ev azalan)."""
    from src.ai.commentary import parse_first_price
    out = []
    for r in valid:
        if (r.get("final_decision") or "").upper() != "AL":
            continue
        eq = r.get("entry_quality") or {}
        skor = eq.get("skor")
        guncel = (r.get("kullanilan_on_sinyal") or {}).get("son_kapanis")
        hedef = parse_first_price(r.get("hedef_fiyat"))
        if skor is None or not guncel or hedef is None or hedef <= guncel:
            continue
        ev = (hedef - guncel) * skor / 100
        hedef_pct = (hedef - guncel) / guncel * 100
        out.append({"ticker": (r.get("ticker") or "").upper(),
                    "yildiz": eq.get("yildiz") or "",
                    "hedef_pct": hedef_pct, "ev": ev})
    out.sort(key=lambda x: x["ev"], reverse=True)
    return out[:3]


def _firsat_satirlari(firsatlar):
    """Firsat siralamasini Telegram satirlarina cevirir."""
    lines = ["", "<b>🎯 BUGÜNÜN EN İYİ FIRSATLARI</b>"]
    for i, f in enumerate(firsatlar, 1):
        lines.append(
            f"{i}. <b>{_esc(f['ticker'])}</b> — Giriş: {f['yildiz']} | "
            f"Hedef: +%{f['hedef_pct']:.0f} | EV: +{f['ev']:.1f}")
    return lines


def _hisse_blok(r, portfoyde=False):
    """Tek hisse blogu: '<emoji> TICKER — KARAR' + sade yorum + 'Aksiyon: ...'
    + karar motoru (giris/hedef/stop/tetikleyici)."""
    fd = r.get("final_decision")
    kelime = karar_kelime(fd)
    if not kelime:                       # KILL_SWITCH vb. -> kullaniciya gosterme
        return []
    tkr = _esc(r.get("ticker") or r.get("symbol"))
    blok = [f"{karar_emoji(fd)} <b>{tkr} — {kelime}</b>"]
    yorum = _yorum_cumle(r)
    if yorum:
        if _risk_kelime(r) == "yüksek risk":
            yorum = f"{yorum} (yüksek risk)"
        blok.append(f"<i>{_esc(yorum)}</i>")
    blok.append(f"Aksiyon: {_esc(aksiyon_metni(fd, portfoyde))}")
    for satir in _karar_motoru_satirlari(r):
        blok.append(f"<i>{_esc(satir)}</i>")
    return blok


def _plan_cumlesi(overview, now, is_us):
    """GÜNÜN PLANI: genel piyasa durumu 1-2 sade cümle (teknik oran yok)."""
    if is_us:
        return "ABD piyasası açılışa hazırlanıyor."
    if overview and overview.get("available"):
        notu = (overview.get("brifing_notu") or "").strip()
        if notu:
            return notu
        yon = (overview.get("yon") or "").upper()
        return {"YUKSELIYOR": "Piyasa güne alıcılı başlıyor.",
                "DUSUYOR": "Piyasa güne satıcılı başlıyor.",
                "YATAY": "Piyasa yatay, belirgin yön yok."}.get(
                    yon, "Piyasada belirgin bir yön yok.")
    return "Piyasa için net bir yön sinyali yok; temkinli başla."


def _akademik_render(lines, akademik_ozet, akademik_gundem, portfolio):
    """AKADEMİK & KURUM bolumunu 'lines'a ekler (yerinde). akademik_ozet (Haiku ile
    Turkce + izlenen ABD hisse baglantili) varsa zengin format; yoksa ham (Ingilizce)
    listeye duser. Etkilenen ama portfoyde OLMAYAN izlenen hisseler '💡' firsat satiri."""
    portfolio = portfolio or set()
    if akademik_ozet:
        lines.append("")
        lines.append("<b>BUGÜN TAKİP · AKADEMİK & KURUM</b>")
        firsatlar = {}      # ticker -> kisa not (portfoyde OLMAYAN izlenen hisseler)
        for it in akademik_ozet[:3]:
            ozet = (it.get("ozet") or "").strip()
            if not ozet:
                continue
            etk = [str(x).upper() for x in (it.get("etkilenen") or [])]
            yorum = (it.get("yorum") or "").strip()
            if etk:
                sag = ", ".join(etk) + (f" için {yorum}" if yorum else "")
                lines.append(f"• {_esc(ozet)} → {_esc(sag)}")
            else:
                lines.append(f"• {_esc(ozet)}" + (f" — {_esc(yorum)}" if yorum else ""))
            for t in etk:                       # portfoyde yok ama izlenen -> firsat
                if t not in portfolio and t not in firsatlar:
                    firsatlar[t] = yorum or ozet
        for t, notu in list(firsatlar.items())[:3]:
            notu = (notu[:90] + "…") if len(notu) > 90 else notu
            lines.append(f"💡 Portföyünde yok ama izlemeye değer: {t} — {_esc(notu)}")
    elif akademik_gundem:
        lines.append("")
        lines.append("<b>BUGÜN TAKİP · AKADEMİK & KURUM</b>")
        for h in akademik_gundem[:5]:
            baslik = (h.get("baslik") if isinstance(h, dict) else str(h)) or ""
            kaynak = h.get("kaynak") if isinstance(h, dict) else ""
            if baslik:
                ek = f" <i>[{_esc(kaynak)}]</i>" if kaynak else ""
                lines.append(f"• {_esc(baslik)}{ek}")


def build_message(results, sel, now, overview=None, portfolio=None, kullanici_ad=None,
                  profil_uyari=None, zarar_uyarilari=None, senaryolar=None,
                  portfoy_guncel_gun=None, us_gundem=None, akademik_gundem=None,
                  akademik_ozet=None, sektor_uyarilari=None):
    """SABAH brifingi — GÜNÜN PLANI / PORTFÖY / FIRSATLAR / BUGÜN TAKİP.

    Sadece 5 karar kelimesi (AL/TUT/BEKLE/AZALT/UZAK DUR), izinli emojiler
    (🟢🟡🔴⚡📰), teknik oran yok, kısa. portfolio=None ise sel['portfolio']
    (tüm portföyler birleşik) kullanılır; kullanici_ad başlığı kişiselleştirir.
    """
    is_us = sel.get("market") in ("us", "abd")
    valid = [r for r in results if not r.get("skipped")]
    portfolio = portfolio if portfolio is not None else (sel.get("portfolio") or set())

    def _in_pf(r):
        return (r.get("ticker") or "").upper() in portfolio

    ad = f" · {str(kullanici_ad).capitalize()}" if kullanici_ad else ""
    baslik = "🇺🇸 ABD PİYASASI" if is_us else "GÜNÜN PLANI"
    lines = [f"<b>{baslik}</b>{ad} — {now:%d.%m %H:%M}"]

    if not valid:
        lines.append("")
        lines.append("Bugün net bir sinyal yok. Aksiyon: BEKLE.")
        # ABD brifinginde sinyal olmasa bile gundem + akademik/kurum gosterilir
        if is_us and us_gundem:
            lines.append("")
            lines.append("<b>BUGÜN TAKİP · ABD GÜNDEMİ</b>")
            for h in us_gundem[:5]:
                b = (h.get("baslik") if isinstance(h, dict) else str(h)) or ""
                k = h.get("kaynak") if isinstance(h, dict) else ""
                if b:
                    lines.append(f"• {_esc(b)}" + (f" <i>[{_esc(k)}]</i>" if k else ""))
        if is_us:
            _akademik_render(lines, akademik_ozet, akademik_gundem, portfolio)
        return "\n".join(lines)

    # Genel piyasa durumu (1-2 cümle)
    lines.append(_esc(_plan_cumlesi(overview, now, is_us)))
    # Bugün PPK varsa kritik hatırlatma (tek satır)
    if not is_us:
        try:
            from src.news.macro import bugun_ppk_mi
            if bugun_ppk_mi(now.date()):
                lines.append("🟡 Bugün PPK var — faiz kararı 14:00'te. "
                             "Önemli kararları sonrasına bırak.")
        except Exception:
            pass

    # PORTFÖY (her zaman göster)
    pf_rows = [r for r in valid if _in_pf(r)]
    lines.append("")
    lines.append("<b>PORTFÖY</b>")
    if portfoy_guncel_gun is not None and portfoy_guncel_gun > 3:
        lines.append(f"🟡 Portföyün {portfoy_guncel_gun} gündür güncellenmedi — "
                     "güncel tutarsan önerilerim daha isabetli olur.")
    if pf_rows:
        if zarar_uyarilari:                  # zarardaki pozisyonlar önce (kritik)
            lines += _zarar_satirlari(zarar_uyarilari)
        for r in pf_rows:
            lines += _hisse_blok(r, portfoyde=True)
    else:
        lines.append("Takip ettiğin portföy hissesi yok.")

    # KISISEL SEKTOR UYARISI: kullanicinin geçmişte zayıf kaldığı sektör(ler)
    for u in (sektor_uyarilari or [])[:2]:
        if u:
            lines.append(_esc(u))

    # BUGÜNÜN EN İYİ FIRSATLARI — AL kararlarini expected_value'ya gore sirala (max 3)
    firsatlar = _firsat_siralamasi(valid)
    if firsatlar:
        lines += _firsat_satirlari(firsatlar)

    # FIRSATLAR (max 5) — portföy dışı AL sinyalleri
    firsat = [r for r in valid if not _in_pf(r) and r.get("final_decision") == "AL"]
    firsat.sort(key=lambda r: r.get("score") or 0, reverse=True)
    if firsat:
        lines.append("")
        lines.append("<b>FIRSATLAR</b>")
        for r in firsat[:5]:
            lines += _hisse_blok(r, portfoyde=False)

    # BUGÜN TAKİP — 2-3 şartlı senaryo
    if senaryolar:
        lines.append("")
        lines.append("<b>BUGÜN TAKİP</b>")
        for s in senaryolar[:3]:
            metin = s.get("metin") if isinstance(s, dict) else str(s)
            if metin:
                lines.append(f"• {_esc(metin)}")

    # ABD GÜNDEMİ — son 24 saatteki ABD piyasa haberleri (yalnız ABD brifingi)
    if is_us and us_gundem:
        lines.append("")
        lines.append("<b>BUGÜN TAKİP · ABD GÜNDEMİ</b>")
        for h in us_gundem[:5]:
            baslik = (h.get("baslik") if isinstance(h, dict) else str(h)) or ""
            kaynak = h.get("kaynak") if isinstance(h, dict) else ""
            if baslik:
                ek = f" <i>[{_esc(kaynak)}]</i>" if kaynak else ""
                lines.append(f"• {_esc(baslik)}{ek}")

    # AKADEMİK & KURUM (yalniz ABD): Turkce ozet + izlenen ABD hisse baglantisi
    if is_us:
        _akademik_render(lines, akademik_ozet, akademik_gundem, portfolio)

    if profil_uyari:
        lines.append("")
        lines.append(profil_uyari)

    return "\n".join(lines)


def _record_briefing_memory(results):
    """Sabah brifingindeki dikkat ceken kararlari (AL/SAT/VETO) her kullanicinin
    hafizasina 'karar' tipiyle yazar (kime gonderildigi)."""
    from src.db import database as db
    notable = [r for r in (results or [])
               if not r.get("skipped")
               and r.get("final_decision") in ("AL", "SAT", "GUCLU_SAT", "VETO",
                                               "AZALT", "UZAK_DUR")]
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
        mini = update_decisions.mini_update(verbose=False)   # AL/SAT 1.gun degisimi
        print(f"[{now:%Y-%m-%d %H:%M}] Karar ogrenimi: {guncellenen} sonuc + {mini} 1.gun guncellendi.")
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

    # 2.5) ABD brifingi: son 24 saatteki ABD piyasa gundemi (Reuters/Yahoo/Investing)
    #      + akademik/kurum gundemi (MIT/Stanford/arXiv/NASA/DARPA/FED...)
    us_gundem = []
    akademik_gundem = []
    if is_us:
        try:
            from src.news.us_news import market_news
            us_gundem = market_news(within_hours=24, limit=8)
            print(f"  ABD gundemi: {len(us_gundem)} haber (24s)")
        except Exception as e:
            print(f"  ABD gundemi alinamadi: {type(e).__name__}: {str(e)[:80]}")
            us_gundem = []
        try:
            from src.news.us_news import academic_news
            akademik_gundem = academic_news(within_hours=24, limit=8)
            print(f"  akademik/kurum gundemi: {len(akademik_gundem)} haber (24s)")
        except Exception as e:
            print(f"  akademik gundem alinamadi: {type(e).__name__}: {str(e)[:80]}")
            akademik_gundem = []

    # 2.6) AKADEMIK OZET: akademik/kurum haberlerini Haiku ile Turkceye cevir + izlenen
    #      ABD hisseleriyle (watchlist + ABD portfoy) iliskilendir. Bir kez hesaplanir.
    akademik_ozet = []
    if is_us and akademik_gundem:
        izlenen_us = _us_watchlist_tickers() | set(_us_portfolio_tickers())
        akademik_ozet = _akademik_ozet_baglanti(akademik_gundem, izlenen_us, limit=3)
        print(f"  akademik ozet: {len(akademik_ozet)} haber Turkce+baglantili")

    # 3) Karar gecmisi ogrenimi (hedef hisseler icin)
    try:
        from src.ai.learning import build_learning_notes, weak_sector_warnings
        learning = build_learning_notes(sel["targets"])
        if learning:
            print(f"  karar gecmisi notu: {list(learning.keys())}")
        zayif = weak_sector_warnings()           # L3: zayif sektor uyarilari (notlara katildi)
        if zayif:
            print(f"  zayif sektorler (temkin): {list(zayif.keys())}")
    except Exception as e:
        print(f"  karar gecmisi notu alinamadi: {type(e).__name__}")
        learning = {}

    # ABD + akademik gundemini AI baglamina ekle
    # (piyasa_baglami.abd_gundemi / piyasa_baglami.akademik_gundemi)
    extra_context = None
    if is_us and (us_gundem or akademik_gundem):
        extra_context = {}
        if us_gundem:
            extra_context["abd_gundemi"] = [
                {"baslik": h.get("baslik"), "kaynak": h.get("kaynak")}
                for h in us_gundem]
        if akademik_gundem:
            extra_context["akademik_gundemi"] = [
                {"baslik": h.get("baslik"), "kaynak": h.get("kaynak")}
                for h in akademik_gundem]
    results = evaluate_all(sel["targets"], overview=overview, learning=learning,
                           extra_context=extra_context)

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

    # 6.5) BUGÜN TAKİP: şartlı senaryolar üret + senaryo_takip.json'a kaydet (yalniz BIST)
    senaryolar = []
    if not is_us:
        try:
            from src.ai import senaryo
            from src.news.macro import get_macro
            macro = get_macro()
            notable = [r.get("ticker") for r in results
                       if not r.get("skipped")
                       and r.get("final_decision") in ("AL", "AZALT", "UZAK_DUR")]
            gundem = []
            try:
                from src.news.rss_source import RSSNewsSource
                gundem = [f"[{e['kaynak']}] {e['baslik']}"
                          for e in RSSNewsSource()._all_entries()[:6]]
            except Exception:
                gundem = []
            senaryolar = senaryo.uret(notable, gundem, macro, overview)
            senaryo.kaydet(senaryolar, now.date().isoformat(), macro=macro, overview=overview)
            print(f"  senaryolar: {len(senaryolar)} uretildi/kaydedildi")
        except Exception as e:
            print(f"  senaryo uretimi atlandi: {type(e).__name__}: {str(e)[:80]}")

    # 7) KISISEL gonderim: ortak piyasa/haber govdesi + her kullanicinin kendi
    #    portfoyune ozel "Portföyündeki hisseler" bolumu. DB'de telegram_id'si olan
    #    her kullaniciya kendi mesaji; DB disi env alicilara birlesik mesaj.
    from src.db import database as db
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
        # Profil guven skoru <%85 -> onboarding tamamlanmamis, nazik hatirlatma
        profil_uyari = None
        try:
            skor = (db.get_profile(u["id"]) or {}).get("profil_guven_skoru") or 0
            if skor < 85:
                profil_uyari = ("💡 Seni daha iyi tanırsam daha isabetli öneriler "
                                "verebilirim. Uygulamada Ayarlar → Beni daha iyi tanı")
        except Exception:
            pass
        try:
            zarar_uy = _zarar_uyarilari(u["id"], results)
        except Exception:
            zarar_uy = []
        # Portföy kaç gündür güncellenmedi? (3 günden eskiyse PORTFÖY'de uyarı)
        guncel_gun = None
        try:
            son = db.portfolio_last_update(u["id"])
            if son:
                d = datetime.fromisoformat(str(son)[:10]).date()
                guncel_gun = (now.date() - d).days
        except Exception:
            guncel_gun = None
        # KISISEL SEKTOR UYARISI: kullanicinin geçmişte zayıf kaldığı sektörler
        sektor_uy = []
        try:
            from src.ai.learning import user_weak_sector_warnings
            sektor_uy = list(user_weak_sector_warnings(u["id"]).values())
        except Exception:
            sektor_uy = []
        msg = build_message(results, sel, now, overview=overview,
                            portfolio=pf, kullanici_ad=u.get("ad"),
                            profil_uyari=profil_uyari, zarar_uyarilari=zarar_uy,
                            senaryolar=senaryolar, portfoy_guncel_gun=guncel_gun,
                            us_gundem=us_gundem, akademik_gundem=akademik_gundem,
                            akademik_ozet=akademik_ozet, sektor_uyarilari=sektor_uy)
        # GECE GELEN HABERLER: kullanicinin izledigi hisseleri etkileyenler (varsa)
        try:
            from src.watchlist import load_index, load_personal
            izlenen = set(pf) | {(t or "").upper().replace(".IS", "")
                                 for t in (load_index() + load_personal())}
        except Exception:
            izlenen = set(pf)
        gece_blok = _gece_haber_blok(filtre=izlenen)
        if gece_blok:
            msg = msg + "\n\n" + gece_blok
        try:
            telegram.send_message(msg, chat_id=tg)
            sonuc[str(tg)] = "ok"
        except Exception as e:
            sonuc[str(tg)] = f"hata:{type(e).__name__}"
        gonderilen.add(str(tg))
    # DB'de kullanici olarak olmayan env alicilari (TELEGRAM_CHAT_ID/IDS) -> birlesik
    genel = build_message(results, sel, now, overview=overview,
                          senaryolar=senaryolar, us_gundem=us_gundem,
                          akademik_gundem=akademik_gundem,
                          akademik_ozet=akademik_ozet)   # portfolio=tum birlesik
    gece_blok_genel = _gece_haber_blok()                    # filtresiz (tum hisseler)
    if gece_blok_genel:
        genel = genel + "\n\n" + gece_blok_genel
    for cid in telegram.recipient_ids():
        if str(cid) in gonderilen:
            continue
        try:
            telegram.send_message(genel, chat_id=cid)
            sonuc[str(cid)] = "ok"
        except Exception as e:
            sonuc[str(cid)] = f"hata:{type(e).__name__}"

    # Gece haberleri brifinge eklendi -> temizle (bir sonraki geceye hazir)
    _gece_haber_temizle()

    ok = [c for c, s in sonuc.items() if s == "ok"]
    print(f"[{now:%Y-%m-%d %H:%M}] Telegram kisisel gonderim ({etiket}): {len(ok)}/{len(sonuc)} alici "
          f"({len(results)} hisse). Sonuc: {sonuc}")
    return 0 if ok else 1


if __name__ == "__main__":
    _market = "us" if (len(sys.argv) > 1 and sys.argv[1].lower() in ("us", "abd")) else "bist"
    sys.exit(main(_market))
