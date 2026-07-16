# Borsa MCP emekli edildi (17 Tem 2026)

Borsa MCP artık **birincil veri kaynağı değildir** ve bir izleme/alarm hedefi
olmaktan çıkarılmıştır.

## Neden

MCP'nin devrettiği işlevler yedek kaynaklara taşındı ve orada kararlı çalışıyor:

| İşlev          | Yeni birincil kaynak            |
|----------------|---------------------------------|
| Fiyat          | yfinance (+ SMA/fiyat_cache)    |
| KAP bildirimi  | KAP proxy (+ fallback proxy)    |
| Makro veri     | EVDS / FRED                     |

MCP canlılık kontrolü gün boyu gereksiz "Borsa MCP ölü / mcp_yanit_yok" kırmızı
alarmı üretiyordu. MCP'nin ölü olması artık bir arıza değil, bu yüzden alarm
kaldırıldı.

## Ne değişti

- `src/ops/health_monitor.py`: `_kontrol_mcp` kontrolü ve `mcp_yanit_yok`
  alarmı kaldırıldı (core kontrol listesinden çıkarıldı).
- `src/ops/saglik_karnesi.py`: `_mcp_calisiyor`, "Borsa MCP ölü" kırmızı koşulu,
  karne mesajındaki "Borsa MCP" satırı ve `mcp` metriği kaldırıldı.
- `src/web/templates/saglik.html` + `src/web/app.py`: sağlık panelinden "Borsa
  MCP" satırı kaldırıldı.

## MCP tamamen silinmedi — sessiz yedek olarak duruyor

`src/web/app.py` içindeki `_mcp_price_series` / `_price_series` yolları hâlâ
duruyor: yfinance'in yanlış fiyatladığı fon/BYF sembolleri ve yfinance'in boş
döndüğü durumlar için MCP **sessiz yedek** olarak çağrılır. Çalışırsa kullanılır,
çalışmazsa sessizce yfinance'e düşülür — hiçbir alarm üretmez.
