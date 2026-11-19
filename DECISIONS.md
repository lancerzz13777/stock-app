# Decisions

## Quant system (started 19 Nov 2026)

**Goal:** a personal quant research and paper-trading system in the style of a quant fund, at ₹0 cost.

**What we copy from firms like Jane Street, and what we don't:**
- Their famous edge is market making and arbitrage at microsecond speed. That needs exchange co-location and membership, which free tools can't provide, so it's out of scope.
- What we copy is the method every quant desk uses:
  1. ideas become numbers (alphas);
  2. each is measured by IC on data it was not tuned on;
  3. the alphas are combined;
  4. the combination is turned into a portfolio after costs;
  5. results are discounted for luck;
  6. live results are watched against the backtest.

**Defaults chosen (change any of them in this file):**
- **Code location:** inside stock-app (`screener/quant.py`), so it reuses the price database, charges and walk-forward split. No new repo.
- **Holding period:** monthly (21 trading days), non-overlapping, so the t-stats are honest. Daily data only.
- **Combined weights:** each alpha's mean IC in the choosing years, keeping only alphas with |t| ≥ 2. Weights are never fitted on the unseen years.
- **Luck check:** Deflated Sharpe (Bailey & López de Prado 2014), counting every alpha tried. Below 95% is labelled "could be luck".
- **Positions:** long-only. Retail delivery trades can't be shorted, so short ideas wait for F&O paper trading.

**First result (19 Nov 2026, PC data up to 22 Oct 2026):**
- The combined score predicts next month's returns on unseen years: IC +0.05, t 2.4.
- A top-10 long-only portfolio built from it did NOT beat the Nifty after charges (+5.1%/yr vs +5.8%/yr). The Deflated Sharpe says the result could be luck (19%).
- Conclusion: the signal is real, but how it is turned into trades loses it. That is Phase 2.

## Roadmap

| Phase | What | Status |
|---|---|---|
| 1 | Research engine: 9 alphas, IC/t-stat, walk-forward mix, charges, Deflated Sharpe (`python main.py quant`, menu Q) | done |
| 2 | Portfolio construction: 20-30 names, inverse-volatility weights, sector caps, trade only when the gain beats the charges; Nifty-hedged view | next |
| 3 | India-specific free data alphas: delivery % (NSE full bhavcopy), FII/DII flows, bulk/block deals, results dates | |
| 4 | Survivorship-free history: rebuild the universe from old NSE bhavcopies, including delisted stocks | |
| 5 | Live paper fund: the bot rebalances a paper portfolio monthly; live vs backtest IC tracked to catch alpha decay | |
| 6 | Pairs / stat-arb on sector pairs (needs shorting, so F&O paper only) | |
