"""Denetim (audit) logu: her calistirmada hangi hisseye ne karar verildigini,
veya STALE veri nedeniyle nelerin atlandigini zaman damgali olarak yazar.

Log dosyasi: logs/audit.log (logs/ .gitignore'da, repoya girmez).
"""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
AUDIT_LOG = LOG_DIR / "audit.log"
_TZ = ZoneInfo("Europe/Istanbul")


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _write(line: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def log_run_start(stock_count: int) -> None:
    _write(f"{_now()} | === CALISTIRMA BASLADI | hisse_sayisi={stock_count} ===")


def log_run_end(evaluated: int, skipped: int) -> None:
    _write(f"{_now()} | === CALISTIRMA BITTI | yorumlanan={evaluated} "
           f"atlanan={skipped} ===")


def log_decision(symbol: str, freshness: str | None, action: str,
                 decision: str | None = None, score=None, note: str = "") -> None:
    """Tek bir hisse icin denetim kaydi yazar.

    action: EVALUATED | SKIPPED_STALE | ERROR
    """
    line = (f"{_now()} | {symbol:10s} | freshness={(freshness or '-'):6s} | "
            f"action={action:13s} | karar={(decision or '-'):4s} | "
            f"puan={score if score is not None else '-'}")
    if note:
        line += f" | {note}"
    _write(line)
