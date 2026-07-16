"""Çift-sağlayıcı AI çağrısı: Anthropic BİRİNCİL, NVIDIA acil-durum YEDEĞİ (17 Tem 2026).

NEDEN: Anthropic bir gün erişilemez olursa (kredi bitmesi / 429 limit / servis
kesintisi), YARDIMCI AI işleri (haber etiketleme, gölge haber katmanı, alarm
metinleri) NVIDIA'nın ÜCRETSİZ NIM ucuna (OpenAI-uyumlu) düşerek ÇALIŞMAYA DEVAM
etsin — bot tamamen susmasın.

KALİTE KORUMASI: Ana AL/SAT kararı ("ana_karar") ASLA NVIDIA'ya düşmez. O kalite
kritiktir (gerçek para hareketi); Anthropic yoksa o iş BEKLER/atlanır. Kalitesiz
bir AL/SAT kararı, hiç karar vermemekten daha tehlikelidir.

NORMAL ZAMANDA NVIDIA HİÇ ÇAĞRILMAZ: her iş önce Anthropic'e gider; yalnız Anthropic
bir HATA fırlatırsa (429/401/5xx/timeout/kredi freni) VE iş UYGUNSA NVIDIA denenir.

ANAHTAR YOKSA: fallback tamamen pasiftir; sistem Anthropic-only çalışır (mevcut
davranış, hiçbir şey bozulmaz). `.env`'e NVIDIA_API_KEY konunca fallback KENDİLİĞİNDEN
aktifleşir — kod değişikliği gerekmez.
"""
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
_TZ = ZoneInfo("Europe/Istanbul")

# NVIDIA ücretsiz NIM (build.nvidia.com), OpenAI-uyumlu uç. ~40 istek/dk limiti;
# botun tepe hacmi ~10/dk olduğundan rahat sığar. Kredi kartı gerekmez.
NVIDIA_BASE_URL = "https://integrate.api.nvidia.com/v1"
# Haber etiketleme/sınıflandırma için ücretsiz + Türkçe'de yeterli + hızlı model.
# Alternatif: "deepseek-ai/deepseek-v3" (daha güçlü ama daha yavaş). Llama 3.3 70B
# JSON talimatını iyi izler ve gölge katmandaki sınıflandırma için fazlasıyla yeter.
NVIDIA_MODEL = "meta/llama-3.3-70b-instruct"

# NVIDIA yedeğine DÜŞEBİLECEK iş tipleri (Haiku seviyesi, kalite-kritik DEĞİL).
# "ana_karar" bu kümede YOKTUR -> yönlendirici onu NVIDIA'ya asla düşürmez.
YEDEGE_UYGUN = {"haber", "golge", "alarm", "senaryo", "ozet"}

_FALLBACK_ANAHTAR = "nvidia_fallback"    # DB: gün başına kaç kez NVIDIA'ya düşüldü


def _bugun() -> str:
    return datetime.now(_TZ).date().isoformat()


def nvidia_anahtari() -> str | None:
    """`.env`/ortamdaki NVIDIA_API_KEY (boşsa None). systemd .env'i yükler; standalone
    koşuda haber_sinyal._load_dotenv() zaten çağrılıyor."""
    return (os.environ.get("NVIDIA_API_KEY") or "").strip() or None


def nvidia_aktif() -> bool:
    """Fallback aktif mi? Yalnız anahtar varsa. Anahtar yoksa Anthropic-only."""
    return nvidia_anahtari() is not None


def fallback_bugun(tarih: str = None) -> int:
    """Bugün NVIDIA yedeğine kaç kez düşüldü (panel için)."""
    from src.db import database as db
    tarih = tarih or _bugun()
    try:
        ham = db.get_setting(f"{_FALLBACK_ANAHTAR}:{tarih}")
        return int(ham) if ham else 0
    except Exception:
        return 0


def _fallback_artir(tarih: str = None) -> None:
    from src.db import database as db
    tarih = tarih or _bugun()
    try:
        db.set_setting(f"{_FALLBACK_ANAHTAR}:{tarih}", str(fallback_bugun(tarih) + 1))
    except Exception:
        pass


def yedek_durum(tarih: str = None) -> dict:
    """Panel/karne için tek bakışta yedek durumu."""
    aktif = nvidia_aktif()
    return {
        "aktif": aktif,
        "model": NVIDIA_MODEL if aktif else None,
        "bugun": fallback_bugun(tarih),
        "durum": ("aktif" if aktif else "pasif (anahtar bekleniyor)"),
    }


def _json_ayikla(text: str) -> dict:
    """NVIDIA çıktısından JSON'u güvenli ayıkla (Anthropic'in şema garantisi yok;
    model markdown/```json çitiyle sarabilir)."""
    t = (text or "").strip()
    if t.startswith("```"):
        # ```json ... ``` çitini soy
        t = t.split("```", 2)
        t = t[1] if len(t) >= 2 else (text or "")
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
    t = t.strip()
    try:
        return json.loads(t)
    except Exception:
        # ilk { ... son } arasını dene
        i, j = t.find("{"), t.rfind("}")
        if i != -1 and j != -1 and j > i:
            return json.loads(t[i:j + 1])
        raise


def _nvidia_json(system: str, user: str, schema: dict, max_tokens: int) -> dict:
    """NVIDIA NIM (OpenAI-uyumlu) ile şemaya uygun JSON üret. Anahtar VARSA çağrılır."""
    from openai import OpenAI
    key = nvidia_anahtari()
    cli = OpenAI(base_url=NVIDIA_BASE_URL, api_key=key, timeout=45)
    sema_not = (
        "\n\nÇIKTI KURALI: Yanıtını YALNIZCA geçerli JSON olarak ver — markdown yok, "
        "kod bloğu yok, açıklama yok. Şu JSON şemasına birebir uy:\n"
        + json.dumps(schema, ensure_ascii=False))
    resp = cli.chat.completions.create(
        model=NVIDIA_MODEL,
        messages=[{"role": "system", "content": system + sema_not},
                  {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=0.2,
    )
    return _json_ayikla(resp.choices[0].message.content)


def json_cagir(system: str, user: str, schema: dict, max_tokens: int = 1500,
               is_tipi: str = "haber", anthropic_model: str = "claude-haiku-4-5",
               client=None) -> dict | None:
    """Şemaya uygun JSON üretir. ÖNCE Anthropic; hata + iş uygun + NVIDIA anahtarı
    varsa NVIDIA yedeğine düşer.

    is_tipi: "golge"/"haber"/"alarm"/"senaryo"/"ozet" -> yedeğe UYGUN.
             "ana_karar" -> yedeğe UYGUN DEĞİL (Anthropic yoksa None döner, beklesin).

    Dönüş: parse edilmiş dict, ya da None (üretilemedi -> çağıran atlar/bekletir).
    """
    # 1) BİRİNCİL: Anthropic
    try:
        import anthropic
        client = client or anthropic.Anthropic()
        resp = client.messages.create(
            model=anthropic_model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"format": {"type": "json_schema", "schema": schema}})
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(text)
    except Exception as e:
        anthropic_hata = f"{type(e).__name__}: {str(e)[:120]}"

    # 2) YEDEK: NVIDIA — yalnız iş UYGUNSA ve anahtar VARSA
    if is_tipi not in YEDEGE_UYGUN:
        print(f"  [saglayici] {is_tipi}: Anthropic hatası ({anthropic_hata}) — "
              f"KALİTE KRİTİK, NVIDIA'ya DÜŞÜLMEDİ, iş beklemede.")
        return None
    if not nvidia_aktif():
        print(f"  [saglayici] {is_tipi}: Anthropic hatası ({anthropic_hata}) — "
              f"NVIDIA anahtarı bekleniyor (.env NVIDIA_API_KEY boş), fallback pasif.")
        return None
    try:
        sonuc = _nvidia_json(system, user, schema, max_tokens)
        _fallback_artir()
        print(f"  [saglayici] {is_tipi}: Anthropic hatası ({anthropic_hata}) "
              f"-> NVIDIA yedeğine DÜŞÜLDÜ (başarılı, {NVIDIA_MODEL}).")
        return sonuc
    except Exception as e2:
        print(f"  [saglayici] {is_tipi}: hem Anthropic ({anthropic_hata}) hem "
              f"NVIDIA ({type(e2).__name__}: {str(e2)[:120]}) başarısız.")
        return None


def main(argv) -> int:
    """Durum/test yardımcı komutları."""
    komut = argv[1] if len(argv) > 1 else "durum"
    if komut == "durum":
        import json as _j
        # standalone: .env yükle
        try:
            from src.news.haber_sinyal import _load_dotenv
            _load_dotenv()
        except Exception:
            pass
        print(_j.dumps(yedek_durum(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv))
