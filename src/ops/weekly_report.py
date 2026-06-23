"""Haftalik PERFORMANS raporu (cron: Pazartesi 08:30, sabah brifinginden ONCE).

NOT: src/alerts/weekly_summary.py'den FARKLI gorevdir. Bu dosya PERFORMANS
odaklidir (karar isabeti, portfoy K/Z, ogrenme). weekly_summary ise PIYASA
odaklidir (watchlist fiyat hareketleri + uyari sayisi). Ikisi farkli baslik kullanir.

Gecen haftanin (son 7 gun) ozetini tek Telegram mesaji olarak gonderir:
  - Verilen kararlar: kaç AL / TUT / SAT
  - Bu kararlardan kaçi dogru cikti (decisions.sonuc)
  - En iyi / en kotu karar
  - Model portfoy haftalik (kapanan islemler realize + acik pozisyon)
  - Kullanici bazli portfoy durumu (Serhat + Yigit ayri)
  - BIST-100 ile karsilastirma
Format: sade, Telegram (HTML), <= ~1500 karakter.
"""
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")


def _load_dotenv():
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

from src.notify import telegram
from src.db import database as db


def _bucket(karar: str):
    """Karari AL / TUT / SAT kovasina indirger (yoksa None)."""
    k = (karar or "").upper()
    if "KILL" in k:
        return None
    if "AL" in k:
        return "AL"
    if "SAT" in k or "AZALT" in k or "UZAK" in k:
        return "SAT"
    if "TUT" in k or "BEKLE" in k:
        return "TUT"
    return None


def _deg(sonuc: str):
    """decisions.sonuc ('+3.2% · DOGRU') icinden yuzdeyi cikarir."""
    m = re.search(r"([+-]?\d+(?:\.\d+)?)%", sonuc or "")
    return float(m.group(1)) if m else None


def _fayda(bucket, deg):
    """Kararin getirdigi fayda (en iyi/en kotu siralamasi icin)."""
    if deg is None:
        return None
    if bucket == "AL":
        return deg
    if bucket == "SAT":
        return -deg          # SAT sonrasi dusus = fayda
    return -abs(deg)         # TUT: hareket ettiyse aleyhte


def _decisions_ozet(basla, bit):
    with db.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM decisions WHERE tarih >= ? AND tarih <= ? ORDER BY id",
            (basla, bit))]
    sayim = Counter()
    sonuclu = dogru = 0
    en_iyi = en_kotu = None
    for r in rows:
        b = _bucket(r.get("karar"))
        if not b:
            continue
        sayim[b] += 1
        s = r.get("sonuc")
        if s:
            sonuclu += 1
            if "DOGRU" in s:
                dogru += 1
            f = _fayda(b, _deg(s))
            if f is not None:
                kayit = {"ticker": r.get("ticker"), "bucket": b,
                         "deg": _deg(s), "fayda": f}
                if en_iyi is None or f > en_iyi["fayda"]:
                    en_iyi = kayit
                if en_kotu is None or f < en_kotu["fayda"]:
                    en_kotu = kayit
    return {"sayim": sayim, "sonuclu": sonuclu, "dogru": dogru,
            "en_iyi": en_iyi, "en_kotu": en_kotu, "toplam": len(rows)}


def _model_portfoy_hafta(basla, bit):
    """Gecen hafta kapanan islemlerin realize getirisi + acik pozisyon durumu."""
    try:
        poz = db.list_model_positions()
    except Exception:
        return None
    kapanan = [p for p in poz if (p.get("durum") == "kapali")
               and p.get("kapanis_tarihi")
               and basla <= str(p["kapanis_tarihi"])[:10] <= bit
               and isinstance(p.get("kz_yuzde"), (int, float))]
    acik = [p for p in poz if p.get("durum") == "acik"
            and isinstance(p.get("kz_yuzde"), (int, float))]
    realize = (round(sum(p["kz_yuzde"] for p in kapanan) / len(kapanan), 1)
               if kapanan else None)
    acik_ort = (round(sum(p["kz_yuzde"] for p in acik) / len(acik), 1)
                if acik else None)
    return {"kapanan": len(kapanan), "realize_%": realize,
            "acik": len(acik), "acik_ort_%": acik_ort}


def _kullanici_portfoy():
    """Her kullanici icin toplam portfoy kar/zarar yuzdesi (get_portfolio.ozet)."""
    out = []
    try:
        from src.web.app import get_portfolio
    except Exception:
        return out
    for u in db.list_users():
        try:
            ozet = (get_portfolio(u["ad"]) or {}).get("ozet") or {}
            kz_y = ozet.get("kz_yuzde")
            if kz_y is not None:
                out.append({"ad": u["ad"], "kz_yuzde": round(kz_y, 1)})
        except Exception:
            continue
    return out


def _bist100_hafta():
    try:
        from src.news.market_overview import get_market_overview
        return get_market_overview().get("bist100_haftalik_%")
    except Exception:
        return None


def _ogrenme_ozet(basla, bit):
    """Haftanin degerlendirilmis kararlarindan sektor bazli ogrenme ozeti (L5)."""
    try:
        from src.ai.learning import _sektor_of, _outcome_wrong
    except Exception:
        return None
    with db.get_conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM decisions WHERE tarih >= ? AND tarih <= ? "
            "AND sonuc IS NOT NULL", (basla, bit))]
    sek_agg, tic_yanlis, toplam, dogru = {}, Counter(), 0, 0
    for r in rows:
        w = _outcome_wrong(r.get("sonuc"))
        if w is None:
            continue
        toplam += 1
        if not w:
            dogru += 1
        else:
            tic_yanlis[r.get("ticker")] += 1
        sek = _sektor_of(r.get("ticker"))
        if sek:
            a = sek_agg.setdefault(sek, {"toplam": 0, "dogru": 0})
            a["toplam"] += 1
            if not w:
                a["dogru"] += 1
    zayif = None
    for sek, a in sek_agg.items():
        if a["toplam"] >= 2:
            oran = a["dogru"] / a["toplam"]
            if zayif is None or oran < zayif[1]:
                zayif = (sek, oran, a)
    en_yanlis = tic_yanlis.most_common(1)[0] if tic_yanlis else None
    return {"toplam": toplam, "dogru": dogru, "sektorler": sek_agg,
            "zayif": zayif, "en_yanlis_ticker": en_yanlis}


def build_message(now, basla, bit):
    dec = _decisions_ozet(basla, bit)
    mp = _model_portfoy_hafta(basla, bit)
    kp = _kullanici_portfoy()
    bist = _bist100_hafta()
    og = _ogrenme_ozet(basla, bit)

    L = [f"<b>📅 Haftalık Performans Raporu</b> — {basla} → {bit}",
         "<i>Karar isabeti · portföy · öğrenme</i>", ""]

    s = dec["sayim"]
    L.append(f"📊 <b>Kararlar:</b> {s.get('AL',0)} AL · {s.get('TUT',0)} TUT · {s.get('SAT',0)} SAT")
    if dec["sonuclu"]:
        oran = round(dec["dogru"] / dec["sonuclu"] * 100)
        L.append(f"🎯 <b>İsabet:</b> {dec['dogru']}/{dec['sonuclu']} doğru (%{oran})")
    else:
        L.append("🎯 <i>Sonuçlanan karar yok (henüz erken)</i>")
    if dec["en_iyi"] and dec["en_iyi"]["deg"] is not None:
        e = dec["en_iyi"]
        L.append(f"⭐ <b>En iyi:</b> {e['ticker']} ({e['bucket']}, {e['deg']:+g}%)")
    if dec["en_kotu"] and dec["en_kotu"]["deg"] is not None and dec["en_kotu"] is not dec["en_iyi"]:
        e = dec["en_kotu"]
        L.append(f"⚠️ <b>En kötü:</b> {e['ticker']} ({e['bucket']}, {e['deg']:+g}%)")

    # L5: Ogrenme ozeti (genel + sektor bazli + en cok yanlis)
    if og and og["toplam"]:
        L.append("")
        L.append(f"🧠 <b>Öğrenme:</b> {og['toplam']} karar değerlendirildi, "
                 f"{og['dogru']}/{og['toplam']} doğru "
                 f"(%{round(og['dogru'] / og['toplam'] * 100)})")
        sek_parts = [f"{sek} {a['dogru']}/{a['toplam']}"
                     for sek, a in sorted(og["sektorler"].items(),
                                          key=lambda kv: kv[1]["dogru"] / kv[1]["toplam"])]
        if sek_parts:
            L.append("   <i>Sektör: " + " · ".join(sek_parts[:4]) + "</i>")
        if og["zayif"]:
            sek, _, a = og["zayif"]
            L.append(f"⚠️ <b>Bu hafta dikkat:</b> {sek} hisselerinde "
                     f"{a['toplam'] - a['dogru']}/{a['toplam']} yanlış")
        elif og["en_yanlis_ticker"]:
            t, n = og["en_yanlis_ticker"]
            L.append(f"⚠️ <b>En çok yanlış:</b> {t} ({n} karar)")

    if mp:
        L.append("")
        parts = []
        if mp["realize_%"] is not None:
            parts.append(f"{mp['kapanan']} işlem kapandı, ort %{mp['realize_%']:+g}")
        if mp["acik_ort_%"] is not None:
            parts.append(f"{mp['acik']} açık poz. ort %{mp['acik_ort_%']:+g}")
        if parts:
            L.append("🤖 <b>Model portföy:</b> " + " · ".join(parts))

    if kp:
        L.append("")
        L.append("💼 <b>Portföyler:</b> " + "  ·  ".join(
            f"{p['ad'].capitalize()} %{p['kz_yuzde']:+g}" for p in kp))
    if bist is not None:
        L.append(f"📈 <b>BIST-100 (hafta):</b> %{bist:+g}")

    msg = "\n".join(L)
    if len(msg) > 1500:
        msg = msg[:1480].rsplit("\n", 1)[0] + "\n…"
    return msg


def main() -> int:
    now = datetime.now(_TZ)
    if not telegram.is_configured():
        print(f"[{now:%Y-%m-%d %H:%M}] Telegram yapilandirilmamis - haftalik rapor atlandi.")
        return 0
    db.init_db()
    bugun = now.date()
    basla = (bugun - timedelta(days=7)).isoformat()    # gecen Pzt civari
    bit = (bugun - timedelta(days=1)).isoformat()      # dunku gun (gecen Paz)
    msg = build_message(now, basla, bit)
    sonuc = telegram.broadcast(msg)
    ok = [c for c, st in sonuc.items() if st == "ok"]
    print(f"[{now:%Y-%m-%d %H:%M}] Haftalik rapor: {len(ok)}/{len(sonuc)} aliciya gonderildi.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
