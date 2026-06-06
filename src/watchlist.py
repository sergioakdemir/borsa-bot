"""Izleme listesi yukleyici. Iki katman: config/watchlist.json.

- bist_endeks : BIST-30 otomatik tarama listesi
- kisisel     : kullanicinin kisisel izleme listesi (simdilik bos)

load_watchlist() ikisinin birlesik, sirali, tekrarsiz halini dondurur.
"""
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "config" / "watchlist.json"
_DEFAULT = ["THYAO", "GARAN", "ASELS", "KCHOL", "TUPRS"]


def _clean(lst):
    return [str(t).upper().replace(".IS", "").strip() for t in (lst or []) if str(t).strip()]


def _data():
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_index() -> list[str]:
    d = _data()
    return _clean(d.get("bist_endeks") or d.get("bist") or d.get("tickers") or [])


def load_personal() -> list[str]:
    return _clean(_data().get("kisisel") or [])


def load_watchlist() -> list[str]:
    seen, out = set(), []
    for t in load_index() + load_personal():
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out or _DEFAULT
