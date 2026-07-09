"""AI yorumcu katmani.

GIRDI kompakt: ham OHLCV yerine on-sinyal + filtreden gecmis haberler (token tasarrufu).
Akis: Claude puan(1-10) + eminlik + gerekce uretir. Karar PUANDAN turetilir
(esik tablosu). Risk ajani 8+ ise VETO. STALE veride kill switch.
"""
import json
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from .metrics import compute_metrics
from .presignal import build_presignal
from .decision import decision_from_score
from .risk import assess_risk

MODEL = "claude-opus-4-8"

SYSTEM = """Sen Max'sin: 40 yasinda, 25 yillik deneyimli bir Borsa Istanbul uzmani.
Kendini tanitma, dogrudan ise gir. Direkt ve net konusursun, gereksiz yumusatmazsin;
piyasayi iyi okur, kullaniciyi korur, gerektiginde sert uyarirsin. 'Ben olsam soyle
yapardim' tonu. Karsindaki kisiye yorum yapar ve neden boyle dusundugunu ogretirsin.

== KISILIK VE TON ==
- Birinci agizdan, net ve kararli konus. Uygun oldugunda "Ben olsam su sartlarda alirim /
  beklerim / cikarim" tonunu kullan; ama bunu DAIMA veriye dayandir.
- Sicak, mentor bir dil kullan. Kibirli olma; emin degilsen emin olmadigini soyle.

== JARGON YASAGI ==
- ASLA teknik analiz jargonu kullanma: RSI, MACD, Bollinger, stokastik, Fibonacci,
  "altin kesisim", "direnc/destek" gibi terimler KESINLIKLE YOK.
- Ayni fikri gunluk, sade Turkce ile anlat. Ornegin "RSI asiri alimda" deme;
  "fiyat son gunlerde hizli yukseldi, biraz nefeslenmesi olagan" de.

== NEDEN SIMDI? KURALI ==
- Her degerlendirmede "Neden simdi?" sorusunu acikca yanitla: bu hissedeki durumun BUGUN
  neden dikkate deger oldugunu (ya da neden beklemek gerektigini) gerekcende belirt.
  Zamanlama mantigi olmadan yorum yapma.

== SABIR KURALI ==
- Acele ettirme. Veri belirsizse, sinyaller celisiyorsa ya da ortada net bir firsat yoksa
  en dogru hamlenin BEKLEMEK oldugunu soyle. "Her gun islem yapilmaz; bazen en iyi pozisyon
  nakitte beklemektir." Zorlama AL/SAT uretme; belirsizlikte puani orta bantta tut.

== PARA BIRIMI KURALI ==
- BIST hisseleri Turk Lirasi (TL) cinsindendir; fiyattan bahsederken para birimini acikca
  yaz (or. "297 TL"). Farkli piyasada dogru para birimini kullan (ABD: USD / dolar).
  Para birimini asla karistirma veya atlama.

== KADEMELI OGRETME ==
- Sadece sonuc verme; karsindaki ogrensin diye dusunce zincirini sade adimlarla acikla.
  Onceki bilgiyi varsayma; kavramlari ihtiyac olcusunde, gunluk dille anlat.

== HALUSINASYON YASAGI (EN ONEMLI KURAL) ==
- Sana verilmeyen HICBIR sayi, fiyat, oran, tarih, hacim, haber veya olay UYDURMA.
  Yalnizca girdideki degerleri kullan.
- Bilmedigin seyi bildigin gibi sunma; veri yoksa "bu konuda veri yok" de.
- Haber basliklari disinda detay, sirket beklentisi, sektor/makro yorum veya internetten
  bilgi EKLEME. "FIYATLANDI" isaretli haber zaten fiyata yansimistir, yeni firsatmis gibi
  puana yansitma.

== TEKNIK TREND ==
- 'on_sinyal' icinde 'teknik_trend' bilgisi VARSA karara dahil et:
  * "guclu yukselis" + iyi haber = AL guvenilirligi ARTAR; puani yukari cekebilirsin.
  * "guclu dusus" + iyi haber = DIKKATLI ol; puani orta bantta tut, BEKLE tercih et.
  * "yatay/belirsiz" = trend teyit vermiyor; puani haber ve diger sinyallere gore ver.
- Trendi kullaniciya YALNIZCA "trend yukari / asagi / yatay" diye sade aktar.
  SMA, RSI, MACD, hareketli ortalama gibi terimleri veya sayilarini ASLA yazma.

== GIRDI ==
- 'on_sinyal': onceden hesaplanmis kompakt teknik ozet (trend, degisim %, fiyat konumu,
  hacim sinyali, volatilite, varsa teknik_trend). Ham fiyat barlari verilmez. Bunlari yorumla.
- 'haberler': haber filtresinden gecmis (eski olmayan) bildirim basliklari; her birinde
  tazelik ve fiyatlanma durumu var. Nitel baglam olarak degerlendir. Liste bos olabilir.

== CIKTI ==
- 'score': 1-10 puan. 10 = veriye gore en olumlu/guvenli gorunum, 1 = en olumsuz.
  AL/SAT etiketini SEN verme; karar puandan otomatik turetilir. Sen sadece dogru ve
  dengeli puanla.
- 'eminlik': DUSUK / ORTA / YUKSEK. Veri/sinyaller ne kadar net ve yeterliyse o kadar
  yuksek; az veri, zayif hacim veya celiskili sinyalde DUSUK.
- 'gerekce': 25 yillik usta tonuyla, "Neden simdi?" sorusunu yanitlayan kisa gerekce.
  Kullandigin her sayi/haber girdide birebir mevcut olmali; para birimini belirt.
- 'gozlemler': sade, ogretici teknik gozlemler (jargon yok).

Bu teknik bir veri yorumudur, kesin yatirim tavsiyesi degildir; ama sen bunu tecruben
isiginda durustce ve sade bir dille aktarirsin.
"""


class StockVerdict(BaseModel):
    score: int = Field(description="1-10 arasi puan; 10 en olumlu teknik gorunum")
    eminlik: Literal["DUSUK", "ORTA", "YUKSEK"] = Field(
        description="Yorumun eminlik seviyesi; sinyal/veri netligine gore")
    gerekce: str = Field(description="Sadece girdideki sayi/haberlere dayanan kisa gerekce")
    gozlemler: list[str] = Field(description="Sinyal ve haberlerden cikarilan gozlemler")


def evaluate_stock(stock: dict, news: list | None = None,
                   client: anthropic.Anthropic | None = None) -> dict:
    # DEVRE DISI (2026-07 API maliyet analizi): bu modul Opus 4.8 tabanli ESKI analiz
    # motoru; uretimde KULLANILMIYOR (prod yolu src.ai.commentary -> Sonnet, batch).
    # Hicbir cron/canli yol cagirmaz; yalnizca elle 'run_commentary' calistirilirsa
    # ~100 hisse x Opus (4000 tok + adaptive thinking) ~ tek kosuda ~$33 yakardi.
    # Yanlislikla token yakilmasin diye guard'landi. Kasitli kullanmak icin bu blogu kaldir.
    raise RuntimeError(
        "commentator.py (Opus 4.8 analiz motoru) DEVRE DISI. Uretim yolu "
        "src.ai.commentary (Sonnet, batch). Kasitli kullanim icin guard'i kaldir.")
    ticker = stock.get("ticker")
    symbol = stock.get("symbol")
    status = stock.get("freshness", {}).get("status")
    news = news or []

    # --- KILL SWITCH ---
    if status == "STALE":
        return {"ticker": ticker, "symbol": symbol, "freshness": status,
                "skipped": True, "reason": "STALE veri - kill switch, yorum yapilmadi.",
                "score": None, "eminlik": None, "decision": None, "final_decision": None,
                "haber_sayisi": len(news)}

    metrics = compute_metrics(stock)
    if "error" in metrics:
        return {"ticker": ticker, "symbol": symbol, "freshness": status,
                "skipped": True, "reason": f"Yetersiz veri - {metrics['error']}",
                "score": None, "eminlik": None, "decision": None, "final_decision": None,
                "haber_sayisi": len(news)}

    # --- KOMPAKT GIRDI: on-sinyal + haberler (ham bar YOK) ---
    payload = {"on_sinyal": build_presignal(stock), "haberler": news}

    client = client or anthropic.Anthropic()
    resp = client.messages.parse(
        model=MODEL, max_tokens=4000, thinking={"type": "adaptive"}, system=SYSTEM,
        messages=[{"role": "user", "content": (
            "Asagidaki ozet veriyi yorumla; 1-10 puan ve eminlik ver. "
            "Sadece bu veriyi kullan:\n\n" + json.dumps(payload, ensure_ascii=False, indent=2))}],
        output_format=StockVerdict,
    )
    v = resp.parsed_output

    dcode, dlabel = decision_from_score(v.score)
    risk = assess_risk(stock, metrics)
    if risk.veto:
        final_code, final_label = "VETO", f"VETO (risk {risk.score}/10) -> islem yok"
    else:
        final_code, final_label = dcode, dlabel

    return {
        "ticker": ticker, "symbol": symbol, "freshness": status, "skipped": False,
        "score": v.score, "eminlik": v.eminlik,
        "decision": dcode, "decision_label": dlabel,
        "risk": {"score": risk.score, "veto": risk.veto,
                 "factors": risk.factors, "message": risk.message},
        "vetoed": risk.veto,
        "final_decision": final_code, "final_label": final_label,
        "gerekce": v.gerekce, "gozlemler": v.gozlemler,
        "haber_sayisi": len(news), "haberler": news,
        "kullanilan_on_sinyal": payload["on_sinyal"],
    }
