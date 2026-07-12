"""Haber kaynagi ac/kapa yapilandirmasi (config/news_sources.json).

Denetim (12 Tem 2026) bulgulari:
  - RSS DEFAULT feed'leri 92 BIST hissesinden yalniz 2'sine (METRO, AKSA) haber
    getirdi; BIST'in gercek haber kaynagi KAP.
  - Akademik/uzay feed'leri (NASA, arXiv, MIT, NSF, Semantic Scholar...) hisse
    kararina GIRMIYOR; yalniz ABD brifingi "AKADEMIK & KURUM" bolumu (dekoratif).
  - Kripto feed'leri yalniz CNCK/IONQ icin; kullanici kripto ile ilgilenmiyor.
  - Mynet/ParaAnaliz olu (HTTP 404).

Bu modul yalniz haber TOPLAMA/GOSTERIM'i etkiler; karar kurallarina DOKUNMAZ.
Geri acmak icin config/news_sources.json'da ilgili bayragi true yap / feed adini
disabled_feeds'ten cikar (kod degisikligi gerekmez).
"""
import json
from pathlib import Path

_CFG_PATH = Path(__file__).resolve().parents[2] / "config" / "news_sources.json"
_CACHE = None

_VARSAYILAN = {
    "akademik_gundem_enabled": False,
    "kripto_gundem_enabled": False,
    "disabled_feeds": ["Mynet", "ParaAnaliz"],
}


def _yukle() -> dict:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        _CACHE = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        _CACHE = dict(_VARSAYILAN)
    return _CACHE


def akademik_acik() -> bool:
    return bool(_yukle().get("akademik_gundem_enabled", False))


def kripto_acik() -> bool:
    return bool(_yukle().get("kripto_gundem_enabled", False))


def _kapali_feedler() -> set:
    return {str(x) for x in _yukle().get("disabled_feeds", [])}


def feed_acik(ad: str) -> bool:
    """Verilen feed adi (ornek 'Mynet') acik mi?"""
    return ad not in _kapali_feedler()


def aktif_feedler(feeds: list) -> list:
    """Feed listesini disabled_feeds'e gore filtreler (ad alanina bakar)."""
    kapali = _kapali_feedler()
    return [f for f in feeds if (f.get("ad") if isinstance(f, dict) else f) not in kapali]
