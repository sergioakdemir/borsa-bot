"""Izleme listesi (watchlist) yukleyici. Tek kaynak: config/watchlist.json.
Simdi 5 hisse; ileride 20-30 olabilir - sadece JSON'a eklenir."""
import json
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "config" / "watchlist.json"
_DEFAULT = ["THYAO", "GARAN", "ASELS", "KCHOL", "TUPRS"]


def load_watchlist() -> list[str]:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        lst = data.get("bist") or data.get("tickers") or []
        cleaned = [t.upper().replace(".IS", "").strip() for t in lst if t.strip()]
        return cleaned or _DEFAULT
    except Exception:
        return _DEFAULT
