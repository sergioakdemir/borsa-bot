# Analiz Yol Haritası — ölçüm altyapısı ve ertelenmiş istatistik

Bu belge, karar motoru üzerinde **hangi analizin ne zaman** yapılacağını kayıt altına alır.
Amaç: az veriyle erken hüküm vermeyi önlemek, ve "hangi analizi yapmıştık / neden
ertelemiştik" sorusunu sonradan tahmine bırakmamak.

---

## Durum (21 Temmuz 2026)

- **v2.1 dondurmada.** Karar eşikleri, filtreler, vetolar ve prompt DEĞİŞTİRİLMEYECEK.
- v2.1 dönemi 16 Temmuz'da başladı → bugün itibarıyla **~5 işlem günü**.
- v2.1 kararlarının **hiçbiri** 5 günlük ileri getiri ufkuna ulaşmadı.
- Bugüne kadar ölçülen ve **karar için kullanılmayacak kadar zayıf** bulgular:
  - Puan ölçeği v1→v2/v2.1 arasında ~1 puan aşağı kaydı (eşik değişmeden efektif
    seçicilik ~3 kat arttı).
  - Eşik 8→7 simülasyonu iki dönemde de negatif alpha üretti (örneklem küçük).
  - Yeni ölçekte puan-getiri ilişkisi monoton görünmüyor (puan 6 bandı puan 8'i
    geçiyor) — **doğrulanması gereken bir şüphe, henüz bulgu değil.**

---

## ŞİMDİ KURULU OLAN (21 Temmuz 2026)

`scripts/rank_ic_izleme.py` — salt-okur ölçüm scripti. Karar motoruna dokunmaz.

Kapsam:
1. **Look-ahead assertion'ları** (rapor değil, test): `feature_snapshot_ts <=
   decision_ts`, `price_entry_ts > decision_ts`, `return_end_ts > price_entry_ts`.
   İhlalde script tüm çıktıyı bastırır ve `TEST FAILED - RAPOR GEÇERSİZ` yazar.
   Giriş fiyatı = **karar günü açılışı** (karardan sonraki ilk işlem yapılabilir
   fiyat). Aynı günün kapanışı giriş olarak asla kullanılmaz.
2. **Tekil işlem defteri**: FLAT→OPEN→CLOSED. Açık pozisyonda gelen AL yeni işlem
   sayılmaz; yalnız BEKLE→AL geçişi gerçek giriştir.
3. **İşlem maliyeti**: 0 / 10 / 25 bp tek yön (gidiş+dönüş uygulanır).
4. **Günlük Rank IC**: Spearman, tie handling = ortalama sıra. Gün ancak
   `>=20 geçerli ticker`, `>=3 farklı puan`, `eksik ileri getiri <%20` şartlarını
   sağlarsa hesaplanır; aksi halde `INSUFFICIENT_CROSS_SECTION` olarak işaretlenir
   ve **ortalamaya katılmaz**.
5. **Örneklem dürüstlüğü**: her tabloda n; `n<10` → `GÜVENİLMEZ` etiketi.

Çıktı: `logs/rank_ic_gunluk.csv`
```
date, strategy_version, prompt_version, watchlist_version, horizon,
ticker_count, valid_ticker_count, unique_score_count, ic,
market_return, market_regime, durum
```
`prompt_version` = o tarihe kadar `src/ai/commentary.py`'ye dokunan son commit SHA.
`watchlist_version` = o günkü evrenin `adet:hash` özeti.
Bu iki alan Ağustos'ta **"puan mı, prompt mu, evren mi değişti"** sorusunu ayırmak
için zorunludur; metadata olmadan IC serisi yorumlanamaz.

Çalıştırma: `python scripts/rank_ic_izleme.py`
Aynı `(date, horizon)` tekrar çalıştırılırsa satır **güncellenir**, çift sayım olmaz.

---

## AĞUSTOS ORTASINA ERTELENDİ

**Ön koşul (İKİSİ BİRDEN):**
- v2.1 en az **~30 işlem günü** biriktirmiş olmalı
- v2.1 **dondurması bitmiş** olmalı

Bu koşullar sağlanmadan aşağıdakilerin hiçbiri çalıştırılmayacak. Az veriyle
üretilen bu tür istatistikler kendilerine olduğundan fazla güven telkin eder.

| Analiz | Neden erteledik |
|---|---|
| **Newey-West düzeltmesi** | IC serisi otokorelasyonlu; ~8 gözlemle HAC standart hata anlamsız |
| **Blok bootstrap** | Blok uzunluğu seçmek için yeterli seri yok |
| **ICIR** (IC ortalaması / std) | Payda ~8 gözlemle son derece gürültülü |
| **Holding portföy motoru** | Pozisyon çakışması/ağırlıklandırma; tekil işlem sayısı henüz tek haneli |
| **Full-minus-one attribution** | Hangi sinyalin katkı verdiği — bileşen başına yeterli gözlem yok |
| **Sektör benchmark'ları** | Sektör başına 3-12 hisse; sektör-nötr alpha şu an ölçülemez |
| **Historical-as-live replay** | Prompt/evren sürüm haritası yeni kuruldu; geriye dönük replay için metadata birikmeli |
| **Vade kalibrasyonu** (1/3/5/10g optimal tutma) | Ufuk seçimi aşırı-uydurmaya en açık karar; en son yapılmalı |

**Ağustos ortasında ilk sorulacak soru** (öncelik sırasıyla):
1. v2.1 puanı kesitte ayırt edici mi? (ortalama IC ve işareti, yeterli günle)
2. Eşik nerede olmalı — ölçek kaymasına göre yeniden kalibre edilmeli mi?
3. Ölçek kayması kasıtsızdı; bırakılacak mı, düzeltilecek mi? (bu bir **karar**,
   kaza olarak kalmamalı)

---

## Değişiklik kaydı

- **2026-07-21** — belge oluşturuldu; `scripts/rank_ic_izleme.py` kuruldu ve bir kez
  çalıştırıldı. v2.1 dondurmada, karar motorunda değişiklik yok.
