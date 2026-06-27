"""Şartlı senaryo takibi.

Sabah brifinginde bot "X olursa Y hisse etkilenir" şeklinde 2-3 şartlı senaryo
üretir (ucuz Haiku ile) ve data/senaryo_takip.json'a baz makro anlık görüntüyle
birlikte yazar. Gün içi taramada (run_alerts) bu senaryolar kontrol edilir:
  - tip=makro: usdtry/bist100 baz değere göre eşiği geçti mi (sayısal),
  - tip=haber: taze KAP başlıklarında anahtar kelimeler geçti mi.
Gerçekleşen senaryo "⚡ Beklediğimiz gelişme geldi: ..." olarak bildirilir ve
durumu 'gerceklesti' yapılır (tekrar bildirilmez).

EMOJİ: sadece izinli set (🟢🟡🔴⚡📰).
"""
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
TAKIP_PATH = ROOT / "data" / "senaryo_takip.json"
HABER_MODEL = "claude-haiku-4-5"
_TZ = ZoneInfo("Europe/Istanbul")

# Anlam (Haiku) kontrolu icin gunluk butce: ~$0.001/kontrol -> 40 kontrol ~ $0.04/gun
# (yogun haber gunlerinde senaryo kontrolleri erken durmasin diye 25'ten 40'a cikarildi)
_MAX_HAIKU_GUNLUK = 40

# Haber basliginin senaryoyu ANLAMCA tetikleyip tetiklemedigini soran sema
_TETIK_SCHEMA = {
    "type": "object",
    "properties": {
        "tetikleniyor": {"type": "boolean",
                         "description": "Basliklardan biri bu senaryoyu tetikliyor mu"},
    },
    "required": ["tetikleniyor"],
    "additionalProperties": False,
}

_SENARYO_SCHEMA = {
    "type": "object",
    "properties": {
        "senaryolar": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metin": {"type": "string",
                              "description": "Kısa şartlı cümle: 'X olursa Y için Z' "
                                             "(en fazla ~15 kelime, teknik oran yok)"},
                    "tip": {"type": "string", "enum": ["makro", "haber"]},
                    "gosterge": {"type": "string",
                                 "enum": ["usdtry", "bist100", "faiz", "petrol", "haber"]},
                    "yon": {"type": "string", "enum": ["yukari", "asagi"]},
                    "esik_yuzde": {"type": "number",
                                   "description": "makro tetik eşiği (%); haber ise 0"},
                    "anahtar_kelimeler": {"type": "array", "items": {"type": "string"},
                                          "description": "haber tipi için başlıkta aranacak "
                                                         "kelimeler; makro ise boş"},
                    "hisse": {"type": "string"},
                    "beklenen_karar": {"type": "string",
                                       "enum": ["AL", "TUT", "BEKLE", "AZALT", "UZAK DUR"]},
                },
                "required": ["metin", "tip", "gosterge", "yon", "esik_yuzde",
                             "anahtar_kelimeler", "hisse", "beklenen_karar"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["senaryolar"],
    "additionalProperties": False,
}


def _baseline(macro, overview) -> dict:
    """Gün içi karşılaştırma için baz makro anlık görüntü."""
    macro = macro or {}
    overview = overview or {}
    return {
        "usdtry": macro.get("usdtry"),
        "bist100_gunluk_%": overview.get("bist100_gunluk_%"),
        "faiz": macro.get("politika_faizi") or macro.get("tr_10y_faiz"),
    }


def uret(notable_tickers, gundem, macro, overview, client=None) -> list:
    """2-3 şartlı senaryo üretir (Haiku). Anahtar yok/hata -> []."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return []
    try:
        import anthropic
        client = client or anthropic.Anthropic()
        macro = macro or {}
        overview = overview or {}
        durum = {
            "usdtry": macro.get("usdtry"),
            "politika_faizi": macro.get("politika_faizi"),
            "bist100_gunluk_%": overview.get("bist100_gunluk_%"),
            "one_cikan_hisseler": list(notable_tickers)[:12],
            "gundem_basliklari": list(gundem or [])[:6],
        }
        sistem = (
            "Sen 25 yıllık tecrübeli bir Türk borsa uzmanısın. Bugün İZLENMESİ gereken "
            "2-3 ŞARTLI senaryo üret: 'X olursa Y hisse(ler) için Z'. Senaryolar gün "
            "içinde GERÇEKLEŞİP GERÇEKLEŞMEDİĞİ kontrol edilebilir olmalı. İki tip: "
            "'makro' (usdtry/bist100/faiz/petrol bir eşiği geçerse) veya 'haber' "
            "(belirli bir gelişme haberi çıkarsa — anahtar_kelimeler ver). beklenen_karar "
            "SADECE şu 5'ten biri: AL/TUT/BEKLE/AZALT/UZAK DUR. metin kısa ve sade, teknik "
            "oran/sayı dökme. Sadece verilen güne ait gerçekçi senaryolar; veri uydurma.")
        resp = client.messages.create(
            model=HABER_MODEL, max_tokens=600, system=sistem,
            messages=[{"role": "user", "content": json.dumps(durum, ensure_ascii=False)}],
            output_config={"format": {"type": "json_schema", "schema": _SENARYO_SCHEMA}})
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text).get("senaryolar", [])
    except Exception:
        return []


def kaydet(senaryolar, tarih, macro=None, overview=None) -> None:
    """Senaryoları baz makro anlık görüntüyle birlikte json'a yazar (durum=bekliyor)."""
    try:
        kayit = {
            "tarih": tarih,
            "baseline": _baseline(macro, overview),
            "senaryolar": [{**s, "durum": "bekliyor"} for s in (senaryolar or [])],
        }
        TAKIP_PATH.parent.mkdir(exist_ok=True)
        TAKIP_PATH.write_text(json.dumps(kayit, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


def yukle() -> dict:
    try:
        return json.loads(TAKIP_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _makro_tetik(s, baseline, guncel_usdtry, guncel_bist_gunluk) -> bool:
    """makro senaryosu eşiği geçti mi? (usdtry baza göre, bist100 günlük mutlak)."""
    gos = s.get("gosterge")
    yon = s.get("yon")
    esik = abs(float(s.get("esik_yuzde") or 0)) or 1.0
    if gos == "usdtry" and guncel_usdtry and baseline.get("usdtry"):
        deg = (guncel_usdtry - baseline["usdtry"]) / baseline["usdtry"] * 100
        return deg >= esik if yon == "yukari" else deg <= -esik
    if gos == "bist100" and guncel_bist_gunluk is not None:
        return (guncel_bist_gunluk >= esik if yon == "yukari"
                else guncel_bist_gunluk <= -esik)
    return False        # faiz/petrol: gün içi sayısal kaynak yok -> haber kelimesiyle yakalanır


def _haber_tetik(s, basliklar) -> bool:
    """haber senaryosu: anahtar kelimelerden biri taze başlıklarda geçiyor mu?"""
    kelimeler = [k.lower() for k in (s.get("anahtar_kelimeler") or []) if k]
    if not kelimeler:
        return False
    metin = " ".join(basliklar).lower()
    return any(k in metin for k in kelimeler)


def _haber_tetik_semantik(s, basliklar, client=None) -> bool:
    """ANLAM kontrolu (Haiku): kelime eşleşmese de başlıklar senaryoyu tetikliyor mu?
    Örn. senaryo 'ABD-İran anlaşırsa THYAO rahatlar', başlık 'Washington ve Tahran
    müzakere masasına oturdu' -> evet. Anahtar yok/hata -> False (sessiz)."""
    if not basliklar or not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    senaryo_metin = (s.get("metin") or "").strip()
    if not senaryo_metin:
        return False
    try:
        import anthropic
        client = client or anthropic.Anthropic()
        haber_txt = "\n".join(f"- {b}" for b in list(basliklar)[:12] if b)
        sistem = (
            "Bir borsa senaryosunun bugünkü haber başlıklarıyla GERÇEKLEŞİP "
            "gerçekleşmediğini değerlendiriyorsun. Kelimeler farklı olsa bile AYNI "
            "gelişme anlatılıyorsa (eş anlam, dolaylı ifade, şehir/lider adı vb.) "
            "tetikleniyor=true de. Yalnızca açık bir anlam eşleşmesi varsa true; "
            "emin değilsen false. Sadece şemaya uygun JSON dön.")
        icerik = (f"Senaryo: {senaryo_metin}\n\nBugünkü başlıklar:\n{haber_txt}\n\n"
                  "Bu başlıklardan biri bu senaryoyu tetikliyor mu?")
        resp = client.messages.create(
            model=HABER_MODEL, max_tokens=60, system=sistem,
            messages=[{"role": "user", "content": icerik}],
            output_config={"format": {"type": "json_schema", "schema": _TETIK_SCHEMA}})
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return bool(json.loads(text).get("tetikleniyor"))
    except Exception:
        return False


def kontrol_et(basliklar=None, guncel_usdtry=None, guncel_bist_gunluk=None) -> list:
    """Bekleyen senaryoları kontrol eder; gerçekleşenleri döndürür ve durumu günceller.

    Döner: tetiklenen senaryo dict listesi (her birine 'bildirim' metni eklenmiş)."""
    kayit = yukle()
    senaryolar = kayit.get("senaryolar") or []
    if not senaryolar:
        return []
    baseline = kayit.get("baseline") or {}
    basliklar = basliklar or []
    tetiklenen = []
    degisti = False

    # Gunluk Haiku (anlam) kontrol butcesi: tarih degisince sifirlanir
    bugun = datetime.now(_TZ).date().isoformat()
    butce = kayit.get("haiku_gunluk") or {}
    if butce.get("tarih") != bugun:
        butce = {"tarih": bugun, "sayi": 0}
    _client = [None]   # lazy paylasilan anthropic client (tek olusturulur)

    def _anlamca_tetikler(s):
        """Kelime eşleşmedi; bütçe varsa Haiku ile anlam kontrolü yap."""
        if not basliklar or butce["sayi"] >= _MAX_HAIKU_GUNLUK:
            return False
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        if _client[0] is None:
            try:
                import anthropic
                _client[0] = anthropic.Anthropic()
            except Exception:
                return False
        butce["sayi"] += 1
        return _haber_tetik_semantik(s, basliklar, client=_client[0])

    for s in senaryolar:
        if s.get("durum") != "bekliyor":
            continue
        if s.get("tip") == "haber":
            # Once ucuz kelime eslesmesi; tutmazsa Haiku anlam kontrolu (butce dahilinde)
            vurdu = _haber_tetik(s, basliklar) or _anlamca_tetikler(s)
        else:
            vurdu = (_makro_tetik(s, baseline, guncel_usdtry, guncel_bist_gunluk)
                     or _haber_tetik(s, basliklar))
        if vurdu:
            s["durum"] = "gerceklesti"
            hisse = s.get("hisse") or ""
            karar = s.get("beklenen_karar") or "BEKLE"
            s["bildirim"] = (f"⚡ <b>Beklediğimiz gelişme geldi:</b> {s.get('metin')}. "
                             f"<b>{hisse}</b> için <b>{karar}</b>.")
            tetiklenen.append(s)
            degisti = True
    # Butce sayaci degistiyse de (Haiku cagrildi) kaydet -> gunluk limit korunur
    butce_degisti = kayit.get("haiku_gunluk") != butce
    kayit["haiku_gunluk"] = butce
    if degisti or butce_degisti:
        try:
            TAKIP_PATH.write_text(json.dumps(kayit, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except Exception:
            pass
    return tetiklenen
