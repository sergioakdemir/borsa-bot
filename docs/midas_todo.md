# Midas Atlas WS Entegrasyonu — TODO (proxy bekliyor)

**Durum:** Mekanizma tamamen çözüldü ve entegrasyona hazır, ama **Türk residential/VPS proxy bekliyor.** O gelene kadar askıda.

## Engel

`cf_clearance`, Turnstile'ı çözen tarayıcının **IP + User-Agent** çiftine bağlanır. borsa-bot sunucusu datacenter IP'sinden (`138.199.146.205`) çıktığı için Cloudflare **403** veriyor — hem WS hem token endpoint'i reddediyor. Empirik olarak doğrulandı (2026-07-11).

## Çözüm

Hem Turnstile çözümü hem WS bağlantısı **aynı Türk residential/mobil proxy egress IP**'sinden geçmeli (sticky session — IP sabit kalmalı, rotating olmaz).

> Not: `.env`'de zaten bir Türk proxy var — `KAP_PROXY_URL=…@tr.decodo.com:40001` (Decodo/Smartproxy TR). KAP için kullanılıyor. Midas için de kullanılabilir AMA cf_clearance IP-binding'i için **sticky (sabit IP) oturum** şart; rotating endpoint her istekte IP değiştirirse cf_clearance kırılır. Decodo'nun sticky-session portu/parametresiyle test edilmeli.

## Çözülmüş mekanizma (referans)

- **Token / refresh:** `POST https://api.atlas.getmidas.com/sso-bff/v1/oauth2/web/token`
  - Header: `Content-Type: application/x-www-form-urlencoded`
  - Body: `grant_type=refresh_token` (başka parametre YOK)
  - refresh_token **httpOnly cookie**'de taşınır (BFF deseni). SPA `refreshLeadSeconds:60` ile süresi dolmadan 60 sn önce yeniler.
- **WebSocket:** `wss://ws.atlas.getmidas.com/ws` — **Centrifugo** (centrifuge-js SDK).
  - Connect frame'de **JWT şart**: `{"connect":{"token":"<access_token>","name":"js"},"id":1}`
  - Abone ol: `{"subscribe":{"channel":"trade-tr-THYAO-mi-instant"},"id":2}`
- **access_token:** 15 dk ömürlü JWT. Elle aktarım için çok kısa; refresh cookie ile programatik yenilenmeli.
- **Yetki:** Hesapta `TR_INSTANT_MARKET_DATA` + `US_INSTANT_MARKET_DATA` var, MKK ACTIVE → gerçek zamanlı BIST verisi erişilebilir. Tek engel CF egress.
- **Veri tazeliği:** `-mi-instant` = anlık tick; yfinance'in ~15 dk BIST gecikmesine karşı kategorik üstün.

## Proxy geldiğinde yapılacaklar

1. Türk residential/VPS sticky proxy'yi `.env` `KAP_PROXY_URL`'e (veya ayrı `MIDAS_PROXY_URL`'e) ekle.
2. Turnstile'ı **o proxy egress IP'sinden** çöz; `MIDAS_ACCESS_TOKEN`, `MIDAS_CF_CLEARANCE`, `MIDAS_CF_BM`, `MIDAS_REFRESH_COOKIE`, `MIDAS_UA`'yı `.env`'e ekle.
3. `midas_probe.py`'yi **proxy ile** yeniden koş — CF geçerse refresh + WS akışı doğrulanır.
4. Doğrulanınca: kalıcı entegrasyonu `src/` altına yaz (token auto-refresh döngüsü + Centrifugo reconnect + subscribe yönetimi).

## Notlar

- Teşhis aracı (kalıcı değil): `scratchpad/midas_probe.py` — JWT decode, refresh testi, WS connect+subscribe.
- SPA config kaynağı: `atlas.getmidas.com` HTML içindeki `window.__PUBLIC_CONFIG__`.
