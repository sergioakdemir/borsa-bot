# Midas Yerine Bot — Yol Haritası (Analiz)

> Görev 4: Kod yazılmadı; yalnızca mevcut kod tabanı + Midas karşılaştırması üzerinden analiz.
> Tarih: 2026-06-24.

## Özet sonuç
Bot, Midas'ın **danışman/karar destek** katmanını çoktan geçti (AI yorumu, öğrenme,
senaryo takibi, alarmlar, paper trading, model portföy). Midas'ın botta **olmayan**
asıl işlevi **emir iletimi (alım/satım yürütme)** ve buna bağlı **anlık fiyat +
otomatik portföy senkronu**. Bunlar lisans/altyapı gerektirir; bot bir aracı kurum
değil. Dolayısıyla gerçekçi hedef: **Midas'ı tamamen değiştirmek değil**, "kararı bot
ver, emri Midas'ta gir" sürtünmesini en aza indirmek.

## 1. Midas'ta olup botta olmayan özellikler

| Özellik | Midas | Bot (bugün) | Önem |
|---|---|---|---|
| Emir iletimi (al/sat yürütme) | ✅ komisyonsuz ABD / düşük BIST | ❌ sadece tavsiye | Kritik (lisans gerekir) |
| Anlık (canlı) fiyat | ✅ gerçek zamanlı | ⚠️ yfinance ~15 dk gecikmeli (BIST) | Yüksek |
| Otomatik portföy senkronu | ✅ emir sonrası anında | ⚠️ manuel giriş / ekran görüntüsü OCR'ı (`/api/portfolio/parse-image`) | Yüksek |
| Kesirli pay (ABD) | ✅ | — (sadece takip) | Orta |
| Nakit/bakiye yönetimi | ✅ | ❌ | Düşük (danışman için gereksiz) |
| Anlık emir defteri / derinlik | ✅ | ❌ | Düşük |

Botun Midas'ta **olmayan** üstünlükleri (korunmalı): AI karar + sade gerekçe,
karar karnesi/öğrenme (`decisions` + `learning.py`), şartlı senaryo bildirimi,
fiyatlanmamış KAP haberi yakalama, stop-loss & fiyat alarmı, paper trading +
100K model portföy, BIST-100'e göre başarı ölçümü.

## 2. Anlık fiyat için ücretsiz alternatifler

- **yfinance**: BIST'te ~15 dk gecikmeli; ABD'de pratikte ~gerçek zamanlıya yakın.
  Şu an birincil kaynak.
- **bigpara (Hürriyet)**: Kodda zaten kullanılıyor (`_bigpara_price`, şu an yalnız
  `GMSTR.F`). BIST için gecikmesi yfinance'ten düşük; **tüm BIST sembollerine
  genişletilebilir** — en düşük maliyetli "anlık fiyata yakın" iyileştirme.
- **Investing/Mynet** scraping: gecikme düşük ama kırılgan (HTML değişimi).
- **TradingView / resmi veri yayıncıları**: gerçek zamanlı ama TOS/lisans engeli;
  ücretsiz ve yasal kalıcı çözüm değil.
- **Sonuç**: Gerçek tick verisi ücretsiz+yasal olarak zor. Uygulanabilir adım:
  **bigpara'yı birincil BIST fiyat kaynağı yapmak** (yfinance'i yedek bırakarak)
  → gecikmeyi 15 dk'dan birkaç dakikaya indirir, kod altyapısı zaten mevcut.

## 3. Kullanıcı deneyimi açısından en kritik eksik

**Portföyün gerçeği yansıtmaması.** Kullanıcı Midas'ta işlem yapıyor ama bot bunu
bilmiyor; portföy manuel/gecikmeli güncelleniyor. Bu, botun tüm kişisel
çıktısını (zarar uyarısı, stop-loss, sabah brifingi PORTFÖY bölümü) zayıflatan
tek nokta. (Bu turda eklenen "aldım/sattım hatırlatması" ve "3 gündür
güncellenmedi" uyarısı bu sürtünmeyi azaltmaya yönelik ilk adım.)

İkincil kritik eksik: **fiyat gecikmesi** — "anlık" kararı zayıflatır (madde 2).

### Önerilen sıra (kod yazılmadan, öncelik)
1. **Portföy senkron sürtünmesini azalt**: Midas ekran görüntüsü OCR akışını öne
   çıkar (zaten var), "güncel mi?" hatırlatmaları (bu turda eklendi).
2. **bigpara'yı tüm BIST'e yay** → fiyat gecikmesini düşür.
3. Emir iletimi gerçekten istenirse: lisanslı bir aracı kurumun API'siyle
   entegrasyon (büyük hukuki/teknik iş) — ya da bot "danışman", Midas "uygulayıcı"
   konumlandırmasında kalsın.
