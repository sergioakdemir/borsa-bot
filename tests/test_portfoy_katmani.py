"""Portfoy katmani (4 kapi) — 4-stop otopsisi vakalariyla regresyon testi.

31 Tem 2026 otopsisi: v2.1'in dort AL'i (ASML/TSM 22 Tem, NVDA 24 Tem,
TURSG 22 Tem) dordu de stop'la kapandi, ortalama -%6.5. Otopsi uc yapisal
bosluk buldu:
  1) EV dordunden ucunde NEGATIFTI (ASML -1.206, TSM -2.662, NVDA -0.308)
     ama EV yalnizca "bilgi" idi; hicbirini engellemedi.
  2) Sektor tavani ABD defterinde FIILEN KAPALIYDI (SEKTOR_HISSE'de tek bir
     ABD hissesi yoktu) -> ASML+TSM+NVDA ayni temada acildi.
  3) Sektor etiketi ayni olmasa bile fiyat davranisi ayni olabiliyor
     (korelasyon 0.47-0.72) -> etiket bazli tavan yetmiyor.

Bu test, KAYITLI gercek girdilerle (karar_denetim'den birebir) yeni kapilarin
bu dort karari engelleyip engellemedigini olcer.

Calistir:  ./venv/bin/python tests/test_portfoy_katmani.py
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ai import commentary as C
from src.ai import presignal

SONUC = []


def kontrol(ad, kosul, detay=""):
    SONUC.append(bool(kosul))
    print(f"[{'GECTI' if kosul else 'KALDI'}] {ad} {detay}")


def kayit(t, mk, puan, risk_ai, eq, em, hab, ev, giris, stop):
    """22/24 Tem karar_denetim kayitlarindaki DEGERLERIN birebir aynisi."""
    return {"ticker": t, "market": mk, "final_decision": "AL", "karar": "AL",
            "karar_ham": "AL", "score": puan, "risk": {"score": risk_ai},
            "entry_quality": {"skor": eq}, "eminlik": em, "haber_sayisi": hab,
            "expected_value": {"ev": ev}, "giris_seviyesi": giris,
            "stop_loss": stop, "skipped": False,
            "haberler": [{"tazelik": "YENI"}] * hab}


def vakalar():
    return [
        kayit("ASML", "us", 8, 5, 67, "Yüksek", 3, -1.206, 1801.51, 1719.43),
        kayit("TSM", "us", 8, 6, 60, "Orta", 3, -2.662, 424.61, 403.38),
        kayit("NVDA", "us", 8, 5, 62, "Orta", 3, -0.308, 208.76, 199.53),
        kayit("TURSG", "bist", 8, 6, 85, "Orta", 6, 2.912, 6.65, 6.43),
    ]


def iz(r, motor):
    for k in (r.get("_denetim") or []):
        if k.get("motor") == motor:
            return k
    return {}


# O gunku breadth (%38 notr) — bugunku canli deger testi kirletmesin.
presignal.market_breadth = lambda: {"oran": 38.0, "guclu": 35,
                                    "toplam": 92, "durum": "nötr"}

print("=== 4-STOP VAKASI, YENI KAPILARDAN GECIRILDI ===")
sonuc = C._apply_karar_filtreleri(copy.deepcopy(vakalar()), verbose=True)
son = {r["ticker"]: r for r in sonuc}
print()
for t in ("ASML", "TSM", "NVDA", "TURSG"):
    r = son[t]
    print(f"  {t:6} {r['final_decision']:6} | EV izi={iz(r,'Beklenen değer (EV)').get('sonuc')}"
          f" | sektör={iz(r,'Sektör tavanı').get('sonuc')}"
          f" | korelasyon={iz(r,'Korelasyon freni').get('sonuc')}"
          f" | stop/gürültü={iz(r,'Stop/gürültü oranı').get('deger')}")
print()

engellenen = [t for t, r in son.items() if r["final_decision"] != "AL"]
print(f"ENGELLENEN: {len(engellenen)}/4 -> {engellenen}")
print()

# --- KAPI 1: negatif EV --------------------------------------------------
for t in ("ASML", "TSM", "NVDA"):
    kontrol(f"K1 {t}: negatif EV kapisi calisti",
            iz(son[t], "Beklenen değer (EV)").get("sonuc") == "takildi"
            and son[t]["final_decision"] == "BEKLE",
            f"-> EV izi={iz(son[t],'Beklenen değer (EV)').get('deger')}")
kontrol("K1 TURSG: pozitif EV -> kapiya takilmadi",
        iz(son["TURSG"], "Beklenen değer (EV)").get("sonuc") == "gecti",
        f"-> EV={iz(son['TURSG'],'Beklenen değer (EV)').get('deger')}")

# --- KAPI 2: sektor tavani ABD'ye --------------------------------------
from src.ai.learning import _sektor_of
kontrol("K2 ABD sektor eslemesi eklendi (ASML/TSM/NVDA = ayni sektor)",
        _sektor_of("ASML") == _sektor_of("TSM") == _sektor_of("NVDA")
        == "Yarı İletken (ABD)",
        f"-> {_sektor_of('ASML')}")
kontrol("K2 Sigorta tavan listesine eklendi (TURSG)",
        _sektor_of("TURSG") == "Sigorta" and "Sigorta" in C._TAVAN_SEKTORLER)

# Sektor tavani IZOLE test: EV'leri pozitif yapip yalniz tavani calistir.
izole = vakalar()
for r in izole:
    r["expected_value"] = {"ev": 3.0}          # EV kapisini devre disi birak
s2 = {r["ticker"]: r for r in C._apply_karar_filtreleri(copy.deepcopy(izole), verbose=False)}
abd_al = [t for t in ("ASML", "TSM", "NVDA") if s2[t]["final_decision"] == "AL"]
kontrol("K2 izole: ayni sektorde 3 AL -> en fazla 2'si gecer",
        len(abd_al) <= C._SEKTOR_AL_TAVANI,
        f"-> AL kalan: {abd_al} (tavan {C._SEKTOR_AL_TAVANI})")

# --- KAPI 3: korelasyon freni ------------------------------------------
# IZOLE: yalniz ASML + TSM (sektor tavani 2'ye takilmasin, EV pozitif olsun).
# Gercek korelasyonlari ~0.72-0.76 -> ikincisi frene takilmali.
ikili = [r for r in vakalar() if r["ticker"] in ("ASML", "TSM")]
for r in ikili:
    r["expected_value"] = {"ev": 3.0 if r["ticker"] == "ASML" else 2.0}
s3 = {r["ticker"]: r for r in C._apply_karar_filtreleri(copy.deepcopy(ikili),
                                                        verbose=False)}
kor_iz = iz(s3["TSM"], "Korelasyon freni")
kontrol("K3 yuksek korelasyonlu ikinci aday frene takildi",
        kor_iz.get("sonuc") == "takildi" and s3["TSM"]["final_decision"] == "BEKLE",
        f"-> TSM: {kor_iz.get('deger')}")
kontrol("K3 EV'si yuksek olan (ASML) tutuldu",
        s3["ASML"]["final_decision"] == "AL",
        f"-> ASML: {iz(s3['ASML'],'Korelasyon freni').get('deger')}")

# Dusuk korelasyonlu cift frene TAKILMAMALI (yanlis pozitif kontrolu)
capraz = [r for r in vakalar() if r["ticker"] in ("ASML", "TURSG")]
for r in capraz:
    r["expected_value"] = {"ev": 3.0}
s4 = {r["ticker"]: r for r in C._apply_karar_filtreleri(copy.deepcopy(capraz),
                                                        verbose=False)}
kontrol("K3 dusuk korelasyonlu cift (ABD+BIST) frene TAKILMIYOR",
        s4["ASML"]["final_decision"] == "AL" and s4["TURSG"]["final_decision"] == "AL",
        f"-> TURSG korelasyon: {iz(s4['TURSG'],'Korelasyon freni').get('deger')}")

# --- KAPI 4: beta-bilincli stop raporu (kapi DEGIL) ---------------------
# Rapor yalniz AL kalan kayitlar icin uretilir (BEKLE'ye dusenlerde anlamsiz).
oranlar = {t: iz(r, "Stop/gürültü oranı").get("deger")
           for t, r in {**s2, **s3, **s4}.items()
           if r["final_decision"] == "AL"}
kontrol("K4 AL kalan her kayitta stop/gurultu orani hesaplandi",
        oranlar and all(v is not None for v in oranlar.values()),
        f"-> {oranlar}")
kontrol("K4 rapor KAPI DEGIL: oran dusuk olsa bile karar AL kalir",
        all(r["final_decision"] == "AL"
            for r in {**s2, **s3, **s4}.values()
            if iz(r, "Stop/gürültü oranı").get("sonuc") == "takildi"),
        "-> dar bulunanların hiçbiri BEKLE'ye çekilmedi")

# --- Denetim izi metni duzeltildi mi ------------------------------------
bos = {"ticker": "QQQ", "market": "us", "final_decision": "AL", "karar_ham": "AL",
       "score": 8, "risk": {"score": 5}, "_denetim": []}
metin = next((k["aciklama"] for k in C._denetim_tamamla(bos)
              if k["motor"] == "Sektör tavanı"), "")
kontrol("İZ: sektor eslemesi yoksa gercek sebep yaziliyor",
        "eşlemesi tanımlı değil" in metin,
        f"-> '{metin}'")

print(f"\n{sum(SONUC)}/{len(SONUC)} test gecti")
sys.exit(0 if all(SONUC) else 1)
