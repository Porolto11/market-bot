import pandas as pd
import yfinance as yf

from invest_bot import hours

TICKERS = {"CSPX": "CSPX.L", "CNDX": "CNDX.AS", "VVSM": "VVSM.DE", "MOL": "MOL.BD", "AAPL": "AAPL",
           "VWCE": "VWCE.DE", "VHYL": "VHYL.AS", "^GSPC": "^GSPC", "^STOXX50E": "^STOXX50E",
           "EURHUF": "EURHUF=X", "USDHUF": "USDHUF=X"}


def history(ticker: str, period: str = "2y") -> pd.DataFrame:
    if ticker not in TICKERS:
        raise ValueError(f"unknown ticker: {ticker}")
    df = yf.Ticker(TICKERS[ticker]).history(period=period, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"no price data from yfinance for {ticker} ({TICKERS[ticker]})")
    return df


def download(symbols, period: str = "2y") -> dict:
    """{yfinance symbol: daily history} in one batch call; symbols with no data are left out."""
    df = yf.download(list(symbols), period=period, auto_adjust=False, group_by="ticker", progress=False,
                     threads=True, multi_level_index=True)
    out = {}
    if df is None or df.empty:
        return out
    for s in symbols:
        try:
            part = df[s].dropna(how="all")
        except KeyError:
            continue
        if not part.empty and "Close" in part and part["Close"].notna().any():
            out[s] = part
    return out


def fresh(hists, now) -> tuple[dict, dict]:
    """(histories without NaN-close rows and without a bar for a session that has not closed yet at `now`,
    {symbol: last bar date ISO, or None} of the stale ones: last valid bar older than the exchange's last completed
    session). FX pairs and unknown exchanges only lose their NaN-close rows."""
    out, stale = {}, {}
    for s, h in hists.items():
        if h is None or "Close" not in h:
            out[s] = h
            continue
        h = h[h["Close"].notna()]
        code = hours.mic_for(s)
        if code:
            done = hours.last_completed_session(code, now)
            h = h[h.index.date <= done]  # today's unfinished bar (a live price) is not a close
            last = h.index[-1].date() if len(h) else None
            if last is None or last < done:
                stale[s] = last.isoformat() if last else None
        out[s] = h
    return out, stale


def info(symbol: str) -> dict:
    """yfinance Ticker.info (fundamentals, analyst targets, earnings timestamps)."""
    return yf.Ticker(symbol).info or {}


def news(symbol: str) -> list:
    """Recent news items for a yfinance symbol: Ticker.news, else Yahoo search news (Ticker.news can come back empty
    when Yahoo's endpoint changes)."""
    return yf.Ticker(symbol).news or yf.Search(symbol, news_count=8, max_results=0).news or []


def last(ticker: str) -> float:
    df = history(ticker, period="5d")
    close = df["Close"].dropna()
    if close.empty:
        raise RuntimeError(f"no close price from yfinance for {ticker}")
    return float(close.iloc[-1])
