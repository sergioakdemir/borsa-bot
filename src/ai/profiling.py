"""Onboarding + profil cikarimi (Claude ile dogal sohbet).

- onboarding_reply(): 25 yillik uzman personasiyla dogal, samimi sohbet eder ve
  sirayla yatirimci profilini ogrenmeye calisir (form gibi degil).
- extract_profile_from_chat(): sohbet gecmisini Claude'a verip yapilandirilmis
  profil JSON cikarir ve DB'ye kaydeder (yalniz emin olunan alanlar).

Profil alanlari kullanici_profil tablosuyla birebir; guven skoru DB tarafinda
doluluk oranindan hesaplanir.
"""
import json
import os
from pathlib import Path

# Onboarding sohbeti hizli/ucuz model (Haiku); profil cikarimi dogruluk icin Sonnet.
ONBOARDING_MODEL = "claude-haiku-4-5-20251001"
EXTRACT_MODEL = "claude-sonnet-4-6"
MODEL = EXTRACT_MODEL          # geriye donuk uyumluluk

ONBOARDING_SYSTEM = (
    "Sen 25 yillik tecrubeli bir Turk borsa uzmanisin. Kullaniciyi bir yatirimci "
    "olarak gercekten anlamak istiyorsun. Dogal, samimi ve sicak konus; anket/form "
    "gibi DEGIL, gercek bir sohbet gibi. Jargon (RSI/MACD) kullanma. Her seferinde "
    "tek-iki soru sor, kullanici cevap verince kisa onayla ve dogal bir gecisle "
    "devam et. Bir cevaptan birden fazla bilgi cikarsa tekrar sorma.\n"
    "Sohbet boyunca (sirayla degil, akisa gore) sunlarin HEPSINI ogrenmeye calis:\n"
    "- Portfoy buyuklugu\n- Aylik birikim kapasitesi\n- Ek sermaye koyabilir mi\n"
    "- Borsada kac yildir var (tecrube)\n- Daha once buyuk zarar yasadi mi, ne hissetti\n"
    "- %10 dususte ne yapar (bekler/satar/alir)\n- %20 dususte ne yapar\n"
    "- Kisa vade mi uzun vade mi (1ay/3ay/6ay/1yil/3yil+)\n"
    "- Yakin vadede nakit ihtiyaci var mi\n"
    "- Ana hedef: hizli kazanc / korunma / uzun vadeli buyume\n"
    "- Hangi sektorleri takip ediyor\n- Gunde kac saat borsayla ilgileniyor\n"
    "- En buyuk korkusu (kayip / firsat kacirmak / belirsizlik)\n"
    "- Daha once basarili bir yatirim yapti mi, ne hissetti\n"
    "- Risk/odul tercihi: az kazanc az risk mi, cok kazanc cok risk mi\n"
    "Duygusal sorularda (zarar/basari deneyimi, korku) empatik ol, yargilamadan dinle. "
    "Kullanici bilmiyorum derse zorlamadan gec. Eksik kalan konulari ilerleyen "
    "mesajlarda dogal sekilde tekrar sor. Kisa, sohbet tonunda yaz (markdown/yildiz "
    "yok). Yeterince taniyinca kibarca 'seni artik daha iyi taniyorum' deyip ozetle.\n\n"
    "SON ADIM (profili yeterince ogrendikten sonra): kullaniciya Telegram bildirimlerini "
    "ac diye sor, AYNEN su yonergeyle: 'Son olarak Telegram bildirimlerini acmak ister "
    "misin? Acmak icin: 1) Telegram'da @usy_borsa_takip_bot'u ac, 2) /start yaz, 3) Bot "
    "sana bir numara gonderecek, o numarayi buraya yaz. Numara olmadan da devam "
    "edebilirsin.' Kullanici numarayi yazarsa tesekkur et; yazmazsa zorlamadan bitir."
)

_EXTRACT_SYSTEM = (
    "Bir sohbetten yatirimci profili cikaran bir ayikla motorusun. Sana kullanici ile "
    "uzman arasindaki sohbet verilir. SADECE asagidaki JSON'u dondur, baska metin yazma. "
    "Bir alani sohbetten net cikaramiyorsan null birak (UYDURMA). Para degerlerini sayi "
    "yaz (orn '250 bin' -> 250000). Enum alanlarda yalniz verilen secenekleri kullan.\n"
    '{\n'
    '  "portfoy_buyuklugu": number|null,        // TL toplam portfoy\n'
    '  "aylik_birikim": number|null,            // TL aylik eklenebilen\n'
    '  "ek_sermaye_mumkun": true|false|null,    // ek para koyabilir mi\n'
    '  "tecrube_seviyesi": "yeni"|"orta"|"tecrubeli"|null,  // borsada kac yil\n'
    '  "risk_toleransi": "dusuk"|"orta"|"yuksek"|null,\n'
    '  "panik_egilimi": "dusuk"|"orta"|"yuksek"|null,   // dususte panikleme\n'
    '  "yatirim_vadesi": "1ay"|"3ay"|"6ay"|"1yil"|"3yil"|"uzun"|null,\n'
    '  "nakit_ihtiyaci": "dusuk"|"orta"|"yuksek"|null,  // yakin vadede nakit\n'
    '  "nakit_ihtiyac_tarihi": string|null,\n'
    '  "ana_hedef": "hizli_kazanc"|"korunma"|"uzun_vadeli_buyume"|null,\n'
    '  "kayip_toleransi_yuzde": number|null,     // kabul edebilecegi max % kayip\n'
    '  "dusus_tepkisi_10": "bekler"|"satar"|"alir"|null,   // %10 dususte ne yapar\n'
    '  "dusus_tepkisi_20": "bekler"|"satar"|"alir"|null,   // %20 dususte ne yapar\n'
    '  "sektor_tercihi": string|null,           // takip ettigi sektorler (virgullu)\n'
    '  "gunluk_takip_saat": number|null,        // gunde kac saat borsayla ilgilenir\n'
    '  "ana_korku": "kayip"|"firsat_kacirma"|"belirsizlik"|null,\n'
    '  "onceki_basari": string|null,            // basarili/zararli deneyimi + hissi (kisa)\n'
    '  "risk_tercihi": "az_kazanc_az_risk"|"cok_kazanc_cok_risk"|"dengeli"|null,\n'
    '  "ogrenme_seviyesi": "baslangic"|"orta"|"ileri"|null,\n'
    '  "aciklama_ister": true|false|null,\n'
    '  "telegram_id": number|null   // kullanicinin yazdigi 7-15 haneli Telegram numarasi (varsa)\n'
    '}'
)

_ENUM = {
    "tecrube_seviyesi": {"yeni", "orta", "tecrubeli"},
    "risk_toleransi": {"dusuk", "orta", "yuksek"},
    "panik_egilimi": {"dusuk", "orta", "yuksek"},
    "yatirim_vadesi": {"1ay", "3ay", "6ay", "1yil", "3yil", "uzun"},
    "nakit_ihtiyaci": {"dusuk", "orta", "yuksek"},
    "ogrenme_seviyesi": {"baslangic", "orta", "ileri"},
    "ana_hedef": {"hizli_kazanc", "korunma", "uzun_vadeli_buyume"},
    "dusus_tepkisi_10": {"bekler", "satar", "alir"},
    "dusus_tepkisi_20": {"bekler", "satar", "alir"},
    "ana_korku": {"kayip", "firsat_kacirma", "belirsizlik"},
    "risk_tercihi": {"az_kazanc_az_risk", "cok_kazanc_cok_risk", "dengeli"},
}
_FLOAT = {"portfoy_buyuklugu", "aylik_birikim", "kayip_toleransi_yuzde",
          "gunluk_takip_saat"}
_BOOL = {"ek_sermaye_mumkun", "aciklama_ister"}
_STR = {"nakit_ihtiyac_tarihi", "sektor_tercihi", "onceki_basari"}


def _load_dotenv():
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _to_anthropic(messages):
    """[{rol/role, metin/content/text}] -> anthropic messages."""
    out = []
    for m in messages or []:
        rol = m.get("rol") or m.get("role") or "user"
        rol = "assistant" if rol in ("assistant", "bot") else "user"
        icerik = m.get("metin") or m.get("content") or m.get("text") or ""
        if icerik:
            out.append({"role": rol, "content": str(icerik)})
    return out


def _transcript(messages) -> str:
    sat = []
    for m in messages or []:
        rol = m.get("rol") or m.get("role") or "user"
        ad = "Uzman" if rol in ("assistant", "bot") else "Kullanici"
        icerik = m.get("metin") or m.get("content") or m.get("text") or ""
        if icerik:
            sat.append(f"{ad}: {icerik}")
    return "\n".join(sat)


def onboarding_reply(messages, profile=None, client=None) -> str:
    """Sohbet gecmisine gore uzmanin bir sonraki dogal yanitini uretir."""
    import anthropic
    _load_dotenv()
    client = client or anthropic.Anthropic()
    msgs = _to_anthropic(messages)
    if not msgs:
        msgs = [{"role": "user", "content": "Merhaba"}]
    system = ONBOARDING_SYSTEM
    if profile:
        bilinen = {k: profile.get(k) for k in (
            "portfoy_buyuklugu", "risk_toleransi", "yatirim_vadesi", "nakit_ihtiyaci",
            "panik_egilimi", "tecrube_seviyesi", "ana_hedef") if profile.get(k) is not None}
        if bilinen:
            system += ("\n\nKullanici hakkinda zaten bildiklerin (tekrar sorma, "
                       f"eksikleri sor): {json.dumps(bilinen, ensure_ascii=False)}")
    resp = client.messages.create(
        model=ONBOARDING_MODEL, max_tokens=400, system=system, messages=msgs)
    return "".join(getattr(b, "text", "") for b in resp.content
                   if getattr(b, "type", "") == "text").strip()


def _telegram_id(v):
    """7-15 haneli Telegram numarasini int olarak dondurur (gecersizse None)."""
    if v is None:
        return None
    digits = "".join(ch for ch in str(v) if ch.isdigit())
    if 7 <= len(digits) <= 15:
        return int(digits)
    return None


def _coerce(d: dict) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if v is None:
            continue
        if k in _ENUM:
            if str(v).lower() in _ENUM[k]:
                out[k] = str(v).lower()
        elif k in _FLOAT:
            try:
                out[k] = float(v)
            except (ValueError, TypeError):
                pass
        elif k in _BOOL:
            out[k] = 1 if (v is True or str(v).lower() in ("true", "evet", "1")) else 0
        elif k in _STR:
            s = str(v).strip()
            if s:
                out[k] = s
    return out


def extract_profile_from_chat(kullanici_id, messages, client=None) -> dict:
    """Sohbetten profil alanlarini cikarir ve DB'ye kaydeder. Guncel profili dondurur."""
    from src.db import database as db
    import anthropic
    _load_dotenv()
    client = client or anthropic.Anthropic()
    transcript = _transcript(messages)
    if not transcript.strip():
        return db.get_profile(kullanici_id) or {}
    try:
        resp = client.messages.create(
            model=EXTRACT_MODEL, max_tokens=500, system=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": f"Sohbet:\n{transcript}"}])
        text = "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", "") == "text").strip()
        i, j = text.find("{"), text.rfind("}")
        data = json.loads(text[i:j + 1]) if i >= 0 and j > i else {}
    except Exception:
        data = {}
    alanlar = _coerce(data)
    tid = _telegram_id(data.get("telegram_id"))
    if alanlar:
        profile = db.upsert_profile(kullanici_id, **alanlar)
    else:
        profile = db.get_profile(kullanici_id) or {"kullanici_id": kullanici_id,
                                                   "profil_guven_skoru": 0}
    # telegram_id'yi DB'ye degil, geri donen profile gecici alan olarak koy;
    # asil guncellemeyi onboarding_step() update_telegram_id ile yapar.
    if tid:
        profile = {**(profile or {}), "telegram_id": tid}
    return profile
