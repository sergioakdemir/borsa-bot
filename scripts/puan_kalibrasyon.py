"""Puan kalibrasyon kontrolu: decisions tablosundaki 'puan' ile kararin GERCEK
ileri getirisi (karar tarihinden bugune fiyat degisimi) arasindaki iliskiyi olcer.

Saglikli kalibrasyonda puan yukseldikce ortalama getiri artmali (pozitif korelasyon).
Bozuksa (yuksek puanlar daha iyi sonuc vermiyorsa) rapor uyarir.

Kullanim:  venv/bin/python -m scripts.puan_kalibrasyon [BASLANGIC_TARIHI]
           (varsayilan baslangic: 2026-06-22)
"""
import sys
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore")

# Fiyat sembolu: ABD tickerlari oldugu gibi, BIST'e '.IS' eklenir.
_US = {"SPCX", "NVDA", "AMD", "TSM", "ASML", "RKLB", "IONQ", "RGTI", "ACHR",
       "BFLY", "MU", "CNCK", "RXT", "QQQ", "VOO"}


def _sym(ticker: str) -> str:
    t = (ticker or "").upper().replace(".IS", "")
    return t if t in _US else t + ".IS"


def _pearson(xs, ys) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / (sxx * syy) ** 0.5


def hesapla(baslangic: str = "2026-06-22") -> dict:
    import yfinance as yf
    from src.db.database import get_conn

    c = get_conn()
    rows = c.execute(
        "SELECT ticker, karar, puan, tarih FROM decisions "
        "WHERE tarih >= ? AND puan IS NOT NULL ORDER BY tarih", (baslangic,)
    ).fetchall()

    # Her benzersiz tickeri bir kez indir (tum pencere), seriden getiri hesapla.
    tickers = sorted({r["ticker"] for r in rows})
    seri = {}
    for t in tickers:
        try:
            df = yf.download(_sym(t), start=baslangic, progress=False, auto_adjust=True)
            if df is not None and len(df) >= 2:
                seri[t] = df["Close"]
        except Exception:
            pass

    kayitlar = []
    for r in rows:
        s = seri.get(r["ticker"])
        if s is None or len(s) < 2:
            continue
        try:
            # giris: karar tarihinden >= ilk islem gunu; cikis: son mevcut gun
            kesit = s[s.index >= r["tarih"]]
            if len(kesit) < 2:
                continue
            giris = float(kesit.iloc[0])
            son = float(kesit.iloc[-1])
            if giris == 0:
                continue
            getiri = (son - giris) / giris * 100
            kayitlar.append({"ticker": r["ticker"], "karar": r["karar"],
                             "puan": int(r["puan"]), "getiri": round(getiri, 2)})
        except Exception:
            pass

    # Puan seviyesine gore grupla
    grup = {}
    for k in kayitlar:
        grup.setdefault(k["puan"], []).append(k["getiri"])
    puan_ozet = {p: {"adet": len(v), "ort_getiri": round(sum(v) / len(v), 2)}
                 for p, v in sorted(grup.items())}

    xs = [k["puan"] for k in kayitlar]
    ys = [k["getiri"] for k in kayitlar]
    korelasyon = _pearson(xs, ys)

    # Monotonluk: puan arttikca ort. getiri de artiyor mu? (ardisik seviyeler)
    seviyeler = sorted(puan_ozet)
    ortlar = [puan_ozet[p]["ort_getiri"] for p in seviyeler]
    artislar = sum(1 for a, b in zip(ortlar, ortlar[1:]) if b > a)
    gecisler = max(1, len(ortlar) - 1)
    monoton_oran = artislar / gecisler

    bozuk = (korelasyon is not None and korelasyon < 0.10) or monoton_oran < 0.5

    return {"baslangic": baslangic, "ornek": len(kayitlar),
            "puan_ozet": puan_ozet, "korelasyon": korelasyon,
            "monoton_oran": round(monoton_oran, 2), "bozuk": bozuk}


def main():
    bas = sys.argv[1] if len(sys.argv) > 1 else "2026-06-22"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    r = hesapla(bas)
    print(f"PUAN KALIBRASYON RAPORU  ({now})")
    print(f"Baslangic: {r['baslangic']}  |  Ornek (fiyatli karar): {r['ornek']}")
    print("-" * 46)
    print(f"{'PUAN':>4} {'ADET':>6} {'ORT.GETIRI':>12}")
    for p, d in r["puan_ozet"].items():
        print(f"{p:>4} {d['adet']:>6} {d['ort_getiri']:>+11.2f}%")
    print("-" * 46)
    kor = r["korelasyon"]
    print(f"Puan-getiri korelasyonu (Pearson): "
          f"{kor:+.3f}" if kor is not None else "Korelasyon: hesaplanamadi")
    print(f"Monotonluk (puan arttikca getiri artma orani): %{int(r['monoton_oran']*100)}")
    print()
    if r["bozuk"]:
        print("⚠️  KALIBRASYON BOZUK: yuksek puan daha iyi getiriyi ONGORMUYOR.")
        print("    -> SYSTEM promptuna yuksek-puan disiplin notu eklenmeli.")
    else:
        print("✅ Kalibrasyon makul: puan yukseldikce getiri egilimi pozitif.")
    return r


if __name__ == "__main__":
    main()
