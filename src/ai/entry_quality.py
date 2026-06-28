"""Giriş kalitesi skoru (0-100): bir hisseye ŞU AN girmek ne kadar iyi?

AL kararı verildiğinde commentary.py bu skoru hesaplar ve verdict kaydına ekler;
sabah brifinginde yıldız + öneri olarak gösterilir, fırsat sıralamasında
expected_value ağırlığı olarak kullanılır.

Skor 5 bileşenden oluşur (tam ağırlık toplam 100):
  - Trend (25)      : fiyat 10/50 günlük ortalama üzerinde mi
  - Volatilite (20) : günlük volatilite düşükse giriş daha iyi
  - Likidite (15)   : hacim ortalamanın üstünde mi
  - Momentum (20)   : son 5 günlük hareket
  - Risk (20)       : risk puanı (düşükse iyi)

NULL yönetimi: bir bileşenin verisi yoksa (örn. ma10/ma50 None) o bileşen
hesaplamaya katılmaz; kalan bileşenlerin ağırlığı toplam 100'e normalize edilir.
Böylece eksik veri "kötü" gibi cezalandırılmaz. `veri_guveni` (0-100) kaç ağırlık
biriminin dolu olduğunu gösterir: ne kadar yüksekse skor o kadar güvenilir.
"""

# Bileşen tam ağırlıkları (hepsi doluysa toplam 100).
_AGIRLIK = {"trend": 25, "volatilite": 20, "likidite": 15, "momentum": 20, "risk": 20}


def _trend_frac(sig):
    """Fiyat 10/50 ortalamanın üstünde mi? Döner (frac[0-1], dolu_mu).

    Veri var sayılması için fiyat + en az bir ortalama gerekir. Skor, MEVCUT
    ortalamaların kaçının üstünde olduğunun oranıdır (eksik MA cezalandırılmaz)."""
    fiyat = sig.get("son_kapanis")
    ma10, ma50 = sig.get("ma10"), sig.get("ma50")
    mevcut = [m for m in (ma10, ma50) if m is not None]
    if fiyat is None or not mevcut:
        return 0.0, False
    ust = sum(1 for m in mevcut if fiyat > m)
    # 0 üst -> 0.2 (taban), hepsi üst -> 1.0; arada lineer.
    frac = 0.2 + 0.8 * (ust / len(mevcut))
    return frac, True


def _volatilite_frac(sig):
    """Günlük volatilite düşükse giriş iyi. <%1=1.0, %1-2=.75, %2-3=.5, %3+=.25."""
    v = sig.get("volatilite_%")
    if v is None:
        return 0.0, False
    if v < 1:
        return 1.0, True
    if v < 2:
        return 0.75, True
    if v < 3:
        return 0.5, True
    return 0.25, True


def _likidite_frac(sig):
    """Hacim ortalamanın üstündeyse 1.0, altındaysa ~0.47."""
    h = sig.get("hacim_vs_ort_%")
    if h is None:
        return 0.0, False
    return (1.0 if h > 0 else 0.47), True


def _momentum_frac(sig):
    """Son 5 günlük hareket: +%2 üzeri=1.0, 0-2=.75, negatif=.25."""
    m = sig.get("son5g_degisim_%")
    if m is None:
        return 0.0, False
    if m >= 2:
        return 1.0, True
    if m >= 0:
        return 0.75, True
    return 0.25, True


def _risk_frac(risk_puani):
    """Risk puanı 1-3=1.0, 4-5=.75, 6-7=.5, 8+=.25."""
    if risk_puani is None:
        return 0.0, False
    r = int(risk_puani)
    if r <= 3:
        return 1.0, True
    if r <= 5:
        return 0.75, True
    if r <= 7:
        return 0.5, True
    return 0.25, True


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


def _oneri(skor: int, veri_guveni: int) -> str:
    if veri_guveni < 40:
        return "Veri yetersiz, dikkatli ol"
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

    Eksik veri olan bileşenler hesaplamaya katılmaz; kalan bileşenlerin ağırlığı
    100'e normalize edilir. Döner: {skor, veri_guveni, yildiz, oneri, kirilim}.
    """
    sig = sig or {}
    bilesenler = {
        "trend": _trend_frac(sig),
        "volatilite": _volatilite_frac(sig),
        "likidite": _likidite_frac(sig),
        "momentum": _momentum_frac(sig),
        "risk": _risk_frac(risk_puani),
    }
    dolu_agirlik = sum(_AGIRLIK[k] for k, (_, dolu) in bilesenler.items() if dolu)
    toplam_agirlik = sum(_AGIRLIK.values())

    if dolu_agirlik > 0:
        # Normalize: dolu bileşenlerin ağırlığı 100'e ölçeklenir.
        skor = round(sum(frac * _AGIRLIK[k] for k, (frac, dolu) in bilesenler.items()
                         if dolu) / dolu_agirlik * 100)
    else:
        skor = 0
    veri_guveni = round(dolu_agirlik / toplam_agirlik * 100)

    # kirilim: her bileşenin 0-100 katkısı (dolu değilse None).
    kirilim = {k: (round(frac * 100) if dolu else None)
               for k, (frac, dolu) in bilesenler.items()}

    return {
        "skor": skor,
        "veri_guveni": veri_guveni,
        "yildiz": _yildiz(skor),
        "oneri": _oneri(skor, veri_guveni),
        "kirilim": kirilim,
    }


if __name__ == "__main__":
    ornek = {"son_kapanis": 100, "ma10": 95, "ma50": 90,
             "volatilite_%": 0.8, "hacim_vs_ort_%": 30, "son5g_degisim_%": 3.5}
    print(hesapla(ornek, risk_puani=3))
    print(hesapla({"son_kapanis": 50, "ma10": 55, "ma50": 60,
                   "volatilite_%": 3.5, "hacim_vs_ort_%": -20,
                   "son5g_degisim_%": -4}, risk_puani=9))
    # Eksik veri: yalnız son_kapanis + ma10 var (ma50/volatilite/hacim None)
    print(hesapla({"son_kapanis": 100, "ma10": 95}, risk_puani=4))
