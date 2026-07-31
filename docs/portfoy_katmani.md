# Portföy katmanı — 4 kapı

*Son güncelleme: 31 Temmuz 2026*

## Neden eklendi

v2.1'in dört AL kararı (ASML, TSM — 22 Tem; NVDA — 24 Tem; TURSG — 22 Tem)
**dördü de stop'la** kapandı, ortalama **−%6.5**. Otopside her karar tek başına
savunulabilir çıktı (güçlü temel, olumlu haber, eşiği geçen puan) — ama birlikte
alındıklarında ortaya çıkan şey **tek yönlü, korelasyonlu, negatif beklenen
değerli bir bahisti.**

Otopsinin sayıları:

| | Beta | Stop | Stopu tetikleyen endeks hareketi | Gerçekleşen |
|---|---|---|---|---|
| ASML | 1.87 | −4.56% | **−2.44%** | QQQ −3.29% ✔ |
| TSM | 1.60 | −5.00% | **−3.13%** | QQQ −3.29% ✔ |
| TURSG | 0.98 | −3.31% | −3.38% | XU100 −3.19% ~sınır |
| NVDA | 1.07 | −4.42% | −4.13% | QQQ −0.31% ✘ |

Yani ASML ve TSM için **piyasanın normal bir düzeltmesi tek başına stopu
süpürmeye yetiyordu**; hisse hiçbir şey yapmasa bile. TSM'in kaybı zaten saf
betaydı (alfa +0.01).

Sistemin bunu görecek hiçbir mekanizması yoktu: tek tek hisseyi değerlendiriyor,
**portföyü hiç değerlendirmiyordu.**

## Kapılar

### 1. Negatif EV kapısı — `commentary._apply_karar_filtreleri` 0e

EV artık `bilgi` değil **kapı**: `EV < 0` ise AL üretilmez, BEKLE'ye düşer,
denetim izine `takildi` + gerçek EV değeri yazılır.

Önceki hâlin asimetrisi kritikti: EV'nin **tek** etkisi `EV > 1.5` ise pozisyonu
**büyütmekti** (`_pozisyon_kademe`). Yani sistem "kazanma ihtimali yüksek"
dediğinde daha çok risk alıyor, "kaybetme ihtimali yüksek" dediğinde **aynı**
riski alıyordu. 4 stop vakasının üçünde EV karar anında negatifti
(ASML −1.206, TSM −2.662, NVDA −0.308) ve hiçbiri engellenmedi.

**EV hesaplanamazsa (None) kapı uygulanmaz** — eksik veri yüzünden sağlam AL'ları
düşürmek bu kapının amacı değil.

### 2. Sektör tavanı ABD'ye açıldı — `learning.SEKTOR_HISSE` + `_TAVAN_SEKTORLER`

`SEKTOR_HISSE`'de **tek bir ABD hissesi yoktu**; `_sektor_of` ABD için hep `None`
dönüyor, tavan ABD defterinde **fiilen kapalı** kalıyordu. Eklenen eşlemeler:

| Sektör | Hisseler |
|---|---|
| Yarı İletken (ABD) | NVDA, AMD, TSM, ASML, MU, OSS |
| Kuantum (ABD) | IONQ, RGTI |
| Uzay/Havacılık (ABD) | RKLB, SPCX, RKTO, ACHR |
| Sağlık Teknolojisi (ABD) | BFLY |
| Kripto/Fintech (ABD) | CNCK |
| BT Hizmetleri (ABD) | RXT |

Ayrıca **Sigorta** BIST tavan listesine eklendi (TURSG/ANSGR/RAYSG tavan
dışındaydı). Tavan: sektör başına günde **2 AL**.

QQQ/VOO gibi geniş endeks ETF'leri **bilerek eşlenmedi** — bir sektör değiller;
yığılmalarını korelasyon freni yakalar.

### 3. Korelasyon freni — `_KORELASYON_ESIGI = 0.60`, `_KORELASYON_GUN = 60`

Sektör tavanı yalnız **etiketi** aynı olanları yakalar. Oysa "aynı bahis" olmak
için aynı etiketi taşımak gerekmez: ASML yarı iletken *ekipmanı*, TSM *dökümhane*,
NVDA *çip tasarımı* — farklı iş modelleri, ama günlük getiri korelasyonları
0.47–0.77.

Bu fren doğrudan **fiyat davranışına** bakar: aynı gün hayatta kalan AL adaylarının
son 60 günlük günlük-getiri korelasyonu eşiği aşıyorsa yalnız **en yüksek EV'li**
olan geçer, diğerleri BEKLE'ye düşer.

**Fail-open:** fiyat verisi çekilemezse fren uygulanmaz (geçici bir ağ hatası
sağlam AL'ları düşürmesin); iz `uygulanmadi` yazar.

### 4. Beta-bilinçli stop raporu — **kapı değil, şeffaflık**

`stop mesafesi / (beta × endeksin tipik günlük oynaklığı)` oranı hesaplanır.
Oran **< 1.5** ise karar gözlemlerine ve denetim izine şu uyarı düşer:

> Uyarı: stop piyasa gürültüsüne dar (stop/beta-oynaklık oranı X < 1.5);
> endeksin normal bir günlük hareketi bile stopu tetikleyebilir.

Kararı **değiştirmez**. Amaç, "beta 1.87 olan bir hisseye %4.5 stop koymak,
*endeks %2.4 düşerse çıkarım* demektir" gerçeğini karar anında görünür kılmak.

## 4 stop vakası yeni kapılardan geçirilince

`tests/test_portfoy_katmani.py` (13/13):

```
ENGELLENEN: 3/4 -> ['ASML', 'TSM', 'NVDA']
  ASML   BEKLE  | EV izi=takildi (-1.206)
  TSM    BEKLE  | EV izi=takildi (-2.662)
  NVDA   BEKLE  | EV izi=takildi (-0.308)
  TURSG  AL     | EV gecti (+2.912) | stop/gürültü=2.03
```

Negatif EV kapısı tek başına 3'ünü durduruyor. EV'ler pozitif olsaydı bile
sektör tavanı + korelasyon freni devreye giriyor (izole testte 3 ABD adayından
yalnız 1'i hayatta kaldı: NVDA tavana, TSM ASML ile 0.77 korelasyonla frene
takıldı).

Kalan tek AL (TURSG) gerçekte de dördün **en az kaybedeniydi** (−%4.96).

---

## BİLİNEN SINIR — stop seviyeden değil kapanıştan uygulanıyor

**Durum:** `update_trades` bir pozisyonu stop'la kapatırken, kapanış fiyatı olarak
**günün kapanışını** yazıyor; stop **seviyesini** değil. Gün içinde stop seviyesi
delinse bile pozisyon o seviyeden değil, seansın sonundaki fiyattan kapanmış
sayılıyor.

**Ölçülen etki** (4 stop vakası):

| | Tasarlanan stop | Gerçekleşen kayıp | Fark |
|---|---|---|---|
| ASML | −4.56% | **−8.12%** | 3.56 puan |
| TSM | −5.00% | −6.01% | 1.01 puan |
| NVDA | −4.42% | −5.87% | 1.45 puan |
| TURSG | −3.31% | −4.66% | 1.35 puan |
| **ortalama** | **−4.32%** | **−6.16%** | **1.84 puan** |

Yani risk yönetimi kâğıt üstünde −4.3% planlıyor, defterde −6.2% gerçekleşiyor.
Pozisyon boyutlandırma (`VARSAYILAN_RISK_TL`) tasarlanan stop mesafesini
kullandığı için **işlem başına riske edilen tutar sistematik olarak aşılıyor.**

**Neden burada duruyor:** doğru çözüm gün içi (intraday) fiyat takibiyle stop
seviyesinden çıkış kaydetmek. Bu, günlük bar yerine dakikalık/tick verisi ve
ayrı bir izleme koşusu gerektiriyor — ayrı bir iş. Bu belge, rakamın **bilerek**
böyle olduğunu ve k/z karnelerinin bu kadar kötümser okunması gerektiğini
kayıt altına alır.

**Geçici okuma kuralı:** kapanan işlemlerin k/z'sine bakarken, stop'la kapanan
her işlemde ~1.5–2 puanlık ekstra kaybın bu mekanikten geldiğini varsay.
