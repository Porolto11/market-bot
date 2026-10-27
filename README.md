# invest-bot

A Telegram market bot that runs in GitHub Actions as an always-on listener (`python -m invest_bot.listen`, see
`.github/workflows/listen.yml`).

## How it runs

- **Always on.** Each listener run long-polls Telegram for 345 minutes, so command replies arrive within seconds.
  Near the end it saves the state and starts the next run itself (`workflow_dispatch` of `listen.yml`); a run every
  2 hours from the schedule is only a fallback that restarts a broken chain (it exits at once while another
  listener run is active or queued). A public repo gets these Actions minutes free.
- **Scans inside the listener** on weekdays at 06:50 and 21:15 UTC: 07:50 and 22:15 Budapest in winter (08:50 and
  23:15 in summer). The slot done is kept in the state, so a restart never repeats it; a slot missed by up to 3
  hours (no listener was running) is caught up.
- **One state writer.** The listener is the only process that writes the state while it runs. It rewrites the
  file only when the state changed and pushes it to the `state` branch at once (offset-only changes at most once a
  minute, and always before it exits). `scan.yml` and `commands.yml` are kept for manual and debug runs only; they
  share the listener's concurrency group, so a manual run waits until the listener run ends (and is replaced if the
  listener's next run is queued after it).

## What it does

On weekdays at 06:50 and 21:15 UTC (after the US close) it scans about 80 stocks and ETFs and sends BUY /
WAIT_FOR_DIP / SELL calls to Telegram. Only completed daily closes count: today's unfinished bar is dropped, and a
symbol without a close for its exchange's last completed session gets no BUY or SELL call ("stale data"). It never trades: you place the orders yourself in the K&H app. Each BUY line says how many whole
shares fit your budget. The BUY section is a BUY PLAN: the top 3 picks your cash (or the budget) can actually buy,
e.g. "Buy 3× Micron (MU) at ≤ $905.5 ≈ 897 000 HUF + fee 2 150 HUF (0.24%)", and a total line. The K&H fee is 0.24% with a
minimum per market (by the exchange suffix: US 3.49 USD, BÉT 349 HUF, Xetra/Euronext/LSE/Dublin 3.49 EUR, Nordic,
Swiss, Vienna, Madrid and Milan 11.99 EUR, converted to the trade currency, e.g. DKK for Novo Nordisk). A pick needs its
smallest ticket with a K&H fee of at most 1% to fit into a third of the cash (when fewer fit, one may use the rest);
the cash is split equally in whole shares and the leftover goes to the highest-ranked pick. Value + fee + K&H's 1% FX
conversion spread always stay within the cash less a 1% buffer. The other BUY calls are a watchlist (names and scores). The picks are spread out:
at most 2 per sector (Yahoo's sector; ETFs count as one) and none whose 6-month daily returns correlate more than
0.75 with a higher pick ("skipped: too similar to MU"). The Monday morning scan also sends a short weekly
summary: portfolio value in HUF and its change since the last summary (deposits and withdrawals left out), the
week's best and worst holding, the `/stats` line, the market regime and one line each for EUR/HUF and USD/HUF
against the last 3 months ("cheaper than 85% of the last 3 months (good time to convert)" or "pricier than ... consider
waiting", only when the rate is in the cheapest or priciest 20%). From Dec 1 to Dec 20 it adds a tax check: this year's
realised gains and the 15% tax due (losses carried from the 2 years before, if declared), the holdings at a loss and how much tax selling them would save (losses offset
gains of the same year), a reminder that a sale must settle in the calendar year (act by about Dec 20) and that
Hungary has no wash-sale rule (buying straight back is allowed; check with your tax advisor).

A BUY turns into WAIT when earnings are 3 trading days away or less (4-7 days only adds a flag), or when a Yahoo
headline of the last 3 days on it is negative (downgrade, guidance cut, lawsuit, probe, SEC, recall, misses, fraud,
...) and names the company (its ticker or name: Yahoo also lists sector and crypto stories); a held stock with such a headline gets a flag line. A daily close-to-close move of +7% / -5% or more of a
holding or a top-10 pick goes into the ALERTS section (under the headline) with up to 2 headlines, once per symbol and day,
and is sent even when the calls did not change (headlines naming the company first). A holding whose last close is 15% or more
below its peak close since your first buy (trade log), or 15% or more below your average cost in HUF, is an alert
too ("consider selling"), at most once a week per symbol. So is a core ETF (CSPX, CNDX, VVSM, VWCE) 8% or more below its
52-week high ("good moment to add if you have cash"; 15%: "Big dip"); the same dip is alerted again only
once it is 5 more points deeper or 20 trading days later. A trailing stop on a holding the scan still rates a BUY
(score 70+, bullish trend) says "hold unless it falls further" instead of "consider selling".

When your cash cannot buy one share of a top BUY pick, a SWITCH line may suggest selling a whole weak holding for it:
"SWITCH: sell N× X (score a) → buy M× Y (score b). Tax now ≈ T HUF, fees ≈ F HUF." Only stocks scoring at least 25
below the pick and under 60 (or with a SELL call) qualify, never the ETFs; losers go first. Tax is 15% SZJA (no szocho on
exchange trades) of the gain after this year's realised losses and the declared losses of the 2 years before (FIFO
from the trade log, at the official MNB rates of the buy and sell days), fees are the K&H commission of both trades. A switch is
suggested only when 25+ points of gap x 0.2% per point x the switched value beats tax + fees: a rough heuristic, not
a forecast. At most 2 per message; /stats scores them as SWITCH (won when the bought stock beat the sold one).

Failures are not silent: an aborted scan (e.g. too few symbols priced) sends "⚠️ Scan failed: <reason>. Next try
<time>.", and each scan writes `state/last_scan.txt` (its UTC time). On a weekday with no scan for more than 26
weekday hours, the listener warns once a day. Telegram 400/403 refusals are logged and dropped, not retried for
ever; long messages are split at section boundaries.

## Secrets (repo Settings > Secrets and variables > Actions)

- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`: the bot token and the chat to message.
- `CASH_HUF`, `PORTFOLIO_JSON` (optional fallbacks): cash and holdings, used only while the bot state (below) has none.

Optional repo variable `CLOUD_BUDGET_HUF` (default 200 000): the budget used to size BUY calls when no cash is given.
The listener also uses the job's own `GITHUB_TOKEN` (no setup) to request its next run.

## Privacy

- Secrets (the bot token, the chat id, the optional cash and portfolio fallbacks) live only in GitHub Secrets.
- The state (holdings, cash, trade log, sent calls) is `state/state.enc` on the `state` branch, Fernet-encrypted
  with a key derived from `TELEGRAM_TOKEN`. The plaintext files next to it hold only the Telegram offset, a
  pending-reply flag, the last scan time and the last warning date.
- The Actions logs are public in a public repo, so they print counts, times and error type names only: no tickers,
  amounts, chat ids, message text or tokens.

## Commands

Send these to the bot in its private chat. The listener answers within seconds. Only `TELEGRAM_CHAT_ID` is
answered.

- `/bought SYM QTY PRICE`, `/sold SYM QTY PRICE`: record a trade you made in K&H. QTY is whole shares only. PRICE
  is in the instrument's currency and takes a decimal comma or dot (`120,5` or `120.5`). SYM is a bot ticker or bare
  ticker (`MOL`, `CSPX`, `SAP`, `OTP`) or a Yahoo symbol (`MOL.BD`, `NVDA`); a symbol found only on Yahoo is
  recorded in Yahoo's currency, which the reply names. Cash changes by the value plus the K&H fee; each sale keeps
  a FIFO realised P/L in HUF.
- `/deposit HUF`, `/withdraw HUF`, `/cash HUF` (sets your real balance; send it after entering your holdings).
  Whole forints without separators: `120000`, not `120.000`.
- `/holdings`, `/top` (top 10 of the last scan), `/stats` (hit rate and average edge vs CSPX.L of the sent
  calls after about 1, 3 and 6 months; "too early" under 10 scored calls), `/tax` (realised P/L this year, the estimated 15% tax (MNB rates, losses carried 2 years) and the
  unrealised P/L per holding), `/scan SYM` (live single-symbol analysis), `/undo`, `/help`.

Holdings, cash and the trade log live in `state/state.enc` on the `state` branch, encrypted with a key derived from
`TELEGRAM_TOKEN`. If the token changes, the bot starts empty, keeps the old file as `state.enc.bad-<time>` and asks
you to re-enter holdings. If the state branch cannot be read, the job stops instead of starting empty. With
holdings, the scan adds SELL calls and a holdings section. HUF values use approximate Yahoo FX (the tax figures use the official MNB rates).

## Run locally

    pip install -r requirements-cloud.txt
    python -m invest_bot.cloud --state /tmp/x.enc

With `TELEGRAM_TOKEN` unset it prints the message instead of sending it. Add `--force` to ignore the dedupe state
and `--budget-huf N` to set a budget. `--state-file plain.json` (only without `TELEGRAM_TOKEN`) reads a
plaintext state with test holdings, e.g. `{"log": [{"t": "cash", "huf": 20000, "at": "2026-10-01T10:00:00+00:00"}]}`
(trade entries as the bot logs them: t, sym, qty, price, ccy, fx, fee, at), prints the message and saves nothing. Tests: `pytest`.

## Disclaimer

Not financial advice. The calls are simple rule-based signals from public Yahoo Finance data, which can be wrong or
delayed. There is no K&H login or automation of any kind; you decide and you trade at your own risk.
