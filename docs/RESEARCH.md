# Research notes: elemzeskozpont.hu and stdev.hu (2026-10-27)

**Source.** Nagy Attila Zoltán, an evidence-based investing education site. He states he is not a tax or investment adviser. Most claims cite published studies. Treat the tax notes as pointers and verify them at NAV.

## Tax: confirms ROADMAP item 5
- **Rate.** ETÜ (ellenőrzött tőkepiaci ügylet: an EEA/OECD broker, or BSE trades) is taxed at 15% SZJA with no szocho. Non-ETÜ gains also carry szocho.
- **Netting.** Gains and losses net across ETÜ products. Commissions and account fees are deductible.
- **Loss carry.** Losses from the 2 preceding years offset this year's gains, but only if they were **declared** (line 172, column "a"). The bot should remind the user to declare loss years.
- **FX.** Use the official MNB rate on the buy day and on the sell day.
- **Deadlines.** K&H sends the tax certificate (adóigazolás) by 31 January (docs/KH.md); the tax return by 20 May (form SZJA, sheet 04, line 172).
- **Dividends.**
  - EEA-listed: 15%, with no szocho.
  - Non-EEA (e.g. US stocks): 15% plus 13% szocho (capped at 24× the minimum wage), and the US withholds 30% (the treaty was terminated in 2024).
- **Irish UCITS ETFs.** About 15% US withholding applies inside the fund. Accumulating funds give no holder-level dividend to declare.

## Strategy evidence relevant to the bot
| Finding | Evidence | Bot implication |
|---|---|---|
| 12-1 momentum works; short-term reversal is no longer tradeable after costs (AQR net −2%/yr 1998–2011) | Strong | Keep the skip month. Matches the audit: plain momentum ≥ composite score. |
| A 10–15% stop on momentum winners cut max drawdown from −50% to −17% and raised returns (Han-Zhou-Zhu, 1926–2013) | Strong (long-short) | ROADMAP 11: exits defined at entry, stop of about 12%. |
| Momentum crashes come 1–3 months after big market sell-offs | Strong (Daniel-Moskowitz) | Reduce size, don't stop, after a large index sell-off. Test in the backtest first: the audit found a regime gate cut returns. |
| Analyst targets are about 15% too high and only 38% are hit within 12 months; the most optimistic growth forecasts gave about 3%/yr vs 15% for the least optimistic | Strong | Supports dropping analyst upside and growth from the score (ROADMAP 9). |
| Stocks on daily top-gainer/top-loser lists underperform by about 1.7%/month the next month (Kumar 2017) | Strong, before costs | Flag "in the news / crowded" picks. P2 candidate. |
| MA(21)/MA(200) and 52-week-high signals | Strong, but correlated with momentum | Little extra information. Don't add. |
| A slow index trend filter (10-month SMA) cuts drawdowns, but Sharpe is not always better and fast rules fail out of sample | Strong | Each exit realises 15% tax; test this before ROADMAP 14. |
| CAPE and the Buffett indicator work only at 10–20 year horizons | Strong | Context only, never a timing signal. |
| Smart-beta ETFs underperform after launch | Strong | Keep plain cap-weighted UCITS ETFs for the core. |
| A 15-stock portfolio beats the index only 27% of the time, with volatility of about 29% vs 15% | Strong | No single stock above 20%; momentum picks stay a satellite. |
| Backtest Sharpe barely predicts live results (R² < 0.025); anomalies lose about 43% after publication | Strong | Judge by forward paper trading (ROADMAP 10), not by the backtest. |

## stdev.hu
- **What's there.** Hungarian economic-policy-uncertainty and geopolitical indices.
- **Data access.** Only the EPU index can be downloaded (.xlsx). There is no API and no licence is stated.
- **Validation.** No return-predictability evidence is given.
- **Verdict.** Don't use as a signal.
