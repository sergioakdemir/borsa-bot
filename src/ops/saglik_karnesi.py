"""Gunluk sistem saglik karnesi (cron: her gun 18:45).

Amac: sistem sessizce bozuldugunda ANINDA gorunur olsun. Her aksam TEK bir
Telegram mesajiyla o gunun tam durumunu admin'lere ozetler: karar uretimi,
veri hatasiyla atlananlar, gun kalitesi, veri kaynaklari (KAP/fiyat/haber),
gece isleri, AI hatalari, olu sembol ve genel DURUM.

Ayrica ANLIK KIRMIZI ALARM esikleri (brifing icindeki run_batch %5 sari / %10
kirmizi ile tamamlanir): BIST karari beklenenin %70 alti, gece isi 0 kayit,
kaynak 2 gun bos, AI hata >%10 -> DURUM=SORUNLU ve mesaj basina kirmizi.

Karar KURALLARINA dokunmaz; yalniz izleme/raporlama.
Calistirma: python -m src.ops.saglik_karnesi [--print]   (--print: gonderme, ekrana bas)
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_TZ = ZoneInfo("Europe/Istanbul")
CACHE_PATH = ROOT / "data" / "fiyat_cache.json"

BIST_BEKLENEN = 92          # watchlist bist_endeks
US_BEKLENEN = 16            # instruments market='US' aktif
BIST_ALT_ESIK = 0.70        # beklenenin %70 alti -> kirmizi
BRIFING_BEKLEME_SAAT = 10   # bu saatten ONCE 0 karar = brifing henuz kosmadi (normal),
                            # SONRA 0 karar = gercek sorun (kirmizi)


def _bugun():
    return datetime.now(_TZ).date().isoformat()


def _karar_sayilari(tarih):
    """Bugun uretilen BIST/US karar sayisi + KILL_SWITCH."""
    from src.db import database as db
    from src.ops.gun_kalitesi import _is_us
    with db.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT ticker, karar FROM decisions WHERE tarih=?", (tarih,))]
    bist = sum(1 for r in rows if not _is_us(r["ticker"]))
    us = sum(1 for r in rows if _is_us(r["ticker"]))
    kill = sum(1 for r in rows if "KILL" in (r["karar"] or "").upper())
    return bist, us, kill


def _karar_dagilimi(tarih):
    """Gunun kararlarini tipe gore sayar: AL / BEKLE / UZAK_DUR / TUT / AZALT.
    Sade panel ozeti icin — sahibi 'kac AL cikti' sorusunu tek bakista gorsun."""
    from src.db import database as db
    with db.get_conn() as c:
        rows = c.execute("SELECT karar FROM decisions WHERE tarih=?", (tarih,)).fetchall()
    dagilim = {}
    for (k,) in rows:
        ad = (k or "").upper()
        if "KILL" in ad:
            continue                         # veri freni; karar sayilmaz
        dagilim[ad] = dagilim.get(ad, 0) + 1
    return {
        "al": dagilim.get("AL", 0),
        "uzak_dur": dagilim.get("UZAK_DUR", 0),
        "bekle": dagilim.get("BEKLE", 0),
        "tut": dagilim.get("TUT", 0),
        "azalt": dagilim.get("AZALT", 0),
    }


def _fiyat_cache_yas_dk():
    if not CACHE_PATH.exists():
        return None
    try:
        mtime = datetime.fromtimestamp(CACHE_PATH.stat().st_mtime, _TZ)
        return (datetime.now(_TZ) - mtime).total_seconds() / 60
    except OSError:
        return None


def _kap_durum(tarih):
    """(canli_mi, bugunku_kap_bildirim_sayisi)."""
    from src.db import database as db
    ornek = str(db.get_setting(f"kap_ornek:{tarih}", "0")) == "1"
    try:
        with db.get_conn() as c:
            n = c.execute(
                "SELECT COUNT(*) FROM haber_etki WHERE substr(haber_tarihi,1,10)=? "
                "AND (kaynak IS NULL)", (tarih,)).fetchone()[0]
    except Exception:
        n = 0
    return (not ornek), n


def _kap_basari(tarih):
    """Gunun KAP cekim basari metrikleri (kap_source._say sayaclarindan).
    (oran %|None, ok, fail, r429, fallback, toplam)."""
    from src.db import database as db
    g = lambda ad: int(db.get_setting(f"{ad}:{tarih}", 0) or 0)
    ok, fail, r429, fb = g("kap_ok"), g("kap_fail"), g("kap_429"), g("kap_fallback")
    toplam = ok + fail
    oran = (ok / toplam * 100) if toplam else None
    return {"oran": oran, "ok": ok, "fail": fail, "r429": r429,
            "fallback": fb, "toplam": toplam}


def _haber_istatistik():
    """RSS havuzu (24s) + kac BIST hissesi eslesti (canli, tek fetch)."""
    try:
        from src.news.rss_source import RSSNewsSource
        from src.watchlist import load_index
        rss = RSSNewsSource()
        havuz = rss.recent_count()
        eslesme = sum(1 for t in load_index() if rss.get_news(t, limit=1))
        return havuz, eslesme
    except Exception:
        return None, None


def _gece_isleri():
    """(is_adi -> 'ok'|'gec'|'yok') son gece bakim islerinin durumu."""
    from src.db import database as db
    isler = {"trades": "update_trades", "karne": "update_decisions",
             "yukselis_hafizasi": "yukselis_hafizasi"}
    out = {}
    for etiket, is_adi in isler.items():
        yas = db.kalp_yasi_saat(is_adi)
        out[etiket] = "yok" if yas is None else ("gec" if yas > 30 else "ok")
    return out


def _pasif_sembol_sayisi():
    """bist100.json'da PASIF isaretli sembol sayisi (veri cekiminden cikarilmis).

    23 Tem 2026: 8 sembol (KOZAL, SOYLM, ZORLU, TIRE, KERVT, YKGYO, ADNAC, FINBN)
    yfinance'te 1 yildir veri dondurmuyordu ve her fiyat cache kosusunda sessizce
    hata uretiyordu (146/154). Pasiflendiler. Karne bunu GORUNUR tutar: yoksa
    "154 hedefliyorduk, 146 cekiyoruz" farki kimsenin gozune carpmaz.
    Bu sayac, _olu_sembol_sayisi()'ndan FARKLI bir seyi olcer (bkz. orasi).
    """
    try:
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parents[2] / "config" / "bist100.json"
        return len(json.loads(p.read_text(encoding="utf-8")).get("pasif") or {})
    except Exception:
        return 0


def _olu_sembol_sayisi():
    """KARAR seviyesinde olu sembol: art arda >=3 gun KILL_SWITCH alan sembol sayisi.

    DIKKAT: bu, _pasif_sembol_sayisi() ile AYNI SEY DEGILDIR. Burasi karar motoruna
    girip veri gelmedigi icin KILL_SWITCH yiyen sembolleri sayar (orn. GMSTR.F);
    orasi ise fiyat cache evreninden bastan cikarilmis sembolleri. 22 Tem 2026
    denetiminde bu ikisi karistirilmisti ("karne 1 diyor ama 8 hata var").
    """
    from src.db import database as db
    with db.get_conn() as c:
        son3 = [r[0] for r in c.execute(
            "SELECT DISTINCT tarih FROM decisions ORDER BY tarih DESC LIMIT 3")]
        if len(son3) < 3:
            return 0
        rows = c.execute(
            "SELECT ticker FROM decisions WHERE tarih IN (?,?,?) AND karar='KILL_SWITCH'",
            (son3[0], son3[1], son3[2])).fetchall()
    from collections import Counter
    cc = Counter(r[0] for r in rows)
    return sum(1 for t, n in cc.items() if n >= 3)


def kredi_durumu(tarih=None) -> dict:
    """AI kredisi var mi bitti mi? commentary.kredi_freni_koy'un koydugu gunluk
    bayragi ('ai_kredi_bitti:<gun>') okur — canli API cagrisi YAPMAZ (panel 30 sn'de
    bir yenileniyor; her yenilemede cagri para/kota harcardi).
    doldu=True -> kredi BITTI."""
    from src.db import database as db
    tarih = tarih or _bugun()
    try:
        bayrak = db.get_setting(f"ai_kredi_bitti:{tarih}")
    except Exception:
        bayrak = None
    # GERCEK harcama (loglardan, KESIN). "Kac dolar kaldi" TAHMINI artik
    # gosterilmez — otomatik yenileme ($5->$20) devrede oldugu icin hem yanlis
    # hem gereksizdi (19 Tem 2026). Tahmin alanlari geriye-donuk uyum icin
    # birakildi ama panel/karne artik `harcama`yi kullanir.
    try:
        from src.ops import kredi_takip
        harcama = kredi_takip.harcama_ozeti(tarih)
    except Exception:
        harcama = {}
    return {"bitti": bool(bayrak), "sebep": str(bayrak or "")[:160],
            "harcama": harcama}


def motor_durumu() -> dict:
    """Motor fonksiyonlarinin son tetiklenme zamani + kod yolu bagli mi.

    'Son tetiklenme' izlenebilir olanlar decisions'tan okunur (veto: risk>=9 karar,
    kill: KILL_SWITCH karari). EQ/stop/bilanco freni icin ayri sayac YOK -> kod
    yolu grep ile dogrulanir (haftalik_tarama.py ile ayni yaklasim); bu yuzden
    'son tetiklenme' yerine 'BAGLI/KOPUK' gosterilir.
    """
    from src.db import database as db
    out = {}
    try:
        with db.get_conn() as c:
            r = c.execute("SELECT MAX(tarih) FROM decisions WHERE risk>=9").fetchone()
            out["cift_risk_vetosu"] = {"tip": "sayac", "son": r[0] if r else None}
            r = c.execute("SELECT MAX(tarih) FROM decisions "
                          "WHERE karar='KILL_SWITCH'").fetchone()
            out["kill_switch"] = {"tip": "sayac", "son": r[0] if r else None}
    except Exception:
        out["cift_risk_vetosu"] = {"tip": "sayac", "son": None}
        out["kill_switch"] = {"tip": "sayac", "son": None}
    # Kod yolu kontrolu: ilgili modul/filtre commentary'de hala cagriliyor mu?
    try:
        kaynak = (ROOT / "src" / "ai" / "commentary.py").read_text(encoding="utf-8")
    except OSError:
        kaynak = ""
    for ad, imza in (("eq_filtresi", "entry_quality"),
                     ("stop_hedef_motoru", "stop_hedef"),
                     ("bilanco_freni", "BILANCO FRENI")):
        out[ad] = {"tip": "kod", "bagli": (imza in kaynak) if kaynak else None}
    return out


def _motorlar_ok(motor: dict) -> bool:
    """Kod yolu kontrolu yapilan motorlarin hepsi bagli mi? (nabiz satiri icin)"""
    return all(v.get("bagli") is not False
               for v in motor.values() if v.get("tip") == "kod")


def nabiz(m: dict) -> str:
    """GUNLUK NABIZ (kalp atisi): tek satir ozet. Sorun olmasa BILE her aksam
    gonderilir — gelmezse cron/sistem cokmus demektir. Kredi bittiyse basa 🔴."""
    kredi = m.get("kredi") or {}
    if kredi.get("bitti"):
        kredi_txt = "BİTTİ"
    else:
        h = kredi.get("harcama") or {}
        if h:
            kredi_txt = (f"bugün ${h['bugun']:.2f} · ay ${h['ay']:.2f}"
                         + (" ⚠️" if h.get("anomali") else ""))
        else:
            kredi_txt = "var"
    kb = m.get("kap_basari") or {}
    kap_txt = f"%{kb['oran']:.0f}" if kb.get("oran") is not None else "veri yok"
    motor_txt = "ok" if m.get("motorlar_ok") else "SORUN"
    karar_n = (m.get("bist") or 0) + (m.get("us") or 0)
    # NVIDIA yedegi bugun devreye girdiyse nabza ekle (Anthropic sorun yasadi demek).
    nv = m.get("nvidia") or {}
    nv_txt = f" | NVIDIA yedek: {nv['bugun']} kez" if nv.get("bugun") else ""
    # Rozet: kredi bitti her seyi ezer (karar uretimi tamamen durur).
    if kredi.get("bitti"):
        rozet = "🔴"
    elif m.get("durum", "").startswith("🔴"):
        rozet = "🔴"
    elif m.get("durum", "").startswith("⚠️"):
        rozet = "⚠️"
    else:
        rozet = "✅"
    return (f"{rozet} {m['tarih']}: {karar_n} karar üretildi | kredi: {kredi_txt} | "
            f"KAP: {kap_txt} | AI hata: {m.get('ai_hata', 0)} | motorlar: {motor_txt}"
            f"{nv_txt}")


def topla(tarih=None) -> dict:
    """Gunun tum saglik metriklerini toplar."""
    from src.db import database as db
    from src.ops import gun_kalitesi
    tarih = tarih or _bugun()
    db.init_db()

    bist, us, kill = _karar_sayilari(tarih)
    karar_dagilim = _karar_dagilimi(tarih)
    log_ist = gun_kalitesi._log_gun_istatistik().get(tarih, {})
    atlanan = log_ist.get("hata", 0)
    taranan = log_ist.get("taranan", 0)
    gk = gun_kalitesi.siniflandir(tarih, log_ist={tarih: log_ist} if log_ist else None)

    kap_canli, kap_n = _kap_durum(tarih)
    kap_basari = _kap_basari(tarih)
    cache_yas = _fiyat_cache_yas_dk()
    havuz, haber_eslesme = _haber_istatistik()
    gece = _gece_isleri()
    ai_hata = 0
    try:
        ai_hata = db.ai_hata_sayisi(tarih)
    except Exception:
        pass
    olu = _olu_sembol_sayisi()
    pasif_sembol = _pasif_sembol_sayisi()
    kredi = kredi_durumu(tarih)
    motor = motor_durumu()
    motorlar_ok = _motorlar_ok(motor)
    # ALPHA OLCUM SAGLIGI (15 Tem 2026): benchmark cekimi bos donerse piyasa_farki
    # NULL kalir ve alpha basari oranlari olculemez. 26 Haz-15 Tem arasi bu sessizce
    # oldu (39 AL kararinin 23'u olcusuz) -> artik karnede gorunur ve alarm verir.
    try:
        from src.ops.update_decisions import alpha_olcum_sagligi
        alpha = alpha_olcum_sagligi()   # varsayilan 14g (degerlendirme gecikmesi)
    except Exception:
        alpha = None
    # IS 4: GOLGE haber sinyali denetim + isabet (canliya deger mi izleme).
    try:
        from src.news import haber_sinyal
        golge_denetim = haber_sinyal.denetim_ozeti(tarih)
        golge_isabet = haber_sinyal.isabet_ozeti()
    except Exception:
        golge_denetim, golge_isabet = {}, {}
    # NVIDIA acil-durum yedegi: aktif/pasif + bugun kac kez devreye girdi.
    try:
        from src.ai import saglayici
        nvidia = saglayici.yedek_durum(tarih)
    except Exception:
        nvidia = {"aktif": False, "bugun": 0, "durum": "bilinmiyor", "model": None}
    simdi = datetime.now(_TZ)
    hafta_ici = simdi.weekday() < 5               # brifing yalniz hafta ici (cron 1-5)
    # Gun-basi yanlis alarm onleme: bugun VE brifing saati (09:00) daha gecmediyse,
    # 0/az karar NORMALDIR (brifing henuz kosmadi) -> kirmizi verme, "bekleniyor" de.
    brifing_bekleniyor = (tarih == _bugun() and hafta_ici
                          and simdi.hour < BRIFING_BEKLEME_SAAT
                          and bist < BIST_BEKLENEN * BIST_ALT_ESIK)

    # --- KIRMIZI kosullar ---
    kirmizi = []
    # Kredi bitti -> karar uretimi tamamen durur; en agir kirmizi.
    if kredi["bitti"]:
        kirmizi.append("AI kredisi BITTI (karar uretimi durdu)")
    if not motorlar_ok:
        kopuk = [a for a, v in motor.items() if v.get("bagli") is False]
        kirmizi.append(f"motor kod yolu KOPUK: {', '.join(kopuk)}")
    # Alpha olculemiyorsa basari oranlari anlamsiz -> kirmizi (sessiz kalmasin).
    if alpha and alpha["toplam"] >= 10 and not alpha["saglikli"]:
        kirmizi.append(f"alpha olcumu bozuk: {alpha['bos']}/{alpha['toplam']} "
                       f"kararda piyasa_farki bos (%{alpha['bos_oran']*100:.0f})")
    if taranan and atlanan / taranan > 0.10:
        kirmizi.append(f"watchlist'in %{atlanan/taranan*100:.0f}'i atlandi (>%10)")
    # BIST karar sayisi: yalniz brifing BEKLENEN gunlerde (hafta ici) VE brifing
    # saati gectiyse alarm ver; hafta sonu 0 karar ve brifing-oncesi 0 karar normaldir.
    if hafta_ici and not brifing_bekleniyor and bist < BIST_BEKLENEN * BIST_ALT_ESIK:
        kirmizi.append(f"BIST karari {bist}/{BIST_BEKLENEN} (beklenenin %70 alti)")
    # Gunluk gece isleri (trades, karne) her gece calisir -> bayat/yok ise kirmizi.
    for etiket in ("trades", "karne"):
        if gece.get(etiket) in ("yok", "gec"):
            kirmizi.append(f"gece isi '{etiket}' calismadi/gecikti")
    # yukselis hafizasi yalniz hafta ici calisir; hafta ici bayatsa kirmizi.
    if hafta_ici and gece.get("yukselis_hafizasi") in ("yok", "gec"):
        kirmizi.append("gece isi 'yukselis_hafizasi' calismadi/gecikti")
    if not kap_canli:
        kirmizi.append("KAP kopuk (sahte kaynak)")
    beklenen_cagri = max(1, taranan or (BIST_BEKLENEN + US_BEKLENEN))
    if ai_hata / beklenen_cagri > 0.10:
        kirmizi.append(f"AI hatalari %{ai_hata/beklenen_cagri*100:.0f} (>%10)")

    # --- SARI kosullar ---
    sari = []
    if taranan and 0.05 < atlanan / taranan <= 0.10:
        sari.append(f"watchlist'in %{atlanan/taranan*100:.0f}'i atlandi (>%5)")
    # Cache tazeligi yalniz borsa acikken (hafta ici) anlamli.
    if hafta_ici and cache_yas is not None and cache_yas > 60:
        sari.append(f"fiyat cache {cache_yas:.0f} dk bayat")
    if gk["gun_sinif"] == "KISMI":
        sari.append("gun kalitesi KISMI")
    # KAP basari orani dusukse (yeterli cagri hacmiyle) sari uyari.
    if kap_basari["toplam"] >= 20 and kap_basari["oran"] is not None \
            and kap_basari["oran"] < 70:
        sari.append(f"KAP basari %{kap_basari['oran']:.0f} (<%70)")
    if not hafta_ici:
        sari.append("hafta sonu — brifing beklenmez")

    if gk["gun_sinif"] == "KIRLI" or kirmizi:
        durum = "🔴 SORUNLU"
    elif gk["gun_sinif"] == "KISMI" or sari:
        durum = "⚠️ DİKKAT"
    else:
        durum = "✅ SAĞLIKLI"

    # v2.1 test donemi sayaci (15 kapanis VEYA 10 kesintisiz is gunu)
    try:
        from src.ops import test_donemi as _td
        test_d = _td.durum()
        test_txt = _td.ozet_satir(test_d)
    except Exception:
        test_d, test_txt = None, "hesaplanamadi"

    return {
        "tarih": tarih, "bist": bist, "us": us, "kill": kill, "atlanan": atlanan,
        "karar_dagilim": karar_dagilim,
        "test_donemi": test_d, "test_donemi_txt": test_txt,
        "taranan": taranan, "gun_sinif": gk["gun_sinif"], "gun_sebep": gk["sebep"],
        "kap_canli": kap_canli, "kap_n": kap_n, "kap_basari": kap_basari,
        "cache_yas": cache_yas,
        "havuz": havuz, "haber_eslesme": haber_eslesme, "gece": gece,
        "ai_hata": ai_hata, "olu": olu, "pasif_sembol": pasif_sembol, "kirmizi": kirmizi, "sari": sari, "durum": durum,
        "kredi": kredi, "motor": motor, "motorlar_ok": motorlar_ok,
        "alpha": alpha, "brifing_bekleniyor": brifing_bekleniyor,
        "golge_denetim": golge_denetim, "golge_isabet": golge_isabet,
        "nvidia": nvidia,
    }


def _alpha_txt(a) -> str:
    """Alpha olcum sagligi satiri: kac kararda piyasa_farki dolu / bos."""
    if not a or not a["toplam"]:
        return "veri yok (degerlendirilmis karar yok)"
    return (f"{a['dolu']}/{a['toplam']} dolu, {a['bos']} bos "
            f"(%{a['bos_oran']*100:.0f}) — {'OK' if a['saglikli'] else '🔴 BOZUK'}")


def _golge_satiri(m: dict) -> str:
    """GOLGE haber sinyali karne satiri: gunun eslesmesi + AL isabet karnesi.
    Golge katman canliya alinmaya deger mi sorusunu 1-2 hafta sonra cevaplar."""
    gd = m.get("golge_denetim") or {}
    gi = m.get("golge_isabet") or {}
    ga = gi.get("al") or {}
    if gd.get("havuz") is not None:
        esl = (f"Gölge haber sinyali: {gd['havuz']} havuz -> {gd['konu_esles']} "
               f"konu / {gd['isim_esles']} isim eslesme")
    else:
        esl = "Gölge haber sinyali: bugün tarama yok"
    if ga.get("toplam") and ga.get("isabet_oran") is not None:
        esl += (f" | AL isabet: %{ga['isabet_oran']:.0f} "
                f"({ga['isabet']}/{ga['isabet']+ga['iskalama']})")
    elif ga.get("toplam"):
        esl += f" | AL: {ga['toplam']} sinyal (henüz sonuçlanmadı)"
    return esl


def _golge_v2_satiri() -> str:
    """Is 4: golge_karar_v2 (yeni-katalizor kurali) biriken isabet + canliya
    kalan. CANLI DEGIL — yalniz izleme (bkz. golge_izleme)."""
    try:
        from src.news import golge_izleme
        return golge_izleme.kart_satiri()
    except Exception:
        return "Gölge haber kuralı v2: ölçüm alınamadı"


def _maliyet_satiri(m: dict) -> str:
    """Aksam karnesi icin GERCEK AI maliyeti (loglardan). Anomali varsa uyari."""
    h = (m.get("kredi") or {}).get("harcama") or {}
    if not h:
        return "ölçülemedi"
    s = f"${h['bugun']:.2f} (bu ay ${h['ay']:.2f})"
    if h.get("anomali"):
        s += f" ⚠️ BEKLENMEDİK YÜKSEK — normal ~${h['normal_gunluk']:.2f}/gün"
    return s


def _mesaj(m: dict) -> str:
    ok = lambda b: "✅" if b else "❌"
    cache_txt = (f"taze — {m['cache_yas']:.0f} dk" if (m["cache_yas"] is not None
                 and m["cache_yas"] <= 60) else
                 (f"bayat — {m['cache_yas']:.0f} dk" if m["cache_yas"] is not None else "yok"))
    haber_txt = (f"{m['havuz']} haber, {m['haber_eslesme']} hisse eslesti"
                 if m["havuz"] is not None else "olculemedi")
    kb = m["kap_basari"]
    if kb["toplam"]:
        kap_basari_txt = (f"%{kb['oran']:.0f} ({kb['ok']}/{kb['toplam']})"
                          f" | 429: {kb['r429']} | fallback: {kb['fallback']}")
    else:
        kap_basari_txt = "veri yok (bugun cagri olmadi)"
    g = m["gece"]
    satirlar = [
        # GUNLUK NABIZ: en uste tek satir ozet. Karne her aksam kosulsuz gider;
        # bu satir gelmezse cron/sistem cokmus demektir (kalp atisi).
        nabiz(m),
        "",
        f"📋 SİSTEM KARNESİ — {m['tarih']}",
        f"Karar uretimi: BIST {m['bist']}/{BIST_BEKLENEN} | ABD {m['us']}/{US_BEKLENEN}",
        f"Test donemi: {m.get('test_donemi_txt', '-')}",
        f"Veri hatasiyla atlanan: {m['atlanan']} hisse",
        f"Gun kalitesi: {m['gun_sinif']} ({m['gun_sebep']})",
        "─────",
        "Veri kaynaklari:",
        f"KAP: {'canli' if m['kap_canli'] else 'KOPUK'} — {m['kap_n']} bildirim",
        f"KAP basari orani: {kap_basari_txt}",
        f"Fiyat cache: {cache_txt}",
        f"Haber: {haber_txt}",
        _golge_satiri(m),
        _golge_v2_satiri(),
        "─────",
        f"Gece isleri: trades {ok(g['trades']=='ok')} | karne {ok(g['karne']=='ok')} | "
        f"yukselis hafizasi {ok(g['yukselis_hafizasi']=='ok')}",
        f"AI hatalari: {m['ai_hata']}",
        # GERCEK AI maliyeti (loglardan, tahmin degil). Otomatik yenileme devrede.
        f"Bugünkü AI maliyeti: {_maliyet_satiri(m)}",
        f"Olu sembol (karar/KILL_SWITCH): {m['olu']} | pasif sembol (veri yok, cekimden cikarildi): {m.get('pasif_sembol', 0)}",
        f"Alpha olcumu ({(m.get('alpha') or {}).get('gun', 14)}g): {_alpha_txt(m.get('alpha'))}",
        "─────",
        f"DURUM: {m['durum']}",
    ]
    if m["kirmizi"]:
        satirlar.append("🔴 " + "; ".join(m["kirmizi"]))
    elif m["sari"]:
        satirlar.append("⚠️ " + "; ".join(m["sari"]))
    return "\n".join(satirlar)


def run(gonder: bool = True, verbose: bool = True) -> dict:
    m = topla()
    mesaj = _mesaj(m)
    # CARRY-FORWARD: bugun iletilemeyen KRITIK mesaj (or. PPK 14:30 kayip) varsa
    # aksam karnesinin basina ekle -> kayip sessiz kalmasin. Teslim onaylaninca temizle.
    on_not = ""
    try:
        from src.notify import telegram as _tg
        on_not = _tg.kayip_not_metni()
    except Exception:
        on_not = ""
    mesaj = on_not + mesaj
    if verbose:
        print(mesaj)
    if gonder:
        # Is 2: golge kural v2 otomatik canliya-alma esigi. Durum degistiginde
        # (hazir/dusuk) admin'e AYRI Telegram atar; ayni durumda tekrarlamaz.
        # CANLIYA ALMAZ — yalniz haber verir.
        try:
            from src.news import golge_izleme
            golge_izleme.esik_bildir(verbose=verbose)
        except Exception as e:
            if verbose:
                print(f"[karne] golge v2 esik kontrol hatasi: {type(e).__name__}")
        try:
            from src.notify import telegram
            sonuc = telegram.notify_admins(mesaj, prefix="")
            if on_not and any(s == "ok" for s in sonuc.values()):
                telegram.kayip_temizle()     # carry-forward notu teslim edildi -> temizle
        except Exception as e:
            if verbose:
                print(f"[karne] telegram gonderilemedi: {type(e).__name__}: {str(e)[:80]}")
    return m


if __name__ == "__main__":
    run(gonder=("--print" not in sys.argv))
