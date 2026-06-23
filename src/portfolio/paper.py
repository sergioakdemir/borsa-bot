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


def _usdtry():
    """Guncel USD/TRY kuru (makro). Bulunamazsa None."""
    try:
        from src.news.macro import get_macro
        v = get_macro().get("usdtry")
        return float(v) if v else None
    except Exception:
        return None


def record_from_results(results, tarih=None, verbose: bool = False) -> dict:
    """Sabah brifingi sonuclarindan sanal islemleri acar/kapatir.

    BIST + ABD destekli. Fiyatlar TL bazinda saklanir; ABD hisselerinde USD fiyat
    guncel USD/TRY kuruyla TL'ye cevrilir (adet = SERMAYE_TL / (fiyat_usd * kur)).
    results: commentary.run ciktisi. Doner: {acilan, kapanan}.
    """
    from src.db import database as db

    acilan = kapanan = 0
    fx = None                                       # USD/TRY (lazy)
    for r in results or []:
        if r.get("skipped") or r.get("kill_switch"):
            continue
        market = (r.get("market") or "bist").lower()
        is_us = market in ("us", "abd")
        ticker = (r.get("ticker") or "").upper().replace(".IS", "")
        if not ticker:
            continue
        karar = r.get("final_decision")
        sig = r.get("kullanilan_on_sinyal") or {}
        fiyat_native = sig.get("son_kapanis")
        if not fiyat_native:
            continue

        if is_us:
            if fx is None:
                fx = _usdtry()
            if not fx:
                if verbose:
                    print(f"  [paper] {ticker} ABD atlandi (USD/TRY kuru yok)")
                continue
            kur = fx
        else:
            kur = 1.0
        fiyat_tl = fiyat_native * kur               # TL bazli saklanir
        para_birimi = "USD" if is_us else "TL"

        acik = db.get_open_paper_trade(ticker)

        if karar == "AL":
            if acik:                                  # zaten acik pozisyon var
                continue
            adet = round(SERMAYE_TL / fiyat_tl, 6)
            db.open_paper_trade(ticker, karar, fiyat_tl, adet, tarih=tarih,
                                para_birimi=para_birimi)
            acilan += 1
            if verbose:
                print(f"  [paper] AL  {ticker} @ {fiyat_tl:.2f} TL x {adet} "
                      f"({para_birimi}{' kur '+str(round(kur,2)) if is_us else ''})")
        elif karar in ("SAT", "GUCLU_SAT", "AZALT", "UZAK_DUR"):
            if not acik:
                continue
            giris = acik["fiyat"] or 0.0
            kz_yuzde = round((fiyat_tl - giris) / giris * 100, 2) if giris else None
            db.close_paper_trade(acik["id"], fiyat_tl, kz_yuzde, tarih=tarih)
            kapanan += 1
            if verbose:
                print(f"  [paper] SAT {ticker} @ {fiyat_tl:.2f} TL (giris {giris}) -> "
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
