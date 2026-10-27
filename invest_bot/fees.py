"""K&H Platinum online commission (Kondíciós Lista effective 2026-10-01, docs/KH.md).

The rate is 0.24% of the trade value, with a minimum per market: US 3.49 USD, BÉT 349 HUF, DE/FR/BE/IE/NL/PT/SK/UK
3.49 EUR, AT/DK/FI/GR/LU/NO/IT/ES/CH/SE 11.99 EUR. The market comes from the Yahoo symbol's exchange suffix
(no suffix: US). A minimum in another currency than the trade's is converted by the caller (scan._fee).
"""

RATE = 0.0024
MIN = {"HUF": 349, "USD": 3.49, "EUR": 3.49}  # by currency: only for a symbol whose market is unknown
# Yahoo exchange suffix -> K&H market
SUFFIX_MARKET = {
    ".BD": "BÉT",
    ".DE": "DE", ".F": "DE", ".BE": "DE", ".DU": "DE", ".HM": "DE", ".HA": "DE", ".MU": "DE", ".SG": "DE",
    ".PA": "FR", ".BR": "BE", ".IR": "IE", ".AS": "NL", ".LS": "PT", ".L": "UK", ".IL": "UK",
    ".VI": "AT", ".CO": "DK", ".HE": "FI", ".AT": "GR", ".OL": "NO", ".MI": "IT", ".MC": "ES", ".SW": "CH",
    ".ST": "SE",
}
MARKET_MIN = {"US": (3.49, "USD"), "BÉT": (349, "HUF"),
              **dict.fromkeys(("DE", "FR", "BE", "IE", "NL", "PT", "SK", "UK"), (3.49, "EUR")),
              **dict.fromkeys(("AT", "DK", "FI", "GR", "LU", "NO", "IT", "ES", "CH", "SE"), (11.99, "EUR"))}


def market(symbol) -> str | None:
    """The K&H market of a Yahoo symbol: by its exchange suffix, US without one; None for an unknown suffix."""
    s = str(symbol or "")
    if "." not in s:
        return "US" if s and not s.startswith("^") and "=" not in s else None
    return SUFFIX_MARKET.get(s[s.rindex("."):])


def minimum(symbol, ccy) -> tuple:
    """(amount, currency) of the minimum fee: the symbol's market, else by the trade currency (EUR 3.49 when that
    has no tariff either)."""
    m = market(symbol) if symbol else None
    if m in MARKET_MIN:
        return MARKET_MIN[m]
    return (MIN[ccy], ccy) if ccy in MIN else (MIN["EUR"], "EUR")


def commission(value: float, ccy: str) -> float:
    """Commission for a trade of `value` in currency `ccy`, in that currency (minimum by currency)."""
    if ccy not in MIN:
        raise ValueError(f"unsupported currency: {ccy}")
    return max(value * RATE, MIN[ccy])
