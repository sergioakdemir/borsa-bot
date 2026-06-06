"""Sicak uyari motoru: gunluk (bugun vs onceki kapanis) fiyat degisimini hesaplar
ve siniflandirir.

ACIL : |degisim| >= %5
IZLE : %2 <= |degisim| < %5
yok  : altinda
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..data.factory import get_data_source
from ..markets.bist import BIST

_TZ = ZoneInfo("Europe/Istanbul")
_RANK = {"IZLE": 1, "ACIL": 2}

ACIL_ESIK = 5.0
IZLE_ESIK = 2.0


def level_rank(level) -> int:
    return _RANK.get(level, 0)


def classify(change_pct: float):
    a = abs(change_pct)
    if a >= ACIL_ESIK:
        return "ACIL"
    if a >= IZLE_ESIK:
        return "IZLE"
    return None


def intraday_change(ticker, source=None, market=None, today=None):
    """Bugunun (varsa) onceki kapanisa gore degisimini dondurur."""
    src = source or get_data_source()
    market = market or BIST()
    symbol = market.to_symbol(ticker)
    today = today or datetime.now(_TZ).date()

    df = src.get_history(symbol, start=(today - timedelta(days=14)).isoformat())
    if not df.empty:
        df = df[df["Volume"] > 0]
    if df.empty or len(df) < 2:
        return None

    last_date = df.index[-1].date() if hasattr(df.index[-1], "date") else None
    last_close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])
    change = (last_close - prev_close) / prev_close * 100 if prev_close else 0.0

    return {
        "ticker": ticker, "symbol": symbol,
        "last_date": last_date.isoformat() if last_date else None,
        "is_today": (last_date == today),
        "change": round(change, 2),
        "last_close": round(last_close, 2),
        "prev_close": round(prev_close, 2),
    }


def weekly_change(ticker, source=None, market=None):
    """Son ~5 islem gunundeki yuzde degisim."""
    src = source or get_data_source()
    market = market or BIST()
    symbol = market.to_symbol(ticker)
    today = datetime.now(_TZ).date()
    df = src.get_history(symbol, start=(today - timedelta(days=20)).isoformat())
    if not df.empty:
        df = df[df["Volume"] > 0]
    if df.empty or len(df) < 2:
        return None
    closes = df["Close"].tolist()
    ref = closes[-6] if len(closes) >= 6 else closes[0]
    last = closes[-1]
    return {"ticker": ticker, "symbol": symbol,
            "change": round((last - ref) / ref * 100, 2),
            "last_close": round(last, 2)}
