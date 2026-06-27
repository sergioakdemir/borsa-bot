"""Buyuk BIST hisseleri icin bilanco (finansal sonuc) aciklama tarihleri.

Kaynak zinciri (hisse basina): borsapy Ticker.calendar -> yfinance .calendar ->
KAP finansal takvim (best-effort scraping). Sonuc data/bilanco_takvimi.json'a yazilir:
  [{"ticker": "THYAO", "tarih": "2026-08-04", "donem": "Q2 2026"}, ...]

Haftalik cron (Pazartesi 08:00) ile guncellenir. commentary.py karar uretirken
"bilanco X gun sonra" baglamini, sabah brifingi "Bu hafta bilanco" bolumunu buradan alir.

Calistirma: python -m src.news.bilanco_takvimi
"""
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_TZ = ZoneInfo("Europe/Istanbul")
DATA = ROOT / "data"
TAKVIM_PATH = DATA / "bilanco_takvimi.json"

# Bilanco tarihi takip edilen buyuk hisseler.
HISSELER = ["THYAO", "GARAN", "ASELS", "TUPRS", "AKBNK",
            "EREGL", "BIMAS", "SISE", "KRDMD", "TCELL"]


def _load_dotenv():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _donem(d: date) -> str:
    """Aciklama ayindan rapor donemini tahmin eder (BIST takvimi): Q1~Nis-May,
    Q2(H1)~Tem-Agu, Q3~Eki-Kas, Q4/yil-sonu~Sub-Mar."""
    y, m = d.year, d.month
    if m in (2, 3, 4):
        return f"Q4 {y - 1}"
    if m in (5,):
        return f"Q1 {y}"
    if m in (6, 7, 8):
        return f"Q2 {y}"
    if m in (9, 10, 11):
        return f"Q3 {y}"
    return f"Q1 {y}" if m == 1 else f"Q4 {y}"


def _ilk_tarih(deger):
    """yfinance/borsapy 'Earnings Date' degerinden (date / list / Timestamp) ilk
    gecerli date'i cikarir."""
    if deger is None:
        return None
    if isinstance(deger, (list, tuple)):
        for x in deger:
            r = _ilk_tarih(x)
            if r:
                return r
        return None
    if isinstance(deger, date) and not isinstance(deger, datetime):
        return deger
    if isinstance(deger, datetime):
        return deger.date()
    try:                                          # pandas.Timestamp vb.
        return deger.date()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(str(deger)[:10]).date()
    except Exception:
        return None


def _borsapy_tarih(ticker: str):
    """borsapy Ticker.calendar / earnings_dates -> sonraki bilanco tarihi (date)."""
    try:
        import borsapy as bp
        t = bp.Ticker(ticker)
    except Exception:
        return None
    for attr in ("calendar", "earnings_dates"):
        try:
            val = getattr(t, attr)
        except Exception:
            continue
        if isinstance(val, dict):
            d = _ilk_tarih(val.get("Earnings Date") or val.get("earnings_date"))
            if d:
                return d
        else:
            try:                                  # DataFrame index'i tarihlerdir
                idx = list(val.index)
                d = _ilk_tarih(idx)
                if d:
                    return d
            except Exception:
                continue
    return None


def _yfinance_tarih(ticker: str):
    """yfinance .calendar['Earnings Date'] -> sonraki bilanco tarihi (date)."""
    try:
        import yfinance as yf
        cal = yf.Ticker(f"{ticker}.IS").calendar
    except Exception:
        return None
    if isinstance(cal, dict):
        return _ilk_tarih(cal.get("Earnings Date"))
    try:                                          # eski surum: DataFrame
        return _ilk_tarih(cal.loc["Earnings Date"].tolist())
    except Exception:
        return None


def _kap_tarih(ticker: str):
    """KAP finansal takviminden (best-effort) sonraki bilanco tarihi. KAP JS-agirlikli
    oldugundan cogu zaman bos doner; borsapy/yfinance birincildir."""
    url = f"https://www.kap.org.tr/tr/api/financialCalendarList"
    try:
        from curl_cffi import requests as creq
        r = creq.get(url, impersonate="chrome", timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    today = datetime.now(_TZ).date()
    for row in data:
        kod = str(row.get("stockCode") or row.get("ticker") or "").upper()
        if ticker not in kod.split(","):
            continue
        d = _ilk_tarih(row.get("disclosureDate") or row.get("date"))
        if d and d >= today:
            return d
    return None


def guncelle(verbose: bool = True) -> list:
    """Tum hisseler icin bilanco tarihlerini ceker, data/bilanco_takvimi.json'a yazar.
    Sonuc listesini dondurur. Hicbir tarih gelmezse Telegram uyarisi gonderir."""
    from datetime import timedelta
    bugun = datetime.now(_TZ).date()
    sonuc = []
    # yfinance per-hisse gercek tarih verir (birincil); KAP yedek; borsapy en sonda
    # (tum hisselere AYNI duzenleyici son tarihi dondurdugu icin guvenilmez).
    for tk in HISSELER:
        d = None
        for kaynak in (_yfinance_tarih, _kap_tarih, _borsapy_tarih):
            try:
                d = kaynak(tk)
            except Exception:
                d = None
            if d:
                break
        if not d:
            if verbose:
                print(f"  {tk}: bilanco tarihi alinamadi")
            continue
        tahmini = False
        if d < bugun:                             # gecmis aktuel tarih -> sonraki ceyrek (tahmini)
            while d < bugun:
                d = d + timedelta(days=91)
            tahmini = True
        rec = {"ticker": tk, "tarih": d.isoformat(), "donem": _donem(d)}
        if tahmini:
            rec["tahmini"] = True
        sonuc.append(rec)
        if verbose:
            print(f"  {tk}: {d.isoformat()} ({_donem(d)})"
                  f"{' [tahmini]' if tahmini else ''}")

    sonuc.sort(key=lambda r: r["tarih"])
    try:
        DATA.mkdir(exist_ok=True)
        TAKVIM_PATH.write_text(json.dumps(sonuc, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except OSError as e:
        print(f"[uyari] bilanco_takvimi.json yazilamadi: {type(e).__name__}")

    if not sonuc:                                 # hicbir veri gelmedi -> uyar
        try:
            from src.notify import telegram
            telegram.notify_admins("Bilanço takvimi güncellenemedi: hiçbir hisse "
                                   "için tarih alınamadı (bilanco_takvimi.py).")
        except Exception:
            pass
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] bilanço takvimi: "
              f"{len(sonuc)}/{len(HISSELER)} hisse -> {TAKVIM_PATH}")
    return sonuc


def yukle() -> list:
    """Kayitli bilanco takvimini dondurur (yoksa bos liste)."""
    try:
        return json.loads(TAKVIM_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def gun_farki(ticker: str, bugun: date = None):
    """Bir hissenin bir sonraki bilancosuna kac GUN kaldigini doner (yoksa None).
    Gecmis tarihler atlanir."""
    bugun = bugun or datetime.now(_TZ).date()
    tk = (ticker or "").upper().replace(".IS", "")
    for r in yukle():
        if (r.get("ticker") or "").upper() != tk:
            continue
        try:
            d = datetime.fromisoformat(r["tarih"]).date()
        except Exception:
            continue
        if d >= bugun:
            return (d - bugun).days
    return None


def bu_hafta(bugun: date = None) -> list:
    """Onumuzdeki 7 gun icinde bilanco aciklayacak hisseler (tarih sirali)."""
    bugun = bugun or datetime.now(_TZ).date()
    out = []
    for r in yukle():
        try:
            d = datetime.fromisoformat(r["tarih"]).date()
        except Exception:
            continue
        if 0 <= (d - bugun).days <= 7:
            out.append({**r, "gun_kala": (d - bugun).days, "_d": d})
    out.sort(key=lambda r: r["_d"])
    for r in out:
        r.pop("_d", None)
    return out


if __name__ == "__main__":
    guncelle()
