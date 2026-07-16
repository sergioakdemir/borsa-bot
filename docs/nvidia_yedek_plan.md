# NVIDIA Acil-Durum Yedeği — Plan ve Kurulum (17 Tem 2026)

## Amaç
Anthropic bir gün erişilemez olursa (kredi bitmesi / 429 limit / servis kesintisi),
**yardımcı AI işleri** (haber etiketleme, gölge haber katmanı, alarm metinleri)
NVIDIA'nın **ücretsiz** NIM ucuna düşerek çalışmaya devam etsin — bot tamamen
susmasın. Bu bir **maliyet** projesi değil, **dayanıklılık/kesintisizlik** sigortasıdır.

## Mimari — çift-sağlayıcı sarmalayıcı (`src/ai/saglayici.py`)
```
Yardımcı AI işi (haber/gölge/alarm)
   ↓
[saglayici.json_cagir]  → ÖNCE Anthropic dener
   ├─ Başarılı → sonucu döndür        (NORMAL DURUM: NVIDIA hiç çağrılmaz)
   └─ Hata (429/401/5xx/timeout/kredi freni)
         ↓
      is_tipi YEDEĞE_UYGUN mu? (haber/golge/alarm/senaryo/ozet)
         ├─ EVET + anahtar var → NVIDIA'ya düş, sayaç artır, logla
         ├─ EVET + anahtar YOK → "anahtar bekleniyor", None (iş atlanır)
         └─ HAYIR (ana_karar)  → NVIDIA'ya ASLA düşme, None (iş bekler)
```

## Kalite koruması — ana AL/SAT kararı NVIDIA'ya DÜŞMEZ
- Ana karar `commentary._ai_verdict`'te üretilir ve **saf Anthropic**'tir; yönlendiriciden
  hiç geçmez → yapısal olarak NVIDIA'ya ulaşamaz.
- Ek güvenlik: yönlendiriciye yanlışlıkla `is_tipi="ana_karar"` ile gelirse bile
  `YEDEGE_UYGUN` kümesinde olmadığından NVIDIA denenmez, `None` döner (iş bekler).
- Gerekçe: kalitesiz bir AL/SAT kararı (gerçek para hareketi) hiç karar vermemekten
  daha tehlikelidir. Anthropic yoksa sabah brifingi **bekler**, panel "brifing
  bekleniyor / kredi bitti" der.

## Uygun işler (NVIDIA'ya düşebilir — Haiku seviyesi, kalite-kritik DEĞİL)
| İş | Nokta | is_tipi |
|---|---|---|
| Gölge haber katmanı (yön/güç) | `haber_sinyal._ai_sektor_etki` | `golge` ✅ (bağlandı) |
| Haber → hisse etiketleme | `commentary._haber_etki_analizi` | `haber` (ileride) |
| Alarm metinleri/filtre | `run_alerts._ai_create` | `alarm` (ileride) |
| Senaryo/gündem özeti | `senaryo`, `morning` | `senaryo`/`ozet` (ileride) |

**Kademeli yaklaşım:** İlk entegrasyon yalnız **gölge katman** (sıfır risk — zaten
canlıya/Telegram'a dokunmaz). NVIDIA güvenilirliği gerçek veriyle ölçülüp iyiyse
diğer noktalar da `saglayici.json_cagir`'a çevrilir.

## Anahtar yönetimi
- `.env`'de `NVIDIA_API_KEY=` (boş) → **fallback PASİF**, sistem Anthropic-only
  (mevcut davranış, hiçbir şey bozulmaz).
- Anahtar (`nvapi-...`) konunca → fallback **kendiliğinden aktif**, kod değişikliği yok.

## Model ve uç
- Uç: `https://integrate.api.nvidia.com/v1` (OpenAI-uyumlu, `openai` kütüphanesi).
- Model: `meta/llama-3.3-70b-instruct` (ücretsiz, Türkçe'de yeterli, JSON talimatını
  iyi izler). Alternatif: `deepseek-ai/deepseek-v3` (daha güçlü, daha yavaş).
- Limit: ~40 istek/dk. Botun tepe hacmi ~10/dk → rahat sığar.

## Panel
- `saglik_karnesi.topla()` → `nvidia: {aktif, model, bugun, durum}`.
- Panel "AI Kredisi" kartında **"NVIDIA acil yedek: aktif/pasif · bugün N kez"** satırı.
- Yedek bugün devreye girdiyse özete ve akşam nabzına da eklenir (Anthropic o gün
  sorun yaşadı işareti).

## NVIDIA API anahtarı nasıl alınır (ücretsiz, ~5 dk) — Serhat için
1. Tarayıcıda **build.nvidia.com** aç.
2. Sağ üstten **Login / Sign Up** → Google veya e-posta ile ücretsiz hesap
   (kredi kartı GEREKMEZ).
3. Herhangi bir modeli aç (örn. arama kutusuna "llama 3.3" yaz → *meta / llama-3.3-70b-instruct*).
4. Sağdaki kod panelinde **"Get API Key"** (veya "Generate API Key") düğmesine bas.
5. Üretilen **`nvapi-...`** anahtarını kopyala.
6. Sunucuda `.env` dosyasında `NVIDIA_API_KEY=` satırına yapıştır:
   `NVIDIA_API_KEY=nvapi-xxxxxxxx`
7. Web servisini yeniden başlat: `systemctl restart borsa-web`.
   (Cron işleri `.env`'i her koşuda okur; onlar için restart gerekmez.)
8. Panelde "NVIDIA acil yedek: aktif" görünür. Test için Anthropic çökene kadar
   beklemek gerekmez — anahtar dolu olması yeterli, gerçek devreye giriş yalnız
   Anthropic hata verdiğinde olur.

Anahtar alınana kadar sistem **Anthropic-only** çalışır, hiçbir sorun olmaz.
