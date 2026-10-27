# invest-bot audit (read-only)

- Audited commit: `026d0b1131fdeac5eca22634519312029566449a` (branch `foundation` = origin default). The working tree had uncommitted edits in cloud.py, news.py and watch.py from a concurrent agent, so HEAD was audited from a `git archive` snapshot in `scratchpad/head/`.
- Tests at HEAD: **139 passed** (`pytest -q`, 5.3 s). The working tree did not collect: `tests/test_watch.py` raised a SyntaxError from the in-progress edit.
- Time of the live data: 2026-10-27 06:34–06:45 UTC (Tuesday; EU, US and BÉT closed; HK and NSE open).
- Experiment scripts are in `scratchpad/audit/`: `live_scan.py`, `checks.py`, `lastrow.py`, `bt.py` and `bt2.py`. The data is saved in `live.pkl` and `bt_px.pkl`.
- Evidence tags used throughout:
  - **[V]** VERIFIED: code, tests or an experiment run here.
  - **[S]** STRONGLY SUPPORTED: reliable evidence, but not fully verified in this system.
  - **[H]** HYPOTHESIS: plausible, needs a test.
  - **[U]** UNKNOWN: not enough information to say.
- Priorities are P0–P3.

---

## Deliverable 1 — Executive assessment

**Maturity.** The engineering is careful for a one-person bot. It has the following:
- pure functions with injected I/O;
- 139 offline tests, with the network blocked in conftest;
- an encrypted state, and a state-branch lease so a run cannot overwrite a newer state;
- token scrubbing in error text;
- no LLM anywhere in the decision path [V]: a grep for openai/anthropic/llm/gpt/gemini/ollama only hits docstrings that say "no LLM".

The **quant side is much weaker than the engineering**:
- The 0–100 score is a hand-weighted blend. Nothing in the repo backs the weights. The docstring at scan.py:5 says "only the momentum part is backtested", but no backtest exists at HEAD; it was removed in commit 889bf4d [V].
- My backtest puts the price-only part of the score (momentum + trend + the RSI ≤ 70 rule) **below plain 12-1 momentum** on both universes I tested, and it trades more [V, biased data; see Deliverable 6].
- Analyst upside, worth 20 points, is **negatively** rank-correlated with momentum (−0.41) and trend (−0.53) in today's data [V].

**Strongest parts:**
- state safety (state.sh lease, `STATE_EXPECTED`, keeping a `.bad-*` copy when decryption fails);
- the deterministic pipeline;
- whole-share and fee-aware sizing arithmetic;
- the offline test discipline;
- the abort when fewer than half the symbols have prices (cloud.py:321).

**Weakest parts:**
1. **Data timing and freshness.** Partial intraday bars are used as closes at 15:50 UTC. Today, all 22 EU/UK/HU instruments silently used a stale close. There is no freshness check at all.
2. **Position sizing.** Each BUY is sized at cash/2, with up to 5 BUYs per message.
3. **Concentration.** The top-5 today is 5 Technology names, mostly semiconductors.
4. **Validation.** /stats is the only validation, and its samples are overlapping and not independent.
5. **Failures are silent to the user.** No Telegram message is sent when a scan fails or is skipped.

**Biggest risks:**
- The user buys a concentrated, high-beta semiconductor basket sized to 250% of cash, on stale or intraday prices.
- The tax module may overstate tax. Hungarian exchange-traded gains (*tőzsdei ügylet*) are plausibly 15% SZJA with no 13% szocho, and losses may be carried back [S/U; check with NAV or an advisor].
- The validation is too weak to tell skill from luck.

## Deliverable 2 — Architecture map

```
GitHub Actions (private repo, ubuntu-latest)
 ├─ scan.yml   cron 50 6 / 50 15 * * 1-5 UTC ──► state.sh get (state branch, depth-1 fetch)
 │      └─ python -m invest_bot.cloud
 │           prices.download(86 symbols, 2y, auto_adjust=False, one yf.download batch)
 │           cloud.fetch → prices.info per symbol (8 threads)       [Yahoo, unofficial]
 │           cloud.fx_rates (EURHUF=X, USDHUF=X, GBPHUF=X, DKKHUF=X last close)
 │           scan.run_scan: features → potential() score → _call() → size()
 │           analysis.market_regime (^GSPC, ^STOXX50E) → only printed
 │           news.movers / news.fetch / news.apply (regex on Yahoo headlines; BUY→WAIT)
 │           tax.switches, watch.stops, watch.dips
 │           message() → send_telegram (urllib POST, no parse_mode)
 │           track.log / track.score → state.save (Fernet, key = SHA256(token))
 │      └─ state.sh save (force-with-lease push to `state`)
 └─ commands.yml cron */20 5-21 * * * (every day) ─► state.sh get → precheck.py (stdlib getUpdates peek)
        └─ if work: pip install → commands.run: getUpdates → handle() → reply / outbox → state.save → push
Persistence: state/state.enc (log of trades/cash, sent calls, call log, last scan, alerts, outbox, weekly)
Concurrency: both workflows share the group `bot-state`, with cancel-in-progress false.
```

**Decision logic**
- `potential()` at scan.py:149–188 builds the score from four parts:
  - momentum, 40 points: the universe percentile of the 12-1 momentum (or the 6-month momentum when there is less history) plus the percentile of the 6-month momentum;
  - growth, 25 points: from revenue growth, earnings growth and profit margin;
  - analyst upside, 20 points;
  - trend, 15 points: from `analysis.rate`.
- `_call()` at scan.py:263–272 turns the score into a call:
  - held and bearish → SELL;
  - not held and bearish → AVOID;
  - score ≥ 70 and RSI > 70 → WAIT_FOR_DIP;
  - score ≥ 70 and bullish → BUY.
- A BUY becomes WAIT when earnings are 3 trading days away or less, or when there is a negative headline.

**Missing components and unclear interfaces**
- No data-validation layer.
- No signal identity or lifecycle: a call is just a `(symbol, call)` pair.
- No run-health heartbeat.
- No portfolio-level risk step between scoring and the message.
- No reproducible backtest in the repo.

## Deliverable 3 — Critical findings

| ID | Sev | Tag | Finding | Evidence | Consequence |
|---|---|---|---|---|---|
| C1 | Critical | [V] mechanism, [S] for these symbols at 15:50 | **A partial intraday bar is treated as a close.** yfinance's daily download includes the current session's row, with Close = the live price. | `lastrow.py` at 06:38 UTC: 0700.HK's row for 2026-10-27 has Close 385.60, equal to `fast_info.last_price` 385.60, with marketState REGULAR; the same for RELIANCE.NS. At 15:50 UTC every market in the universe is open (US 13:30–20:00 UTC; Xetra/Euronext/LSE until 16:30 UTC in winter; BÉT until 16:00 UTC). Nothing drops today's row: prices.py:18–32, analysis.py:28. | At the afternoon scan, RSI, SMA, momentum, `last`, the limits, the "big move" alerts and the /stats entry prices are all intraday. Calls can flip between the 06:50 and 15:50 runs, which causes message churn [H]. |
| C2 | Critical | [V] | **Stale close used silently.** At 06:37 UTC, all 22 EU, UK, HU and ETF symbols returned their 2026-10-26 row with O/H/L/Volume but **Close = NaN**. MTELEKOM.BD has no 10-26 row at all; its last row is 10-22. `Close.dropna()` therefore makes the 10-23 close the "last" price, with no warning. | `checks.py` output, the NaNrow lines: SAP.DE, ASML.AS, OTP.BD, MOL.BD, CSPX.L and others. `Ticker.history` shows the same. The cause is a [H]: 10-26 is the first session after the EU/UK DST change (the offset moved to +01:00/+00:00), and AAPL, whose DST change is still to come, is fine. | Limits ("Buy at up to X"), the dip limits, the stops and the movers are computed from a price one session old. The 06:50 scan today would have used it. |
| C3 | Critical | [V] | **Sizing over-commits cash.** Every BUY and WAIT_FOR_DIP gets `cash_huf / 2` (scan.py:291), and the message shows up to 5 BUYs (cloud.py:40,163) plus every dip. | Live run with an example cash of 1,000,000 HUF (a test figure, not a real balance): 11 BUY items whose sizes add up to **4,297,563 HUF**. The message's top 5 alone come to about 2.5× cash. | Following the message literally is impossible. Following it partly means the first two picks take all the cash, decided by score order alone. |
| C4 | High | [V] | **Concentration.** Today's top-5 BUYs: MU, ASML.AS, AMD, TSM, ANET. All five are in Yahoo's sector "Technology", four are semiconductor names, and their average pairwise correlation is **0.56** (126-day daily returns). | `checks.py`. An equal-weight basket of the five has 49% annualised volatility, against 12% for the S&P 500 over the same window. There is no sector or correlation cap anywhere in the code. | One semiconductor drawdown hits every new position at once. |
| C5 | High | [V] | **The market regime is only printed.** `market_regime` is used in `regime_line` and the weekly summary only (cloud.py:325,338,269). `_call` never sees it. | grep. | The header says "Market: US bearish", yet BUY calls still go out. Note that my backtest does **not** support a simple SPY>SMA200 gate either (D6). |
| C6 | High | [V] | **No user-visible failure signal.** The paths that end silently: the yfinance outage abort (cloud.py:321–323 prints and returns 1), a failed send (cloud.py:366–368), and a cron skip (nothing runs at all). GitHub's failure email covers the first two at best; nothing covers the third. | Code. Run history: commands slots 05:00–06:20 UTC today did not run. The scheduled run at 22:46 UTC is outside the `5-21` hour window, so it was a 21:xx slot started ≥ 66 min late. | Silence looks the same as "no new calls". |
| C7 | High | [S]/[U] | **The tax rate may be wrong.** tax.py:1–3,10 uses 28% (15% SZJA + 13% szocho) with offsets only within the same year. Hungarian sources describe *tőzsdei ügylet* / *ellenőrzött tőkepiaci ügylet* (Szja tv. 67/A) as 15% SZJA with no szocho, and losses offsettable against gains in prior years (two years). | Web search: lightyear.com/hu-hu/blog/a-befektetesek-adozasarol-roeviden and the adozona.hu "ellenőrzött tőkepiaci ügylet" articles. **Not confirmed against the statute or NAV.** | /tax, the December helper and the SWITCH tax hurdles could overstate tax by about 1.9×. That suppresses useful switches and gives the user a wrong number. |
| C8 | Medium | [V] | **Fee minimums make small trades expensive.** The default budget is 200,000 HUF, so cash/2 = 100k HUF ≈ 304 USD, and the fee is 3.49 USD = **1.15% per side**. The 0.24% rate only applies from about 1,454 USD (≈ 478k HUF) per trade. The message never shows the fee percentage. | fees.py:6–14; arithmetic with USDHUF 328.9 (live). | The round trip costs 2.3% plus a 0.3% FX spread on each conversion. In the backtest, at 600 USD of capital, the after-tax CAGR fell from 30.6% to 21.7% (the S&P100 momentum variant). |
| C9 | Medium | [V] | **/stats cannot validate anything.** track.py:39–50 logs each sent call per (date, symbol, call): the same BUY on later days is a new sample, and calls are logged only when a message is sent. The returns mix currencies against CSPX.L in USD and leave out fees, tax and dividends. MIN_N is 10. | Code. | The samples overlap and are autocorrelated, so 10 can be reached by 2 symbols in a week. The FX swing in the EUR/HUF names is counted as "edge". |
| C10 | Medium | [V] | **A poison outbox.** A permanent Telegram error (400 "message is too long", 403 "bot was blocked") is retried for ever: commands.py:301–319 → `outbox_pending=1`, so precheck says work=1 on every run, and every 20 minutes the job runs a full pip install and fails. | Code path. | The job fails for ever, each run is a billed minute, and there is no recovery. |
| C11 | Medium | [V] | **Truncation hides sections.** cloud.py:197–198 cuts the text at 4,000 characters. The order is ALERTS → header → BUY → SWITCH → WAIT → **SELL → HOLDINGS → DIPS**, so a busy alert day cuts the SELL calls first. The weekly summary is never length-checked. | Code. | Exactly the messages that matter most (SELL) are dropped on the busiest days. |
| C12 | Medium | [V] | **Alert fatigue.** The ±7% / −5% daily-move alerts on holdings and the top-10, applied to today's top-10 over the last 250 days, would have fired on **123 of 250 days (49%)**, with 259 events. Any fresh alert forces a send (cloud.py:351). | `checks.py`. | Close to two messages a day. The important SELL and stop alerts get lost in the noise. |

## Deliverable 4 — Improvement register (the full list)

Format: ID · priority · tag · problem → fix · files · test · 0 Ft status · effort and risk.

### Data integrity

**R1 · P0 · [V] · Freshness and partial-bar guard.**
- Fix: in `prices.download`, per symbol:
  - (a) drop any row whose Close is NaN;
  - (b) drop the row for *today* when that exchange's session has not closed yet. Use `exchange_calendars` via a suffix→MIC map: .DE→XETR, .AS→XAMS, .PA→XPAR, .CO→XCSE, .BD→XBUD, .L→XLON, US→XNYS;
  - (c) return the last-bar date per symbol.
- In `run_scan`, mark a symbol **stale** when its last bar is older than its exchange's previous completed session. A stale symbol gets no BUY or SELL; it gets the call `DATA?`.
- The header gets "N symbols stale: …".
- Simpler alternative, or as well: move the second cron from 15:50 to about 21:15 UTC, after the US close. That halves the problem at no cost.
- Files: prices.py, scan.py, cloud.py, scan.yml, plus a new `hours.MIC_BY_SUFFIX`.
- Tests:
  - a synthetic history whose last row has Close=NaN → `last` is the previous close and the symbol is flagged stale;
  - a row stamped today while the session is open → it is dropped (freeze `now` at 15:50 UTC);
  - MTELEKOM-style missing session → stale.
- 0 Ft: local. Effort: about half a day. Risk: holiday-calendar mismatches; check against `exchange_calendars`.

**R2 · P1 · [V] · Data-validation rules.**
- Rules: reject or flag Close ≤ 0, duplicate index entries, a one-day move > 40% unless a split shows in `Ticker.splits`, and ≥ 5 identical closes in a row.
- Today only APP's +46% on 2024-11-07 (earnings) would trigger, so the rule should *flag*, not drop.
- Files: new `invest_bot/validate.py`; called from cloud.fetch.
- Test: synthetic frames, one per rule.

**R3 · P2 · [V] · Price field: dividends are ignored.**
- `auto_adjust=False` with Close means momentum leaves dividends out. Over 12 months the gap is −2% to −6%: MOL.BD −5.9%, MTELEKOM −6.0%, ALV −4.4%, NOVO −4.5% (`checks.py`, 12-month close vs adjusted gap).
- That biases the ranking against dividend payers, and /stats compares price-only stocks with an accumulating CSPX.
- Fix: rank on `Adj Close`, and keep `Close` for the limits.
- Files: scan.features, track.py.
- Test: a frame where Close ≠ Adj Close → mom uses Adj.

### Signal and score

**R4 · P1 · [V]/[H] · Make the score honest.**

Evidence:
- The momentum part and the trend part have a Spearman correlation of **0.87**, so trend is mostly a second copy of momentum.
- Upside correlates −0.41 with momentum and −0.53 with trend: analyst targets lag the price.
- Growth and upside are today's values only, so they cannot be backtested (no point-in-time data).
- In the backtest, the price-only composite did worse than pure 12-1 momentum (D6).

Fix:
- Rank on 12-1 momentum (total return).
- Keep a binary absolute filter: price > SMA200, or 12-1 > 0.
- Show growth and analyst upside as *information lines* only, not as points.
- Relabel the score as an "uncalibrated rank". Never call it a probability.
- Keep the old score in shadow for 3–6 months, logged in the state, and compare the two forward (R9).

Files: scan.py (`potential`, `_call`), cloud.py text, README.

Test: the ranking equals the momentum sort; the BUY set is a subset of the filter set.

Risk: this changes the user-facing calls, so stage it behind a flag.

**R5 · P1 · [V] · Concentration cap.**
- Fix: when choosing the top-N BUYs, skip a candidate if:
  - (a) its Yahoo `sector`/`industry` (from info; cache it in the state) already has 2 picks, or
  - (b) its 6-month daily-return correlation with any pick so far is > 0.75.
- Files: scan.py or cloud.py (selection step).
- Test: 5 synthetic, perfectly correlated series → only 2 survive.

**R6 · P2 · [H] · Extension guard.**
- Today's BUYs include MU (+337% over 12 months, +23% above SMA200), DDOG (+43% above SMA200) and SNOW (+41%). The only guard is RSI > 70.
- Do **not** add a rule without testing it. The hypothesis is "picks more than 40% above SMA200 have a worse next-month excess return".
- Test it in `bt.py` with a walk-forward split, then decide.
- 0 Ft.

**R7 · P2 · [V] · Regime.**
- Either remove the regime from the header, or keep it clearly labelled "information only".
- The backtest shows a SPY>SMA200 gate cut CAGR (S&P100: 30.6% → 16.1%) **without** lowering the maximum drawdown (−37% → −41%). Do not gate on it.

**R8 · P2 · [V] · Analyst and earnings data.**
- No currency mismatch was found today: TSM's financialCurrency is TWD but its target is in USD, consistent with the price.
- Fix: keep the check `abs(target/last − 1) > 1.0` → treat the upside as unknown.
- The EU earnings dates are often **past**: MC.PA 2026-07-27, ADYEN.AS 08-13, SU.PA 07-30, ASML 10-14, SAP 10-21. `earnings_in` then returns None and gives no protection.
- Fix: when the date is in the past and more than 80 days old, flag "next earnings date unknown" instead of saying nothing.
- Test: a fund with an old date → the flag appears.

### Validation

**R9 · P1 · [V] · Rebuild /stats as episode-based, HUF-based tracking.**
- Log one entry per (symbol, call) *episode*, from the first appearance until the call changes.
- Value both the pick and the benchmark in HUF at Yahoo FX, net of the fee model.
- Also track a shadow "pure 12-1 momentum top-5" and the current score's top-5 as paper portfolios, rebalanced monthly in the state.
- Report the excess with a block-bootstrap interval only once n ≥ 30 *independent months*.
- Files: track.py, cloud.py.
- Test: repeated identical calls over 5 days → 1 episode.

**R10 · P1 · [V] · Commit the backtest.**
- Add `tools/backtest.py`, adapted from `scratchpad/audit/bt.py`, offline-cacheable.
- Docs: state the biases and drop the claim at scan.py:5 until the backtest is in the repo.

### Risk and portfolio

**R11 · P0 · [V] · Sizing.**
- Budget per pick = cash / max(1, number of BUY picks shown), capped at cash / 3.
- Skip a pick when its fee would be more than 1% of the trade ("too small for the K&H minimum fee").
- Show the total committed money and the fee % per line.
- Files: scan.run_scan, cloud.message.
- Test: the sum of approx_huf over the shown BUYs is ≤ cash; a 100k HUF USD pick shows fee ≈ 1.1% and is flagged.

**R12 · P1 · [V] · Exits.**
- Today the exits are: SELL only when bearish (`rate` score ≤ 1), plus a weekly trailing or hard-stop *alert* at 15%.
- Make it explicit in the message that a stop is an alert, not an order. The price can gap through it, so it is not a guaranteed fill.
- Add a "momentum exit": a holding that falls out of the top 30% of 12-1 momentum for 2 consecutive month-ends → SELL candidate (tax-aware via tax.py).
- This must be validated in `bt.py` first [H].

**R13 · P2 · [S] · FX exposure.**
- About 70% of the universe is USD/EUR, and the HUF returns carry FX risk.
- Show the portfolio's currency mix in the weekly summary. That is information only.

### Tax and fees

**R14 · P0 · [S]/[U] · The tax rule.**
- Make `RATE` configurable (`TAX_RATE` repo variable, default 0.15 or 0.28 after the user confirms with NAV or an advisor).
- Add a carry-back note once confirmed.
- Show the ±1 assumption in /tax.
- Test: the rate is read from the env, and the December text changes with it.

**R15 · P2 · [V] · MNB rate.**
- The law converts at the MNB official rate. The bot stores Yahoo FX per trade (commands.py:139–144); the gap is usually a few tenths of a percent [S].
- Fix: fetch MNB rates from the free MNB SOAP service (`arfolyamok.asmx`, `GetExchangeRates`), with no key, when recording a trade or for `/tax`, and store `fx_mnb` next to `fx`.
- 0 Ft: free; its terms of use and rate limit are unverified.
- Test: mocked SOAP XML.

**R16 · P3 · [V] · Fees in other currencies.**
- DKK (NOVO-B.CO) and GBp use the EUR minimum, converted (scan.py:212–218). Whether K&H even offers Copenhagen or Paris for this account, and at what tariff, is **[U]**.
- Fix: limit the universe to markets the user has confirmed are tradable at K&H.

### Reliability

**R17 · P1 · [V] · Failure notification and heartbeat.**
- (a) On the outage abort and on a send failure, try one short Telegram message: "Scan failed: data outage (N/86 symbols)".
- (b) The scan writes a plaintext `state/last_scan.txt` (a UTC timestamp, not sensitive). On weekdays, if the last scan is more than 26 hours old, `precheck` returns work=1 and `commands.run` sends "No scan since …".
- Files: cloud.py, precheck.py, state.py, state.sh (copy the file).
- Test: an old timestamp → a warning is queued exactly once (dedupe key in the state).

**R18 · P1 · [V] · Permanent Telegram errors.**
- Classify HTTP 400/403 (and "message is too long") as permanent.
- Split long text at section boundaries into chunks of ≤ 4,000 characters instead of truncating; never cut SELL.
- Drop a reply that is permanently undeliverable after logging it.
- Files: cloud.send_telegram, cloud.message, commands.run.
- Test: a 9,000-character message → 3 sends, with SELL inside; a 403 → the outbox is cleared and the return code is 1 once.

**R19 · P2 · [V] · Concurrency.**
- In a GitHub concurrency group, a newly queued run **cancels the pending one** [S, GitHub docs]. With commands every 20 minutes, a pending scan can be cancelled when a commands run is in progress and another one queues up.
- Fix: put the scan at a minute that commands never use (it already is, at :50), and have the commands precheck exit early if a scan ran within the last 5 minutes [H, rare].
- Alternative: a scan-specific group plus the existing lease, and on a lease conflict, re-fetch and re-run the save.

**R20 · P2 · [V] · Minutes.**
- Commands cron: `*/20 5-21 * * *` = 51 runs a day, 7 days a week. Each job is billed at ≥ 1 minute, so about **1,550 min/month nominal**. The scans add about 44.
- That is **~1,600 of the 2,000 free minutes** [V arithmetic; S billing rule].
- Measured: only 6 runs so far (repo created on 2026-10-26), each 8–40 s, so 1 billed minute each.
- Fix: `*/20 5-21 * * 1-5` plus `0 */2 * * 0,6` on weekends → about 1,170 min/month. Or `*/30` → about 1,050.

**R21 · P3 · [V] · yfinance failure modes.**
- A partial batch failure is handled: symbols are left out, and the run aborts if fewer than half remain.
- An info() failure gives `{}`, which yields neutral growth and upside (12.5 + 10 free points: abstention dressed as confidence).
- Fix: when the fundamentals are empty, cap the score below `BUY_SCORE`, or show "fundamentals missing".
- There are no retries or backoff on download.
- Fix: one retry after 30 s when more than 10% of symbols are missing.
- Test: a fake download that drops 20% the first time.

### Security and privacy

**R22 · P2 · [V] · State key.**
- The Fernet key is SHA256("invest-bot-state:" + token) (state.py:29–30). Anyone with the bot token can decrypt the state, and rotating the token wipes it (handled with reset).
- Fix: a separate free secret `STATE_KEY`, falling back to the token-derived key.
- Trade-off: one more secret to keep safe, but a leaked token no longer exposes the holdings.

**R23 · P3 · [V] · Logs.**
- cloud.py:348–349 logs every call except SWITCH. **SELL calls exist only for held symbols, so the log reveals holdings**, against the module docstring. The logs are private, so the severity is low.
- Fix: log counts only.

**R24 · P3 · [V] · Supply chain.**
- Actions are pinned by tag (`@v4`, `@v5`), not by SHA. requirements-cloud.txt pins direct dependencies only; transitive dependencies (requests deps, beautifulsoup4, protobuf, …) float.
- Fix: pin actions by SHA, generate a full lock (`pip-compile --generate-hashes`, free), and run `pip-audit` monthly (free).

**R25 · P3 · [V] · Chat filter.**
- The chat filter is correct (commands.py:266–270).
- precheck counts *any* update, so a stranger messaging the bot triggers full runs. That costs minutes only.
- Fix: precheck peeks at `from.id`.

### Telegram UX

**R26 · P1 · [V] · Fewer, higher-quality alerts.**
- Raise the mover threshold to a volatility-scaled move: |move| > 2.5 × the daily sigma.
- Apply it to *holdings only*, not the top-10.
- Today's 49%-of-days rate should drop a lot [H; measure with `checks.py`].

**R27 · P2 · [V] · Message clarity.**
- Each BUY line should show: the data date (the close used), fee %, the total committed, the sector, and "invalidation: weekly close < SMA200 or drops out of the top-30% momentum".
- Add a short signal ID, `SYM-YYYYMMDD`.

### Zero-cost compliance (all [V] unless marked)

- **yfinance / Yahoo**:
  - free, rate-limited, **unofficial** scraping of Yahoo endpoints;
  - Yahoo's terms allow personal use only [S];
  - it can break without warning;
  - cost 0, risk: outages.
- **Telegram Bot API**: free, needs an account; rate limits of about 1 message per second per chat [S]; no charges.
- **GitHub Actions on a private repo**:
  - free within an allowance of 2,000 min/month on the Free plan [S];
  - jobs stop at the cap unless a payment method and spending limit were set, so it is *costly only if misconfigured*;
  - the user should check that the Actions spending limit is $0 [U].
- **exchange_calendars, pandas, numpy, cryptography, curl_cffi**: local and free.
- **MNB SOAP (proposed)**: free, no account; rate limits and terms not verified [U].
- **pip-audit and pip-tools (proposed)**: local and free.

## Deliverable 5 — Zero-cost architecture

Keep the current architecture. It is already the cheapest one that works: Actions, the state branch, and Telegram. Changes:
1. Add a `validate` step between fetch and scan (R1, R2). Stale or invalid data → the symbol abstains, and the header names it.
2. Move the second scan to 21:15 UTC, after the US close and with every market in the universe closed. Then daily bars are complete closes, and it is still a weekday. Keep 06:50 for EU morning orders.
3. Add a portfolio-risk step before the message (R5, R11): N picks, sector and correlation caps, fee floor, total ≤ cash.
4. Add a heartbeat file and failure messages (R17).
5. Keep the backtest offline, run by hand on a PC (R10, R9).

Ongoing costs: 0 Ft. Limits: about 1,200–1,600 Actions minutes a month; Yahoo availability; Telegram limits; GitHub cron delays of up to about an hour, plus skips (seen).

## Deliverable 6 — Strategy validation plan, and the backtest that was run

**What was run** (`scratchpad/audit/bt.py` and `bt2.py`, 17 s of runtime):
- Data: yfinance `auto_adjust=True` (total return), 2012-01 to 2026-10-26, monthly.
- Signals: computed at the previous close; trades at the first close of the month.
- Selection: top-5 equal weight. Positions that stay in the top-5 are kept; dropped ones are sold, and the cash goes to the new ones.
- Costs: K&H fee max(0.24%, 3.49 USD) per side, plus 0.1% slippage.
- Tax: 28% of each calendar year's net realised gain is paid from cash each January ("CAGR"). "Final after-tax" additionally liquidates at the end. SPY is taxed only at the end.
- Not modelled: FX (both legs are USD), dividends tax, whole shares.
- Universes:
  - (a) the **current** S&P 100 list from memory (one ticker, BK, failed to download);
  - (b) the bot's 53 US stocks.

Full period 2014-01 to 2026-10, starting capital 3,000 USD unless stated:

| Strategy | CAGR (annual tax paid) | Final after-tax | MaxDD | Trades/yr | Monthly excess vs SPY (t) |
|---|---|---|---|---|---|
| SPY buy & hold | +13.8% (no tax drag) | +11.5% | −24% | – | – |
| S&P100 list, 1/N, no costs | +16.3% | – | – | – | – |
| **S&P100, 12-1 momentum top-5** | **+30.6%** | +28.6% | −37% | 35 | +1.47% (t=2.6) |
| S&P100, bot price-score top-5 (bullish, RSI≤70) | +18.2% | +17.3% | −43% | 56 | +0.59% (t=1.1) |
| S&P100, momentum, 600 USD (≈200k HUF) | +21.7% | +19.8% | −40% | 35 | +0.91% (t=1.5) |
| S&P100, momentum + SPY>SMA200 gate | +16.1% | +15.7% | −41% | 37 | +0.36% (t=0.6) |
| S&P100, momentum, 0.5% slippage | +28.2% | +26.2% | −37% | 35 | +1.32% |
| Bot US list, 1/N, no costs | +30.4% | – | – | – | – |
| Bot US list, momentum top-5 | +41.8% | +39.6% | −54% | 27 | +2.34% (t=3.4) |
| Bot US list, bot price-score top-5 | +36.6% | +35.0% | −32% | 49 | +1.98% (t=3.0) |

Without tax (S&P100 momentum, 3,000 USD): +36.9%; so the annual 28% tax costs about 6 points of CAGR.

Sub-periods (S&P100 momentum / price-score / SPY):
- 2014–19: +22.8% / +14.6% / +11.5% before tax-at-end.
- 2020–26: +37.4% / +19.0% / +15.1%.

The price-score lost to momentum in both halves.

Known artifact: the S&P100 price-score run at 600 USD showed −37% CAGR. My "split the leftover cash" rule then makes sub-$5 buys that the minimum fee wipes out. Ignore that row; it only shows that tiny allocations are destructive.

**How to read it (blunt):**
- The bot's own universe is **hindsight-selected**. Its 1/N basket alone made +30.4% a year, against +13.8% for SPY. Every number on that universe is mostly selection bias.
- The current S&P 100 list is survivorship-biased too: it contains PLTR, TSLA, NVDA, UBER and AVGO *because* they rose. The +30.6% overstates live performance by an unknown margin [S].
- The robust, **relative** conclusion is that on both universes and in both sub-periods, the bot's price-score (momentum + trend + RSI filter) did **worse than plain 12-1 momentum**, with 40–60% more trades.
- The 45 points of growth and analyst upside cannot be tested at all, because Yahoo gives no point-in-time history.
- So there is **no evidence the composite score has an edge beyond plain momentum, and some evidence it is worse** [V on biased data; the direction is consistent].
- The minimum fee alone costs about 9 CAGR points at 600 USD of capital.

**The proposed standard**, for each change:
- a hypothesis and the baseline (pure 12-1 momentum top-5, and SPY or CSPX);
- walk-forward: freeze the parameters on 2014–2019 and report 2020–2026 once;
- costs at 0.1% and 0.5% slippage plus the fee minimums for the user's real capital;
- 28% and 15% tax;
- a block bootstrap of the monthly excess (12-month blocks);
- **forward**: the R9 shadow portfolios for at least 12 months before any claim.

Decision rules:
- *Retain* a component only if it beats the simpler alternative out of sample after costs, in both sub-periods.
- *Reject* it if it does not.
- Negative results are recorded in the README.

## Deliverable 7 — Risk specification (deterministic checks before "actionable")

A pick is actionable only if all of these hold:
1. the data is fresh (last bar = the exchange's last completed session) and valid (R1, R2);
2. fundamentals are present, or else the line is marked and the score capped;
3. it ranks in the top-5 *after* the caps: ≤ 2 per sector, correlation ≤ 0.75 with the earlier picks;
4. the fee at the planned size is ≤ 1% (else: "too small");
5. the total committed is ≤ free cash, and the position count is ≤ a configurable maximum (e.g. 8);
6. no earnings within 3 trading days, or an unknown date is flagged;
7. no negative headline about the company (existing).

Each line also states the invalidation rule (R12) and "a stop is an alert, not a guaranteed fill".

- **Warn**: the regime is bearish (information only), very volatile (vol20 > 60%), or the earnings date is unknown.
- **Downgrade to WAIT**: earnings, news, more than 40% above SMA200 *only if* R6 validates it.
- **Abstain**: stale or invalid data, fee > 1%, the cap is reached, or fewer than half the symbols were fetched.
- **Never**: adding size to recover a loss. The code has no such path today [V].

## Deliverable 8 — Telegram alert specification

**Lifecycle per (symbol, call) episode** (stored in the state, keyed `SYM-YYYYMMDD-CALL`):
- `ACTIVE`: first seen, so it is sent.
- `STILL_ACTIVE`: not re-sent, and shown only in /top.
- `EXPIRED`: valid_until has passed.
- `INVALIDATED`: the call changed, so it is sent once as "X: BUY withdrawn (reason)".
- `ERROR`: the send failed. Not logged to /stats until delivered, to stay idempotent. Today `track.log` already runs after a successful send [V].
- A failed send keeps the same IDs, so it never creates a new recommendation.

**Message structure** (hypothetical example data):

```
Invest Bot · close data 2026-10-26 (US) / 2026-10-26 (EU) · sent 06:52 UTC
Market (info only): US bullish, EU bullish · 2 symbols stale: MTELEKOM.BD, …
Cash ≈ 1 000 000 HUF · 3 picks ≈ 960 000 HUF total

NEW BUY
1. MU-20261027  Micron (US, Semis) 12-1 mom +210%, rank 1/80
   Limit 905.50 USD till 2026-10-28 · 3 sh ≈ 897 000 HUF · fee 0.24%
   Invalid if: weekly close < SMA200 or rank leaves top 30%
   Risks: +23% above SMA200, vol 52%, earnings date 2026-12-17
STILL ACTIVE (not re-sent): ASML, TSM — see /top
WITHDRAWN: ANET (earnings in 2 days)
SELL / STOPS …
```

**Failure handling**:
- bounded retry: 2 tries with 5 s backoff on 5xx or timeouts;
- permanent 4xx: drop and log;
- split at 4,000 characters on section boundaries;
- the heartbeat (R17).

## Deliverable 9 — Implementation roadmap, with the file-level plan (instead of Deliverable 10)

**Phase 0 (P0 correctness; about 1–2 days)**
1. **R1 freshness and partial bars.**
   - prices.py: drop NaN-Close rows and today's unfinished row.
   - hours.py: `mic_for(symbol)` and `last_completed_session(mic, now)`.
   - scan.py: `stale` → `DATA?`.
   - cloud.py: header line.
   - scan.yml: cron `15 21 * * 1-5`.
   - Tests: new `tests/test_validate.py` covering the NaN-close, open-session and missing-session cases.
2. **R11 sizing.** scan.run_scan budget split and fee floor; cloud.message total. Tests in test_scan.py and test_cloud.py.
3. **R14 tax rate.** tax.py `RATE` from env `TAX_RATE`; README; tests in test_tax.py. The user must confirm the rate with NAV or an advisor first.

**Phase 1 (P1 reliability and honesty)**

4. R17 heartbeat and failure messages (cloud.py, precheck.py, state.py, state.sh, tests).
5. R18 split and permanent errors (cloud.py, commands.py, tests).
6. R5 concentration caps (scan.py or cloud.py; sector cached from info; tests).
7. R26 alert threshold, volatility-scaled and holdings-only (news.py, tests).
8. R9 episode tracking and shadow portfolios (track.py, tests).
9. R10 commit `tools/backtest.py`; fix the scan.py:5 docstring.
10. R4 behind a flag `SCORE_MODE=momentum|legacy`; run both in shadow.

**Phase 2 (P2, after evidence)**
- R6 extension guard, if it validates;
- R12 momentum exit;
- R3 adjusted close;
- R8 earnings flag;
- R15 MNB rate;
- R20 cron trim;
- R22 STATE_KEY.

**Phase 3**
- R23 logs;
- R24 pins and pip-audit;
- R25 precheck sender check.

Rollback: every change is one commit, and a behavioural change sits behind an env flag with the legacy default for one week. The state stays backward-compatible: new keys are optional and `dict(empty(), **data)` already tolerates them [V].

## Deliverable 11 — Ongoing evaluation

**Weekly**, in the Monday summary (automated, free):
- data health: stale or missing symbols this week, abort count;
- runs: scans expected vs seen (heartbeat), sends failed;
- alert volume per week;
- the shadow-portfolio excess of momentum vs legacy vs CSPX in HUF.

**Monthly** (by hand, 10 minutes):
- `gh run list` minutes (target < 1,500);
- `pip-audit`;
- re-run `tools/backtest.py` with the new month appended (never re-tune on it).

**Disable rules**:
- a component whose shadow trails the simpler baseline by more than 5% over 12 months → turn it off;
- any day with more than 20% of symbols stale → no actionable calls that day (automatic via R1);
- more than 2 consecutive failed scans → the heartbeat warning repeats daily until fixed.

## Deliverable 12 — Independent final challenge

**Three most questionable assumptions**:
1. The C2 cause is a DST glitch, and it is transient. If Yahoo's daily EU closes are routinely NaN until later in the morning, the 06:50 scan is *systematically* one day stale. Smallest test: log the per-symbol last-bar date for 2 weeks (R1 does this).
2. The tax claims: 15% with no szocho, plus the carry-back. They come from secondary sources, not the statute. The user must verify them.
3. That momentum is "better" than the composite. All the backtest universes are survivor- or hindsight-biased, and the comparison is relative, not absolute.

**Counterarguments**:
- Pure momentum has had brutal crashes (2009 momentum crash); the composite's trend and RSI filters gave a lower MaxDD on the bot universe (−32% vs −54%).
- Dropping them may raise drawdowns. Hence: shadow first, not a switch.

**Possibly not worth doing**: R6 (extension guard), R13 (FX lines), R15 (MNB rates; the tax gap is small relative to effort), R19 (the concurrency edge case).

**Unresolved**:
- Yahoo is unofficial and can disappear. There is no free, licensed replacement with EU and HU coverage [U].
- Whether K&H offers every listed venue (Copenhagen, Paris) is unknown.

## Don't do

- ML or deep-learning price prediction, sentiment models, RL. There is no point-in-time dataset and too few independent samples; they would overfit.
- More technical indicators (MACD, Bollinger, …). They are mostly another copy of the existing momentum and trend.
- An LLM commentary layer. It adds cost and can invent facts, and every number here is already computed by code.
- Paid data, a VPS, or intraday data. They break the 0 Ft rule, and intraday timing is irrelevant for monthly-horizon manual trades.
- A regime gate on SPY>SMA200. It was tested here and gave a worse CAGR with no drawdown benefit.
- More alert types. Alert fatigue is already measured at 49% of days.
- Tuning BUY_SCORE, the weights or the thresholds on past data before R9/R10 exist. That is multiple testing on a biased sample.

## Verified facts vs assumptions

| Statement | Status |
|---|---|
| No LLM in the decision path | V (grep) |
| 139 tests pass at HEAD | V |
| Indicators match an independent pandas reference: RSI (Wilder recursion and ewm alpha=1/14), SMA50/200, 12-1 momentum, vol20, dd_52w | V, max relative diff ≤ 1e-14 over 86 symbols |
| Indicator warm-ups: SMA50 n≥50, SMA200 n≥200, RSI n≥15, 12-1 momentum n>252, dd n≥252; NaN handled by dropna | V (analysis.py:28–45, scan.py:115–117) |
| The batch download's index is tz-naive; each symbol's own NaN rows are dropped (prices.py:27); no duplicate dates found | V |
| The daily download contains a live partial bar while a session is open | V (HK/NSE now); S (bot symbols at 15:50) |
| EU/UK/HU closes for 2026-10-26 were NaN at 06:37 UTC on 10-27, so the bot used the 10-23 close | V |
| The cause is the DST transition | H |
| Sizing commits about 4.3× cash with 11 BUYs (1M HUF test) | V |
| Today's top-5 are all Technology, average correlation 0.56 | V |
| Momentum vs trend Spearman 0.87; upside vs momentum −0.41 | V (one snapshot) |
| The regime is not used for gating | V |
| Analyst-target currency mismatch | not observed today (V); still possible for other listings (H) |
| The composite does worse than momentum | V on biased universes; H for live use |
| Tax is 15% with no szocho and has a carry-back | S/U, needs NAV confirmation |
| The MNB rate is legally required; Yahoo differs slightly | S |
| Minutes: about 1,600 of 2,000 nominal | V arithmetic; S billing rule |
| Cron delays and skips | V (a ≥ 66 min delay seen; 05:00–06:20 slots missing today) |
| A pending run in a concurrency group gets cancelled | S (GitHub docs) |
| K&H venue coverage and tariffs for DKK and GBp | U |
| Yahoo's terms allow personal use only | S |
