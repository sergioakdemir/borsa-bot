"""GÖLGE kurallar (21 Tem 2026) — CANLI DEGIL / hicbir canli karari etkilemez.

Amac: Is 2 (yeni katalizor vs uzamis trend) ve Is 5 (suregelen tema konviksiyonu)
icin TESPIT mantigi. Isabet dogrulanmadan (golge backtest) canliya ALINMAZ.

REHBER ILKE: 'YENI katalizor'u yakala, 'UZAMIS trend kovalama'dan kacin.
v1 momentum kovalayip -%5.4 kaybetti — o hatayi TEKRARLAMA:
  * Zaten cok yukselmis (asiri) hisse -> yeni katalizor olsa BILE kovalanmaz.
  * Yalniz 'yeni katalizor + henuz asiri uzamamis' durum kismi firsat sayilir.
"""

# Ayni-yon 1-3g hareket bu %'yi asiyorsa haber_sinyal zaten 'fiyatlanmis' der.
# (haber_sinyal._FIYATLANMIS_ESIK ile ayni referans.)
_FIYATLANMIS_ESIK = 6.0
# Bu %'nin USTUNDE hareket -> yeni katalizor olsa bile ASIRI uzamis, kovalanmaz
# (v1 tuzagi: zirvedeki uzamis hisseyi kovalama). TUPRS Doviz sinyali +13.48%
# bu esigin ustunde -> yine BEKLE; Petrol sinyali +7.83% arasinda -> kismi firsat.
_ASIRI_ESIK = 12.0

# Is 2: YENI/ani eskalasyon izleri (baslikta) — 'bugun DEGISEN' bir sey
_YENI_KATALIZOR_IZ = (
    "vurdu", "vurus", "saldiri", "saldir", "kapan", "kapatti", "kesildi", "kesti",
    "ambargo", "yaptirim", "batti", "patlama", "grev", "ilk kez", "rekor kir",
    "acil", "ihlal", "iptal", "durdur", "tahliye", "seferber", "savas ilan",
    "isgal", "bombal", "vurdular", "alarm", "sert dus", "cakildi", "coktu",
)
# Suregelen trend / rutin fiyat anlatimi izleri — 'devam eden'
_DEVAM_IZ = (
    "yukseliyor", "yukselisini surduruyor", "surduruyor", "suruyor", "devam ediyor",
    "artmaya devam", "tirmanmaya", "yukselise gecti", "yukseldi", "geriledi",
    "yukselisini", "yatay", "sinirli", "hafif",
)

_TR = str.maketrans("ıİşŞğĞüÜöÖçÇ", "iissgguuoocc")


def _norm(s: str) -> str:
    return (s or "").translate(_TR).lower()


def yeni_katalizor_mu(baslik: str) -> bool:
    """Baslik YENI/ani bir eskalasyon mu (True), yoksa suregelen trend devami mi
    (False)? Kelime-tabanli. NET DEGILSE False -> temkinli (v1 tuzagina dusme:
    belirsizde kovalama yok)."""
    n = _norm(baslik)
    yeni = sum(1 for k in _YENI_KATALIZOR_IZ if k in n)
    devam = sum(1 for k in _DEVAM_IZ if k in n)
    return yeni > 0 and yeni >= devam


def golge_karar_v2(yon: str, guc: str, fiyatlanmislik_olcum: str,
                   hareket, baslik: str) -> tuple:
    """Is 2 GÖLGE karar (yeni-katalizor ayrimiyla). CANLI DEGIL.
    Doner: (karar, gerekce). Kararlar: AL / AL_KISMI / BEKLE.

    Mantik:
      - yon yukari + guc>=orta degilse -> BEKLE.
      - deterministik 'fiyatlanmis' DEGILSE -> AL (mevcut golge ile ayni).
      - 'fiyatlanmis' AMA yeni katalizor VE hareket < _ASIRI_ESIK -> AL_KISMI
        (yeni ivme, kismi firsat; zirveyi kovalamiyor cunku asiri degil).
      - 'fiyatlanmis' + (yeni katalizor yok VEYA asiri uzamis) -> BEKLE
        (v1 tuzagi: uzamis trend, kovalama)."""
    if yon != "yukari" or guc not in ("orta", "guclu"):
        return "BEKLE", "yon/guc yetersiz"
    hrk = abs(hareket) if isinstance(hareket, (int, float)) else 0.0
    if fiyatlanmislik_olcum != "fiyatlanmis":
        return "AL", "deterministik fiyatlanmamis"
    if yeni_katalizor_mu(baslik) and hrk < _ASIRI_ESIK:
        return "AL_KISMI", f"yeni katalizor + hareket {hrk:.1f}% (<{_ASIRI_ESIK}) -> kismi firsat"
    if hrk >= _ASIRI_ESIK:
        return "BEKLE", f"asiri uzamis {hrk:.1f}% (>= {_ASIRI_ESIK}) -> kovalama yok"
    return "BEKLE", f"suregelen trend devami (yeni katalizor yok), hareket {hrk:.1f}%"


def tema_yasi(ticker: str, konu: str, tarih: str = None, gun: int = 7) -> int:
    """Is 5: bir hisse+konu icin son N gunde kac AYRI gun sinyal cikti (tema kac
    gundur suruyor). AI'a konviksiyon baglami olarak verilir. CANLI DEGIL."""
    from src.db import database as db
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    _tz = ZoneInfo("Europe/Istanbul")
    son = tarih or datetime.now(_tz).strftime("%Y-%m-%d")
    try:
        ilk = (datetime.strptime(son, "%Y-%m-%d") - timedelta(days=gun)).strftime("%Y-%m-%d")
        with db.get_conn() as c:
            n = c.execute(
                "SELECT COUNT(DISTINCT tarih) FROM haber_sinyal "
                "WHERE ticker=? AND konu=? AND tarih BETWEEN ? AND ?",
                (ticker.upper().replace(".IS", ""), konu, ilk, son)).fetchone()[0]
        return int(n or 0)
    except Exception:
        return 0
