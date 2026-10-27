"""K&H Platinum online commission (Hungarian K&H Pappir / Platinum tariff).

Source: khertekpapir.hu/dijjegyzek
"""

RATE = 0.0024
MIN = {"HUF": 349, "USD": 3.49, "EUR": 3.49}


def commission(value: float, ccy: str) -> float:
    """Commission for a trade of `value` in currency `ccy`, in that currency."""
    if ccy not in MIN:
        raise ValueError(f"unsupported currency: {ccy}")
    return max(value * RATE, MIN[ccy])
