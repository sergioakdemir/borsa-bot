# Yedekleme ve bekleyen manuel işler

*Son güncelleme: 23 Temmuz 2026*

22 Temmuz 2026 denetiminde sistemde **çalışan hiçbir yedek olmadığı** bulundu.
Bu belge neyin düzeltildiğini, neyin insan eli beklediğini ve o adımların tam
olarak nasıl yapılacağını anlatır.

---

## Durum özeti

| Yedek yolu | Durum | Ne gerekiyor |
|---|---|---|
| Yerel günlük tarball | ✅ **ÇALIŞIYOR** (23 Tem'de kuruldu) | — |
| Google Drive gece yedeği | ❌ Kurulamıyor | Aşağıdaki A veya B |
| Hetzner haftalık snapshot | ❌ Token yok | Aşağıdaki C |
| FRED (Fed faizi — yedek değil ama aynı sınıf eksik) | ❌ Anahtar yok | Aşağıdaki D |

---

## ✅ Çalışan: yerel günlük yedek

`src/ops/yerel_yedek.py` — her gece **02:00** (cron).

- **Nereye:** `/root/yedek/borsa-bot-yedek-YYYY-MM-DD.tar.gz` (dizin `0700`, dosya `0600`)
- **Ne:** `data/borsa.db` (sqlite online backup API ile tutarlı kopya) + `.env` +
  `config/*` + `data/*.json` + canlı `crontab` + 1 MB altı loglar
- **Saklama:** 7 gün, eskisi otomatik silinir
- **Doğrulama:** her koşuda tarball **açılır** ve içindeki DB `integrity_check`'ten
  geçirilir — "dosya oluştu" yeterli sayılmaz
- **Alarm:** başarısızlıkta Telegram'a kritik uyarı; ayrıca son başarılı yedek
  2 günden eskiyse alarm (cron hiç koşmasa bile fark edilir)

```bash
# elle çalıştır
cd /root/borsa-bot && venv/bin/python -m src.ops.yerel_yedek

# durum
cd /root/borsa-bot && venv/bin/python -m src.ops.yerel_yedek durum
```

**Geri yükleme:**

```bash
cd /tmp && mkdir geri && tar -xzf /root/yedek/borsa-bot-yedek-2026-07-23.tar.gz -C geri
sqlite3 geri/data/borsa.db "PRAGMA integrity_check;"   # 'ok' bekleriz
systemctl stop borsa-web
cp geri/data/borsa.db /root/borsa-bot/data/borsa.db
systemctl start borsa-web
```

> ⚠️ **Bu yedek aynı diskte durur.** Yanlış silme / bozulma / hatalı migration'dan
> korur; **disk veya sunucu arızasından KORUMAZ.** Sunucu dışı yedek için aşağıdaki
> A/B/C adımları hâlâ gerekli.

---

## ❌ A. Google Drive — neden kurulamadı

Servis hesabı (`borsabot@robust-habitat-500122-v8.iam.gserviceaccount.com`) çalışıyor,
kimlik geçerli, Drive API açık. Ama **servis hesaplarının kendi depo kotası yoktur.**

23 Tem 2026'da canlı doğrulandı:

```
storageQuota: {'limit': '0', 'usage': '0'}
Klasör oluşturma  : ✅ başarılı (klasör 0 bayt yer kaplar)
Dosya yükleme     : ❌ HTTP 403
   "Service Accounts do not have storage quota. Leverage shared drives,
    or use OAuth delegation instead."
```

Yani `GOOGLE_DRIVE_FOLDER_ID`'yi doldurmak **tek başına yetmez** — normal bir Drive
klasörünü servis hesabıyla paylaşsanız bile yüklenen dosyanın sahibi servis hesabı
olur ve kota hatası aynen tekrarlar.

### Çözüm A1 — Google Workspace varsa (en temiz)

Paylaşılan Sürücü'de (Shared Drive) dosyaların sahibi *sürücüdür*, servis hesabı değil.

1. drive.google.com → sol menü **Paylaşılan sürücüler** → **Yeni**
   → ad: `borsa-bot-yedek`
   *(Bu seçenek görünmüyorsa Workspace hesabınız yok → A2'ye geçin.)*
2. Sürücüye sağ tık → **Üye ekle** →
   `borsabot@robust-habitat-500122-v8.iam.gserviceaccount.com` → yetki **İçerik yöneticisi**
3. Sürücüyü aç, adres çubuğundaki ID'yi kopyala:
   `https://drive.google.com/drive/folders/`**`BURASI_ID`**
4. Sunucuda:
   ```bash
   cd /root/borsa-bot
   echo 'GOOGLE_DRIVE_FOLDER_ID=BURASI_ID' >> .env
   venv/bin/python -c "
   from src.ops import drive_sync
   print('yuklendi:', drive_sync.upload('/root/yedek/borsa-bot-yedek-2026-07-23.tar.gz', verbose=True))"
   ```
   `yuklendi: True` görmeli ve dosya Drive'da görünmeli.

### Çözüm A2 — Kişisel Gmail (Workspace yok)

Paylaşılan Sürücü yok, o yüzden **servis hesabı yerine kullanıcı OAuth'u** gerekir.
Serhat/Yiğit'ten biri kendi Drive kotasına yazma izni verir:

1. console.cloud.google.com → proje `robust-habitat-500122-v8`
2. **API'ler ve Servisler → OAuth izin ekranı** → tür **Harici** → uygulama adını
   doldur → **Test kullanıcıları**na kendi Gmail adresini ekle
3. **Kimlik bilgileri → Kimlik bilgisi oluştur → OAuth istemci kimliği** →
   tür **Masaüstü uygulaması** → JSON'u indir
4. JSON'u sunucuya `config/drive_oauth_client.json` olarak koy ve haber ver —
   `drive_sync.py`'yi OAuth refresh-token akışına çevireceğim (bir kerelik tarayıcı
   onayı gerekir, sonrası otomatik).

> Bu iş bana geldiğinde `drive_sync.py`'de kod değişikliği gerekir; şu anki hâli
> yalnız servis hesabı destekliyor.

### Çözüm A3 — Drive'dan tamamen vazgeç (en hızlı)

Sunucu dışı yedek için Drive şart değil. Hetzner snapshot (C) zaten sunucu dışıdır
ve tek bir token'la çözülür. Drive'ı atlayıp **C'yi** yapmak, A2'nin OAuth
zahmetinden daha az iş ve daha güçlü koruma sağlar.

---

## ❌ B. Hetzner haftalık snapshot — token gerekiyor

`src/ops/snapshot.py` hazır ve doğru; **22 Haziran'dan beri her Pazar** şu satırla
başarısız:

```
[2026-07-19 23:00] HATA: HETZNER_API_TOKEN ayarli degil (.env).
```

Sunucu ID (`135951738`) otomatik bulunuyor, tek eksik token.

**Serhat/Yiğit'in yapacağı:**

1. console.hetzner.cloud → ilgili projeyi seç
2. Sol altta **Güvenlik (Security)** → **API tokens** sekmesi
3. **Generate API token** → açıklama: `borsa-bot-snapshot`
4. İzin: **Read & Write** *(snapshot oluşturmak için Read yetmez)*
5. Token yalnız **bir kez** gösterilir — kopyala
6. Sunucuda:
   ```bash
   cd /root/borsa-bot
   echo 'HETZNER_API_TOKEN=BURAYA_TOKEN' >> .env
   echo 'HETZNER_SNAPSHOT_KEEP=4' >> .env      # en yeni 4 snapshot kalsın (opsiyonel)
   venv/bin/python -m src.ops.snapshot          # elle test — snapshot oluşmalı
   ```
7. Doğrula: Hetzner panel → **Snapshots** → `borsa-botu-haftalik-2026-07-XX` görünmeli

> 💰 Hetzner snapshot ücretlidir (kabaca GB başına aylık ~0,01 €). Disk 14 GB
> kullanımda → 4 snapshot ≈ aylık 0,5 € civarı. Ucuz sigorta.

---

## ❌ C. FRED (Fed politika faizi) — ücretsiz anahtar

Yedekle ilgisi yok ama aynı "sessiz eksik" sınıfında, o yüzden burada.

`FRED_API_KEY` hiç tanımlanmadığı için kodda gömülü sabit `5.25` **gerçek veri gibi**
AI'a besleniyordu. 23 Tem'de bu kaldırıldı — artık veri yoksa
`fed_faiz = None` ve prompt'ta *"VERİ YOK — bu konuda yorum yapma"* yazıyor.
Yani sistem artık **yanlış bilmiyor**, ama **bilmiyor**. Gerçek veri için:

1. https://fredaccount.stlouisfed.org/apikeys → hesap aç (ücretsiz, e-posta yeter)
2. **Request API Key** → kullanım amacı: kişisel/araştırma
3. Anahtar anında verilir (32 karakterlik hex)
4. Sunucuda:
   ```bash
   cd /root/borsa-bot
   echo 'FRED_API_KEY=BURAYA_ANAHTAR' >> .env
   venv/bin/python -c "
   from src.ai.commentary import _load_dotenv; _load_dotenv()
   from src.news.macro import _fred_fed_funds
   print('FRED:', _fred_fed_funds())"
   ```
   `(4.33, -25.0)` gibi `(faiz, değişim_bp)` görmeli — `(None, None)` değil.

Anahtar konduğu anda `get_macro()` `fed_faiz`'i doldurur, `bilinmeyen_veriler`'den
düşer ve Fed sürpriz analizi kendiliğinden devreye girer. **Kod değişikliği gerekmez.**

---

## ❌ D. KAP proxy güvenliği — doğrulanamadı

`KAP_PROXY_URL=http://94.138.216.192:8888` (tinyproxy/1.11.0) **kimlik doğrulaması
olmadan** çalışıyor. Tek koruması TR VPS'teki IP kısıtı olmalı — ama bunu bu
sunucudan doğrulayamadım:

- SSH erişimi yok: `root@94.138.216.192: Permission denied (publickey,password)`
- Farklı kaynak IP'den test: residential proxy 8888'i engelliyor (403), TR VPS'e hiç ulaşmıyor

Açık proxy bırakılırsa spam/kötüye kullanım için kullanılır ve IP'niz kara listeye
girer — KAP erişimi de o gün ölür.

**TR VPS'te (94.138.216.192) çalıştırılacak:**

```bash
# 1) tinyproxy sadece BOSS IP'sine mi açık?
grep -iE "^Allow|^Listen|^Port|^BasicAuth" /etc/tinyproxy/tinyproxy.conf

#    BEKLENEN:
#      Allow 138.199.146.205
#    TEHLİKELİ (böyleyse düzeltilmeli):
#      Allow 0.0.0.0/0   ya da   hiç Allow satırı yok

# 2) güvenlik duvarı
ufw status verbose
#    8888 yalnız 138.199.146.205'e açık olmalı:
#      8888/tcp   ALLOW IN   138.199.146.205

# 3) Açıksa kapat:
sudo ufw allow from 138.199.146.205 to any port 8888 proto tcp
sudo ufw deny 8888/tcp
sudo systemctl restart tinyproxy

# 4) Kötüye kullanım izi var mı (dış IP'lerden istek):
sudo grep -oE "connect from [0-9.]+" /var/log/tinyproxy/tinyproxy.log \
  | sort | uniq -c | sort -rn | head -20
#    138.199.146.205 dışında IP çoksa proxy açık kalmış demektir
```

Bu çıktıları bana iletirseniz değerlendirip gerekirse sıkılaştırma yazarım.

---

## Bekleyen işler (öncelik sırası)

| # | İş | Kim | Süre | Neden önemli |
|---|---|---|---|---|
| 1 | Hetzner API token (B) | Serhat/Yiğit | ~5 dk | **Tek sunucu-dışı yedek yolu.** Şu an disk ölürse her şey gider |
| 2 | KAP proxy açık mı (D) | Serhat/Yiğit | ~5 dk | Açıksa IP kara listeye girer, KAP tamamen ölür |
| 3 | FRED anahtarı (C) | Serhat | ~3 dk | Fed verisi şu an tamamen yok (yanlış değil, ama yok) |
| 4 | Drive A1/A2/A3 kararı | Serhat/Yiğit | — | Hetzner yapılırsa düşük öncelik |
