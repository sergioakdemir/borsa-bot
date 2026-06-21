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

MODEL = "claude-sonnet-4-6"

ONBOARDING_SYSTEM = (
    "Sen 25 yillik tecrubeli bir Turk borsa uzmanisin. Kullaniciyi bir yatirimci "
    "olarak gercekten anlamak istiyorsun. Dogal, samimi ve sicak konus; anket/form "
    "gibi degil, gercek bir sohbet gibi. Jargon (RSI/MACD) kullanma.\n"
    "Sirayla sunlari ogren (her seferinde tek-iki soru, sohbetin akisina gore): "
    "portfoy buyuklugu, aylik birikim, risk toleransi, yatirim vadesi, nakit ihtiyaci, "
    "panik egilimi (dususte ne yaparsin), tecrube seviyesi, ana hedef, kayip toleransi. "
    "Kullanici cevap verdikce dogal gecislerle ilerle, verdigi bilgiyi kisa onayla. "
    "Eksik alan kalirsa ilerleyen mesajlarda dogal sekilde tekrar sor. Kullanici "
    "bilmiyorum derse zorlamadan gec. Kisa, sohbet tonunda yaz (markdown/yildiz yok). "
    "Yeterince bilgi toplandiysa kibarca 'seni artik daha iyi taniyorum' diyerek "
    "ozetleyip bitir."
)

_EXTRACT_SYSTEM = (
    "Bir sohbetten yatirimci profili cikaran bir ayikla motorusun. Sana kullanici ile "
    "uzman arasindaki sohbet verilir. SADECE asagidaki JSON'u dondur, baska metin yazma. "
    "Bir alani sohbetten net cikaramiyorsan null birak (UYDURMA). Para degerlerini sayi "
    "yaz (orn '250 bin' -> 250000). Enum alanlarda yalniz verilen secenekleri kullan.\n"
    '{\n'
    '  "portfoy_buyuklugu": number|null,        // TL toplam portfoy\n'
    '  "aylik_birikim": number|null,            // TL aylik eklenebilen\n'
    '  "ek_sermaye_mumkun": true|false|null,\n'
    '  "tecrube_seviyesi": "yeni"|"orta"|"tecrubeli"|null,\n'
    '  "risk_toleransi": "dusuk"|"orta"|"yuksek"|null,\n'
    '  "panik_egilimi": "dusuk"|"orta"|"yuksek"|null,   // dususte panikleme egilimi\n'
    '  "yatirim_vadesi": "1ay"|"3ay"|"6ay"|"1yil"|"uzun"|null,\n'
    '  "nakit_ihtiyaci": "dusuk"|"orta"|"yuksek"|null,\n'
    '  "nakit_ihtiyac_tarihi": string|null,     // varsa yaklasik tarih/aciklama\n'
    '  "ana_hedef": string|null,                // kisa: emeklilik, ev, kisa vade kar...\n'
    '  "kayip_toleransi_yuzde": number|null,     // kabul edebilecegi max % kayip\n'
    '  "ogrenme_seviyesi": "baslangic"|"orta"|"ileri"|null,\n'
    '  "aciklama_ister": true|false|null         // detayli aciklama ister mi\n'
    '}'
)

_ENUM = {
    "tecrube_seviyesi": {"yeni", "orta", "tecrubeli"},
    "risk_toleransi": {"dusuk", "orta", "yuksek"},
    "panik_egilimi": {"dusuk", "orta", "yuksek"},
    "yatirim_vadesi": {"1ay", "3ay", "6ay", "1yil", "uzun"},
    "nakit_ihtiyaci": {"dusuk", "orta", "yuksek"},
    "ogrenme_seviyesi": {"baslangic", "orta", "ileri"},
}
_FLOAT = {"portfoy_buyuklugu", "aylik_birikim", "kayip_toleransi_yuzde"}
_BOOL = {"ek_sermaye_mumkun", "aciklama_ister"}
_STR = {"nakit_ihtiyac_tarihi", "ana_hedef"}


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
        model=MODEL, max_tokens=400, system=system, messages=msgs)
    return "".join(getattr(b, "text", "") for b in resp.content
                   if getattr(b, "type", "") == "text").strip()


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
            model=MODEL, max_tokens=500, system=_EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": f"Sohbet:\n{transcript}"}])
        text = "".join(getattr(b, "text", "") for b in resp.content
                       if getattr(b, "type", "") == "text").strip()
        i, j = text.find("{"), text.rfind("}")
        data = json.loads(text[i:j + 1]) if i >= 0 and j > i else {}
    except Exception:
        data = {}
    alanlar = _coerce(data)
    if alanlar:
        return db.upsert_profile(kullanici_id, **alanlar)
    return db.get_profile(kullanici_id) or {"kullanici_id": kullanici_id,
                                            "profil_guven_skoru": 0}
