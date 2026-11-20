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

**Phase 2 result (19 Nov 2026):**
- Holding the top 25 instead of the top 10 turned the signal into returns: +8.9%/yr on unseen years vs Nifty +5.8%/yr, worst fall 16% vs 24%.
- The variant with sizing by calmness and max 5 per sector did slightly better on unseen years (+9.4%). It isn't used, because it wasn't the best on the choosing years.
- The luck check rose to 39% across the 13 ideas tried. Better, but still not proof. Live paper trading (Phase 5) is the real test.

**Trading agents (19 Nov 2026):**
- Four paper day-traders, ₹10,000 a day each, no leverage, stop on every trade, out by 3:15.
- They are program agents, not Claude agents: free, and they run all day without usage limits.
- They run on the PC through `agents.bat`, and the process keeps the PC awake while it runs.
- 80-day backtest after charges: Breakout +₹903, Trend Rider −₹1,471, Bounce −₹1,530. Quant can't be backtested honestly, because its picks are today's.
- The losing agents keep running on paper as the live check. No retuning on the same days.

**Phase 3 (20 Nov 2026): India-only free data.**
- NSE delivery % (the share of traded stock taken into demat) is free from 14 Nov 2019.
- FII/DII flows and bulk/block deals have no free history, so they can't be tested. Skipped.
- Results dates are events, not a ranking. Skipped.
- Delivery % is the strongest alpha found so far: t 3.8 in the choosing years and 4.0 on unseen years. Delivery surge (last week vs its 3-month normal): t 2.2 / 4.0.
- The combined score's IC on unseen years rose from 0.050 to 0.062.

**Phase 4 (20 Nov 2026): survivorship-free history.**
- Source: NSE's daily files for 10 years, every stock that traded, including delisted ones. ETFs are removed (fund ISINs start with INF).
- Found and fixed in the whole app: NSE's "previous close" is NOT split-adjusted.
  - The evening update had been missing splits since 5 Nov: 5 stocks were repaired on the PC and the laptop.
  - Splits are now detected as a 40%+ jump at the open, which daily price bands rule out for normal trading.
- Stocks that stop trading now count at their last price, not "flat".
- Results on unseen years, top 25: +11.6%/yr on all NSE stocks vs +11.7%/yr on listed-today stocks. The Nifty did +8.3%/yr. Worst fall −16% vs Nifty −14%.
  - Survivorship bias barely moved the 25-stock portfolio, but cut top 10 from +10.5% to +6.6%. Concentrated portfolios are hit hardest by stocks that die.
- Luck check (Deflated Sharpe, 15 ideas): 18%. The edge is not proven yet. Phase 5 (live paper fund) is the real test.

## Roadmap

| Phase | What | Status |
|---|---|---|
| 1 | Research engine: 9 alphas, IC/t-stat, walk-forward mix, charges, Deflated Sharpe (`python main.py quant`, menu Q) | done |
| 2 | Portfolio construction: 4 ways to build the portfolio, the best one chosen on the choosing years only | done |
| 3 | India-specific free data: NSE delivery % (from Nov 2019), 2 alphas, updated every evening | done |
| 4 | Survivorship-free history: 10 years of NSE files incl. 1,000+ delisted stocks, ETFs removed, split-adjusted (`python main.py history`, PC only) | done |
| 5 | Live paper fund: the bot rebalances a paper portfolio monthly; live vs backtest IC tracked to catch alpha decay | |
| 6 | Pairs / stat-arb on sector pairs (needs shorting, so F&O paper only) | |
