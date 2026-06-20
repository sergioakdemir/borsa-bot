"""Analist hedef fiyat / konsensus kaynagi.

Iki siteden scraping yapar (KAP_PROXY_URL fallback ile):
  1. hedeffiyat.com.tr  -> analist (kurum) sayisi, ortalama hedef fiyat, getiri
     potansiyeli ve AL/TUT/SAT dagilimi (birincil kaynak).
  2. borsaveyatirim.com -> analist-bazli hedef fiyat tablosu (konsensus capraz
     kontrol / yedek ortalama).

get_analyst_consensus(ticker) -> {
  "ticker", "analist_sayisi", "ortalama_hedef", "potansiyel",
  "al_sayisi", "tut_sayisi", "sat_sayisi", "konsensus", "kaynak"
}
Veri yoksa available=False ile doner.
"""
import os
import re
import time

# --- onbellekler ---
_URLMAP = {"ts": 0.0, "map": {}}     # hedeffiyat ticker -> /senet/... yolu
_URLMAP_TTL = 24 * 3600
_CACHE = {}                           # ticker -> (ts, sonuc)
_TTL = 1800.0


def _num(s):
    """'454,94' / '1.234,56' -> float."""
    if s is None:
        return None
    s = str(s).strip().replace("₺", "").replace("%", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _proxies():
    url = os.environ.get("KAP_PROXY_URL")
    return {"http": url, "https": url} if url else None


def _fetch(url, timeout=30):
    """Once dogrudan, sonra KAP proxy ile getirir. Basarisizsa None."""
    try:
        from curl_cffi import requests as creq
    except ImportError:
        return None
    for proxies in (None, _proxies()):
        try:
            r = creq.get(url, impersonate="chrome", proxies=proxies, timeout=timeout)
            if r.status_code == 200 and r.text:
                return r.text
        except Exception:
            continue
    return None


def _strip(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or ""))


# ----------------------------------------------------------------------------
# hedeffiyat.com.tr
# ----------------------------------------------------------------------------
def _hedeffiyat_urlmap() -> dict:
    """Ana sayfadaki <option>'lardan ticker -> /senet/... yolu haritasi (cache)."""
    now = time.monotonic()
    if _URLMAP["map"] and (now - _URLMAP["ts"]) < _URLMAP_TTL:
        return _URLMAP["map"]
    html = _fetch("https://hedeffiyat.com.tr/")
    m = {}
    if html:
        for path, tkr in re.findall(
                r'<option value="(/senet/[^"]+)">\s*([A-Z0-9]{3,6})\s*-', html):
            m.setdefault(tkr.upper(), path)
    if m:
        _URLMAP.update(ts=now, map=m)
    return m


def _cat(block, label):
    """Tavsiye blogundan 'Label N' sayisini ceker.

    Onunde harf olmamali (kelime parcasi degil); etiket '.' ile bitebildigi icin
    sonda \\b KULLANILMAZ, dogrudan sayi aranir.
    """
    m = re.search(r"(?<![A-Za-zİıĞğŞşÖöÜüÇç])" + re.escape(label) + r"\s+(\d+)", block)
    return int(m.group(1)) if m else 0


def _hedeffiyat(ticker):
    path = _hedeffiyat_urlmap().get(ticker.upper())
    if not path:
        return {}
    html = _fetch("https://hedeffiyat.com.tr" + path)
    if not html:
        return {}
    t = _strip(html)

    hedef = re.search(r"Ortalama Fiyat Tahmini\s*([\d.,]+)\s*₺", t)
    pot = re.search(r"Ortalama Getiri Potansiyeli\s*%?\s*([\-\d.,]+)", t)
    kurum = re.search(r"Kurum Sayısı\s*(\d+)", t)

    # Tavsiye dagilimi -> AL/TUT/SAT kovalari
    al = _cat(t, "Al") + _cat(t, "Endeks Üstü Get.") + _cat(t, "Güçlü Al") + _cat(t, "Biriktir")
    tut = _cat(t, "Tut") + _cat(t, "End. Paralel Get.") + _cat(t, "Nötr")
    sat = _cat(t, "Sat") + _cat(t, "Endeks Altı Get.") + _cat(t, "Azalt")

    out = {}
    if hedef:
        out["ortalama_hedef"] = round(_num(hedef.group(1)), 2)
    if pot:
        out["potansiyel"] = round(_num(pot.group(1)), 2)
    if kurum:
        out["analist_sayisi"] = int(kurum.group(1))
    if al or tut or sat:
        out.update(al_sayisi=al, tut_sayisi=tut, sat_sayisi=sat)
    return out


# ----------------------------------------------------------------------------
# borsaveyatirim.com (analist hedef tablosu -> konsensus capraz kontrol)
# ----------------------------------------------------------------------------
def _borsaveyatirim(ticker):
    html = _fetch(f"https://www.borsaveyatirim.com/hisse/{ticker.upper()}")
    if not html:
        return {}
    t = _strip(html)
    # satirlar: "<hedef>₺ <potansiyel> YYYY-AA-GG"
    rows = re.findall(r"([\d.,]+)\s*₺\s+([\-\d.,]+)\s+\d{4}-\d{2}-\d{2}", t)
    hedefler = [_num(h) for h, _ in rows if _num(h)]
    if not hedefler:
        return {}
    return {
        "analist_sayisi": len(hedefler),
        "ortalama_hedef": round(sum(hedefler) / len(hedefler), 2),
    }


def _konsensus(al, tut, sat):
    if al >= tut and al >= sat and al > 0:
        return "AL"
    if sat > al and sat >= tut and sat > 0:
        return "SAT"
    if (tut + al + sat) > 0:
        return "TUT"
    return None


def get_analyst_consensus(ticker: str) -> dict:
    """Analist konsensusu (hedeffiyat birincil, borsaveyatirim yedek)."""
    ticker = (ticker or "").upper().replace(".IS", "")
    now = time.monotonic()
    hit = _CACHE.get(ticker)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]

    hf = _hedeffiyat(ticker)
    bvy = _borsaveyatirim(ticker)

    al = hf.get("al_sayisi", 0)
    tut = hf.get("tut_sayisi", 0)
    sat = hf.get("sat_sayisi", 0)
    ortalama = hf.get("ortalama_hedef") or bvy.get("ortalama_hedef")
    analist = hf.get("analist_sayisi") or bvy.get("analist_sayisi")
    kaynaklar = []
    if hf:
        kaynaklar.append("hedeffiyat.com.tr")
    if bvy:
        kaynaklar.append("borsaveyatirim.com")

    out = {
        "ticker": ticker,
        "available": bool(kaynaklar),
        "analist_sayisi": analist,
        "ortalama_hedef": ortalama,
        "potansiyel": hf.get("potansiyel"),
        "al_sayisi": al,
        "tut_sayisi": tut,
        "sat_sayisi": sat,
        "konsensus": _konsensus(al, tut, sat),
        "kaynak": kaynaklar,
    }
    _CACHE[ticker] = (now, out)
    return out


if __name__ == "__main__":
    import json
    import sys
    for tk in (sys.argv[1:] or ["THYAO"]):
        print(json.dumps(get_analyst_consensus(tk), ensure_ascii=False, indent=2))
