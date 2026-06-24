"""Yabanci yatirimci akisi: haftalik net alim/satim + yabanci payi.

Kaynak: Borsa Istanbul "Uluslararasi Yatirimcilar" verisi. Sayfa JS/cografi
korumali oldugundan (KAP/EVDS gibi) TR cikisli KAP_PROXY_URL ile denenir; ulasilamazsa
available=False doner ve cagiran taraf bunu sessizce atlar.

URL ortamdan ezilebilir: FOREIGN_FLOW_URL (sayfa) / FOREIGN_FLOW_JSON_URL (JSON API).

get_foreign_flow() -> {
  available, haftalik_net_alim_tl, yabanci_payi_yuzde, yon ("ALIYOR"/"SATIYOR"/"NOTR"),
  ozet, kaynak
}

KALICI KAYIT: her basarili cekimde piyasa geneli data/yabanci_pay.json'a (tarihe
gore) yazilir; bellekte 6 saat TTL korunur. guncelle_yabanci_pay() (haftalik cron)
ayrica her watchlist hissesinin yabanci payini (borsapy foreignRatio) ekler.
"""
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_CACHE = {}
_TTL = 6 * 3600.0          # haftalik veri; 6 saat onbellek

_TZ = ZoneInfo("Europe/Istanbul")
_PAY_PATH = Path(__file__).resolve().parents[2] / "data" / "yabanci_pay.json"
_MAX_KAYIT = 60            # en fazla son 60 tarihi sakla (~1 yil haftalik)

_DEFAULT_URL = ("https://www.borsaistanbul.com/tr/data/borsa-istanbul-verileri/"
                "uluslararasi-yatirimcilar")

# "NOTR" sayilan esik (mutlak net alim bu degerin altindaysa yon NOTR) - TL
_NOTR_ESIK_TL = 500_000_000      # 0.5 milyar TL


def _proxies():
    url = os.environ.get("KAP_PROXY_URL")
    return {"http": url, "https": url} if url else None


def _fetch(url, timeout=22):
    """Once dogrudan, sonra KAP proxy ile sayfayi getirir. Basarisizsa None."""
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


def _tl_to_float(sayi: str, birim: str | None) -> float | None:
    """'2,5' + 'milyar' -> 2.5e9. TR ondalik (virgul) desteklenir."""
    if sayi is None:
        return None
    s = sayi.strip()
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    b = (birim or "").lower()
    if "milyar" in b:
        v *= 1e9
    elif "milyon" in b:
        v *= 1e6
    elif "bin" in b:
        v *= 1e3
    return v


def _parse_page(html: str) -> dict | None:
    """Sayfa metninden net alim/satim ve yabanci payini ayiklamayi dener."""
    if not html:
        return None
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

    net = None
    yon = None
    # "net alici/satici ... 2,5 milyar TL" veya "net alim/satim 2,5 milyar TL"
    m = re.search(
        r"net\s+(al[ıi]m|al[ıi]c[ıi]|sat[ıi]m|sat[ıi]c[ıi])[^0-9-]{0,40}"
        r"(-?[\d.,]+)\s*(milyar|milyon|bin)?\s*(?:tl|usd|\$|dolar)?",
        t, re.IGNORECASE)
    if m:
        kelime = m.group(1).lower()
        net = _tl_to_float(m.group(2), m.group(3))
        if net is not None:
            if kelime.startswith("sat"):
                net = -abs(net)
                yon = "SATIYOR"
            else:
                net = abs(net)
                yon = "ALIYOR"

    pay = None
    mp = re.search(r"yabanc[ıi][^%0-9]{0,30}(?:pay[ıi]?|oran[ıi]?)[^%0-9]{0,15}"
                   r"%?\s*([\d.,]+)\s*%?", t, re.IGNORECASE)
    if mp:
        try:
            pay = round(float(mp.group(1).replace(".", "").replace(",", ".")), 2)
            if pay > 100:           # yanlis yakalama
                pay = None
        except ValueError:
            pay = None

    if net is None and pay is None:
        return None
    return {"net": net, "yon": yon, "pay": pay}


def _bugun() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


def _yukle_pay() -> dict:
    """data/yabanci_pay.json'i okur (tarih -> kayit). Yoksa/bozuksa {}."""
    try:
        return json.loads(_PAY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _yaz_pay(d: dict) -> None:
    """Kayitlari diske yazar; en yeni _MAX_KAYIT tarihi tutar (eskileri budar)."""
    try:
        if len(d) > _MAX_KAYIT:
            for k in sorted(d)[:-_MAX_KAYIT]:
                d.pop(k, None)
        _PAY_PATH.parent.mkdir(exist_ok=True)
        _PAY_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=1),
                             encoding="utf-8")
    except Exception:
        pass


def _kaydet_piyasa(out: dict) -> None:
    """Basarili get_foreign_flow sonucunu bugunku kayda 'piyasa' olarak yazar."""
    if not out.get("available"):
        return
    d = _yukle_pay()
    gun = _bugun()
    kayit = d.get(gun) or {}
    kayit["piyasa"] = {
        "haftalik_net_alim_tl": out.get("haftalik_net_alim_tl"),
        "yabanci_payi_yuzde": out.get("yabanci_payi_yuzde"),
        "yon": out.get("yon"),
    }
    kayit["guncelleme"] = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")
    d[gun] = kayit
    _yaz_pay(d)


def _hisse_paylari() -> dict:
    """Watchlist'teki her hisse icin borsapy foreignRatio (yabanci pay %). {ticker: oran}.
    borsapy yoksa / hata olursa {} doner."""
    try:
        import borsapy as bp
        from src.watchlist import load_watchlist
    except Exception:
        return {}
    out = {}
    for t in load_watchlist():
        try:
            v = bp.Ticker(t).info.todict().get("foreignRatio")
            if v is not None:
                out[t] = round(float(v), 2)
        except Exception:
            continue
    return out


def guncelle_yabanci_pay() -> dict:
    """HAFTALIK tam guncelleme (cron): piyasa geneli + her watchlist hissesinin
    yabanci payi -> data/yabanci_pay.json (tarihe gore). Ozet dondurur."""
    flow = get_foreign_flow()              # aggregate (icinde _kaydet_piyasa calisir)
    hisseler = _hisse_paylari()
    d = _yukle_pay()
    gun = _bugun()
    kayit = d.get(gun) or {}
    if flow.get("available"):
        kayit["piyasa"] = {
            "haftalik_net_alim_tl": flow.get("haftalik_net_alim_tl"),
            "yabanci_payi_yuzde": flow.get("yabanci_payi_yuzde"),
            "yon": flow.get("yon"),
        }
    if hisseler:
        kayit["hisseler"] = hisseler
    kayit["guncelleme"] = datetime.now(_TZ).strftime("%Y-%m-%d %H:%M")
    d[gun] = kayit
    _yaz_pay(d)
    return {"tarih": gun, "piyasa_available": flow.get("available"),
            "hisse_sayisi": len(hisseler), "dosya": str(_PAY_PATH)}


def get_foreign_flow() -> dict:
    """Haftalik yabanci net alim/satim + yabanci payi + yon."""
    now = time.monotonic()
    hit = _CACHE.get("flow")
    if hit and (now - hit[0]) < _TTL:
        return hit[1]

    out = {"available": False, "kaynak": "Borsa Istanbul"}
    url = os.environ.get("FOREIGN_FLOW_URL", _DEFAULT_URL)
    parsed = _parse_page(_fetch(url))

    if parsed:
        net = parsed.get("net")
        yon = parsed.get("yon")
        if yon is None and net is not None:
            if abs(net) < _NOTR_ESIK_TL:
                yon = "NOTR"
            else:
                yon = "ALIYOR" if net > 0 else "SATIYOR"
        out.update({
            "available": True,
            "haftalik_net_alim_tl": net,
            "yabanci_payi_yuzde": parsed.get("pay"),
            "yon": yon or "NOTR",
            "ozet": _ozet(net, parsed.get("pay"), yon or "NOTR"),
        })
    else:
        out["neden"] = "Borsa Istanbul verisi alinamadi (cografi/JS engeli)."

    if out.get("available"):
        _kaydet_piyasa(out)          # her basarili cekimde dosyaya da yaz
    _CACHE["flow"] = (now, out)
    return out


def _milyar(net_tl) -> str:
    if net_tl is None:
        return "?"
    return f"{net_tl / 1e9:+.1f} milyar TL"


def _ozet(net_tl, pay, yon) -> str:
    ad = {"ALIYOR": "NET ALICI", "SATIYOR": "NET SATICI"}.get(yon, "NÖTR")
    s = f"Yabancı bu hafta {ad}"
    if net_tl is not None:
        s += f" ({_milyar(net_tl)})"
    if pay is not None:
        s += f"; yabancı payı %{pay:g}"
    return s + "."


def briefing_line() -> str | None:
    """Sabah brifingi icin tek satir (veri yoksa None)."""
    f = get_foreign_flow()
    if not f.get("available"):
        return None
    emoji = "🌍"
    ad = {"ALIYOR": "NET ALICI", "SATIYOR": "NET SATICI"}.get(f.get("yon"), "NÖTR")
    net = f.get("haftalik_net_alim_tl")
    parca = f"{emoji} Yabancı: {ad}"
    if net is not None:
        parca += f" ({_milyar(net)})"
    return parca


if __name__ == "__main__":
    # Haftalik cron giris noktasi: piyasa + her hisse yabanci payini json'a yazar.
    ozet = guncelle_yabanci_pay()
    print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] yabanci pay guncellendi: "
          f"piyasa={'var' if ozet['piyasa_available'] else 'yok'}, "
          f"{ozet['hisse_sayisi']} hisse -> {ozet['dosya']}")
    print(json.dumps(get_foreign_flow(), ensure_ascii=False, indent=2))
