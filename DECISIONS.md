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

## Phase 5: live paper fund (20 Nov 2026)
- Runs inside the bot's evening job on the laptop: every 21 trading days it re-runs the quant engine and moves a ₹10 lakh paper portfolio to its picks, with the recipe that won on the choosing years. It is valued every evening against the Nifty.
- Fills at that evening's NSE close with every delivery charge. This flatters it slightly: a real buyer gets the next morning's price.
- Stocks it keeps are not resized, which matches the backtest and saves charges. Expensive shares round down to whole shares, and the leftover stays as cash.
- Alpha decay check: each re-balance stores the full ranking. A month later its live IC is measured and shown next to the backtest IC (about 0.06). If live IC stays near 0 for 6+ months, the edge has faded.
- 22 Nov 2026: ranking moved to the PC. On the 1.8 GB laptop the quant engine ran out of memory, slowed to a crawl and never finished, so the fund made no buy on 20 Nov. The PC now ranks using the full history including delisted stocks, and sends a small plan to the laptop over ssh (`push-plan`, daily 8:30 PM task that skips if the plan is under 6 days old). The laptop only trades the plan; a plan more than 10 days old isn't traded. The first buy was made on 20 Nov closing prices: 25 stocks, 3% cash.
- Where to see it: a Telegram line in each evening report, a message at each re-balance, and a section on the dashboard.

## Phase 6: pairs trading research (22 Nov 2026)
- Method: the distance method of Gatev, Goetzmann & Rouwenhorst (2006). A year picks the 20 closest pairs among the top 200 most-traded stocks (a stand-in for F&O). The next 6 months trade the gap: open at 2 sd (or 1.5 sd), close when the lines cross. A new set starts every month. Walk-forward only, with delisted stocks included, at 0.07% cost per leg per side.
- Result on the unseen years: the best version (same sector, 1.5 sd) made +0.8%/yr against 6.5% from a liquid fund. Sharpe was negative and the t-stat 0.5. It won 65% of trades, but the wins were tiny.
- Even with zero costs it made only +1.7%/yr, so the costs aren't what kills it: the signal itself is gone on NSE's liquid stocks. That matches later studies showing pairs profits fading as markets got faster.
- Decision: don't build F&O pairs paper trading. `python main.py pairs` re-runs the test if the data or the idea changes (for example, cointegration-based pairs).

## Server care and honest numbers (22 Nov 2026)
- Health alarm: the PC's 8:30 PM task asks the laptop how it is. A main loop silent for over 20 minutes (the bot writes a heartbeat every 5), no evening update for 4+ days, or agents that skipped a trading day all send "🚨 Stock bot needs a look" to Telegram from the PC. If the laptop doesn't answer at all, that alarms too.
- Backups: the same task has the laptop copy the tables that can't be downloaded again (agent trades, fund, wallet, state, chat, collected NSE data) into `data/backup.sqlite`, then copies it to `data/server-backups/` on the PC. 14 days are kept. Prices aren't backed up because they can be downloaded again.
- "Database is locked": long jobs held a write open while waiting on Yahoo or NSE (25 stocks at a time, and a 2-minute rate-limit sleep). They now commit after each stock, feed or day, so the bot's other jobs never wait long.
- Strategy Lab on the full history: it runs on the PC with delisted stocks included, and the result is sent to the laptop with the weekly plan. The laptop keeps the PC's result for 35 days instead of re-running on listed-today stocks. First honest run (22 Nov): the winner, Risk-adjusted momentum, made +9.9%/yr on unseen years against the Nifty's +9.3%, with a worst fall of −68%. On listed-today stocks the Lab had shown +32.9%/yr. Most of that gap came from stocks that later went bust being left out. Any number from listed stocks only is labelled "⚠️ listed stocks only, so flattered".
- Who-is-buying data collected every evening: FII/DII net buying, bulk and block deals, results dates. NSE gives these for the current day only, so collecting daily is the only free way to build a history. They become testable in a year or two.
- Insider trades: NSE's history works from 2019 up to early 2026. Recent months come back empty from the same API (checked 22 Nov from both machines), so the daily count reads 0. `python main.py flows-backfill` saves the history for testing an insider-buying alpha.

## Agents trimmed, and a better quant fund (23 Nov 2026)
- Bounce Bot and Trend Rider retired: both lost in their ~80-day backtests (−₹1,629 with 22% of trades won, −₹1,424 with 32% won) and in their first live days. Breakout Bot, the only one that made money in testing (+₹810, 157 trades), gets their money: ₹30,000 a day, up to ₹10,000 a trade, 3 trades. Quant Bot stays at ₹10,000. Their past trades stay on the dashboard.
- Next-morning prices: a re-balance is decided in the evening and traded at the next trading day's opening price, not the close already seen when deciding. This removes a small flattering bias. The backtest still uses closes, so live and backtest differ slightly.
- Weekly scorecard every Friday evening on Telegram: the fund vs the Nifty this week and since the start, best and worst holdings, live IC, and the agents' week.
- Sector-relative ranking tested (each stock ranked only against its own sector) on the full history, delisted stocks included. On the choosing years it lost: Sharpe 0.62 vs 0.77 for market-wide ranking, and test IC 0.057 vs 0.062. It did a little better on the unseen years (+12.0%/yr vs +11.6%), but choosing it for that would be peeking at the test. Not used.

## Roadmap

| Phase | What | Status |
|---|---|---|
| 1 | Research engine: 9 alphas, IC/t-stat, walk-forward mix, charges, Deflated Sharpe (`python main.py quant`, menu Q) | done |
| 2 | Portfolio construction: 4 ways to build the portfolio, the best one chosen on the choosing years only | done |
| 3 | India-specific free data: NSE delivery % (from Nov 2019), 2 alphas, updated every evening | done |
| 4 | Survivorship-free history: 10 years of NSE files incl. 1,000+ delisted stocks, ETFs removed, split-adjusted (`python main.py history`, PC only) | done |
| 5 | Live paper fund: the bot rebalances a paper portfolio monthly; live vs backtest IC tracked to catch alpha decay | started 20 Nov 2026 |
| 6 | Pairs / stat-arb on sector pairs (needs shorting, so F&O paper only) | researched 22 Nov 2026: no edge, not built |
| 7 | Better quant fund: next-morning prices, weekly scorecard, sector-relative ranking tested | done 23 Nov 2026 (sector-relative not used: lost on the choosing years) |
