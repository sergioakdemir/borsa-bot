"""Giriş kalitesi skoru (0-100): bir hisseye ŞU AN girmek ne kadar iyi?

AL kararı verildiğinde commentary.py bu skoru hesaplar ve verdict kaydına ekler;
sabah brifinginde yıldız + öneri olarak gösterilir, fırsat sıralamasında
expected_value ağırlığı olarak kullanılır.

Skor 5 bileşenden oluşur (toplam 100):
  - Trend (25)      : fiyat 10/50 günlük ortalama üzerinde mi
  - Volatilite (20) : günlük volatilite düşükse giriş daha iyi
  - Likidite (15)   : hacim ortalamanın üstünde mi
  - Momentum (20)   : son 5 günlük hareket
  - Risk (20)       : risk puanı (düşükse iyi)
"""


def _trend_skoru(sig) -> int:
    """Fiyat 10/50 günlük ortalama üzerinde mi? İkisi=25, biri=15, hiçbiri=5."""
    fiyat = sig.get("son_kapanis")
    ma10, ma50 = sig.get("ma10"), sig.get("ma50")
    if fiyat is None:
        return 5
    ust = 0
    if ma10 is not None and fiyat > ma10:
        ust += 1
    if ma50 is not None and fiyat > ma50:
        ust += 1
    return {2: 25, 1: 15}.get(ust, 5)


def _volatilite_skoru(sig) -> int:
    """Günlük volatilite düşükse giriş iyi. <%1=20, %1-2=15, %2-3=10, %3+=5."""
    v = sig.get("volatilite_%")
    if v is None:
        return 10
    if v < 1:
        return 20
    if v < 2:
        return 15
    if v < 3:
        return 10
    return 5


def _likidite_skoru(sig) -> int:
    """Hacim ortalamanın üstündeyse 15, altındaysa 7."""
    h = sig.get("hacim_vs_ort_%")
    if h is None:
        return 7
    return 15 if h > 0 else 7


def _momentum_skoru(sig) -> int:
    """Son 5 günlük hareket: +%2 üzeri=20, 0-2=15, negatif=5."""
    m = sig.get("son5g_degisim_%")
    if m is None:
        return 15
    if m >= 2:
        return 20
    if m >= 0:
        return 15
    return 5


def _risk_skoru(risk_puani) -> int:
    """Risk puanı 1-3=20, 4-5=15, 6-7=10, 8+=5."""
    if risk_puani is None:
        return 10
    r = int(risk_puani)
    if r <= 3:
        return 20
    if r <= 5:
        return 15
    if r <= 7:
        return 10
    return 5


def _yildiz(skor: int) -> str:
    if skor >= 80:
        return "★★★★★"
    if skor >= 60:
        return "★★★★☆"
    if skor >= 40:
        return "★★★☆☆"
    if skor >= 20:
        return "★★☆☆☆"
    return "★☆☆☆☆"


def _oneri(skor: int) -> str:
    if skor >= 80:
        return "Şimdi girilebilir"
    if skor >= 60:
        return "İyi giriş noktası"
    if skor >= 40:
        return "Sabırlı ol, biraz bekle"
    if skor >= 20:
        return "Geri çekilme bekle"
    return "Henüz erken"


def hesapla(sig: dict, risk_puani=None) -> dict:
    """Bir hisse için 0-100 giriş kalitesi skoru hesaplar.

    sig: commentary.market_data çıktısı (son_kapanis, ma10, ma50, volatilite_%,
    hacim_vs_ort_%, son5g_degisim_%). risk_puani: AI risk skoru (1-10).
    Döner: {skor, yildiz, oneri, kirilim}.
    """
    sig = sig or {}
    kirilim = {
        "trend": _trend_skoru(sig),
        "volatilite": _volatilite_skoru(sig),
        "likidite": _likidite_skoru(sig),
        "momentum": _momentum_skoru(sig),
        "risk": _risk_skoru(risk_puani),
    }
    skor = sum(kirilim.values())
    return {
        "skor": skor,
        "yildiz": _yildiz(skor),
        "oneri": _oneri(skor),
        "kirilim": kirilim,
    }


if __name__ == "__main__":
    ornek = {"son_kapanis": 100, "ma10": 95, "ma50": 90,
             "volatilite_%": 0.8, "hacim_vs_ort_%": 30, "son5g_degisim_%": 3.5}
    print(hesapla(ornek, risk_puani=3))
    print(hesapla({"son_kapanis": 50, "ma10": 55, "ma50": 60,
                   "volatilite_%": 3.5, "hacim_vs_ort_%": -20,
                   "son5g_degisim_%": -4}, risk_puani=9))
