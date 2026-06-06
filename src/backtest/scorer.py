"""Backtest icin deterministik skorlayici.

Backtest binlerce tarihsel pencere uretir; her biri icin canli Claude cagirmak
pratik degildir (maliyet/hiz). Bu yuzden ayni on-sinyallerden kural-tabanli bir
1-10 puan uretilir. Sonra ayni esik tablosu + risk veto uygulanir.
"""


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def deterministic_score(ps: dict) -> int:
    if "hata" in ps or ps.get("trend") is None:
        return 5

    score = 5.0
    trend = ps.get("trend")
    if trend == "yukselen":
        score += 1.5
    elif trend == "dusen":
        score -= 1.5

    score += _clip((ps.get("donem_degisim_%") or 0) / 3.0, -1.5, 1.5)
    score += _clip((ps.get("gunluk_degisim_%") or 0) / 2.0, -1.0, 1.0)

    pos = ps.get("fiyat_konumu_%")
    if pos is not None:
        score += _clip((pos - 50) / 25.0, -1.0, 1.0)

    if ps.get("hacim_sinyali") == "yuksek":
        score += 0.5 if trend == "yukselen" else (-0.5 if trend == "dusen" else 0.0)

    return int(max(1, min(10, round(score))))
