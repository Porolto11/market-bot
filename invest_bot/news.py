"""Headline filter and big-move alerts for the scan (yfinance news; any failure means no news).

News items come in two shapes: old (title, publisher, providerPublishTime epoch) and new (content.title,
content.provider.displayName, content.pubDate ISO). Titles are made plain one-line text and cut to MAX_TITLE
characters. Telegram messages go out without parse_mode, so nothing in a headline is read as markup.
"""
import re
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

from invest_bot import fmt, prices, scan

DAYS = 3  # headlines from the last 3 days
MAX_TITLE = 120
TIMEOUT = 20  # seconds for the whole news fetch
WORKERS = 8
UP, DOWN = 0.07, -0.05  # big daily move
# "SEC filing(s)" is routine (insider sales, 13F): not negative by itself
NEGATIVE = re.compile(r"\b(?:downgrade[sd]?|cuts guidance|lowers guidance|guidance cut|lawsuits?|investigations?|"
                      r"probes?|sec(?! filings?)|recalls?|misses|fraud|delist(?:s|ed|ing)?|bankruptcy|resigns|"
                      r"halted)\b", re.I)
# not the company's own name: skipped when picking the name word a headline must contain
GENERIC = {"inc", "the", "group", "holdings", "holding", "co", "corp", "plc", "ag", "se", "nv", "sa", "eli", "ishares",
           "core", "vanguard", "vaneck"}


def clean(title) -> str:
    """One line of plain text, at most MAX_TITLE characters."""
    t = re.sub(r"[\x00-\x1f\x7f\s]+", " ", str(title or "")).strip()
    return t if len(t) <= MAX_TITLE else t[:MAX_TITLE - 1].rstrip() + "…"


def _when(v):
    try:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return datetime.fromtimestamp(v, timezone.utc)
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def parse(raw) -> list:
    """[(published UTC datetime, title, source)] newest first; malformed items are skipped."""
    out = []
    for item in raw if isinstance(raw, list) else []:
        try:
            c = item.get("content") if isinstance(item.get("content"), dict) else item
            title = clean(c.get("title"))
            when = _when(c.get("providerPublishTime") or c.get("pubDate") or c.get("displayTime"))
            src = c.get("publisher") or (c.get("provider") or {}).get("displayName") or ""
            if title and when:
                out.append((when, title, clean(src)))
        except Exception:
            continue
    return sorted(out, key=lambda x: x[0], reverse=True)


def recent(headlines, now) -> list:
    return [h for h in headlines if h[0] >= now - timedelta(days=DAYS)]


def name_word(symbol):
    """The first non-generic word of the UNIVERSE name (Micron; Eli Lilly -> Lilly), or None."""
    words = [w for w in scan.UNIVERSE.get(symbol, ("",))[0].split() if w.lower() not in GENERIC]
    return words[0] if words else None


def mentions(symbol, title) -> bool:
    """True when the title names the company: the ticker root as "$V" or "(V)", or as a whole word (case as written)
    when it has 3+ characters (NVDA, SAP, MOL; V, MA and GE are too common), or name_word in any case."""
    raw = re.split(r"[.\-]", symbol)[0]
    root = re.escape(raw)
    if re.search(rf"\${root}(?![\w-])|\({root}\)", title):
        return True
    if len(raw) >= 3 and re.search(rf"(?<![\w-]){root}(?![\w-])", title):
        return True
    word = name_word(symbol)
    return bool(word) and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", title, re.I) is not None


def negative(headlines, now, symbol=None):
    """The newest negative headline title of the last DAYS days, or None. With a symbol only a title that mentions
    the company counts (Yahoo lists sector and crypto stories under a ticker too)."""
    return next((t for _, t, _ in recent(headlines, now)
                 if NEGATIVE.search(t) and (symbol is None or mentions(symbol, t))), None)


def fetch(symbols, news_fn=prices.news, timeout=TIMEOUT) -> dict:
    """{symbol: parsed headlines} fetched in parallel; failed or late symbols get []."""
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        return {}

    def one(s):
        try:
            return parse(news_fn(s))
        except Exception:
            return []
    pool = ThreadPoolExecutor(WORKERS)
    futs = {s: pool.submit(one, s) for s in symbols}
    wait(futs.values(), timeout=timeout)
    pool.shutdown(wait=False, cancel_futures=True)
    return {s: f.result() if f.done() else [] for s, f in futs.items()}


def apply(items, news, now):
    """A BUY with a fresh negative headline becomes WAIT; any other item with one gets item['news'] = title."""
    for i in items:
        title = negative(news.get(i["symbol"]) or [], now, i["symbol"])
        if not title:
            continue
        if i["call"] == "BUY":
            i.update(call="WAIT", reason=f"Fresh negative news: '{title}'. Wait for clarity. {i.get('why', '')}".strip())
        i["news"] = title


def last_move(hist):
    """(last close-to-close change, date of the last close) or (None, None)."""
    try:
        c = hist["Close"].dropna()
        return float(c.iloc[-1] / c.iloc[-2] - 1), c.index[-1].date()
    except Exception:
        return None, None


def movers(histories, symbols) -> list:
    """[(symbol, change, date)] with a last daily move of >= +7% or <= -5%."""
    out = []
    for s in dict.fromkeys(symbols):
        ch, d = last_move(histories.get(s))
        if ch is not None and (ch >= UP or ch <= DOWN):
            out.append((s, ch, d))
    return out


def alert_lines(moves, news, now) -> list:
    """'📉 AMD −8.2% today' per move (the short ticker), then up to 2 headlines of the last DAYS days (title (source)), the ones that
    mention the company first."""
    lines = []
    for s, ch, d in moves:
        pct = f"{ch * 100:+.1f}%".replace("-", "−")
        short = fmt.tick(s)
        lines.append(f"{'📈' if ch > 0 else '📉'} {short} {pct} " + ("today" if d == now.date() else f"on {d:%a %d %b}"))
        for _, title, src in sorted(recent(news.get(s) or [], now), key=lambda h: not mentions(s, h[1]))[:2]:
            lines.append(f"   📰 {title}" + (f" ({src})" if src else ""))
    return lines
