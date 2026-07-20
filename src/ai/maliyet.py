"""Ortak AI maliyet hesabi + TOKEN OZET loglama.

Neden: Eskiden yalniz 3 yol (TR/US brifingi, uyarilar) tahmini_maliyet
yaziyordu; digerleri (gun_sonu, senaryo, yukselis_hafizasi, update_decisions,
web 'Bota Sor' sohbet) loglanmiyordu. Ayrica batch fallback'a dusen brifing
SENKRON (2x) fiyata calisip yine BATCH fiyatiyla loglaniyordu -> log toplami
Console gercegini AZ gosteriyordu. Artik tum AI cagrilari BU yardimcidan
gecer; fiyat DOGRU model + tier (batch/senkron) ile hesaplanir ve log toplami
gercek faturaya yaklasir.

Format, mevcut brifing satiriyla BIREBIR aynidir (denetim araclari bozulmasin):
  [YYYY-AA-GG SS:DD] TOKEN OZET[ (etiket)]: input=.., output=.., cache_hit=..,
  cache_write=.., tahmini_maliyet=$0.0000
"""
from datetime import datetime

# Standart API fiyati ($/1M token): (input, output). Batch API = yarisi (%50).
# Cache: yazma = input x1.25, okuma (hit) = input x0.10.
_FIYAT = {
    "claude-opus-4-8":   (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-5":   (3.0, 15.0),
    "claude-haiku-4-5":  (1.0, 5.0),
}
_VARSAYILAN = (3.0, 15.0)   # bilinmeyen model -> Sonnet varsay (temkinli ust sinir)


def _model_fiyat(model: str):
    m = model or ""
    for k, v in _FIYAT.items():
        if m.startswith(k):
            return v
    return _VARSAYILAN


def bos_acc() -> dict:
    """Dongu icinde biriktirmek icin sifir toplayici."""
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}


def usage_dict(usage) -> dict:
    """Anthropic response.usage nesnesi -> {input, output, cache_write, cache_read}."""
    return {
        "input":       getattr(usage, "input_tokens", 0) or 0,
        "output":      getattr(usage, "output_tokens", 0) or 0,
        "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read":  getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def ekle(acc: dict, usage) -> dict:
    """Bir response.usage'i acc toplayicisina ekler (dongu icinde birikim)."""
    if usage is None:
        return acc
    u = usage_dict(usage)
    for k in acc:
        acc[k] += u.get(k, 0)
    return acc


def hesapla(acc: dict, model: str, batch: bool = False) -> float:
    """acc + model + tier -> tahmini USD maliyet."""
    pin, pout = _model_fiyat(model)
    if batch:
        pin, pout = pin * 0.5, pout * 0.5
    pin /= 1_000_000
    pout /= 1_000_000
    return (acc["input"] * pin
            + acc["output"] * pout
            + acc["cache_write"] * pin * 1.25
            + acc["cache_read"] * pin * 0.10)


def logla(acc: dict, model: str, etiket: str = "", batch: bool = False,
          tarih: str = None) -> float:
    """TOKEN OZET satirini stdout'a yazar (cron log yonlendirmesiyle ilgili
    dosyaya duser). etiket verilirse kaynak 'TOKEN OZET (etiket):' olarak
    isaretlenir. Doner: hesaplanan maliyet."""
    maliyet = hesapla(acc, model, batch)
    ts = tarih or datetime.now().strftime("%Y-%m-%d %H:%M")
    tag = f" ({etiket})" if etiket else ""
    print(f"[{ts}] TOKEN OZET{tag}: input={acc['input']}, output={acc['output']}, "
          f"cache_hit={acc['cache_read']}, cache_write={acc['cache_write']}, "
          f"tahmini_maliyet=${maliyet:.4f}")
    return maliyet
