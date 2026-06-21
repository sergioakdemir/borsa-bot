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
    if len(karar_sonuc) < 2:        # anlamli bir egilim icin en az 2 sonuc
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
    return parca


def build_learning_notes(tickers, limit: int = 10) -> dict:
    """{ticker: not} - sadece not uretilebilen hisseler icin."""
    notes = {}
    for raw in tickers or []:
        t = str(raw).split(":")[0].upper().replace(".IS", "")
        note = build_learning_note(t, limit=limit)
        if note:
            notes[t] = note
    return notes
