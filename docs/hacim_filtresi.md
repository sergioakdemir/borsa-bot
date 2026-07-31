# `Volume > 0` filtresi — neden kaldırıldı, nerede bilerek bırakıldı

*Son güncelleme: 31 Temmuz 2026*

Kod tabanında uzun süre `df[df["Volume"] > 0]` deseni "geçersiz barı ele" amacıyla
kullanıldı. Bu **yanlış bir ölçüt**: Yahoo günlük barı önce `Volume=0` ile yayınlar
ve hacmi saatler sonra konsolide eder. Endekslerde (XU100.IS) hacim **ertesi güne
kadar** 0 kalır; döviz paritelerinde (USDTRY=X) **hiçbir zaman** dolmaz.

Sonuç: hacim filtresi, fiyatı gayet dolu olan **en güncel barı** "hiç yokmuş" gibi
eler ve son bar bir gün geriye düşer. Bu hata üretime **üç kez** çıktı:

| Tarih | Nerede | Sonuç |
|---|---|---|
| 22 Tem 2026 | `ops/update_decisions.py` | Hisse bugünkü barı sayarken endeks saymıyor (asimetri) → 137 kararın `piyasa_farki` NULL kaldı |
| 31 Tem 2026 | `ai/commentary.py` (`market_data`) | Sabah brifinginde watchlist'in son 39 hissesi sahte KILL_SWITCH yedi, yalnız 54 karar üretildi |
| 31 Tem 2026 | tarama sonucu 8 yer daha | Aşağıdaki tablo |

**Doğru ölçüt hacim değil:**
- Bar geçerli mi? → `df["Close"].notna()` (fiyat var mı)
- Bar kapandı mı? → `src/data/freshness.canli_bar_at` (takvim: son bar bugüne aitse
  **ve piyasa hâlâ açıksa** henüz kapanmamıştır)

Hangisinin kullanılacağı çağıranın **canlı barı isteyip istemediğine** bağlıdır.

---

## Düzeltilenler

| Dosya | Ne istiyordu | Uygulanan |
|---|---|---|
| `ai/commentary.py` `market_data` | son **kapanmış** bar | `canli_bar_at` |
| `ops/update_trades.py` | son kapanış (gece 23:36) | `canli_bar_at` |
| `ops/update_paper_trades.py` | son kapanış (23:35) | `canli_bar_at` |
| `ops/update_model_portfoy.py` | son kapanış (23:45) | `canli_bar_at` |
| `portfolio/model.py` `_bist100_getiri` | XU100 son kapanışı | `canli_bar_at` |
| `alerts/engine.py` (2 yer) | **canlı** bar (`is_today` döndürüyor) | filtre kaldırıldı |
| `news/market_overview.py` | **canlı** bar (piyasa yönü) | filtre kaldırıldı |
| `portfolio/engine.py` | **canlı** bar (+ `check_freshness`) | filtre kaldırıldı |
| `web/app.py` (grafik) | **canlı** bar | filtre kaldırıldı |

Regresyon testleri: `tests/test_veri_freni.py`, `tests/test_alerts_hacimsiz_bar.py`.

---

## Bilinçli bırakılanlar — dokunmayın

Aşağıdaki yerlerde `Volume > 0` **kalmıştır ve zararsızdır.** Ortak sebep:
hepsi **geçmiş** veri okur. Bir bar birkaç saat içinde konsolide olduğu için, günler
veya aylar öncesine bakan kodda hacmi 0 olan bar gerçekten tatil/işlemsiz gündür.

| Dosya | Ne yapıyor | Neden zararsız |
|---|---|---|
| `news/priced_in.py:32` | haber tarihi etrafındaki fiyat tepkisi | Pencere geçmişte (haber tarihi + 8 gün); hacim çoktan konsolide |
| `ops/update_haber_etki.py:93` | 30dk/2sa/1gün haber etkisi (23:40) | Geçmiş günlerin barlarını arıyor, güncel bara bakmıyor |
| `backtest/full_chain.py:145` | tam zincir backtest | Tarihsel veri |
| `backtest/engine.py:22` | backtest motoru | Tarihsel veri |
| `backtest/aggressive.py:64` | agresif strateji backtest | Tarihsel veri |
| `backtest/monthly.py:95` | aylık backtest | Tarihsel veri |

Ayrıca filtre **olmayan** iki hacim kullanımı var (bar elemiyorlar, sadece hesap
yapıyorlar) — bunlar zaten doğru:

- `news/fundamental_source.py:219` — ortalama hacim hesabı
- `news/priced_in.py:50` — hacim sıçraması karşılaştırması

---

## Yeni kod yazarken

Günlük bar okuyan yeni bir yol eklerken **`Volume > 0` yazmayın.** Sırasıyla:

```python
df = df[df["Close"].notna()]              # her zaman: fiyatsız bar atılır
df = canli_bar_at(df, symbol=symbol)      # yalnızca "son KAPANIŞ" isteniyorsa
```

`canli_bar_at` `src/data/freshness.py` içindedir ve piyasa takvimini kullanır
(BIST/ABD seans saatleri + TR resmi tatilleri).
