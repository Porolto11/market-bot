from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd

MARKET = {"CSPX": "XLON", "CNDX": "XAMS", "VVSM": "XETR", "MOL": "XBUD", "AAPL": "XNYS",
          "VWCE": "XETR", "VHYL": "XAMS"}  # VWCE on Xetra assumed: verify; VHYL.DE has no yfinance data, so Amsterdam
BUFFER = timedelta(minutes=15)
MIC_BY_SUFFIX = {".DE": "XETR", ".AS": "XAMS", ".PA": "XPAR", ".CO": "XCSE", ".BD": "XBUD", ".L": "XLON"}
INDEX_MIC = {"^GSPC": "XNYS", "^STOXX50E": "XETR"}


@lru_cache(maxsize=None)
def _calendar(code: str):
    return xcals.get_calendar(code)


def _utc_minute(now: datetime) -> pd.Timestamp:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return pd.Timestamp(now.astimezone(timezone.utc)).floor("min")


def _is_open(code: str, ts: pd.Timestamp) -> bool:
    return bool(_calendar(code).is_open_on_minute(ts))


def is_tradeable(ticker: str, now: datetime) -> bool:
    """Open, at least 15 min after the open and at least 15 min before the close."""
    if ticker not in MARKET:
        raise ValueError(f"unknown ticker: {ticker}")
    ts = _utc_minute(now)
    cal = _calendar(MARKET[ticker])
    if not cal.is_open_on_minute(ts):
        return False
    session = cal.minute_to_session(ts)
    opens = cal.session_open(session)
    closes = cal.session_close(session)
    # strict on both sides: open+15min < now < close-15min
    return opens + BUFFER < ts < closes - BUFFER


def add_trading_days(ticker: str, day: date, n: int) -> date:
    """The n-th session after `day` on the ticker's exchange (a closed day counts from the session before it)."""
    if ticker not in MARKET:
        raise ValueError(f"unknown ticker: {ticker}")
    cal = _calendar(MARKET[ticker])
    start = cal.date_to_session(pd.Timestamp(day), direction="previous")
    return cal.session_offset(start, n).date()


def sessions_between(ticker: str, start: date, end: date) -> int:
    """Sessions on the ticker's exchange after `start`, up to and including `end`."""
    if ticker not in MARKET:
        raise ValueError(f"unknown ticker: {ticker}")
    if end <= start:
        return 0
    cal = _calendar(MARKET[ticker])
    return len(cal.sessions_in_range(pd.Timestamp(start) + pd.Timedelta(days=1), pd.Timestamp(end)))


def any_open(now: datetime) -> bool:
    ts = _utc_minute(now)
    return any(_is_open(code, ts) for code in MARKET.values())


def mic_for(symbol) -> str | None:
    """The exchange calendar of a yfinance symbol: by suffix, XNYS without one; None for FX pairs (=X), other
    indexes and unknown suffixes."""
    if symbol in INDEX_MIC:
        return INDEX_MIC[symbol]
    for suffix, code in MIC_BY_SUFFIX.items():
        if symbol.endswith(suffix):
            return code
    return None if "." in symbol or "=" in symbol or symbol.startswith("^") else "XNYS"


def last_completed_session(code, now: datetime) -> date:
    """The exchange's latest session that closed at or before `now`."""
    ts = _utc_minute(now)
    cal = _calendar(code)
    day = ts.tz_localize(None).normalize()
    for session in reversed(cal.sessions_in_range(day - pd.Timedelta(days=14), day + pd.Timedelta(days=1))):
        if cal.session_close(session) <= ts:
            return session.date()
    raise ValueError(f"no {code} session closed in the 14 days before {now}")
