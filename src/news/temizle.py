"""Adversarial haber temizleme katmani — İŞ 1 (19 Tem 2026).

Akademik tehdit (IEEE SaTML 2026): kotu niyetli biri KAP/RSS haber metnine
GORUNMEZ karakter, HOMOGLYPH (Kiril/Yunan lookalike) veya GIZLI HTML gomerek
haber-etki AI'ini kandirabilir (homoglyph %42-58, gizli-metin %92 basari;
yillik getiriyi 17.7 puana kadar dusurebilir). Haber katmani ham metni AI'a
verdigi icin bu katman, haber AI'a girmeden ONCE ham metni temizler ve
temizlenemeyen/supheli haberi KARANTINAYA alir (golge sinyale girmez).

Uygulanan savunmalar:
  1. Unicode NFKC normalizasyonu (fullwidth/ligature/uyumluluk katlama).
  2. Gorunmez karakter temizleme: zero-width, BOM, bidi override/isolate,
     variation selector, tag char, diger Cf/Cc kontrol karakterleri.
  3. Homoglyph / karisik-script tespiti: bir KELIMEDE hem Latin hem
     Kiril/Yunan varsa -> supheli (guvenli cozulemez -> karantina).
  4. Gizli HTML temizleme: display:none / visibility:hidden / opacity:0 /
     font-size:0 / hidden / aria-hidden elementlerini icerikleriyle atar.

BAGIMSIZ MODUL: golge katmani (haber_sinyal) IZOLE kalsin diye buradan
haber_sinyal/commentary/morning/run_alerts IMPORT EDILMEZ. rss_source ve
haber_sinyal buradan import eder (tersine bagimlilik yok). Yalniz std-lib
(re, unicodedata, datetime, pathlib) kullanir.
"""
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Istanbul")
_ROOT = Path(__file__).resolve().parents[2]
_KARANTINA_LOG = _ROOT / "logs" / "haber_karantina.log"

# ── Gorunmez / kontrol / bidi karakterleri ─────────────────────────────────
# zero-width space/non-joiner/joiner/word-joiner, BOM, mongolian vowel sep,
# LRM/RLM (yon isaretleri)
_ZERO_WIDTH = "​‌‍⁠﻿᠎‎‏"
# bidi embedding/override/isolate (metin yonunu gizlice ceviren saldiri) + ALM
_BIDI = "‪‫‬‭‮⁦⁧⁨⁩؜"
_TEHLIKELI = _ZERO_WIDTH + _BIDI


def _gorunmez_var(s: str) -> bool:
    if any(ch in s for ch in _TEHLIKELI):
        return True
    for ch in s:
        o = ord(ch)
        if 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF or 0xE0000 <= o <= 0xE007F:
            return True
    return False


def _gorunmez_temizle(s: str) -> str:
    for ch in _TEHLIKELI:
        s = s.replace(ch, "")

    def _tut(ch: str) -> bool:
        o = ord(ch)
        if 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF or 0xE0000 <= o <= 0xE007F:
            return False               # variation selector / tag char
        if ch in "\n\t ":
            return True                # normal bosluk/satir sonu kalsin
        return unicodedata.category(ch) not in ("Cf", "Cc", "Co", "Cn")

    return "".join(ch for ch in s if _tut(ch))


# ── Homoglyph / karisik-script tespiti ─────────────────────────────────────
# Sik kullanilan Kiril/Yunan homoglyphlarin Latin karsiligi (tespit + gosterim).
_HOMOGLYPH = {
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X", "а": "a", "е": "e",
    "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "к": "k", "м": "m",
    "т": "t", "в": "b", "н": "h", "і": "i", "ѕ": "s", "ј": "j",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
    "ο": "o", "α": "a", "ρ": "p",
}


def _script(ch: str) -> str | None:
    o = ord(ch)
    if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
        return "latin"
    if 0x00C0 <= o <= 0x024F:          # Latin-1 suppl + Latin extended (TR: ş,ğ,ı,İ,â)
        return "latin"
    if 0x0400 <= o <= 0x04FF:
        return "cyrillic"
    if 0x0370 <= o <= 0x03FF:
        return "greek"
    return None


_KELIME = re.compile(r"\w+", re.UNICODE)


def _karisik_script_kelimeler(s: str) -> list[str]:
    """Ayni kelimede Latin + Kiril/Yunan karisiyorsa DONER. Homoglyph
    saldirisinin imzasi: 'Аkbank' (Kiril А + Latin 'kbank'). Tamamen Kiril
    yazilmis mesru bir kelime (or. Rusca isim) tetiklemez — yalniz KARISIM."""
    supheli = []
    for m in _KELIME.finditer(s):
        w = m.group(0)
        scripts = {sc for sc in (_script(c) for c in w) if sc}
        if "latin" in scripts and ({"cyrillic", "greek"} & scripts):
            supheli.append(w)
    return supheli


def _homoglyph_katla(s: str) -> str:
    """Kiril/Yunan homoglyphlari Latin karsiligina cevirir (log/kiyas icin)."""
    return "".join(_HOMOGLYPH.get(ch, ch) for ch in s)


# ── Gizli HTML temizleme ───────────────────────────────────────────────────
_GIZLI_STIL = (r"(?:display\s*:\s*none|visibility\s*:\s*hidden"
               r"|opacity\s*:\s*0(?![.\d])|font-size\s*:\s*0(?![.\d]))")
_RE_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?</\1\s*>", re.I | re.S)
_RE_STIL_GIZLI = re.compile(
    r'<([a-zA-Z][\w:-]*)\b[^>]*style\s*=\s*["\'][^"\']*' + _GIZLI_STIL
    + r'[^"\']*["\'][^>]*>.*?</\1\s*>', re.I | re.S)
_RE_ATTR_GIZLI = re.compile(
    r'<([a-zA-Z][\w:-]*)\b[^>]*?(?:\shidden(?=[\s/>])|aria-hidden\s*=\s*["\']?true)'
    r'[^>]*>.*?</\1\s*>', re.I | re.S)
_RE_TAG = re.compile(r"<[^>]+>")


def _gizli_html_temizle(html: str) -> tuple[str, bool]:
    """display:none/visibility:hidden/opacity:0/hidden/aria-hidden elementlerini
    (ic metinleriyle) atar. Doner: (temiz_html, gizli_bulundu_mu)."""
    bulundu = False
    s = html
    for rx in (_RE_SCRIPT_STYLE, _RE_STIL_GIZLI, _RE_ATTR_GIZLI):
        s2 = rx.sub("", s)
        if s2 != s:
            bulundu = True
        s = s2
    return s, bulundu


# ── Kamuya acik API ────────────────────────────────────────────────────────
def temizle_metin(s: str) -> tuple[str, set]:
    """Tek (HTML'siz) metni temizler. Doner: (temiz_metin, nedenler:set)."""
    nedenler: set[str] = set()
    s = s or ""
    nfkc = unicodedata.normalize("NFKC", s)
    if nfkc != s:
        nedenler.add("nfkc-degisti")
    s = nfkc
    if _gorunmez_var(s):
        nedenler.add("gorunmez-karakter")
    s = _gorunmez_temizle(s)
    karisik = _karisik_script_kelimeler(s)
    if karisik:
        nedenler.add("karisik-script:" + ",".join(karisik[:3]))
    return s.strip(), nedenler


def haber_temizle(baslik: str, ozet: str, kaynak: str | None = None) -> dict:
    """Bir haberin baslik+ozetini temizler. `ozet` HTML olabilir.

    Doner dict:
      baslik, ozet            -> temizlenmis metinler
      supheli   (bool)        -> herhangi bir mudahale/anomali bulundu mu
      karantina (bool)        -> guvenli cozulemez (homoglyph) -> golge sinyale GIRMEMELI
      nedenler  (list[str])   -> tespit edilen anomaliler
    """
    nedenler: set[str] = set()
    ozet_html_temiz, gizli = _gizli_html_temizle(ozet or "")
    if gizli:
        nedenler.add("gizli-html")
    ozet_txt = _RE_TAG.sub("", ozet_html_temiz)

    b_temiz, b_ned = temizle_metin(baslik or "")
    o_temiz, o_ned = temizle_metin(ozet_txt)
    nedenler |= b_ned | o_ned

    # Homoglyph / karisik-script guvenli cozulemez (hangi kelime kastedildi
    # bilinemez) -> KARANTINA. Gorunmez karakter + gizli HTML temizlenebildigi
    # icin tek baslarina karantina sebebi degil (temizlenip gecer, ama loglanir).
    karantina = any(n.startswith("karisik-script") for n in nedenler)
    return {
        "baslik": b_temiz,
        "ozet": o_temiz,
        "supheli": bool(nedenler),
        "karantina": karantina,
        "nedenler": sorted(nedenler),
    }


def karantina_logla(kaynak: str | None, nedenler, baslik: str = "") -> None:
    """Supheli/karantina haberini admin loguna yazar (stdout + logs dosyasi).
    Golge katman disinda; hicbir karari degistirmez — yalniz iz birakir."""
    nd = ", ".join(nedenler) if not isinstance(nedenler, str) else nedenler
    src = kaynak or "?"
    msg = f"şüpheli haber: {src} — {nd} | {(baslik or '')[:100]}"
    print(f"[haber_karantina] {msg}")
    try:
        _KARANTINA_LOG.parent.mkdir(exist_ok=True)
        ts = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
        with _KARANTINA_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{ts}\t{msg}\n")
    except Exception:
        pass
