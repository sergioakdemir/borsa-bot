"""EQ GOLGE (shadow mode) degerlendirmesi — 15 Tem 2026.

SORU: EQ esigi (60) cok yuksek mi? Yani "kil payi elenen" AL adaylari
(55 <= EQ < 60) aslinda yukseliyor mu?

YONTEM: commentary._apply_karar_filtreleri, bu banttaki AL adaylarini eq_golge
tablosuna yazar (canli karar DEGISMEZ — hepsi BEKLE'ye cekilir). Bu modul, AL
degerlendirme penceresi (5 islem gunu) dolan golgeleri fiyatla olcer ve alpha
kriterini (degisim>0 VE piyasa_farki>=0) uygular — decisions ile AYNI kural.

CIKTI: "esik 55 olsaydi ne olurdu" — kac golge basarili olurdu.
DIKKAT: bu bir ONERI degildir; esigi degistirme karari kullanicinindir.

Kullanim:
    python -m src.ops.eq_golge            # degerlendir + rapor
    python -m src.ops.eq_golge rapor      # yalniz rapor
"""
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Europe/Istanbul")
KAPANIS_GUN = 5          # AL penceresi: decisions ile ayni (update_decisions._KAPANIS_GUN)
MIN_ORNEK = 5            # bundan az golgede yorum yapma


def _bugun() -> date:
    return datetime.now(_TZ).date()


def _islem_gunu_gecti(tarih_iso: str) -> int:
    """tarih_iso'dan bugune kac IS GUNU gecti (hafta sonu haric)."""
    try:
        d = date.fromisoformat(str(tarih_iso)[:10])
    except ValueError:
        return 0
    n, g = 0, d + timedelta(days=1)
    while g <= _bugun():
        if g.weekday() < 5:
            n += 1
        g += timedelta(days=1)
    return n


def degerlendir(verbose: bool = True) -> dict:
    """Penceresi dolan golgeleri fiyatla olcer, sonucu yazar."""
    from src.db import database as db
    from src.ops.update_decisions import (_benchmark_change, _history,
                                          AL_PIYASA_ESIGI)

    bekleyen = db.eq_golge_listele(degerlendirilmis=False)
    olculen = atlanan = 0
    for g in bekleyen:
        if _islem_gunu_gecti(g["tarih"]) < KAPANIS_GUN:
            continue                      # pencere dolmadi, sonraki kosuda bak
        ticker, market = g["ticker"], (g["market"] or "bist")
        sembol = ticker if market in ("us", "abd") else f"{ticker}.IS"
        try:
            df = _history(sembol, g["tarih"])
            kap = df["Close"].dropna()
            if len(kap) < KAPANIS_GUN + 1 or not g["fiyat"]:
                atlanan += 1
                continue
            yeni = float(kap.iloc[min(KAPANIS_GUN, len(kap) - 1)])
            degisim = round((yeni - g["fiyat"]) / g["fiyat"] * 100, 2)
        except Exception:
            atlanan += 1
            continue

        # _benchmark_change ticker'dan pazari kendi cozer (_is_bist) -> duz ticker.
        pf = _benchmark_change(ticker, g["tarih"], KAPANIS_GUN)
        piyasa_farki = round(degisim - pf, 2) if pf is not None else None
        # decisions ile AYNI alpha kurali: degisim>0 VE piyasa_farki>=AL_PIYASA_ESIGI
        if piyasa_farki is None:
            sonuc = f"{degisim:+.1f}% · OLCULEMEDI (benchmark yok)"
        elif degisim > 0 and piyasa_farki >= AL_PIYASA_ESIGI:
            sonuc = f"{degisim:+.1f}% · DOGRU · piyasa {piyasa_farki:+.1f}p"
        else:
            sonuc = f"{degisim:+.1f}% · YANLIS · piyasa {piyasa_farki:+.1f}p"
        db.eq_golge_sonuc_yaz(g["id"], degisim, piyasa_farki, sonuc)
        olculen += 1
        if verbose:
            print(f"  [golge] {ticker} EQ{g['eq_skor']}: {sonuc}")

    if verbose:
        print(f"[eq_golge] {olculen} golge olculdu, {atlanan} atlandi, "
              f"{len(bekleyen) - olculen - atlanan} pencere beklemede")
    return {"olculen": olculen, "atlanan": atlanan,
            "bekleyen": len(bekleyen) - olculen - atlanan}


def rapor() -> dict:
    """'Esik 55 olsaydi ne olurdu' ozeti."""
    from src.db import database as db
    hepsi = db.eq_golge_listele(degerlendirilmis=True, limit=1000)
    olculu = [g for g in hepsi if g.get("piyasa_farki") is not None]
    dogru = [g for g in olculu if "DOGRU" in (g.get("sonuc") or "")]
    n = len(olculu)
    return {
        "toplam_golge": len(db.eq_golge_listele(limit=1000)),
        "degerlendirilen": n,
        "basarili": len(dogru),
        "oran": (100.0 * len(dogru) / n) if n else None,
        "yeterli_ornek": n >= MIN_ORNEK,
    }


def ozet_satir() -> str:
    """Karne/panel icin tek satir."""
    r = rapor()
    if not r["degerlendirilen"]:
        bekleyen = r["toplam_golge"]
        return (f"{bekleyen} golge kaydedildi, hicbiri degerlendirilmedi "
                f"(pencere 5 is gunu)")
    txt = (f"{r['basarili']}/{r['degerlendirilen']} golge alpha-basarili "
           f"(%{r['oran']:.0f})")
    if not r["yeterli_ornek"]:
        txt += f" — ornek yetersiz (<{MIN_ORNEK}), yorum yapma"
    return txt


def main(argv) -> int:
    if len(argv) > 1 and argv[1] == "rapor":
        pass
    else:
        degerlendir()
    r = rapor()
    print()
    print("=== EQ GOLGE: 'esik 55 olsaydi ne olurdu' ===")
    print(f"  toplam golge (EQ 55-60)  : {r['toplam_golge']}")
    print(f"  degerlendirilen          : {r['degerlendirilen']}")
    if r["degerlendirilen"]:
        print(f"  alpha-basarili olurdu    : {r['basarili']} (%{r['oran']:.1f})")
        if not r["yeterli_ornek"]:
            print(f"  UYARI: ornek < {MIN_ORNEK} — bu oranla esik degistirme.")
    else:
        print("  Henuz degerlendirilmis golge yok (pencere 5 is gunu).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
