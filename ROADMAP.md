# Roadmap

This combines the 2026-10-27 audit with the "top-notch bot" plan. Each item is one tested commit. When an item changes behaviour, the old behaviour stays the default until the evidence supports switching.

## Now: the analyst upgrade (2026-10-28, from the user's brief)
**Stable baseline:** the live score and calls as of 2265f35. Experiments, pass rule and every result are in
docs/EXPERIMENTS.md.

The 5 weakest calls (audit trace: data → `scan.potential` → `scan._call` → `watch.stops`/`dips` → `cloud.message`):
1. **RSI > 70 → WAIT_FOR_DIP** delayed the best momentum buys and cost about 9 pts/yr in both periods.
   **Fixed (6431e37):** off by default; `RSI_HOT=70` rolls back.
2. **The trailing-stop alert** ("15% below its peak → consider selling"). Under the live hold rule it cut CAGR
   in both periods and added a third more trades. **Fixed:** off by default; `TRAIL_STOP=0.15` rolls back; the 20%
   cost stop stays. (An earlier "−20 pts" note here was measured on a different, unconditional stop: corrected in
   docs/EXPERIMENTS.md, cycle 2.)
3. **Hold until bearish** (live) vs a monthly review keeping the top 10. The top-10 version was much better in
   2014-19 and on bot US, but lower in 2020-. Not shipped. **Next:** re-test with a wider hold (top 15) and decide
   after the forward test.
4. **35 of the 100 score points (analyst upside, growth) can't be tested,** and analyst targets run high
   (docs/RESEARCH.md). Open; the shadow model test in /stats compares the live score with plain momentum.
5. **Messages lack an exit thesis:** holding period, invalidation, review date. Item 11's exit line is a start.
   **Next:** after items 2-3, say "held for momentum, reviewed monthly; out if it leaves the top 10 or −20% from
   cost".

Measured and rejected: buying dips in leaders, 52-week-high proximity, volatility-scaled momentum, a top 3
instead of 5 (much deeper drawdowns: the buy plan's 3 picks are more concentrated than the backtest), and trend
exits.

## P0: correctness (do first)
1. **Fresh data only.**
   - Drop rows with a NaN close.
   - Drop today's unfinished bar (exchange calendars).
   - Stale symbols abstain from BUY and SELL.
   - Move the evening scan to after the US close (~21:15 UTC).
2. **Sizing.**
   - Split real cash across the picks shown, so the total stays at or below cash.
   - Show the fee %, and skip buys where the fee is above 1%.
3. **Concentration.** At most 2 picks per sector, and skip a pick whose correlation with a higher pick is above 0.75.
4. **No silent failures.**
   - The scan sends "scan failed" when it aborts.
   - A heartbeat warns when there has been no scan for more than 26 h.
   - Long messages are split.
   - Telegram 400/403 errors are permanent and are not retried.
5. **Tax rate: CONFIRMED** (elemzeskozpont.hu, 2025).
   - Exchange trades (ellenőrzött tőkepiaci ügylet, K&H qualifies) are taxed at **15% SZJA with no szocho**.
   - Gains and losses net within the same year. Declared losses from the previous 2 years offset this year's gains.
   - Gains are converted to HUF at the official MNB rate on the buy and sell days (so item 17 moves up).
   - Dividends: EEA-listed dividends carry no szocho. Non-EEA dividends (e.g. US) carry 13% szocho.
   - Change 28% → 15% in tax.py and add the 2-year loss carry.
   - **ETFs** (elemzeskozpont.hu): label each ETF as Acc or Dist.
     - Prefer Acc (CSPX, CNDX, VVSM, VWCE, IWDA, EIMI): no payouts, so no dividend tax or reinvest fees.
     - Dist (VHYL) payouts are taxable income; the bot notes this on calls.
     - Only UCITS ETFs are allowed, because US-domiciled ETFs lack an EU KID and K&H can't sell them to retail.

## P1: reliability and evidence
6. Stooq backup prices (no key). BLOCKED 2026-10-27: Stooq now serves a JavaScript bot check instead of CSV; not worked around. Use the Alpha Vantage earnings calendar, 1 call/day, if `ALPHAVANTAGE_KEY` is set.
7. Volatility-scaled move alerts on holdings only, to cut the noise. Done: a holding's move must also be ≥ 2.5 daily σ (news.movers).
8. **Backtest in the repo.** Walk-forward, after fees and tax, against CSPX. Fix the "backtested" docstring.
   Done: `python tools/backtest.py` (monthly top 5, K&H fees, 15% tax, vs SPY, survivorship-biased). 2014-today, S&P100:
   plain momentum +32.5%/yr after tax, the bot's price score +20.6%, SPY +12.6%. A 12-15% stop vs cost lowered CAGR
   by 1-6 points without a smaller drawdown; 20-25% was about neutral, so the hard stop is 20% (a crash guard).
9. **Shadow momentum.** Plain 12-1 momentum plus a trend filter runs next to the current score (`SCORE_MODE`). Switch after 3–6 months if it keeps winning. Started 2026-10-27: each scan logs the top 5 of both models (track.shadow), /stats shows the race; the `SCORE_MODE` switch comes only when the evidence is in.
10. **Paper portfolio as /stats.**
    - Follow every call with virtual HUF, after fees and tax, against CSPX.
    - Count each call once per episode.
11. **Exits defined at entry.**
    - Each BUY gets an ID, a stop, a trend exit and a review date.
    - Lifecycle: ACTIVE → STOP_HIT / TREND_EXIT / CLOSED, with follow-up messages.
    - Done (lean): every BUY PLAN pick shows its exit (−20% hard stop vs cost, watch.HARD_STOP; 12% failed the backtest); the stop alerts
      and the trend SELL are the follow-ups. IDs and a review date only if the paper portfolio needs them.
12. **Volatility-based position sizing,** with at most 5–6 positions.
13. Trim the commands cron at weekends to save Actions minutes.

Evidence from docs/RESEARCH.md that shapes P1:
- Item 11's stop is about 12% (Han-Zhou-Zhu, long-short). Our long-only backtest disagrees: 20% (item 8).
- Item 9 drops analyst upside and growth from the score (analyst targets are about 15% too high).
- Messages remind the user to declare loss years, since an undeclared loss can't be carried.

**K&H facts (docs/KH.md) to apply next:**
- **FX:** the spread is 1%, not 0.3%. Done (rules.FX_SPREAD).
- **Minimum fees:** 11.99 EUR for Nordic, Swiss, Spanish and Italian listings (e.g. Novo Nordisk on .CO). Done (fees.MARKET_MIN, by exchange suffix).
- **Buy taxes:** France 0.4% (LVMH, Airbus, Schneider), Spain/Italy 0.2%, UK 0.5%, Irish shares 1%. Done (fees.buy_tax; ETFs exempt).
- **Package:** the fee rate is set per package (Platinum 0.24% … Start 0.34%).
- **Tax certificate date:** 31 January. Done (/tax, the December check, docs/RESEARCH.md).
- **Later:** import the WebTrader Excel export of transactions.
- **Multi-currency cash.** Platinum accounts can hold USD, EUR and HUF. Done (state.book per currency, scan.pay;
  conversions only from HUF; still to check in WebTrader whether USD/EUR cash can really be held).
  - Track cash per currency: `/cash 250000 HUF`, `/cash 500 USD`, `/cash 300 EUR`.
  - Buys spend the instrument's own currency first. Only the shortfall is converted, at 1%.
  - Sells credit their own currency; there's no auto-convert.
  - The buy plan shows "pay from USD balance" or "convert ≈ X HUF (1% ≈ Y)".

## P2: after the evidence
18. **Momentum-crash sizing:** smaller buys for 1–3 months after a big index sell-off. Backtest it first.
19. **Crowded-stock flag:** stocks on daily top-gainer/top-loser lists (Kumar 2017).
20. **US-stock dividend note:** 15% + 13% szocho, plus 30% US withholding.
14. **Core ETF dual momentum** (CSPX / EU ETF / cash). Only if the backtest shows smaller drawdowns.
15. **Telegram buttons:** "✅ I bought it" on BUY calls, instead of typing /bought.
16. **Self-monitoring:** "my calls lag CSPX for 3 months — trust them less".
17. MNB official FX rates for the tax figures, and a separate state-encryption key.

## Don't build
These can't be validated on this data, or they cost money:
- a market-regime gate (it cut returns in the backtest);
- ML or AI price prediction;
- sentiment scores;
- more indicators;
- paid data;
- auto-trading.
