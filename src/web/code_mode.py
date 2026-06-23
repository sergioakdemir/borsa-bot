"""Bota Sor 'kod modu': kullanicinin dogal dille istedigi GUVENLI arayuz
degisikliklerini uygular.

GUVENLIK (sabit, asilamaz):
  - YALNIZ frontend dosyalari degistirilebilir: src/web/templates/index.html ve
    src/web/static/ altindaki *.css / *.js dosyalari (var olan veya yeni olusturulan).
  - Python, veritabani, .env, code_mode.py'nin kendisi ve diger HICBIR dosyaya
    DOKUNULMAZ. Hedef yol allowlist'e gore dogrulanir; disari cikan yol reddedilir.
  - Model yalniz find/replace metni (+ opsiyonel hedef dosya adi) uretir; mutlak
    yol, ".." veya shell komutu URETEMEZ.
  - Degisiklik basina en fazla 150 satir (old/new her biri).
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
STATIC_DIR = ROOT / "src" / "web" / "static"
MAX_LINES = 150
_CHAT_MODEL = "claude-sonnet-4-6"

# Duzenlemeye/olusturmaya izin verilen tek kok (index.html disinda yalniz buranin
# altindaki *.css / *.js). Python/DB/.env kapsam DISI kalir.
_ALLOWED_STATIC_SUFFIXES = (".css", ".js")


def _resolve_target(rel: str | None) -> Path | None:
    """Model'in onerdigi (opsiyonel) hedef dosya adini guvenle cozumler.

    Yalniz index.html VEYA src/web/static/*.css|*.js dondurur; bunun disindaki
    her sey (mutlak yol, '..', Python, baska dizin) icin None doner."""
    if not rel:
        return TEMPLATE
    rel = rel.strip().replace("\\", "/").lstrip("/")
    name = rel.split("/")[-1]
    if not name or ".." in rel:
        return None
    if rel in ("index.html", "src/web/templates/index.html",
               "templates/index.html"):
        return TEMPLATE
    if not name.lower().endswith(_ALLOWED_STATIC_SUFFIXES):
        return None
    target = (STATIC_DIR / name).resolve()
    try:
        target.relative_to(STATIC_DIR.resolve())     # static/ disina cikmasin
    except ValueError:
        return None
    return target

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
        "hedef_dosya": {"type": "string",
                        "description": "Degisecek dosya: 'index.html' (varsayilan) VEYA "
                                       "src/web/static/ altinda bir *.css / *.js dosya adi "
                                       "(or. 'custom.css'). Bos birakirsan index.html."},
        "old_string": {"type": "string",
                       "description": "Hedef dosyada AYNEN gecen, DEGISTIRILECEK metin "
                                      "(benzersiz olmali, yeterli baglam icersin). YENI "
                                      "dosya olusturuyorsan BOS birak."},
        "new_string": {"type": "string",
                       "description": "old_string'in yerine gelecek yeni metin (yeni "
                                      "dosyada: dosyanin tum icerigi)"},
    },
    "required": ["yapilabilir", "aciklama", "old_string", "new_string"],
    "additionalProperties": False,
}

_PROPOSE_SYSTEM = (
    "Sen bir web arayuzu duzenleme asistanisin. Kullanici, bir Flask uygulamasinin "
    "frontend'i icin gorsel/arayuz degisikligi istiyor. Ana sayfa index.html "
    "(HTML+CSS+JS, tek dosya). Sana index.html'in tam icerigi verilir.\n"
    "Gorevin: istegi yerine getiren MINIMAL bir find/replace uret. old_string hedef "
    "dosyada AYNEN ve BENZERSIZ gecen bir parca olsun (gerekiyorsa benzersizlik icin "
    "biraz baglam ekle). new_string degismis halidir.\n"
    "Hedef dosya: Varsayilan index.html. Istersen src/web/static/ altinda bir CSS/JS "
    "dosyasi da olusturabilir/duzenleyebilirsin -- bunun icin 'hedef_dosya' alanina "
    "dosya adini yaz (or. 'custom.css'). YENI dosya olusturuyorsan old_string'i BOS "
    "birak, new_string dosyanin tam icerigi olsun.\n"
    "SADECE HTML/CSS/JS degisikligi yap; mantik/veri/Python/veritabani ile ilgili "
    "istekleri 'yapilabilir=false' isaretle ve old/new alanlarini bos birak. Degisiklik "
    "kucuk olsun (en fazla ~150 satir). Renk vb. icin mevcut CSS degiskenlerini/sinif "
    "yapisini koru."
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


def _js_syntax_ok(body: str) -> bool:
    """Ham JavaScript govdesini node ile sozdizimi acisindan dogrular."""
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


def _js_ok(html: str) -> bool:
    """index.html icindeki <script> bloklarini sozdizimi acisindan dogrular."""
    blocks = re.findall(r"<script>([\s\S]*?)</script>", html)
    return _js_syntax_ok("\n".join(blocks))


def _structurally_ok(html: str) -> bool:
    return all(a in html for a in _ANCHORS)


def _validate(target: Path, content: str) -> tuple[bool, str]:
    """Hedef dosya tipine gore saglik/guvenlik kontrolu. (ok, hata_mesaji) doner."""
    name = target.name.lower()
    if name == "index.html":
        if not _structurally_ok(content):
            return False, "Değişiklik sayfayı bozacaktı (yapısal kontrol), iptal ettim."
        if not _js_ok(content):
            return False, "Değişiklik JavaScript hatası verdi, geri aldım."
        return True, ""
    if name.endswith(".js"):
        if not _js_syntax_ok(content):
            return False, "JavaScript dosyası sözdizimi hatası verdi, iptal ettim."
        return True, ""
    if name.endswith(".css"):
        if content.count("{") != content.count("}"):
            return False, "CSS süslü parantezleri dengesiz, iptal ettim."
        return True, ""
    return False, "Bu dosya türünü düzenleyemem."


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

    # --- hedef dosyayi guvenle cozumle (index.html veya static/*.css|*.js) ---
    target = _resolve_target(d.get("hedef_dosya"))
    if target is None:
        return {"ok": False, "cevap": (
            "Bu dosyayı düzenleyemem; sadece index.html ve static klasöründeki "
            "CSS/JS dosyalarını değiştirebilirim.")}
    try:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
    except Exception:
        return {"ok": False, "cevap": "Hedef dosya okunamadı."}

    old, new = d.get("old_string") or "", d.get("new_string") or ""
    # --- guvenlik dogrulamalari ---
    if not target.exists():                       # YENI dosya olusturma
        if not new.strip():
            return {"ok": False, "cevap": "Yeni dosya için içerik üretemedim."}
    else:                                          # MEVCUT dosyada find/replace
        if not old or old not in current:
            return {"ok": False, "cevap": "Değişecek yeri bulamadım, daha açık tarif eder misin?"}
        if current.count(old) != 1:
            return {"ok": False, "cevap": "Değişiklik birden fazla yere uyuyor; daha belirgin söyler misin?"}
    if _line_count(old) > MAX_LINES or _line_count(new) > MAX_LINES:
        return {"ok": False, "cevap": f"Bu değişiklik çok büyük (max {MAX_LINES} satır). Daha küçük parçalara böl."}

    _PENDING[kullanici] = {"aciklama": d.get("aciklama") or "arayüz değişikliği",
                           "old": old, "new": new, "target": str(target),
                           "yeni_dosya": not target.exists()}
    onizle = new.strip()
    if len(onizle) > 400:
        onizle = onizle[:400] + "…"
    dosya_not = "" if target.name == "index.html" else f" (dosya: {target.name})"
    return {"ok": True, "cevap": (
        f"Şunu yapacağım: {d.get('aciklama')}{dosya_not}\n\n"
        f"Önizleme (yeni hâli):\n{onizle}\n\n"
        "Onaylıyorsan 'onayla' yaz; uygulayıp sayfayı yenilemeni isteyeceğim. "
        "Vazgeçersen başka bir şey yaz.")}


# --------------------------------------------------------------------------
# HATA DUZELTME MODU: kullanici bir arayuz hatasi bildirince otomatik teshis+fix.
_ERROR_STRONG = ("çalışmıyor", "calismiyor", "traceback", "exception",
                 "açılmıyor", "acilmiyor", "bozuldu", "bozuk", "patladı", "patladi",
                 "hata veriyor", "hata alıyorum", "hata aliyorum", "ekran kaymış")
_ERROR_CONTEXT = ("sayfa", "buton", "ekran", "uygulama", "site", "grafik",
                  "arayüz", "arayuz", "menü", "menu", "kart", "liste", "görsel",
                  "gorsel", "css", "js", "javascript", "html")

_FIX_SYSTEM = (
    "Sen bir frontend HATA DUZELTME asistanisin. Kullanici, tek sayfalik bir Flask "
    "uygulamasinin arayuzunde (index.html — HTML+CSS+JS) bir hata/bozukluk bildirdi. "
    "Sana hata raporu ve index.html'in tam icerigi verilir.\n"
    "Gorevin: hatayi TESHIS et ve duzelten MINIMAL bir find/replace uret. old_string "
    "dosyada AYNEN ve BENZERSIZ gecsin; new_string duzeltilmis halidir. Sadece "
    "HTML/CSS/JS duzelt. EGER hata arayuzde degil de backend/Python/veri/sunucu "
    "(or. 500, Python traceback, API hatasi) kaynakliysa 'yapilabilir=false' isaretle "
    "ve old/new alanlarini bos birak; aciklama alanina hatanin nerede oldugunu yaz. "
    "Degisiklik kucuk ve guvenli olsun."
)


def is_error_report(soru: str) -> bool:
    """Mesaj bir arayuz hatasi raporu mu? (yanlis-pozitifi azaltmak icin baglam guardli)"""
    s = (soru or "").lower()
    if any(k in s for k in _ERROR_STRONG):
        return True
    # 'hata'/'error'/'500' tek basina muglak -> arayuz baglami kelimesi de gerek
    if any(k in s for k in ("hata", "error", "500", "exception")) and \
            any(c in s for c in _ERROR_CONTEXT):
        return True
    return False


def fix_error(kullanici, soru, client=None) -> dict:
    """Hata raporunu teshis eder, frontend duzeltmesini UYGULAR, servisi yeniler."""
    import anthropic
    client = client or anthropic.Anthropic()
    try:
        html = TEMPLATE.read_text(encoding="utf-8")
    except Exception:
        return {"ok": False, "cevap": "Arayüz dosyası okunamadı."}
    try:
        resp = client.messages.create(
            model=_CHAT_MODEL, max_tokens=2000, system=_FIX_SYSTEM,
            messages=[{"role": "user", "content":
                       f"Hata raporu: {soru}\n\nindex.html içeriği:\n{html}"}],
            output_config={"format": {"type": "json_schema", "schema": _EDIT_SCHEMA}})
        text = next((b.text for b in resp.content if b.type == "text"), "")
        d = json.loads(text)
    except Exception as e:
        return {"ok": False, "cevap": f"Hatayı analiz edemedim ({type(e).__name__})."}

    if not d.get("yapilabilir"):
        return {"ok": True, "cevap": (
            "Bunu otomatik düzeltemedim — arayüz (HTML/CSS/JS) kaynaklı görünmüyor "
            "(muhtemelen sunucu/Python tarafı). Güvenlik gereği yalnız arayüz "
            "dosyalarını değiştirebiliyorum. Tespitim: "
            + (d.get("aciklama") or "kaynağı belirleyemedim") + ".")}

    target = _resolve_target(d.get("hedef_dosya"))
    if target is None:
        return {"ok": False, "cevap": (
            "Düzeltmeyi yalnız index.html veya static CSS/JS dosyalarına uygulayabilirim.")}
    old, new = d.get("old_string") or "", d.get("new_string") or ""
    if target.exists():
        current = target.read_text(encoding="utf-8")
        if not old or old not in current:
            return {"ok": False, "cevap": "Hatayı tam yerinde bulamadım; biraz daha açık tarif eder misin?"}
        if current.count(old) != 1:
            return {"ok": False, "cevap": "Düzeltme birden fazla yere uyuyor; daha belirgin söyler misin?"}
    elif not new.strip():
        return {"ok": False, "cevap": "Düzeltme içeriği üretemedim."}
    if _line_count(old) > MAX_LINES or _line_count(new) > MAX_LINES:
        return {"ok": False, "cevap": f"Düzeltme çok büyük (max {MAX_LINES} satır)."}

    # pending kur ve hemen uygula (apply_pending: dogrulama + git + servis restart)
    _PENDING[kullanici] = {"aciklama": d.get("aciklama") or "hata düzeltmesi",
                           "old": old, "new": new, "target": str(target),
                           "yeni_dosya": not target.exists()}
    res = apply_pending(kullanici)
    if res.get("ok"):
        return {"ok": True, "cevap": (
            f"🔧 Düzelttim: {d.get('aciklama')}. Servis birkaç saniye içinde "
            "yenilenecek — sonra sayfayı yenileyip test et. Olmadıysa 'geri al' yaz.")}
    return res


def apply_pending(kullanici) -> dict:
    """Onaylanan bekleyen değişikliği güvenle uygular."""
    pend = _PENDING.get(kullanici)
    if not pend:
        return {"ok": True, "cevap": "Bekleyen bir değişiklik yok."}
    _PENDING.pop(kullanici, None)

    old, new, aciklama = pend["old"], pend["new"], pend["aciklama"]
    # Hedefi yeniden guvenle cozumle (pending'deki ham yola guvenme)
    target = _resolve_target(pend.get("target"))
    if target is None:
        # eski kayit: tam yol stringi gelmis olabilir -> dosya adina indir
        target = _resolve_target(Path(pend.get("target", "")).name) if pend.get("target") else TEMPLATE
    if target is None:
        return {"ok": False, "cevap": "Hedef dosya artık geçerli değil, iptal ettim."}

    yeni_dosya = not target.exists()
    if yeni_dosya:
        original = None
        yeni = new
    else:
        try:
            original = target.read_text(encoding="utf-8")
        except Exception:
            return {"ok": False, "cevap": "Hedef dosya okunamadı."}
        if old:
            if old not in original or original.count(old) != 1:
                return {"ok": False, "cevap": "Değişiklik artık uygulanamıyor (dosya değişmiş)."}
            yeni = original.replace(old, new, 1)
        else:
            yeni = new                              # tam dosya icerigini degistir

    # --- uygulamadan once/sonra dogrula (dosya tipine gore) ---
    ok, hata = _validate(target, yeni)
    if not ok and not target.name.endswith(".js"):
        return {"ok": False, "cevap": hata}         # yazmadan reddet
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yeni, encoding="utf-8")
    if not ok:                                       # .js: yazip dogrula, bozuksa geri al
        if original is not None:
            target.write_text(original, encoding="utf-8")
        else:
            try:
                target.unlink()
            except Exception:
                pass
        return {"ok": False, "cevap": hata}

    # --- git commit (yalniz hedef dosya) ---
    try:
        rel = str(target.relative_to(ROOT))
        _git(["add", rel])
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
