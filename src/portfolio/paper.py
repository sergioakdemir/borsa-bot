"""Paper trading (sanal islem) motoru.

Mantik:
  - Sabah AL karari cikan her BIST hissesi icin acik pozisyon yoksa sanal alim
    yapilir. Sanal adet = SERMAYE_TL / o anki fiyat (varsayilan 1000 TL).
  - SAT/GUCLU_SAT karari cikan ve acik pozisyonu olan hisse kapatilir; kar/zarar
    yuzdesi kaydedilir.
  - Her gece acik pozisyonlarin guncel (kagit) kar/zarari guncellenir
    (src/ops/update_paper_trades.py).
"""
SERMAYE_TL = 1000.0


def record_from_results(results, tarih=None, verbose: bool = False) -> dict:
    """Sabah brifingi sonuclarindan sanal islemleri acar/kapatir.

    results: commentary.run ciktisi (kayit listesi).
    Doner: {acilan, kapanan} sayilari.
    """
    from src.db import database as db

    acilan = kapanan = 0
    for r in results or []:
        if r.get("skipped") or r.get("kill_switch"):
            continue
        if (r.get("market") or "bist") != "bist":   # sanal sermaye TL bazli
            continue
        ticker = (r.get("ticker") or "").upper().replace(".IS", "")
        if not ticker:
            continue
        karar = r.get("final_decision")
        sig = r.get("kullanilan_on_sinyal") or {}
        fiyat = sig.get("son_kapanis")
        if not fiyat:
            continue

        acik = db.get_open_paper_trade(ticker)

        if karar == "AL":
            if acik:                                  # zaten acik pozisyon var
                continue
            adet = round(SERMAYE_TL / fiyat, 4)
            db.open_paper_trade(ticker, karar, fiyat, adet, tarih=tarih)
            acilan += 1
            if verbose:
                print(f"  [paper] AL  {ticker} @ {fiyat} x {adet} (sanal {SERMAYE_TL:g} TL)")
        elif karar in ("SAT", "GUCLU_SAT", "AZALT"):
            if not acik:
                continue
            giris = acik["fiyat"] or 0.0
            kz_yuzde = round((fiyat - giris) / giris * 100, 2) if giris else None
            db.close_paper_trade(acik["id"], fiyat, kz_yuzde, tarih=tarih)
            kapanan += 1
            if verbose:
                print(f"  [paper] SAT {ticker} @ {fiyat} (giris {giris}) -> "
                      f"%{kz_yuzde} k/z")

    return {"acilan": acilan, "kapanan": kapanan}


def summary() -> dict:
    """Paper trading ozeti: islem sayisi, basari orani, toplam sanal k/z."""
    from src.db import database as db
    trades = db.list_paper_trades()
    kapali = [t for t in trades if t.get("durum") == "kapali"]
    acik = [t for t in trades if t.get("durum") == "acik"]

    realize_kz_tl = 0.0
    kazanan = 0
    for t in kapali:
        giris = t.get("fiyat") or 0.0
        cikis = t.get("kapanis_fiyati") or 0.0
        adet = t.get("adet_sanal") or 0.0
        kz = (cikis - giris) * adet
        realize_kz_tl += kz
        if kz > 0:
            kazanan += 1

    acik_kz_tl = 0.0
    for t in acik:
        if t.get("kz_yuzde") is not None:
            acik_kz_tl += (t["fiyat"] or 0.0) * (t.get("adet_sanal") or 0.0) * \
                          (t["kz_yuzde"] / 100.0)

    basari = round(kazanan / len(kapali) * 100, 1) if kapali else None
    return {
        "islem_sayisi": len(trades),
        "acik_sayisi": len(acik),
        "kapali_sayisi": len(kapali),
        "kazanan": kazanan,
        "basari_orani_%": basari,
        "realize_kz_tl": round(realize_kz_tl, 2),
        "acik_kz_tl": round(acik_kz_tl, 2),
        "toplam_kz_tl": round(realize_kz_tl + acik_kz_tl, 2),
        "sermaye_tl": SERMAYE_TL,
    }
