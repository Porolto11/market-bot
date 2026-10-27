"""Strategy pieces the Telegram bot uses: instruments, dip rule, limit ticks and whole-share sizing."""
import math
from datetime import date

from invest_bot import fees, hours

SLIP = 0.002  # buy limit = last x 1.002, sell limit = last x 0.998
MIN_MOVE = 0.03  # smallest expected one-month move / dip depth
DIP_DAYS = 10  # trading days a wait_for_dip stays open
RSI_HOT = 70
FX_SPREAD = 0.01  # K&H converts at 1% on top of the market rate (docs/KH.md), on the converted amount only

# bot ticker -> (ISIN, currency)
INSTRUMENTS = {
    "CSPX": ("IE00B5BMR087", "USD"),
    "CNDX": ("IE00B53SZB19", "EUR"),
    "VVSM": ("IE00BMC38736", "EUR"),
    "MOL": ("HU0000153937", "HUF"),
    "AAPL": ("US0378331005", "USD"),
    "VWCE": ("IE00BK5BQT80", "EUR"),
    "VHYL": ("IE00B8GKDB10", "EUR"),
}


def _huf(x) -> str:
    return f"{x:,.0f}".replace(",", " ")


def _px(x) -> str:
    """A price without exponent notation or float noise: 12345.67, 100.5, 3000."""
    return f"{x:.4f}".rstrip("0").rstrip(".")


def _tick(px: float, ccy: str) -> float:
    """Limit on a valid price step. HUF (BÉT): whole forints from 1000, 0.1 below — verify BÉT tick table."""
    if ccy == "HUF":
        return float(round(px)) if px >= 1000 else round(px, 1)
    return round(px, 2)


def _move(ind) -> float:
    """One-month 1-sigma move proxy (also the dip depth): max(3%, annual vol / sqrt(12))."""
    return max(MIN_MOVE, (ind.get("vol_20d") or 0) / math.sqrt(12))


def dip_deadline(ticker, today) -> date:
    """A new wait_for_dip stays open for DIP_DAYS sessions on the ticker's exchange."""
    return hours.add_trading_days(ticker, today, DIP_DAYS)


def dip_rule(ind, rating, last, ccy, prior_dip=None, today_iso=None):
    """(action, limit, kind) for a buy. kind: 'expired' (prior dip past its deadline -> buy_now), 'open' (prior dip
    keeps its limit), 'dip' (bearish or RSI over 70 -> wait for a dip of max(3%, vol/sqrt(12))) or 'buy'."""
    if prior_dip and not today_iso:
        raise ValueError("dip_rule needs today_iso with a prior_dip")
    mv = _move(ind)
    rsi = ind.get("rsi14")
    if prior_dip and _iso(prior_dip["valid_until"]) < today_iso:
        return "buy_now", _tick(last * (1 + SLIP), ccy), "expired"
    if prior_dip:
        limit = float(prior_dip["limit"]) if prior_dip.get("limit") else _tick(last * (1 - mv), ccy)
        return "wait_for_dip", limit, "open"
    if rating == "bearish" or (rsi is not None and rsi > RSI_HOT):
        return "wait_for_dip", _tick(last * (1 - mv), ccy), "dip"
    return "buy_now", _tick(last * (1 + SLIP), ccy), "buy"


def buy_qty(amount_huf, cap_huf, limit, rate, ccy) -> int:
    """Whole shares for min(amount, cap) HUF, value + fee within the cap."""
    cap_huf = cap_huf / (1 + FX_SPREAD) if ccy != "HUF" else cap_huf  # the bank's conversion spread comes on top
    qty = max(0, math.floor(min(amount_huf, cap_huf) / (limit * rate)))
    while qty > 0 and (qty * limit + fees.commission(qty * limit, ccy)) * rate > cap_huf:
        qty -= 1
    return qty


def _iso(d) -> str:
    return str(d)[:10]
