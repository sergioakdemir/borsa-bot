"""Açık trade'lerin (gerçek işlem defteri) günlük güncellemesi.

Her gece cron. Her açık trade için:
  - Güncel fiyattan anlık getiri yüzdesi hesaplanır.
  - max_profit / max_drawdown (en iyi / en kötü görülen yüzde) güncellenir.
  - Fiyat STOP'a değer/altına inerse veya HEDEF'e değer/üstüne çıkarsa pozisyon
    kapatılır (kapanis_sebep='stop' / 'hedef', pnl_yuzde + holding_days ile).

Karar bazlı kapanış (AZALT/UZAK_DUR/SAT) commentary.py'de yapılır; bu cron yalnız
stop/hedef tetiğiyle kapatır ve uç değerleri (max_drawdown/max_profit) günceller.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_TZ = ZoneInfo("Europe/Istanbul")


def _yf_sembol(ticker: str, para_birimi: str = "TL") -> str:
    """Ticker'in yfinance sembolu (enstruman ana tablosundan). ABD'de '.IS' eklenmez.

    ABD tespiti ve sembol enstruman ana tablosundan (instruments) okunur; böylece
    USD işaretlenmemiş ABD hisseleri (örn. NVDA) de '.IS' eki almadan doğru
    sembolle fiyatlanır. Tabloda olmayan ticker BIST varsayılır."""
    from src.db import database as db
    inst = db.get_instrument(ticker)
    if inst is not None:
        return db.instrument_symbol(ticker)
    is_us = (para_birimi or "TL").upper() == "USD" or db.is_us_instrument(ticker)
    base = ticker.upper().replace(".IS", "")
    return base if is_us else f"{base}.IS"


def _son_bar(ticker: str, para_birimi: str = "TL"):
    """Son işlem gününün (close, high, low) üçlüsü (yerel para). Yoksa None.

    Gün içi yüksek/düşük günlük OHLC verisinden alınır; kapanış fiyatına ek olarak
    intraday_high_pct/intraday_low_pct güncellemesinde kullanılır."""
    from src.data.factory import get_data_source
    symbol = _yf_sembol(ticker, para_birimi)
    start = (datetime.now(_TZ).date() - timedelta(days=10)).isoformat()
    try:
        df = get_data_source().get_history(symbol, start=start)
    except Exception:
        return None
    if df is None or df.empty:
        return None
    if "Volume" in df.columns:
        f = df[df["Volume"] > 0]
        if not f.empty:
            df = f
    if df.empty:
        return None
    close = float(df["Close"].iloc[-1])
    high = float(df["High"].iloc[-1]) if "High" in df.columns else close
    low = float(df["Low"].iloc[-1]) if "Low" in df.columns else close
    return close, high, low


def _son_fiyat(ticker: str, para_birimi: str = "TL"):
    """Güncel kapanış (yerel para). Geriye dönük uyum için korunur."""
    bar = _son_bar(ticker, para_birimi)
    return bar[0] if bar else None


def _gun_farki(baslangic, bitis) -> int | None:
    try:
        a = datetime.fromisoformat(str(baslangic)[:10]).date()
        b = datetime.fromisoformat(str(bitis)[:10]).date()
        return (b - a).days
    except Exception:
        return None


def _islem_gunu(baslangic, bugun_date) -> int | None:
    """baslangic (dahil) -> bugun (haric) arasi gecen ISLEM GUNU (Pzt-Cum) sayisi.
    Hafta sonlarini eler (resmi tatilleri saymaz; yaklasik). Parse hatasinda None."""
    try:
        import numpy as np
        a = datetime.fromisoformat(str(baslangic)[:10]).date()
        b = bugun_date if hasattr(bugun_date, "isoformat") else \
            datetime.fromisoformat(str(bugun_date)[:10]).date()
        return int(np.busday_count(a.isoformat(), b.isoformat()))
    except Exception:
        return None


def _fiyat_str(fiyat: float) -> str:
    """Fiyati gereksiz ondalik olmadan yazar: 295.0 -> '295', 295.5 -> '295.5'."""
    return f"{fiyat:g}"


def _kapanis_bildir(t: dict, sebep: str, native: float, pnl_y: float) -> None:
    """Stop/hedef tetigiyle kapanan trade icin ilgili kullaniciya Telegram bildirimi
    gonderir. Kullanicinin telegram_id'si yoksa (veya kullanici_id=0 sistem geneli)
    yoneticilere dusulur ki bildirim kaybolmasin. Hata olursa cron'u dusurmez."""
    try:
        from src.db import database as db
        from src.notify import telegram
    except Exception:
        return

    birim = "$" if (t.get("para_birimi") or "TL").upper() == "USD" else "TL"
    fiyat = _fiyat_str(native)
    kz = "Kâr" if pnl_y >= 0 else "Zarar"
    isaret = "+" if pnl_y >= 0 else "-"
    yuzde = f"{isaret}%{abs(pnl_y):.1f}"
    if sebep == "stop":
        mesaj = (f"🔴 STOP-LOSS: {t['ticker']} {fiyat} {birim}'ye düştü. "
                 f"Pozisyon kapatıldı. {kz}: {yuzde}")
    else:
        mesaj = (f"🎯 HEDEF: {t['ticker']} {fiyat} {birim}'ye ulaştı. "
                 f"Pozisyon kapatıldı. {kz}: {yuzde}")

    chat_id = None
    uid = t.get("kullanici_id") or 0
    if uid:
        try:
            u = db.get_user_by_id(uid)
            if u and u.get("telegram_id"):
                chat_id = u["telegram_id"]
        except Exception:
            chat_id = None

    try:
        if chat_id:
            telegram.send_message(mesaj, chat_id=chat_id)
        else:                                  # sahibi/telegram_id yok -> kaybolmasin
            telegram.notify_admins(mesaj, prefix="")
    except Exception as e:
        print(f"    [bildirim] {t['ticker']} kapanis bildirimi gonderilemedi: "
              f"{type(e).__name__}")


def _trade_bildir(t: dict, mesaj: str) -> None:
    """Genel trade bildirimi (time-stop / trailing-stop). Sahibine (telegram_id
    varsa), yoksa yoneticilere dusulur. Hata cron'u dusurmez."""
    try:
        from src.db import database as db
        from src.notify import telegram
    except Exception:
        return
    chat_id = None
    uid = t.get("kullanici_id") or 0
    if uid:
        try:
            u = db.get_user_by_id(uid)
            if u and u.get("telegram_id"):
                chat_id = u["telegram_id"]
        except Exception:
            chat_id = None
    try:
        if chat_id:
            telegram.send_message(mesaj, chat_id=chat_id)
        else:
            telegram.notify_admins(mesaj, prefix="")
    except Exception as e:
        print(f"    [bildirim] {t.get('ticker')} bildirim gonderilemedi: "
              f"{type(e).__name__}")


def run(verbose: bool = True) -> dict:
    from src.db import database as db
    db.init_db()
    bugun = datetime.now(_TZ).date().isoformat()
    acik = db.list_trades(durum="acik")
    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] açık trade: {len(acik)}")

    guncellenen = kapanan = 0
    for t in acik:
        bar = _son_bar(t["ticker"], t.get("para_birimi"))
        if bar is None:
            if verbose:
                print(f"  {t['ticker']}: fiyat yok, atlandı")
            continue
        native, gun_yuksek, gun_dusuk = bar
        entry = t.get("entry_fiyat") or 0.0
        if not entry:
            continue
        pct = (native - entry) / entry * 100

        # max_profit / max_drawdown (kapanışa göre yüzde) güncelle
        eski_mp = t.get("max_profit")
        eski_md = t.get("max_drawdown")
        yeni_mp = round(pct if eski_mp is None else max(eski_mp, pct), 2)
        yeni_md = round(pct if eski_md is None else min(eski_md, pct), 2)
        db.update_trade_extremes(t["id"], yeni_md, yeni_mp)

        # intraday_high_pct / intraday_low_pct: gün içi yüksek/dip ile genişlet
        # (fitilleri de yakalar; kapanışa dayalı max_profit/drawdown'dan farkı bu)
        gun_yuksek_pct = (gun_yuksek - entry) / entry * 100
        gun_dusuk_pct = (gun_dusuk - entry) / entry * 100
        eski_ih = t.get("intraday_high_pct")
        eski_il = t.get("intraday_low_pct")
        yeni_ih = round(gun_yuksek_pct if eski_ih is None
                        else max(eski_ih, gun_yuksek_pct), 2)
        yeni_il = round(gun_dusuk_pct if eski_il is None
                        else min(eski_il, gun_dusuk_pct), 2)
        db.update_trade_intraday(t["id"], yeni_ih, yeni_il)
        guncellenen += 1

        is_al = (t.get("karar") or "").upper().startswith("AL")
        birim = "$" if (t.get("para_birimi") or "TL").upper() == "USD" else "TL"

        # TRAILING STOP (AL): kâr eşiklerini geçince stop'u yukarı çek.
        #   max_profit >= %6 -> stop = entry + kârın yarısı (entry*(1 + mp*0.5/100))
        #   max_profit >= %4 -> stop = entry (başabaş), stop hâlâ entry altındaysa
        # Stop yalnız YUKARI çekilir (asla düşürülmez).
        cur_stop = t.get("stop_fiyat")
        if is_al and entry and cur_stop is not None:
            yeni_stop = None
            if yeni_mp >= 6.0:
                yeni_stop = round(entry * (1 + (yeni_mp * 0.5) / 100.0), 2)
            elif yeni_mp >= 4.0 and cur_stop < entry:
                yeni_stop = round(entry, 2)
            if yeni_stop is not None and yeni_stop > cur_stop:
                db.update_trade_stop(t["id"], yeni_stop)
                t["stop_fiyat"] = yeni_stop        # sonraki tetik güncel stop'u kullansın
                _trade_bildir(t, f"🔒 STOP GÜNCELLENDİ: {t['ticker']} stop "
                                 f"{_fiyat_str(cur_stop)} → {_fiyat_str(yeni_stop)} {birim}")
                if verbose:
                    print(f"  {t['ticker']:7} TRAILING stop {cur_stop} -> {yeni_stop}")

        # TIME STOP (AL): 5 işlem günü geçmiş, kâr <%1 ve şu an zararda -> aday işaretle
        # (yalnız ilk kez; time_stop_adayi=1 ise tekrar bildirim yok).
        if is_al and not t.get("time_stop_adayi"):
            gecen = _islem_gunu(t.get("acilis_tarihi"), bugun)
            if gecen is not None and gecen >= 5 and yeni_mp < 1.0 and pct < 0:
                db.mark_time_stop(t["id"], 1)
                _trade_bildir(t, f"⏰ TIME STOP ADAYI: {t['ticker']} 5 gündür "
                                 f"kıpırdamıyor. Pozisyonu değerlendir.")
                if verbose:
                    print(f"  {t['ticker']:7} TIME-STOP adayı ({gecen} işlem günü, "
                          f"maxP %{yeni_mp}, güncel %{round(pct, 2)})")

        # Stop / hedef tetiği -> kapat
        stop = t.get("stop_fiyat")
        hedef = t.get("hedef_fiyat")
        hedef2 = t.get("hedef2_fiyat")
        sebep = None
        if hedef2 is not None and hedef is not None:
            # YENİ trade (kademeli hedef): hedef1'e ulaşınca KAPATMA -> stop'u girişe
            # çek (kârı garantile), hedefi hedef2 yap. hedef2'ye ulaşınca tamamen kapat.
            if native >= hedef and hedef < hedef2:
                entry_f = t.get("entry_fiyat")
                # Stop'u en az girişe çek; trailing daha yukarı çektiyse dokunma.
                if entry_f is not None and (stop is None or stop < entry_f):
                    db.update_trade_stop(t["id"], entry_f)
                db.update_trade_hedef(t["id"], hedef2)          # hedef_fiyat = hedef2
                _trade_bildir(t, f"🎯 İLK HEDEF: {t['ticker']} +%{pct:.1f} — kârın "
                                 f"yarısını almayı düşün. Stop girişe çekildi, yeni "
                                 f"hedef {_fiyat_str(hedef2)} {birim}.")
                if verbose:
                    print(f"  {t['ticker']:7} İLK HEDEF @ {native:.2f} -> stop=giriş, "
                          f"hedef={hedef2}")
                continue                                        # bu bar'da kapanış yok
            if native >= hedef:                                 # hedef==hedef2 -> ikinci hedef
                sebep = "hedef2"
            elif stop is not None and native <= stop:
                sebep = "stop"
        else:
            # ESKİ trade'ler (hedef2_fiyat boş): mevcut davranış korunur.
            if stop is not None and native <= stop:
                sebep = "stop"
            elif hedef is not None and native >= hedef:
                sebep = "hedef"
        if sebep:
            pnl_y = round(pct, 2)
            hold = _gun_farki(t.get("acilis_tarihi"), bugun)
            db.close_trade(t["id"], native, kapanis_sebep=sebep, pnl_yuzde=pnl_y,
                           holding_days=hold, tarih=bugun)
            _kapanis_bildir(t, sebep, native, pnl_y)
            kapanan += 1
            if verbose:
                print(f"  {t['ticker']:7} {sebep.upper()} @ {native:.2f} -> %{pnl_y} "
                      f"(giriş {entry})")
        elif verbose:
            print(f"  {t['ticker']:7} giriş {entry} -> {native:.2f} : %{pct:.2f} "
                  f"(maxP %{yeni_mp} / maxD %{yeni_md})")

    if verbose:
        print(f"[{datetime.now(_TZ):%Y-%m-%d %H:%M}] {guncellenen} güncellendi, "
              f"{kapanan} kapandı (stop/hedef).")
    return {"guncellenen": guncellenen, "kapanan": kapanan}


if __name__ == "__main__":
    run()
