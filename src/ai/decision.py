"""Karar kelimeleri: tek dogru kaynak (single source of truth).

Kullaniciya gosterilen SADECE 5 karar kelimesi vardir:
    AL · TUT · BEKLE · AZALT · UZAK DUR

UZAK DUR = portfoyde yoksa "girme", portfoyde varsa SAT ile esdeger.

Sistemin ic karar kodlari (final_decision) korunur (AL, TUT, SAT, GUCLU_SAT,
AZALT, BEKLE, VETO, KILL_SWITCH...); bu modul onlari kullaniciya gosterilen 5
kelimeye ve emoji/aksiyon metnine cevirir. Tum Telegram mesajlari bu modulu
kullanir, boylece karar dili tek yerden yonetilir.

EMOJI:  🟢 olumlu · 🟡 bekle/dikkat · 🔴 risk
"""

# Puan -> ic karar kodu esik tablosu (commentator.py + backtest kullanir).
#   7-10 = AL · 6 = BEKLE (acele etme) · 4-5 = TUT · 3 = AZALT · 1-2 = UZAK_DUR


def decision_from_score(score: int) -> tuple[str, str]:
    """(ic_kod, gosterilen_kelime) dondurur. Gosterilen kelime 5'liden biridir."""
    if score >= 7:
        return "AL", "AL"
    if score >= 6:
        return "BEKLE", "BEKLE"
    if score >= 4:
        return "TUT", "TUT"
    if score >= 3:
        return "AZALT", "AZALT"
    return "UZAK_DUR", "UZAK DUR"


# Ic karar kodu -> kullaniciya gosterilen 5 kelimeden biri.
# (Eski/legacy kodlar da haritalanir; yeni AI ciktisi dogrudan AZALT/UZAK_DUR verir.)
_KELIME = {
    "AL": "AL",
    "AL_TEMKINLI": "BEKLE",
    "TUT": "TUT",
    "BEKLE": "BEKLE",
    "AZALT": "AZALT",
    "SAT": "AZALT",           # legacy: zayif sat -> azalt
    "GUCLU_SAT": "UZAK DUR",  # legacy: guclu sat -> uzak dur
    "UZAK_DUR": "UZAK DUR",
    "VETO": "UZAK DUR",       # risk vetosu kullaniciya "uzak dur" olarak gosterilir
}

# Gosterilen kelime -> emoji (sadece izinli 4 emoji + karar emojileri)
_EMOJI = {
    "AL": "🟢",
    "TUT": "🟡",
    "BEKLE": "🟡",
    "AZALT": "🔴",
    "UZAK DUR": "🔴",
}

# 5 kelimenin gecerli kume (disari acik)
KARARLAR = ("AL", "TUT", "BEKLE", "AZALT", "UZAK DUR")


def karar_kelime(final_decision: str) -> str | None:
    """Ic karar kodunu kullaniciya gosterilen kelimeye cevirir.
    KILL_SWITCH gibi 'gosterme' durumlarinda None doner."""
    d = (final_decision or "").upper().strip()
    if not d or "KILL" in d:
        return None
    return _KELIME.get(d, "TUT")


def karar_emoji(final_decision: str) -> str:
    """Ic karar kodu icin emoji (gosterilen kelimeye gore)."""
    k = karar_kelime(final_decision)
    return _EMOJI.get(k, "🟡")


def aksiyon_metni(final_decision: str, portfoyde: bool = False) -> str:
    """Karar icin NET yonlendirme cumlesi. UZAK DUR portfoy durumuna gore degisir."""
    k = karar_kelime(final_decision)
    if k == "AL":
        return "Kademeli alım yapılabilir."
    if k == "TUT":
        return "Pozisyonu koru."
    if k == "BEKLE":
        return "Acele etme, teyit için bekle."
    if k == "AZALT":
        return "Pozisyonu kademeli azalt."
    if k == "UZAK DUR":
        return "Pozisyondan çık (sat)." if portfoyde else "Girme, uzak dur."
    return "Pozisyonu koru."
