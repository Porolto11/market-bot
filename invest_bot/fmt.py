"""Formatting helpers for the Telegram texts (plain text, no parse_mode: emojis only, never markup).

Display only: no decision logic. Short tickers (ASML, not ASML.AS, which Telegram would turn into a link), prices
with a currency sign, rounded HUF estimates, score badges, Budapest local times and splitting at 4000 characters.
"""
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from invest_bot import prices, rules

BUD = ZoneInfo("Europe/Budapest")
MAX_LEN = 4000  # Telegram allows 4096 characters
SIGN = {"USD": "$", "EUR": "€", "GBP": "£"}
NUMBERS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟")
REGIME = {"bullish": "🟢 uptrend", "neutral": "🟡 sideways", "bearish": "🔴 downtrend"}
FLAG = {"US": "🇺🇸", "EU": "🇪🇺"}
FLAGS = {"very volatile": "🎢 very volatile", "overheated": "🌡️ overheated",
         "far below 52w high": "📉 far below its 52-week high"}
_BOT = {v: k for k, v in prices.TICKERS.items()}


def tick(symbol) -> str:
    """The bot's short ticker (CSPX for CSPX.L), else the symbol without its exchange suffix (ASML for ASML.AS)."""
    s = str(symbol)
    return _BOT.get(s) or s.split(".")[0]


def label(name, symbol) -> str:
    """'Micron (MU)', or just 'MOL' when the name is the ticker."""
    t = tick(symbol)
    return t if not name or name in (t, symbol) else f"{name} ({t})"


def price(x, ccy) -> str:
    """An exact price with its currency: $915.33, €12.5, £3.2, 3000 HUF, 120 GBp."""
    if ccy in SIGN:
        return f"{SIGN[ccy]}{rules._px(x)}"
    return f"{rules._px(x)} {ccy}"


def approx(x) -> str:
    """A HUF estimate rounded to a sensible precision, space thousands separator: 302 000, 59 900, 2 440."""
    step = 1000 if abs(x) >= 100_000 else 100 if abs(x) >= 10_000 else 10 if abs(x) >= 1000 else 1
    return rules._huf(round(x / step) * step)


def huf(x) -> str:
    return f"{approx(x)} HUF"


def signed(x, digits=1, unit="%") -> str:
    """+11.8% / −14.0% (a real minus sign; a value that rounds to 0 is +0.0%)."""
    x = 0.0 if round(x, digits) == 0 else x
    return f"{x:+.{digits}f}{unit}".replace("-", "−")


def signed_huf(x) -> str:
    return f"{x:+,.0f} HUF".replace(",", " ").replace("-", "−")


def badge(score) -> str:
    return "🔥" if score >= 90 else "✅" if score >= 75 else "👍" if score >= 70 else ""


def scored(score, out_of="") -> str:
    """'99/100 🔥', '40' (no badge below 70)."""
    return f"{score}{out_of} {badge(score)}".rstrip()


def local(now) -> datetime:
    if isinstance(now, datetime):
        return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(BUD)
    return datetime(now.year, now.month, now.day, tzinfo=BUD)


def stamp(now) -> str:
    """'Tue 27 Oct, 07:45' in Budapest time."""
    t = local(now)
    return f"{t:%a} {t.day} {t:%b}, {t:%H:%M}"


def day(d, today=None) -> str:
    """'Wed' within the next 6 days of `today`, else 'Mon 9 Nov'. d: a date or an ISO date string."""
    try:
        d = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
    except ValueError:
        return str(d)
    if today is not None and 0 <= (d - today).days <= 6:
        return f"{d:%a}"
    return f"{d:%a} {d.day} {d:%b}"


def regime(reg) -> str:
    """'🇺🇸 US 🟢 uptrend · 🇪🇺 EU 🔴 downtrend'."""
    return " · ".join(f"{FLAG.get(k, '🌍')} {k} {REGIME.get(v.get('rating'), '⚪ no data')}" for k, v in reg.items())


def flag(f) -> str:
    if f.startswith("earnings in"):
        return "⚠️ E" + f[1:]
    return FLAGS.get(f, f"⚠️ {f}")


def split(text, limit=MAX_LEN) -> list:
    """Chunks of at most `limit` characters, cut at section boundaries (blank lines), else at line ends."""
    out, cur = [], ""
    for section in text.split("\n\n"):
        for part in _fit(section, limit):
            if cur and len(cur) + 2 + len(part) > limit:
                out.append(cur)
                cur = part
            else:
                cur = f"{cur}\n\n{part}" if cur else part
    if cur or not out:
        out.append(cur)
    return out


def _fit(section, limit) -> list:
    """A section as is when it fits, else cut at line ends (a single line longer than the limit is hard-cut)."""
    if len(section) <= limit:
        return [section]
    out, cur = [], ""
    for line in section.split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out
