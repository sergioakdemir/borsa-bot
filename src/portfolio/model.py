"""Model portfoy: botun kendi sanal portfoyu (100.000 TL, bugunden itibaren).

- Sabah AL karari (puan >= 7) -> 20.000 TL'lik sanal alim (acik pozisyon yoksa,
  nakit yeterse, max 5 acik pozisyon).
- SAT/GUCLU_SAT -> acik pozisyonu kapat, kar/zarar kaydet.
- Her gece guncel fiyatla kagit k/z guncellenir; stop-loss -%10 veya 20 gun
  max bekleme dolunca pozisyon otomatik kapanir (ops/update_model_portfoy.py).
- BIST-100 (XU100.IS) ile ayni donem getirisi karsilastirilir.

paper_trades'ten farkli, bagimsiz bir portfoy. AL esigi MODEL portfoy icin 7;
canli karar/yorum esigi (commentary) 8'de kalir.
"""
BASLANGIC_TL = 100_000.0
POZ_TL = 20_000.0            # her pozisyon 20K TL
MAX_OPEN = 5                 # ayni anda en fazla 5 acik pozisyon
AL_PUAN_ESIK = 7            # model portfoy AL esigi (canli yorum esigi 8)
STOP_LOSS = -0.10           # -%10 stop-loss
MAX_HOLD_GUN = 20          # max bekleme 20 gun


def _usdtry():
    """Guncel USD/TRY kuru (makro). Bulunamazsa None."""
    try:
        from src.news.macro import get_macro
        v = get_macro().get("usdtry")
        return float(v) if v else None
    except Exception:
        return None


def model_cash() -> float:
    """Sanal nakit = baslangic - tum alim maliyeti + tum kapanis getirisi."""
    from src.db import database as db
    cash = BASLANGIC_TL
    for t in db.list_model_positions():
        cash -= (t.get("adet") or 0) * (t.get("alis_fiyati") or 0)        # alim
        if t.get("durum") == "kapali" and t.get("kapanis_fiyati") is not None:
            cash += (t.get("adet") or 0) * t["kapanis_fiyati"]            # satim
    return cash


def record_from_results(results, tarih=None, verbose: bool = False) -> dict:
    """Sabah sonuclarindan model portfoyu gunceller (AL ac / SAT kapat)."""
    from src.db import database as db
    acilan = kapanan = 0
    cash = model_cash()
    acik_sayisi = len(db.list_model_positions(durum="acik"))
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
                    print(f"  [model] {ticker} ABD atlandi (USD/TRY kuru yok)")
                continue
            kur = fx
        else:
            kur = 1.0
        fiyat = fiyat_native * kur                   # TL bazli
        para_birimi = "USD" if is_us else "TL"
        acik = db.get_open_model_position(ticker)

        if karar == "AL":
            puan = r.get("score") or r.get("puan") or 0
            if acik or cash < POZ_TL or acik_sayisi >= MAX_OPEN or puan < AL_PUAN_ESIK:
                continue
            adet = round(POZ_TL / fiyat, 6)
            gerekce = (r.get("gerekce") or "")[:300]
            db.open_model_position(ticker, adet, fiyat, karar_gerekce=gerekce,
                                   alis_tarihi=tarih, para_birimi=para_birimi)
            cash -= adet * fiyat
            acik_sayisi += 1
            acilan += 1
            if verbose:
                print(f"  [model] AL  {ticker} @ {fiyat:.2f} TL x {adet} ({para_birimi})")
        elif karar in ("SAT", "GUCLU_SAT", "AZALT", "UZAK_DUR"):
            if not acik:
                continue
            giris = acik["alis_fiyati"] or 0.0
            adet = acik["adet"] or 0.0
            kz_tl = round((fiyat - giris) * adet, 2)
            kz_y = round((fiyat - giris) / giris * 100, 2) if giris else None
            db.close_model_position(acik["id"], fiyat, kz_tl, kz_y, tarih=tarih)
            cash += adet * fiyat
            kapanan += 1
            if verbose:
                print(f"  [model] SAT {ticker} @ {fiyat:.2f} TL -> {kz_tl} TL")
    return {"acilan": acilan, "kapanan": kapanan}


def _bist100_getiri(baslangic_tarih):
    """baslangic_tarih -> bugun XU100.IS yuzde getirisi (yoksa None)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from src.data.factory import get_data_source
    tz = ZoneInfo("Europe/Istanbul")
    try:
        start = (datetime.fromisoformat(baslangic_tarih).date() - timedelta(days=4)).isoformat()
    except (ValueError, TypeError):
        return None
    try:
        df = get_data_source().get_history("XU100.IS", start=start)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if "Volume" in df.columns:
        f = df[df["Volume"] > 0]
        if len(f) >= 2:
            df = f
    closes = [float(x) for x in df["Close"].tolist()]
    if len(closes) < 2:
        return None
    return round((closes[-1] - closes[0]) / closes[0] * 100, 2)


def summary() -> dict:
    """Model portfoy ozeti: pozisyonlar + toplam getiri + BIST100 kiyasi."""
    from src.db import database as db
    pos = db.list_model_positions()
    acik = [p for p in pos if p.get("durum") == "acik"]
    kapali = [p for p in pos if p.get("durum") == "kapali"]

    cash = model_cash()
    acik_deger = sum((p.get("guncel_fiyat") or p.get("alis_fiyati") or 0) * (p.get("adet") or 0)
                     for p in acik)
    toplam_deger = cash + acik_deger
    getiri_tl = toplam_deger - BASLANGIC_TL
    getiri_y = round(getiri_tl / BASLANGIC_TL * 100, 2)

    realize = sum(p.get("kz_tl") or 0 for p in kapali)
    kazanan = sum(1 for p in kapali if (p.get("kz_tl") or 0) > 0)
    basari = round(kazanan / len(kapali) * 100, 1) if kapali else None

    en_iyi = en_kotu = None
    if kapali:
        s = sorted(kapali, key=lambda p: (p.get("kz_tl") or 0))
        en_kotu = {"ticker": s[0]["ticker"], "kz_tl": round(s[0].get("kz_tl") or 0, 2)}
        en_iyi = {"ticker": s[-1]["ticker"], "kz_tl": round(s[-1].get("kz_tl") or 0, 2)}

    # BIST100 kiyasi: en erken alim tarihinden bugune
    tarihler = [p.get("alis_tarihi") for p in pos if p.get("alis_tarihi")]
    bist = _bist100_getiri(min(tarihler)) if tarihler else None

    return {
        "baslangic_tl": BASLANGIC_TL,
        "nakit_tl": round(cash, 2),
        "acik_deger_tl": round(acik_deger, 2),
        "toplam_deger_tl": round(toplam_deger, 2),
        "getiri_tl": round(getiri_tl, 2),
        "getiri_yuzde": getiri_y,
        "bist100_getiri_yuzde": bist,
        "bist100_fark_yuzde": round(getiri_y - bist, 2) if bist is not None else None,
        "acik_sayisi": len(acik),
        "kapali_sayisi": len(kapali),
        "realize_kz_tl": round(realize, 2),
        "basari_orani_%": basari,
        "en_iyi": en_iyi,
        "en_kotu": en_kotu,
        "acik_pozisyonlar": acik,
        "kapali_pozisyonlar": kapali,
    }
