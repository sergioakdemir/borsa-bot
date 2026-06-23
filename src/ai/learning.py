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
    return parca


def build_learning_notes(tickers, limit: int = 10) -> dict:
    """{ticker: not} - sadece not uretilebilen hisseler icin (sektor uyarisi dahil)."""
    notes = {}
    zayif = weak_sector_warnings()
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
        if note:
            notes[t] = note
    return notes


# ---------------------------------------------------------------------------
# L3: Sektor bazli ogrenme
# ---------------------------------------------------------------------------
SEKTOR_HISSE = {
    "GARAN": "Bankacılık", "AKBNK": "Bankacılık", "ISCTR": "Bankacılık",
    "YKBNK": "Bankacılık", "HALKB": "Bankacılık", "VAKBN": "Bankacılık",
    "THYAO": "Havacılık", "PGSUS": "Havacılık", "TAVHL": "Havacılık",
    "TUPRS": "Enerji/Rafineri", "PETKM": "Enerji/Rafineri", "AYGAZ": "Enerji/Rafineri",
    "ASELS": "Savunma", "AGHOL": "Savunma",
    "EREGL": "Demir-Çelik", "KRDMD": "Demir-Çelik", "KORDS": "Demir-Çelik",
    "EKGYO": "Gayrimenkul",
    "TOASO": "Otomotiv", "FROTO": "Otomotiv",
    "BIMAS": "Perakende", "MGROS": "Perakende",
    "ULKER": "Gıda", "CCOLA": "İçecek",
    "TCELL": "Telekom", "TTKOM": "Telekom",
    "KCHOL": "Holding", "SAHOL": "Holding", "DOHOL": "Holding", "ENKAI": "Holding",
    "ARCLK": "Dayanıklı Tüketim", "SISE": "Cam",
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


def weak_sector_warnings(esik: int = 50, min_karar: int = 2) -> dict:
    """Zayif sektorler -> {sektor: uyari_metni}. Basari < esik% ve >= min_karar karar."""
    out = {}
    for sek, a in sector_success_rates().items():
        if a["toplam"] >= min_karar and a.get("oran_%") is not None and a["oran_%"] < esik:
            out[sek] = (f"⚠️ {sek} hisselerinde son kararlar zayıf "
                        f"({a['dogru']}/{a['toplam']} doğru, %{a['oran_%']}); "
                        "bu sektörde daha temkinli ol.")
    return out
