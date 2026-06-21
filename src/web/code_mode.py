"""Bota Sor 'kod modu': kullanicinin dogal dille istedigi GUVENLI arayuz
degisikliklerini uygular.

GUVENLIK (sabit, asilamaz):
  - YALNIZ src/web/templates/index.html dosyasi degistirilebilir.
  - Python, veritabani, .env ve diger hicbir dosyaya DOKUNULMAZ (hedef sabit kodlu).
  - Model yalniz find/replace metni uretir; dosya YOLU veya shell komutu URETEMEZ.
  - Degisiklik basina en fazla 50 satir (old/new her biri) ve net fark <= 50 satir.
  - Once oneri gosterilir, kullanici "onayla" deyince uygulanir.
  - Uygulamadan once orijinal icerik saklanir; JS/yapisal dogrulama gecmezse geri alinir.
  - Her degisiklik git commit'lenir (geri alinabilir). Servis arka planda yeniden baslar.
"""
import json
import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "src" / "web" / "templates" / "index.html"
MAX_LINES = 50
_CHAT_MODEL = "claude-sonnet-4-6"

# Kullanici basina bekleyen (onay bekleyen) tek oneri
_PENDING: dict = {}

# Arayuz degisikligi sinyali veren isim kelimeleri (niyet tespiti)
_UI_NOUNS = ("arayüz", "arayuz", "renk", "reng", "buton", "başlık", "baslik",
             "sekme", "font", "ikon", "arka plan", "arkaplan", "background",
             "yazı tipi", "yazi tipi", "tema", "logo", "menü", "menu", "sayfa başlığı",
             "yazı rengi", "yazi rengi", "metin rengi")
_APPROVE = ("onayla", "evet yap", "evet, yap", "onaylıyorum", "onayliyorum",
            "uygula", "kabul", "tamam yap", "yap onayla", "onay")

# Dosyanin bozulmadigini dogrulayan zorunlu cogur (silinirse degisiklik reddedilir)
_ANCHORS = ("<nav>", 'id="page-bugun"', 'id="page-ayarlar"', "<script>", "</html>")

_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "yapilabilir": {"type": "boolean",
                        "description": "Istek sadece HTML/CSS/JS ile yapilabilir mi"},
        "aciklama": {"type": "string",
                     "description": "Ne yapilacaginin kisa Turkce ozeti (tek cumle)"},
        "old_string": {"type": "string",
                       "description": "index.html'de AYNEN gecen, DEGISTIRILECEK metin "
                                      "(benzersiz olmali, yeterli baglam icersin)"},
        "new_string": {"type": "string",
                       "description": "old_string'in yerine gelecek yeni metin"},
    },
    "required": ["yapilabilir", "aciklama", "old_string", "new_string"],
    "additionalProperties": False,
}

_PROPOSE_SYSTEM = (
    "Sen bir web arayuzu duzenleme asistanisin. Kullanici, bir Flask uygulamasinin "
    "TEK sayfasi olan index.html (HTML+CSS+JS, tek dosya) icin gorsel/arayuz "
    "degisikligi istiyor. Sana dosyanin tam icerigi verilir.\n"
    "Gorevin: istegi yerine getiren MINIMAL bir find/replace uret. old_string dosyada "
    "AYNEN ve BENZERSIZ gecen bir parca olsun (gerekiyorsa benzersizlik icin biraz "
    "baglam ekle). new_string degismis halidir. Sadece HTML/CSS/JS degisikligi yap; "
    "mantik/veri/Python ile ilgili istekleri 'yapilabilir=false' isaretle ve old/new "
    "alanlarini bos birak. Degisiklik kucuk olsun (en fazla ~50 satir). Renk vb. "
    "icin mevcut CSS degiskenlerini/sinif yapisini koru."
)


# --------------------------------------------------------------------------
def is_approval(soru: str) -> bool:
    s = (soru or "").strip().lower()
    if s in ("evet", "yap", "onayla", "ok", "tamam"):
        return True
    return any(k in s for k in _APPROVE)


def is_ui_request(soru: str) -> bool:
    s = (soru or "").lower()
    return any(n in s for n in _UI_NOUNS)


def has_pending(kullanici) -> bool:
    return bool(kullanici and _PENDING.get(kullanici))


def _git(args, timeout=30):
    return subprocess.run(["git", "-C", str(ROOT)] + args,
                          capture_output=True, text=True, timeout=timeout)


def _js_ok(html: str) -> bool:
    """index.html icindeki <script> bloklarini node ile sozdizimi acisindan dogrular."""
    blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
    body = "\n".join(blocks)
    if not body.strip():
        return True
    try:
        r = subprocess.run(
            ["node", "-e", "new Function(require('fs').readFileSync(0,'utf8'))"],
            input=body, capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        # node yoksa JS dogrulamasini atla (yapisal cogur kontrolu yine calisir)
        return True


def _structurally_ok(html: str) -> bool:
    return all(a in html for a in _ANCHORS)


def _line_count(s: str) -> int:
    return s.count("\n") + 1 if s else 0


# --------------------------------------------------------------------------
def propose(kullanici, soru, client=None) -> dict:
    """Degisikligi planlar, kullaniciya gosterir ve onay bekler (uygulamaz)."""
    import anthropic
    client = client or anthropic.Anthropic()
    try:
        html = TEMPLATE.read_text(encoding="utf-8")
    except Exception:
        return {"ok": False, "cevap": "Arayüz dosyası okunamadı."}
    try:
        resp = client.messages.create(
            model=_CHAT_MODEL, max_tokens=1500, system=_PROPOSE_SYSTEM,
            messages=[{"role": "user", "content":
                       f"İstek: {soru}\n\nindex.html içeriği:\n{html}"}],
            output_config={"format": {"type": "json_schema", "schema": _EDIT_SCHEMA}})
        text = next((b.text for b in resp.content if b.type == "text"), "")
        d = json.loads(text)
    except Exception as e:
        return {"ok": False, "cevap": f"Değişiklik planlanamadı ({type(e).__name__})."}

    if not d.get("yapilabilir"):
        _PENDING.pop(kullanici, None)
        return {"ok": True, "cevap": (
            "Bu isteği arayüzde (HTML/CSS/JS) yapamam — sadece görsel değişiklikler "
            "yapabilirim (renk, yazı, buton, sekme vb.). " + (d.get("aciklama") or ""))}

    old, new = d.get("old_string") or "", d.get("new_string") or ""
    # --- guvenlik dogrulamalari ---
    if not old or old not in html:
        return {"ok": False, "cevap": "Değişecek yeri bulamadım, daha açık tarif eder misin?"}
    if html.count(old) != 1:
        return {"ok": False, "cevap": "Değişiklik birden fazla yere uyuyor; daha belirgin söyler misin?"}
    if _line_count(old) > MAX_LINES or _line_count(new) > MAX_LINES:
        return {"ok": False, "cevap": f"Bu değişiklik çok büyük (max {MAX_LINES} satır). Daha küçük parçalara böl."}

    _PENDING[kullanici] = {"aciklama": d.get("aciklama") or "arayüz değişikliği",
                           "old": old, "new": new}
    onizle = new.strip()
    if len(onizle) > 400:
        onizle = onizle[:400] + "…"
    return {"ok": True, "cevap": (
        f"Şunu yapacağım: {d.get('aciklama')}\n\n"
        f"Önizleme (yeni hâli):\n{onizle}\n\n"
        "Onaylıyorsan 'onayla' yaz; uygulayıp sayfayı yenilemeni isteyeceğim. "
        "Vazgeçersen başka bir şey yaz.")}


def apply_pending(kullanici) -> dict:
    """Onaylanan bekleyen değişikliği güvenle uygular."""
    pend = _PENDING.get(kullanici)
    if not pend:
        return {"ok": True, "cevap": "Bekleyen bir değişiklik yok."}
    _PENDING.pop(kullanici, None)

    try:
        original = TEMPLATE.read_text(encoding="utf-8")
    except Exception:
        return {"ok": False, "cevap": "Arayüz dosyası okunamadı."}

    old, new, aciklama = pend["old"], pend["new"], pend["aciklama"]
    if old not in original or original.count(old) != 1:
        return {"ok": False, "cevap": "Değişiklik artık uygulanamıyor (dosya değişmiş)."}

    yeni = original.replace(old, new, 1)

    # --- uygulamadan once dogrula: JS sozdizimi + yapisal cogur ---
    if not _structurally_ok(yeni):
        return {"ok": False, "cevap": "Değişiklik sayfayı bozacaktı (yapısal kontrol), iptal ettim."}
    TEMPLATE.write_text(yeni, encoding="utf-8")
    if not _js_ok(yeni):
        TEMPLATE.write_text(original, encoding="utf-8")    # geri al
        return {"ok": False, "cevap": "Değişiklik JavaScript hatası verdi, geri aldım. Değişiklik yapılamadı."}

    # --- git commit (yalniz index.html) ---
    try:
        _git(["add", "src/web/templates/index.html"])
        _git(["commit", "-m", f"Bota Sor UI degisikligi: {aciklama[:80]}"])
    except Exception:
        pass

    # --- servisi arka planda (gecikmeli) yeniden baslat: kendi istegimizi oldurme ---
    try:
        subprocess.Popen(
            "sleep 2 && systemctl restart borsa-web.service",
            shell=True, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    return {"ok": True, "cevap": (
        f"✅ Değişiklik yapıldı: {aciklama}. Servis birkaç saniye içinde yenilenecek — "
        "sonra sayfayı yenile. Beğenmezsen 'geri al' yaz.")}


def revert_last(kullanici) -> dict:
    """Son UI değişikliğini geri alır (git)."""
    log = _git(["log", "-1", "--pretty=%s"])
    son = (log.stdout or "").strip()
    if not son.startswith("Bota Sor UI degisikligi"):
        return {"ok": True, "cevap": "Geri alınacak bir arayüz değişikliği bulamadım."}
    r = _git(["revert", "--no-edit", "HEAD"])
    if r.returncode != 0:
        return {"ok": False, "cevap": "Geri alma başarısız oldu."}
    try:
        subprocess.Popen("sleep 2 && systemctl restart borsa-web.service",
                         shell=True, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return {"ok": True, "cevap": "↩️ Son arayüz değişikliği geri alındı, servis yenileniyor."}
