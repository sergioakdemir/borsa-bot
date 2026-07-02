"""Karar ogrenimi: gecmis kararlarin sonuclarini ozetleyip AI'a geri besler.

Her hisse icin son N kararin (sonucu doldurulmus olanlarin) dogru/yanlis
dagilimina bakar ve gerekirse tek cumlelik bir "uyari/ton" notu uretir.
Bu not, commentary.py payload'ina 'karar_gecmisi_uyari' olarak eklenir; AI
ayni hatayi tekrarlamamak icin tonunu ayarlar.

Ornek cikti:
  GARAN -> "Son 8 kararında GARAN için 3 kez AL dedin, 2'si yanlış çıktı.
            Bu hissede daha temkinli ol."
"""
from collections import Counter


def _outcome_wrong(sonuc: str) -> bool | None:
    """sonuc metninden DOGRU/YANLIS cikarir. Bos/None ise None (henuz belli degil)."""
    if not sonuc:
        return None
    s = sonuc.upper()
    if "YANLIS" in s or "YANLIŞ" in s:
        return True
    if "DOGRU" in s or "DOĞRU" in s:
        return False
    return None


def build_learning_note(ticker: str, limit: int = 10) -> str | None:
    """Tek hisse icin karar gecmisi notu (yoksa None)."""
    from src.db import database as db
    try:
        rows = db.recent_decisions_for(ticker, limit=limit)
    except Exception:
        return None
    # yalniz sonucu belli olan kararlar (degerlendirilmis)
    karar_sonuc = []
    for r in rows:
        w = _outcome_wrong(r.get("sonuc"))
        if w is None:
            continue
        karar_sonuc.append((str(r.get("karar") or "").upper(), w))
    if len(karar_sonuc) < 1:        # tek degerlendirilmis karar bile yeterli (hizli ogrenme)
        return None

    toplam = len(karar_sonuc)
    yanlis = sum(1 for _, w in karar_sonuc if w)
    if yanlis == 0:
        return None                 # hep dogru -> uyariya gerek yok

    # en cok verilen karar turu ve onun isabeti
    karar_say = Counter(k for k, _ in karar_sonuc)
    en_cok, en_cok_n = karar_say.most_common(1)[0]
    en_cok_yanlis = sum(1 for k, w in karar_sonuc if k == en_cok and w)

    parca = (f"Son {toplam} değerlendirilmiş kararında {ticker} için "
             f"{en_cok_n} kez {en_cok} dedin")
    if en_cok_yanlis:
        parca += f"; bunlardan {en_cok_yanlis} tanesi yanlış çıktı"
    parca += f" (toplam {yanlis}/{toplam} karar yanlıştı)."

    oran = yanlis / toplam
    if oran >= 0.5:
        parca += " Bu hissede geçmiş isabetin düşük; daha temkinli ol ve eminliğini düşür."
    else:
        parca += " Bu hissede geçmiş hatalarını dikkate al."
    # L2: en son yanlis kararin sebebi (Haiku analizi) varsa ekle
    sebep = next((r.get("yanlis_sebep") for r in rows if r.get("yanlis_sebep")), None)
    if sebep:
        parca += f" Son yanlışının sebebi: {sebep}."
    # PIYASAYA KARSI: bu hisse piyasadan (BIST-100) surekli geri kaliyorsa uyar
    farklar = [r.get("piyasa_farki") for r in rows
               if isinstance(r.get("piyasa_farki"), (int, float))]
    if len(farklar) >= 2 and all(f < 0 for f in farklar):
        ort = sum(farklar) / len(farklar)
        parca += (f" Bu hissede son {len(farklar)} kararın hepsinde fiyat BIST-100'ün "
                  f"gerisinde kaldı (ort. {ort:+.1f} puan); piyasadan sürekli geri "
                  "kalıyor, dikkatli ol.")
    return parca


def build_learning_notes(tickers, limit: int = 10) -> dict:
    """{ticker: not} - sadece not uretilebilen hisseler icin (sektor uyarisi +
    adaptif confidence ayari dahil)."""
    notes = {}
    zayif = weak_sector_warnings()
    conf_cache = {}          # sektor -> confidence ayar metni (None=ayar yok)
    for raw in tickers or []:
        t = str(raw).split(":")[0].upper().replace(".IS", "")
        note = build_learning_note(t, limit=limit)
        # L3: hissenin sektoru zayifsa uyariyi nota ekle (yoksa sektor uyarisi tek basina)
        sek = _sektor_of(t)
        sek_uyari = zayif.get(sek) if sek else None
        if note and sek_uyari:
            note = f"{note} {sek_uyari}"
        elif sek_uyari:
            note = sek_uyari
        # Adaptif ogrenme: sektor bazli confidence ayari (son 30 karar) - AI baglamina
        if sek:
            if sek not in conf_cache:
                adj = sector_confidence_adjustment(sek)
                conf_cache[sek] = adj.get("metin") if adj else None
            conf_metin = conf_cache[sek]
            if conf_metin:
                note = f"{note} {conf_metin}" if note else conf_metin
        if note:
            notes[t] = note
    return notes


# ---------------------------------------------------------------------------
# L3: Sektor bazli ogrenme
# ---------------------------------------------------------------------------
SEKTOR_HISSE = {
    # Bankacılık / Finans
    "GARAN": "Bankacılık", "AKBNK": "Bankacılık", "ISCTR": "Bankacılık",
    "YKBNK": "Bankacılık", "HALKB": "Bankacılık", "VAKBN": "Bankacılık",
    "QNBFIN": "Bankacılık", "SKBNK": "Bankacılık", "TSKB": "Bankacılık",
    "GLBMD": "Finans",
    # Havacılık / Yer hizmetleri
    "THYAO": "Havacılık", "PGSUS": "Havacılık", "TAVHL": "Havacılık",
    "CLEBI": "Havacılık",
    # Enerji
    "TUPRS": "Enerji/Rafineri", "PETKM": "Enerji/Rafineri", "AYGAZ": "Enerji/Rafineri",
    "AKSEN": "Enerji", "ZOREN": "Enerji", "ENJSA": "Enerji", "ODAS": "Enerji",
    "EUPWR": "Enerji", "GESAN": "Enerji",
    # Savunma
    "ASELS": "Savunma", "OTKAR": "Savunma",
    # Demir-Çelik / Döküm
    "EREGL": "Demir-Çelik", "KRDMD": "Demir-Çelik", "KORDS": "Demir-Çelik",
    "CEMAS": "Demir-Çelik",
    # Çimento
    "NUHCM": "Çimento",
    # Madencilik
    "KOZAL": "Madencilik", "KOZAA": "Madencilik",
    # Gayrimenkul (GYO)
    "EKGYO": "Gayrimenkul", "TRGYO": "Gayrimenkul", "ISGYO": "Gayrimenkul",
    "ALGYO": "Gayrimenkul", "SNGYO": "Gayrimenkul", "IHLGM": "Gayrimenkul",
    # Otomotiv / Yan sanayi
    "TOASO": "Otomotiv", "FROTO": "Otomotiv", "TTRAK": "Otomotiv",
    "DOAS": "Otomotiv", "BFREN": "Otomotiv", "EGEEN": "Otomotiv",
    # Perakende / Giyim
    "BIMAS": "Perakende", "MGROS": "Perakende", "SOKM": "Perakende",
    "CRFSA": "Perakende", "MAVI": "Perakende", "LCWGK": "Perakende",
    "ADESE": "Perakende", "BMEKS": "Perakende",
    # Gıda / İçecek
    "ULKER": "Gıda", "CCOLA": "İçecek", "AEFES": "İçecek",
    # Telekom
    "TCELL": "Telekom", "TTKOM": "Telekom",
    # Holding
    "KCHOL": "Holding", "SAHOL": "Holding", "DOHOL": "Holding", "ENKAI": "Holding",
    "AGHOL": "Holding", "ALARK": "Holding", "ECZYT": "Holding", "ECILC": "Holding",
    "METRO": "Holding", "BRYAT": "Holding",
    # Sağlık / İlaç
    "MPARK": "Sağlık", "SELEC": "Sağlık", "DEVA": "Sağlık",
    # Sigorta
    "TURSG": "Sigorta", "ANSGR": "Sigorta", "RAYSG": "Sigorta",
    # Teknoloji / Yazılım
    "LOGO": "Teknoloji", "NETAS": "Teknoloji", "KFEIN": "Teknoloji",
    "KAREL": "Teknoloji", "KONTR": "Teknoloji",
    # Kimya / Tarım
    "HEKTS": "Kimya",
    # Dayanıklı Tüketim
    "ARCLK": "Dayanıklı Tüketim", "VESBE": "Dayanıklı Tüketim",
    "VESTEL": "Dayanıklı Tüketim", "SILVR": "Dayanıklı Tüketim",
    # Cam / Sanayi
    "SISE": "Cam", "DNISI": "Sanayi", "UCAK": "Sanayi",
    # BYF / Fon
    "GMSTR": "Değerli Metal",
    # Medya / Turizm
    "HURGZ": "Medya", "NTTUR": "Turizm",
}


def _sektor_of(ticker):
    return SEKTOR_HISSE.get((ticker or "").upper().replace(".IS", ""))


def sector_success_rates(limit: int = 400) -> dict:
    """Degerlendirilmis kararlari sektore gruplar -> {sektor: {toplam, dogru, oran_%}}."""
    from src.db import database as db
    try:
        rows = db.list_decisions(limit=limit)
    except Exception:
        return {}
    agg = {}
    for r in rows:
        w = _outcome_wrong(r.get("sonuc"))
        if w is None:
            continue
        sek = _sektor_of(r.get("ticker"))
        if not sek:
            continue
        a = agg.setdefault(sek, {"toplam": 0, "dogru": 0})
        a["toplam"] += 1
        if not w:
            a["dogru"] += 1
    for a in agg.values():
        a["oran_%"] = round(a["dogru"] / a["toplam"] * 100) if a["toplam"] else None
    return agg


def sector_confidence_adjustment(sektor, limit: int = 30) -> dict | None:
    """Adaptif ogrenme: son `limit` (vars. 30) degerlendirilmis kararin sektor
    bazli basari oranina gore confidence ayari onerir.

      - Basari < %60 : confidence -10 (bu sektorde daha temkinli ol)
      - Basari > %80 : confidence +5  (bu sektorde guclusun)
      - aksi halde   : ayar yok (None)

    En az 3 degerlendirilmis karar gerekir; yetersiz veri / notr durumda None.
    Doner: {sektor, oran_%, toplam, adjustment, metin}.
    """
    from src.db import database as db
    if not sektor:
        return None
    try:
        rows = db.list_decisions(limit=800)     # id DESC: en yeni kararlar once
    except Exception:
        return None
    sonuclar = []
    for r in rows:
        if _sektor_of(r.get("ticker")) != sektor:
            continue
        w = _outcome_wrong(r.get("sonuc"))
        if w is None:
            continue
        sonuclar.append(w)
        if len(sonuclar) >= limit:              # yalniz son `limit` karar
            break
    if len(sonuclar) < 3:
        return None
    toplam = len(sonuclar)
    dogru = sum(1 for w in sonuclar if not w)
    oran = round(dogru / toplam * 100)
    if oran < 60:
        adjustment, ton = -10, "daha temkinli ol"
    elif oran > 80:
        adjustment, ton = 5, "bu sektörde güçlüsün"
    else:
        return None
    metin = (f"{sektor}'da son {toplam} kararda başarı %{oran} — "
             f"confidence {adjustment:+d} ({ton}).")
    return {"sektor": sektor, "oran_%": oran, "toplam": toplam,
            "adjustment": adjustment, "metin": metin}


def weak_sector_warnings(esik: int = 50, min_karar: int = 2) -> dict:
    """Zayif sektorler -> {sektor: uyari_metni}. Basari < esik% ve >= min_karar karar."""
    out = {}
    for sek, a in sector_success_rates().items():
        if a["toplam"] >= min_karar and a.get("oran_%") is not None and a["oran_%"] < esik:
            out[sek] = (f"⚠️ {sek} hisselerinde son kararlar zayıf "
                        f"({a['dogru']}/{a['toplam']} doğru, %{a['oran_%']}); "
                        "bu sektörde daha temkinli ol.")
    return out


# ---------------------------------------------------------------------------
# L4: Kullanici bazli sektor analizi
# Botun kararlari sistem genelidir; bir KULLANICININ "sektor basarisi" =
# o kullanicinin PORTFOYUNDEKI hisseler icin botun degerlendirilmis kararlarinin
# isabeti (kullanicinin maruz kaldigi sektorlerde nasil gittigi).
# ---------------------------------------------------------------------------
def user_sector_success_rates(kullanici_id, limit: int = 800) -> dict:
    """Kullanicinin portfoyundeki hisseler icin kararlari sektore gruplar ->
    {sektor: {toplam, dogru, yanlis, oran_%}} (yalniz sonucu belli kararlar)."""
    from src.db import database as db
    try:
        pf = {(p.get("ticker") or "").upper().replace(".IS", "")
              for p in db.list_portfolio(kullanici_id) if p.get("ticker")}
    except Exception:
        pf = set()
    if not pf:
        return {}
    try:
        rows = db.list_decisions(limit=limit)
    except Exception:
        return {}
    agg = {}
    for r in rows:
        t = (r.get("ticker") or "").upper().replace(".IS", "")
        if t not in pf:
            continue
        w = _outcome_wrong(r.get("sonuc"))
        if w is None:
            continue
        sek = _sektor_of(t)
        if not sek:
            continue
        a = agg.setdefault(sek, {"toplam": 0, "dogru": 0, "yanlis": 0})
        a["toplam"] += 1
        a["yanlis" if w else "dogru"] += 1
    for a in agg.values():
        a["oran_%"] = round(a["dogru"] / a["toplam"] * 100) if a["toplam"] else None
    return agg


def user_weak_sector_warnings(kullanici_id, esik: int = 50,
                              min_karar: int = 3) -> dict:
    """Kullanicinin geçmişte zayıf kaldığı sektörler -> {sektor: uyari_metni}.
    Basari < esik% ve >= min_karar değerlendirilmiş karar olan sektörler."""
    out = {}
    for sek, a in user_sector_success_rates(kullanici_id).items():
        if a["toplam"] >= min_karar and a.get("oran_%") is not None and a["oran_%"] < esik:
            out[sek] = (f"⚠️ {sek} hisselerinde geçmişte zayıf kaldın "
                        f"({a['yanlis']}/{a['toplam']} yanlış, "
                        f"%{100 - a['oran_%']} başarısız) — bu sektörde daha dikkatli ol.")
    return out
