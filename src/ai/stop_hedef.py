"""Deterministik stop / kademeli hedef motoru.

hesapla(sig, risk_puani) -> {stop, hedef1, hedef2, stop_pct, hedef1_pct, hedef2_pct}

Mesafeler gunluk volatiliteden turetilir; fiyatlar sig['son_kapanis'] uzerinden
hesaplanir. Rakam uydurmaz, AI cagirmaz.
"""

STOP_MIN_PCT = 3.0          # stop mesafesi alt siniri
STOP_MAX_PCT = 5.0          # stop mesafesi ust siniri
VARSAYILAN_STOP_PCT = 4.0   # volatilite_% yoksa
HEDEF1_CARPAN = 1.5         # hedef1 = stop mesafesi x1.5
HEDEF2_CARPAN = 2.5         # hedef2 = stop mesafesi x2.5


def hesapla(sig: dict, risk_puani=None) -> dict | None:
    """Deterministik stop ve iki kademeli hedef seviyesi.

    - Stop mesafesi = volatilite_% x2, [%3, %5] araligina kirpilir; volatilite_%
      yoksa varsayilan %4.
    - hedef1 = stop mesafesi x1.5, hedef2 = stop mesafesi x2.5 (vol-bazli mesafeden;
      ma50 stop ayarindan BAGIMSIZ).
    - Fiyatlar son_kapanis uzerinden.
    - ma50 varsa ve son_kapanis'in %3-5 ustundeyse (ma50 fiyatin %3-5 altinda, yakin
      teknik destek) stop'u ma50 x0.995'e (destegin hemen altina) ayarlar.

    risk_puani su an formulu ETKILEMEZ (imza uyumu / ileride kullanim icin alinir).
    son_kapanis yoksa None doner.
    """
    sig = sig or {}
    son = sig.get("son_kapanis")
    if not son or son <= 0:
        return None

    vol = sig.get("volatilite_%")
    if vol is not None and vol > 0:
        stop_mesafe_pct = max(STOP_MIN_PCT, min(STOP_MAX_PCT, vol * 2))
    else:
        stop_mesafe_pct = VARSAYILAN_STOP_PCT

    # Hedefler vol-bazli stop mesafesinden (ma50 tweak'inden bagimsiz)
    hedef1_pct = round(stop_mesafe_pct * HEDEF1_CARPAN, 2)
    hedef2_pct = round(stop_mesafe_pct * HEDEF2_CARPAN, 2)
    hedef1 = round(son * (1 + hedef1_pct / 100), 2)
    hedef2 = round(son * (1 + hedef2_pct / 100), 2)

    # Stop: once vol-bazli
    stop = round(son * (1 - stop_mesafe_pct / 100), 2)

    # ma50 teknik destek: ma50 son_kapanis'in %3-5 altindaysa stop'u ma50'nin hemen altina
    ma50 = sig.get("ma50")
    if ma50 and ma50 > 0:
        alt_fark = (son - ma50) / son * 100     # ma50, son'un yuzde kaci altinda
        if 3 <= alt_fark <= 5:
            stop = round(ma50 * 0.995, 2)

    stop_pct = round((son - stop) / son * 100, 2)   # gercek stop mesafesi (%)

    return {
        "stop": stop,
        "hedef1": hedef1,
        "hedef2": hedef2,
        "stop_pct": stop_pct,
        "hedef1_pct": hedef1_pct,
        "hedef2_pct": hedef2_pct,
    }
