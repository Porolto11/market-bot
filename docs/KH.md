# K&H Értékpapír: official facts (Kondíciós Lista effective 2026-10-01, doc 4993)

Source: https://www.khertekpapir.hu/ugyfeltamogatas/dokumentumok/dokumentum/4993, plus /dijjegyzek, /ugyfeltamogatas/tozsdei-adozas and /ugyfeltamogatas/tudastar.

## Fees
| Item | Value |
|---|---|
| **Commission by package** | Platinum 0.24% · Gold 0.26% · Silver 0.30% · Bronz 0.32% · Start 0.34%. Daytrade 0.12%. |
| **Platinum eligibility** | 30M HUF average monthly turnover over the last 3 months, or the 12-month transfer-in promotion. |
| **Minimum fee** | US: 3.49 USD. BÉT: 349 HUF. DE/FR/BE/IE/NL/PT/SK/UK: 3.49 EUR. AT/DK/FI/GR/LU/NO/IT/ES/CH/SE: **11.99 EUR**. Charged separately for each trading day of a multi-day fill. |
| **FX conversion** | **1% on top of the market rate**. A round trip HUF→USD→HUF costs about 2%. Conversion runs 09:00–17:00 on business days. |
| **Buy-side transaction taxes** | France 0.4% · Spain 0.2% · Italy 0.2/0.4% · UK stamp duty 0.5% · Irish shares 1%. ETFs are generally exempt. |
| **US sells** | An SEC fee is added. Pre-market orders cost +10 USD each. |
| **Account and custody fees** | Free on Platinum. Lower packages pay 349 HUF/month and 0.015%. |
| **Other** | Deposits, withdrawals, order placement and cancellation, and real-time data (Platinum) are free. Transferring securities out costs 11 000 HUF per ISIN. |

## Trading
- **Markets online:** BÉT, US, Xetra and German regional exchanges, Euronext, LSE, Dublin, the Nordics, Madrid, Milan, Vienna, Zurich, Prague, Warsaw and Toronto.
- **Fractional shares:** only through the regular ETF plan (15th of the month, 0.6% fee). The bot stays whole-shares only.
- **Order types:** market, limit, stop-limit and stop-market, each with a validity date.
- **US pre-market:** 10:00–15:30 Budapest time, limit orders only, +10 USD.
- **Settlement:** US T+1, everywhere else T+2.

## Tax
- **Tax certificate (adóigazolás):** K&H sends it by **31 January** of the following year.
- **Normal trades:** ETÜ status, taxed at 15% with no szocho (reported on line 172). Fees and data costs are deductible.
- **Dividends:**
  - EEA-listed: no szocho.
  - Non-EEA: 13% szocho, withheld automatically.
  - US: per the FAQ (possibly dated), 30% US withholding plus 5% Hungarian SZJA plus 13% szocho.

## Data
- Transaction history exports to Excel at webtrader.khertekpapir.hu/transactions. No CSV and no public API.

## Still to check in WebTrader
- the current package;
- the actual FX rate versus MNB;
- whether US-listed ETFs are blocked;
- validity options;
- whether USD/EUR cash can be held;
- the US dividend withholding actually applied.
