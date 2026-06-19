"""Borsa-bot mobil web arayuzu (Flask).

Yalnizca yerel db ve data klasorlerinden okur; disariya hic baglanmaz.
Port: 8080.

Sekmeler:
- Ana     : Portfoyum + Takip Listesi kartlari (AL/TUT/SAT/VETO, fiyat, % degisim)
- Portfoy : kullanici pozisyonlari, alis fiyati, kar/zarar, hedef/stop
- Karne   : gercek karar gecmisi defteri (simdilik backtest.json'dan)

API:
- /api/stocks     -> takip + sinyal kartlari (zengin: yorum, puan detayi, son haber)
- /api/portfolio  -> pozisyonlar + ozet
- /api/karne      -> defter satirlari
- /api/alerts     -> son uyari/sinyal listesi (bildirim paneli)
- /api/summary    -> firsat / uyari sayilari (ust serit)
"""
import json
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
CONFIG = ROOT / "config"
DB_PATH = DATA / "borsa.db"

app = Flask(__name__)


# ----------------------------------------------------------------------------
# BIST sirket adlari (ticker -> tam unvan). Yerel; disariya cikmaz.
# ----------------------------------------------------------------------------
COMPANY_NAMES = {
    "THYAO": "Türk Hava Yolları",
    "GARAN": "Garanti BBVA",
    "ASELS": "Aselsan",
    "KCHOL": "Koç Holding",
    "TUPRS": "Tüpraş",
    "EREGL": "Ereğli Demir Çelik",
    "AKBNK": "Akbank",
    "YKBNK": "Yapı Kredi Bankası",
    "SISE": "Şişecam",
    "TCELL": "Turkcell",
    "BIMAS": "BİM Mağazalar",
    "FROTO": "Ford Otosan",
    "TOASO": "Tofaş",
    "KOZAL": "Koza Altın",
    "EKGYO": "Emlak Konut GYO",
    "PETKM": "Petkim",
    "ARCLK": "Arçelik",
    "SAHOL": "Sabancı Holding",
    "HALKB": "Halkbank",
    "VAKBN": "VakıfBank",
    "ISCTR": "İş Bankası (C)",
    "TAVHL": "TAV Havalimanları",
    "PGSUS": "Pegasus",
    "MGROS": "Migros",
    "ULKER": "Ülker",
    "CCOLA": "Coca-Cola İçecek",
    "DOHOL": "Doğan Holding",
    "ENKAI": "Enka İnşaat",
    "KORDS": "Kordsa",
    "TTKOM": "Türk Telekom",
}


def company_name(ticker: str) -> str:
    t = (ticker or "").upper()
    return COMPANY_NAMES.get(t, t)


def _norm(s: str) -> str:
    """Turkce duyarsiz arama icin normalize (kucuk harf + tr->ascii)."""
    s = s or ""
    for a, b in (("İ", "i"), ("I", "i"), ("Ş", "s"), ("Ğ", "g"),
                 ("Ü", "u"), ("Ö", "o"), ("Ç", "c")):
        s = s.replace(a, b)
    s = s.lower()
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"),
                 ("ü", "u"), ("ö", "o"), ("ç", "c"), ("â", "a")):
        s = s.replace(a, b)
    return s


# ----------------------------------------------------------------------------
# yardimcilar
# ----------------------------------------------------------------------------
def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _classify(decision: str) -> tuple[str, str]:
    """Karar kodunu (AL/TUT/SAT/VETO) sade etiket + renk anahtarina cevirir."""
    d = (decision or "").upper()
    if "VETO" in d:
        return "VETO", "yellow"
    if "SAT" in d:               # SAT, GUCLU_SAT
        return "SAT", "red"
    if "AL" in d:                # AL, AL_TEMKINLI
        return "AL", "green"
    if "TUT" in d:
        return "TUT", "yellow"
    return "TUT", "yellow"


def _eminlik_tr(e: str) -> str:
    return {"DUSUK": "Düşük", "ORTA": "Orta", "YUKSEK": "Yüksek"}.get(
        (e or "").upper(), (e or "—").title())


def _clamp10(x) -> int:
    try:
        return max(0, min(10, int(round(x))))
    except (TypeError, ValueError):
        return 0


def _puan_detay(rec: dict, sig: dict) -> dict:
    """Mevcut sinyallerden 3 alt puan turetir (0-10): sirket sagligi / fiyat / piyasa.

    - sirket_sagligi : AI ana skoru (saglam=yuksek) ve riskin tersi
    - fiyat          : donem araligindaki fiyat konumu (ucuz=dusuk konum daha cazip degil;
                       burada 'fiyat gucu' olarak konumu kullaniyoruz)
    - piyasa         : trend + donem degisimi + hacim teyidi (momentum)
    """
    risk = (rec.get("risk") or {}).get("score")
    skor = rec.get("score")
    saglik = _clamp10(((skor or 0) + (10 - (risk or 0))) / 2)

    konum = sig.get("fiyat_konumu_%")
    fiyat = _clamp10((konum or 0) / 10)

    piyasa = 5
    if sig.get("trend") == "yukselen":
        piyasa += 2
    elif sig.get("trend") == "dusen":
        piyasa -= 2
    donem = sig.get("donem_degisim_%") or 0
    piyasa += 1 if donem > 0 else (-1 if donem < 0 else 0)
    if sig.get("hacim_sinyali") == "yuksek":
        piyasa += 1
    elif sig.get("hacim_sinyali") == "dusuk":
        piyasa -= 1
    piyasa = _clamp10(piyasa)

    return {"sirket_sagligi": saglik, "fiyat": fiyat, "piyasa": piyasa}


def _son_haber(rec: dict) -> dict | None:
    haberler = rec.get("haberler") or []
    if not haberler:
        return None
    h = haberler[0]
    return {
        "baslik": h.get("baslik"),
        "tarih": h.get("tarih"),
        "tazelik": h.get("tazelik"),
        "fiyatlanma": h.get("fiyatlanma"),
    }


def _stock_card(rec: dict) -> dict:
    """ai_commentary kaydini zengin karta cevirir."""
    sig = rec.get("kullanilan_on_sinyal", {}) or {}
    etiket, renk = _classify(rec.get("final_decision"))
    tkr = (rec.get("ticker") or "").upper()
    return {
        "ticker": tkr,
        "isim": company_name(tkr),
        "etiket": etiket,
        "renk": renk,
        "label_full": rec.get("final_label", ""),
        "fiyat": sig.get("son_kapanis"),
        "gunluk": sig.get("gunluk_degisim_%"),
        "donem": sig.get("donem_degisim_%"),
        "skor": rec.get("score"),
        "risk": (rec.get("risk") or {}).get("score"),
        "eminlik": _eminlik_tr(rec.get("eminlik")),
        "trend": sig.get("trend"),
        "fiyat_konumu": sig.get("fiyat_konumu_%"),
        "hacim": sig.get("hacim_sinyali"),
        # detay panel
        "yorum": rec.get("gerekce", ""),
        "gozlemler": rec.get("gozlemler", []),
        "puan_detay": _puan_detay(rec, sig),
        "son_haber": _son_haber(rec),
        "has_data": True,
    }


def _minimal_card(ticker: str) -> dict:
    """Sinyal verisi olmayan takip hissesi icin bos kart iskeleti."""
    t = (ticker or "").upper()
    return {
        "ticker": t, "isim": company_name(t),
        "etiket": None, "renk": "yellow", "label_full": "",
        "fiyat": None, "gunluk": None, "donem": None,
        "skor": None, "risk": None, "eminlik": "—",
        "trend": None, "fiyat_konumu": None, "hacim": None,
        "yorum": "", "gozlemler": [], "puan_detay": {}, "son_haber": None,
        "has_data": False,
    }


def _commentary_by_ticker() -> dict:
    out = {}
    for x in _read_json(DATA / "ai_commentary.json", []):
        out[(x.get("ticker") or "").upper()] = x
    return out


# ----------------------------------------------------------------------------
# veri toplayicilar
# ----------------------------------------------------------------------------
def _owned_tickers() -> set:
    try:
        with sqlite3.connect(DB_PATH) as c:
            return {(r[0] or "").upper()
                    for r in c.execute("SELECT ticker FROM portfoy")}
    except sqlite3.Error:
        return set()


def get_stocks() -> dict:
    """Ana sayfa: portfoyum (sahip olunan) + takip listesi (watchlist)."""
    comm = _commentary_by_ticker()
    owned = _owned_tickers()
    wl = _read_json(CONFIG / "watchlist.json", {})
    watch = [t.upper() for t in (wl.get("bist_endeks", []) + wl.get("kisisel", []))]

    portfoyum = [_stock_card(comm[t]) for t in sorted(owned) if t in comm]

    takip = []
    seen = set(owned)
    for t in watch:
        if t in seen:
            continue
        seen.add(t)
        takip.append(_stock_card(comm[t]) if t in comm else _minimal_card(t))

    return {"portfoyum": portfoyum, "takip": takip}


def get_search(q: str) -> list[dict]:
    """BIST evreninde ticker/sirket adina gore arama (Turkce duyarsiz)."""
    nq = _norm(q).strip()
    if not nq:
        return []
    comm = _commentary_by_ticker()
    out = []
    for t, name in COMPANY_NAMES.items():
        if nq in _norm(t) or nq in _norm(name):
            out.append(_stock_card(comm[t]) if t in comm else _minimal_card(t))
    return out


def get_portfolio() -> dict:
    comm = _commentary_by_ticker()
    pozisyonlar = []
    toplam_maliyet = toplam_deger = 0.0

    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        kullanici = {r["id"]: r["ad"]
                     for r in c.execute("SELECT id, ad FROM kullanici")}
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM portfoy ORDER BY kullanici_id, id")]

    for r in rows:
        tkr = (r["ticker"] or "").upper()
        adet = r["adet"] or 0.0
        alis = r["alim_fiyati"] or 0.0
        rec = comm.get(tkr, {}) or {}
        sig = rec.get("kullanilan_on_sinyal", {}) or {}
        guncel = sig.get("son_kapanis")
        etiket, renk = _classify(rec.get("final_decision"))
        maliyet = adet * alis
        toplam_maliyet += maliyet

        kz = kz_yuzde = None
        if guncel is not None:
            deger = adet * guncel
            toplam_deger += deger
            kz = deger - maliyet
            kz_yuzde = (kz / maliyet * 100) if maliyet else None
        else:
            toplam_deger += maliyet

        pozisyonlar.append({
            "kullanici": kullanici.get(r["kullanici_id"], "-"),
            "ticker": tkr,
            "isim": company_name(tkr),
            "adet": adet,
            "alis": alis,
            "guncel": guncel,
            "gunluk": sig.get("gunluk_degisim_%"),
            "kz": kz,
            "kz_yuzde": kz_yuzde,
            "etiket": etiket,
            "renk": renk,
            "tarih": r.get("alim_tarihi"),
            # hedef/stop kayitli degil -> alis fiyatindan turetilen basit kurallar
            "hedef": round(alis * 1.15, 2) if alis else None,
            "stop": round(alis * 0.92, 2) if alis else None,
        })

    toplam_kz = toplam_deger - toplam_maliyet
    return {
        "pozisyonlar": pozisyonlar,
        "ozet": {
            "maliyet": toplam_maliyet,
            "deger": toplam_deger,
            "kz": toplam_kz,
            "kz_yuzde": (toplam_kz / toplam_maliyet * 100) if toplam_maliyet else None,
        },
    }


_AY_TR = ["", "Oca", "Şub", "Mar", "Nis", "May", "Haz",
          "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]


def _tarih_kisa(iso: str) -> str:
    """2026-06-15 -> '15 Haz'."""
    try:
        y, m, d = (iso or "").split("-")[:3]
        return f"{int(d)} {_AY_TR[int(m)]}"
    except (ValueError, IndexError):
        return iso or ""


def get_karne() -> dict:
    """Defter mantigi: her hisse icin 'karar -> sonuc' satiri.

    Gercek karar logu gelene kadar backtest.json ozetinden turetilir.
    Bir hisse stratejisi al-tut'u gectiyse ✅, gectiyse degilse ❌.
    """
    bt = _read_json(DATA / "backtest.json", {"hisseler": [], "ozet": {}, "ayar": {}})
    ayar = bt.get("ayar", {})
    son = (ayar.get("end") or "")

    satirlar = []
    for h in bt.get("hisseler", []):
        tkr = (h.get("symbol") or "").replace(".IS", "").upper()
        strat = h.get("strateji_getiri_%")
        altut = h.get("al_tut_getiri_%")
        basari = h.get("basari_orani_%")
        kazandi = (strat is not None and altut is not None and strat >= altut)
        kd = h.get("karar_dagilimi", {}) or {}
        # en cok verilen yonlu karar
        baskin = max(((k, v) for k, v in kd.items() if k != "VETO" and k != "TUT"),
                     key=lambda kv: kv[1], default=("AL", 0))[0]
        etiket, renk = _classify(baskin)
        satirlar.append({
            "ticker": tkr,
            "isim": company_name(tkr),
            "tarih": _tarih_kisa(son),
            "etiket": etiket,
            "renk": renk,
            "kazandi": kazandi,
            "getiri": strat,
            "altut": altut,
            "basari": basari,
            "sebep": (f"strateji {strat:+.1f}% vs al-tut {altut:+.1f}%"
                      if strat is not None and altut is not None else ""),
        })

    return {"satirlar": satirlar, "ozet": bt.get("ozet", {}), "ayar": ayar}


def get_alerts() -> list[dict]:
    """Bildirim paneli: son uyari/sinyaller (en fazla 10).

    Oncelik db.uyari_kayit; bos ise ai_commentary sinyallerinden turetir.
    """
    out = []
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.row_factory = sqlite3.Row
            for r in c.execute(
                    "SELECT * FROM uyari_kayit ORDER BY id DESC LIMIT 10"):
                tkr = (r["ticker"] or "").upper()
                out.append({
                    "ticker": tkr,
                    "isim": company_name(tkr),
                    "tip": "uyari",
                    "mesaj": f"{r['seviye']} hareket: %{r['degisim']:+.2f}",
                    "tarih": r["ts"] or r["tarih"],
                })
    except sqlite3.Error:
        pass

    if out:
        return out

    # turetilmis: guclu sinyaller + fiyatlanmamis haberler
    for rec in _commentary_by_ticker().values():
        tkr = (rec.get("ticker") or "").upper()
        etiket, _ = _classify(rec.get("final_decision"))
        sig = rec.get("kullanilan_on_sinyal", {}) or {}
        if etiket == "AL":
            out.append({"ticker": tkr, "isim": company_name(tkr), "tip": "firsat",
                        "mesaj": f"{rec.get('final_label', 'AL')} sinyali · skor {rec.get('score')}/10",
                        "tarih": None})
        elif etiket in ("SAT", "VETO"):
            out.append({"ticker": tkr, "isim": company_name(tkr), "tip": "uyari",
                        "mesaj": f"{rec.get('final_label', etiket)} · dikkat",
                        "tarih": None})
        for hb in (rec.get("haberler") or []):
            if hb.get("fiyatlanma") == "FIYATLANMADI":
                out.append({"ticker": tkr, "isim": company_name(tkr), "tip": "haber",
                            "mesaj": f"Fiyatlanmamış haber: {hb.get('baslik')}",
                            "tarih": hb.get("tarih")})
    return out[:10]


def get_summary() -> dict:
    """Ust serit: firsat (AL) ve uyari (SAT/VETO/fiyatlanmamis haber) sayilari."""
    firsat = uyari = 0
    for rec in _commentary_by_ticker().values():
        etiket, _ = _classify(rec.get("final_decision"))
        if etiket == "AL":
            firsat += 1
        elif etiket in ("SAT", "VETO"):
            uyari += 1
        if any(h.get("fiyatlanma") == "FIYATLANMADI"
               for h in (rec.get("haberler") or [])):
            uyari += 1
    return {"firsat": firsat, "uyari": uyari, "bildirim": len(get_alerts())}


# ----------------------------------------------------------------------------
# rotalar
# ----------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/stocks")
def api_stocks():
    return jsonify(get_stocks())


@app.route("/api/portfolio")
def api_portfolio():
    return jsonify(get_portfolio())


@app.route("/api/karne")
def api_karne():
    return jsonify(get_karne())


@app.route("/api/alerts")
def api_alerts():
    return jsonify(get_alerts())


@app.route("/api/summary")
def api_summary():
    return jsonify(get_summary())


@app.route("/api/search")
def api_search():
    return jsonify(get_search(request.args.get("q", "")))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
