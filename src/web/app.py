"""Borsa-bot mobil web arayuzu (Flask).

Yalnizca yerel db ve data klasorlerinden okur; disariya hic baglanmaz.
Port: 8080.

Sekmeler:
- Ana     : hisse kartlari (AL/TUT/SAT etiketi, fiyat, % degisim)  <- data/ai_commentary.json
- Portfoy : kullanici pozisyonlari, alis fiyati, kar/zarar         <- db.portfoy + guncel fiyat
- Karne   : backtest sonuclari                                      <- data/backtest.json
"""
import json
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, render_template

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
DB_PATH = DATA / "borsa.db"

app = Flask(__name__)


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


def _commentary_by_ticker() -> dict:
    """ticker -> yorum kaydi (son_kapanis/guncel fiyat icin)."""
    out = {}
    for x in _read_json(DATA / "ai_commentary.json", []):
        out[x.get("ticker", "").upper()] = x
    return out


# ----------------------------------------------------------------------------
# veri toplayicilar
# ----------------------------------------------------------------------------
def get_stocks() -> list[dict]:
    rows = []
    for x in _read_json(DATA / "ai_commentary.json", []):
        sig = x.get("kullanilan_on_sinyal", {}) or {}
        etiket, renk = _classify(x.get("final_decision"))
        rows.append({
            "ticker": x.get("ticker"),
            "etiket": etiket,
            "renk": renk,
            "label_full": x.get("final_label", ""),
            "fiyat": sig.get("son_kapanis"),
            "gunluk": sig.get("gunluk_degisim_%"),
            "donem": sig.get("donem_degisim_%"),
            "skor": x.get("score"),
        })
    return rows


def get_portfolio() -> dict:
    fiyatlar = _commentary_by_ticker()
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
        sig = (fiyatlar.get(tkr, {}) or {}).get("kullanilan_on_sinyal", {}) or {}
        guncel = sig.get("son_kapanis")
        maliyet = adet * alis
        toplam_maliyet += maliyet

        kz = kz_yuzde = None
        if guncel is not None:
            deger = adet * guncel
            toplam_deger += deger
            kz = deger - maliyet
            kz_yuzde = (kz / maliyet * 100) if maliyet else None
        else:
            toplam_deger += maliyet  # fiyat yoksa maliyetle say

        pozisyonlar.append({
            "kullanici": kullanici.get(r["kullanici_id"], "-"),
            "ticker": tkr,
            "adet": adet,
            "alis": alis,
            "guncel": guncel,
            "kz": kz,
            "kz_yuzde": kz_yuzde,
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


def get_karne() -> dict:
    return _read_json(DATA / "backtest.json", {"hisseler": [], "ozet": {}, "ayar": {}})


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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
