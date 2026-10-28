# Strategy experiments

Every hypothesis tried, with its result and decision, failures included. Tool: `tools/backtest.py`.
- Method: monthly top 5, K&H fees, 0.1% slippage, 15% tax, from $3000.
- Price data: the cached Yahoo closes for 2012-01-03 .. 2026-10-26.

**Biases.** Today's universes (survivorship). There are no point-in-time fundamentals, so growth and analyst
targets can't be tested. Backtests barely predict live results; the forward test in /stats outranks this file.

**Pass rule (pre-registered 2026-10-28).** Against the baseline, on S&P100 (the less biased universe), in both
2014-2019 and 2020-today:
- better after-tax CAGR;
- max drawdown no more than 3 points worse.

"bot US" (the bot's own US list, picked with hindsight) is reported, but it does not decide.

Variants tried so far: 25. Expect about 1 in 20 to pass by luck.

## 2026-10-28: baseline and the audit of the current calls

Each cell is after-tax CAGR / max drawdown.

| Variant | S&P100 2014-19 | S&P100 2020- | bot US 2014-19 | bot US 2020- | Verdict |
|---|---|---|---|---|---|
| **bot**: the price part of the live score (momentum + trend), BUY only if the trend is bullish and RSI ≤ 70; this is the **baseline** | +16.9 / -25 | +21.2 / -39 | +29.5 / -29 | +48.1 / -31 | baseline |
| bot without the RSI ≤ 70 wait (no WAIT_FOR_DIP) | **+25.9 / -28** | **+29.9 / -38** | +36.6 / -33 | +61.6 / -39 | **PASS → shipped** |
| bot + hold while in the top 10 (sell only below it) | +18.5 / -25 | +26.5 / -29 | +24.3 / -28 | +54.0 / -26 | PASS, next candidate |
| bot without the RSI wait + hold top 10 | +24.2 / -36 | +32.2 / -34 | +36.2 / -30 | +64.7 / -35 | fail: drawdown in 2014-19; the two passing changes don't stack |
| plain 12-1 momentum (mom) | +25.4 / -30 | +37.9 / -29 | +34.3 / -32 | +51.0 / -51 | fail vs bot: drawdown 2014-19 (-30 vs -25) |
| mom + above the 200-day average | +22.8 / -31 | +35.6 / -28 | +32.4 / -32 | +49.7 / -46 | fail |
| mom, RSI ≤ 70 only | +16.9 / -25 | +31.3 / -26 | +29.7 / -30 | +47.4 / -48 | this filter costs about 8 pts/yr against mom |
| 6-month momentum | +28.4 / -23 | +39.5 / -27 | +39.2 / -29 | +77.4 / -35 | beats mom; not tested against bot; watch: likely luck among many variants |
| the live score's price part without the RSI rule or trend gate | +25.4 / -28 | +32.5 / -37 | +35.9 / -33 | +62.0 / -38 | similar to "bot without the RSI wait" |
| nearness to the 52-week high | -3.1 / -23 | -7.7 / -50 | +8.1 / -18 | +8.7 / -27 | fail badly |
| mom within 10% of the 52-week high | +13.2 / -26 | +14.3 / -23 | +31.1 / -29 | +38.4 / -25 | fail |
| momentum per unit of volatility | +23.3 / -18 | +22.4 / -29 | +27.0 / -18 | +57.3 / -27 | lower drawdown, lower CAGR: fail |
| **buy the dip**: top-20% momentum stocks down 5-15% over the last month | +17.4 / -19 | +20.9 / -45 | +18.1 / -22 | +17.6 / -38 | fail: dips in leaders are not opportunities |
| mom, top 3 instead of 5 | +40.3 / -47 | +51.3 / -42 | +44.6 / -40 | +75.1 / -50 | more return, far larger drawdown: fail (concentration) |
| bot, top 3 | +20.4 / -32 | +24.8 / -48 | +37.0 / -31 | +78.8 / -35 | fail |
| mom, hold while in the top 10 / 15 / 25 | +25.5/-31, +25.3/-31, +22.5/-36 | +43.8/-21, +36.6/-25, +37.4/-28 | | | top 10 passes against mom |

**Exits.** Each is checked daily and sold at that day's close.

| Variant | S&P100 2014-19 | S&P100 2020- | Verdict |
|---|---|---|---|
| mom (no exit rule) | +25.4 / -30 | +37.9 / -29 | reference |
| sell below the 200-day average | +15.9 / -35 | +30.6 / -44 | fail |
| sell below the 100- / 50-day average | -0.6 / -32, -4.4 / -39 | +14.8 / -41, +6.6 / -49 | fail badly (whipsaw) |
| bot + sell below the 200-day average | +12.8 / -31 | +16.2 / -42 | fail: the live "trend turned bearish" SELL works this way |
| 15% trailing stop from the 6-month high | +5.5 / -28 | +9.3 / -47 | fail badly: the live trailing-stop alert does this |
| 25% trailing stop | +16.2 / -31 | +27.4 / -36 | fail |
| 12% / 15% / 20% / 25% stop vs cost (full period) | 32.3 / 30.9 / 32.1 / 32.6 vs 32.5 | | 12-15% hurts, 20-25% about neutral → HARD_STOP = 20% |

**Index ETF dips.** SPY's average 3-month forward return, 2013-2026:
- on all days: +3.7%;
- after closing ≥ 8% below its 52-week high: +6.6% (n = 482 overlapping days, only a handful of episodes);
- after ≥ 15% below: +7.7%.

Weak support for the core-ETF dip alert. Kept.

## Decisions
1. **Shipped:** the RSI "overheated → wait for a dip" rule is off by default (`RSI_HOT` unset).
   - Rollback: set the `RSI_HOT=70` env var.
   - Hypothesis to watch live: BUY calls on high-RSI momentum stocks do no worse than the others in /stats.
2. **Next candidates,** one at a time, each against the then-current baseline:
   - the top-10 hold buffer;
   - removing the daily "trend turned bearish" SELL and the 15% trailing-stop alert, in favour of the monthly review.
