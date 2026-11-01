# Personal NSE Stock Screener

A personal tool for **day trading** and **swing trading** NSE stocks on a small budget. It scans and scores stocks,
plans trades showing profit and maximum loss after charges, backtests the strategies, paper-trades them with fake
money, watches for dips and bad news, and sends alerts to your phone on Telegram.
**Everything runs on free data and free services. The app never buys or sells anything.**

> Screening ideas only. This is not advice and nothing is guaranteed. SEBI's study of FY2023 found about **7 in 10 individual
> intraday traders lost money**. Paper-trade for 2-3 months before using real money.

## Open it

Double-click **Stock Screener** on your Desktop. The colour menu shows market open/closed and your latest backtest verdicts:

```
STRATEGIES Y  Strategy Lab   S  Strategy picks   G  Check a stock (investor checklists)
PRACTICE   T  Trading desk (paper wallet: buy → money locked → live P&L → exit alerts → exit)

DAY TRADING  (buy & sell the same day)        SWING  (hold a few days to weeks)
  1  Live day-trade scanner                     5  Swing plan (download latest first)
  2  Day-trade scan once / session review       6  Swing plan (quick)
  3  Day-trade backtest (60 days)               7  Ranked list of all stocks
  4  Day-trade paper results                    8  Swing backtest (10 years vs Nifty)
                                                9  Swing paper results
MARKET                SETUP
 10  Dips / crashes    12  Update all data      14  Telegram setup / test
 11  Latest news       13  Start Telegram bot   15  Open settings (config.yaml)
                       L   Live data setup (Angel One, real-time)
```

## Strategies from famous investors, tested on NSE (menu → Y, S, G)

**Y: Strategy Lab.** Seven published strategies, each run with and without a "market filter" (cash when the Nifty is below its
200-day average), on 10 years of prices for ~2,300 NSE stocks, after charges. Each one picks its top 10 at every month end.

| Strategy | From |
|---|---|
| Momentum 12-1 | Jegadeesh & Titman (1993); basis of NSE's Nifty200 Momentum 30 |
| Risk-adjusted momentum | NSE Momentum index method (6/12-month return ÷ volatility) |
| Minervini Trend Template | Mark Minervini, *Trade Like a Stock Market Wizard* |
| CAN SLIM (price rules) | William O'Neil, *How to Make Money in Stocks* |
| 52-week-high breakouts | George & Hwang (2004); Nicolas Darvas, *How I Made $2,000,000…* |
| Low volatility | Haugen & Baker; NSE's Nifty100 Low Volatility 30 |
| Dual momentum | Gary Antonacci, *Dual Momentum Investing* |

**How the winner is chosen honestly:** the champion is picked using **2017–2021 only**, then checked on **2022–2026**, data it
never saw. Simply taking whatever made the most money over all 10 years would be hindsight. Some pattern always
looks amazing in hindsight and then fails.

**Result (20 Oct 2026):** **Risk-adjusted momentum** won 2017–2021 (+38%/yr) and **held up on the unseen 2022–2026 years:
+22.8%/yr against Nifty +6.2%**, after charges. The price was **falls of up to −41%** along the way. With only ₹3,000 (2 stocks) the same rules
returned ~37%/yr but with falls up to −61%, because fewer stocks means more luck. Caveats: only stocks listed today are included
(survivorship bias flatters results), and past results never guarantee future ones.

**S: Strategy picks.** What the champion selects **today**: the top 10 buy zone and ranks 11–20 as the keep zone, each with the reason and
investor-checklist scores. Enter your budget and it sizes 1–2 affordable picks: **buy, hold until the month-end review, then keep
if still in the top 20, sell if not**. That's exactly what was tested. If the market filter says cash, it tells you to stay in cash.
To practise, open the Trading Desk (T) → B → **H (hold)**. Hold positions use delivery charges, aren't squared off, and the Telegram bot
sends a **month-end review** (✅ keep / ❌ sell for each one, plus the new list). `/picks` sends the list any time.

**G: Check a stock.** Any stock against the value and growth investors' checklists, using today's fundamentals:
Graham (*The Intelligent Investor*), Buffett (*The Warren Buffett Way*), Lynch (*One Up on Wall Street*), Raamdeo Agrawal's QGLP
(*The Joys of Compounding*), O'Neil's CAN SLIM, plus Greenblatt's Magic Formula rank (*The Little Book That Beats the Market*).
**These can't be backtested with free data** (there's no history of P/E, ROE etc.), so they're shown as a quality check, not as proof.

## Live (real-time) prices with Angel One (menu → L)

Yahoo is free but can be **several minutes late**, which is too slow for day trading. The proper, legal fix is a broker's
official data API. **Angel One SmartAPI is free** for Angel One account holders and gives real-time NSE prices.

1. **Open an Angel One account** (free: PAN, Aadhaar, bank details) on angelone.in or in their app. This is the only
   step that takes time (KYC is usually 1–2 days).
2. **Turn on TOTP:** smartapi.angelbroking.com/enable-totp → log in → scan the QR with Google Authenticator and **also
   copy the text key** shown under it.
3. **Create an API key:** smartapi.angelbroking.com → sign up → *Create an App* → *Trading APIs* (redirect URL can be
   `http://127.0.0.1`) → copy the **API Key**.
4. Open `.env` (menu **L** opens it for you) and fill in:
   ```
   ANGEL_API_KEY=...            ANGEL_CLIENT_CODE=your Angel login ID (e.g. A123456)
   ANGEL_MPIN=your 4-digit PIN  ANGEL_TOTP_SECRET=the text key from step 2
   ```
5. Menu **L** again. It logs in and shows live SBIN/RELIANCE/TCS prices with their exchange time.

After that, everything switches over automatically: the status bar shows **Live: Angel One ✔**, the Trading Desk refreshes
**every 5 seconds** instead of 30, and the scanner and bot use real-time 5-minute candles. If Angel ever fails (wrong key,
outage), the app falls back to Yahoo, says so on screen, and tries Angel again every 15 minutes. The backtest still
uses Yahoo's 60-day history.

**Your demat on the phone:** once connected, tap **🏦 My demat** (or type `demat`) in Telegram. It shows your real holdings,
profit/loss and cash, and marks the ones the current strategy still likes. This is **read-only**: the app never places
real orders. You buy and sell in the Angel One app yourself. Your keys stay in `.env` on your PC.
After filling `.env`, restart the bot (close its window, then menu 13) so it picks up the keys.

## It updates itself

With the bot running you don't need to update anything by hand:

| What | When |
|---|---|
| Stock list, daily prices (last week re-checked), fundamentals (weekly), news | every weekday 4:15 PM |
| Missed update (PC was off) | as soon as the bot starts again |
| **Strategy Lab re-run.** It re-chooses the best strategy on data up to today (`lab.split_date: auto` = choose on everything up to 4 years ago, check on the last 4 years) | every month-end review, or if >35 days old. You get a message if the winner **changes** |
| **Day-trade setup re-test** on the last 60 days | weekly. If it lost money, day-trade alerts **pause** (still paper-tested quietly) and come back on when it works again |
| Market holidays | detected automatically (no prices from today means no scans, and orders wait) |

## Paper wallet & Trading Desk (menu → T)

Practise day trading with **fake money and real live prices**. You start with ₹10,000 of fake cash (`wallet.starting_cash`).

1. **B (buy):** type the stock (e.g. `SBIN`) and the amount (e.g. `1500`). It shows the live price, how many shares
   that buys, and the profit or loss if the target or stop is hit. You can accept the default stop (−1%) and target (+2%) or type your own.
   On confirm, that money is **LOCKED**: it leaves your free cash.
2. **While you hold it**, the desk refreshes prices every 30 s and shows **P&L if you exit now** plus **What to do**:
   - 🎯 **TARGET HIT → EXIT**: book the profit
   - 🛑 **STOP HIT → EXIT**: cut the loss before it grows
   - 💡 **half-way to target**: move your stop up to your buy price so the trade can't turn into a loss
   - ⏳ from 3:05 PM: exit soon; ⏰ 3:15 PM: **EXIT NOW**
   Each alert beeps once, shows on screen, and goes to your Telegram.
3. **X (exit):** sells at the live price and tells you **"You made ₹X after charges"**. The locked money plus or minus the result goes
   back to your free cash.
4. Forgot to exit? At **3:20 PM** it's closed automatically, like a broker would (real brokers charge extra for this).

Other keys: **A** exit all · **D** add fake money · **H** history (every trade, win rate, all-time P&L) · **Q** back.

**From your phone (Telegram):** `/buy SBIN 1500` (optional `stop 960 target 1000`) · `/exit 1` or `/exit SBIN` or `/exit all` ·
`/wallet`. When the bot is running (menu 13), it watches your positions every minute and messages you when it's time to exit.

Buying only works during market hours (Mon–Fri 9:15 AM–3:00 PM) because fills need live prices.

## Day trading: how it works

**Strategy: Opening Range Breakout (ORB)** on 5-minute candles, about the most common intraday method:
1. The **opening range** is the high and low of the first 15 minutes (9:15–9:30).
2. **Buy signal:** a 5-minute candle closes **above the range high**, above **VWAP** (the day's average price) and on
   **1.5× normal volume**.
3. **Stop-loss** goes below the range, kept between 0.3% and 1%. **Target** = 2× the risk.
4. **Everything is closed by 3:15 PM.** No new trades after 1:30 PM. **Max 2 trades a day.** Only the 100 most-traded stocks.

Charges are **intraday** rates (brokerage 0.03% or ₹20, STT 0.025% on the sell side only, no DP charge). All of this can be changed in
`config.yaml` → `intraday:`.

**Your day-trading routine:**
1. Before 9:30 AM: open the menu → **1 Live scanner**. Enter your budget. Leave it running: it refreshes every 5 minutes
   and shows the stocks closest to a breakout.
2. When a signal appears, you get a card: **BUY qty at ~price, target, stop, profit and max loss after charges**. It's
   automatically logged as a **paper trade** (fake money).
3. At 3:10 PM a red banner says **SQUARE OFF**.
4. After 3:30 PM: **4 Paper results** shows how each signal actually ended. **2** shows what every breakout did today.

**Honest limits of free data:** Yahoo's live 5-minute candles can be **several minutes late**. Every scan shows how old
its data is and turns red ("STALE") above 15 minutes, so check the live price in your broker app before acting. For
serious live day trading you'd later want a broker data feed (paid, e.g. Zerodha Kite Connect), which can be plugged in.

**Leverage:** brokers let you trade ~5× your money intraday. `leverage: 1` (none) is the default. With ₹1,500 and no
leverage a trade risks about ₹10–15 and targets about ₹20–30. Leverage multiplies **both** profit and loss. Only raise it after
months of positive paper trading.

**Backtest so far (19 Oct 2026, 60 days, 100 stocks):** 120 trades, 44% wins, **+₹84 total after charges** on ₹1,500,
i.e. roughly break-even, ₹0.70 per trade. 60 days is one market mood, not proof.

## Swing trading

Holds a few days to weeks (delivery). The rules are in `config.yaml` → `scoring`, `planner`.
**10-year backtest on all ~2,300 stocks: −15.8%/yr against Nifty +10.3%/yr. It lost badly.** The app warns you about this on every
swing plan. Treat swing ideas as paper-trading material only, or tune the rules and re-run option 8.

## Telegram setup (free, about 5 minutes)

1. In Telegram, open **@BotFather** → send `/newbot` → choose a name → copy the **token**.
2. Menu **14** → it opens `.env` in Notepad. Paste the token after `TELEGRAM_BOT_TOKEN=` and save.
3. Send "hi" to your new bot in Telegram.
4. Menu **14** again → it shows your chat id. Paste it after `TELEGRAM_CHAT_ID=` and save.
5. Menu **14** once more → a ✅ message arrives on your phone.

Then menu **13** starts the bot. Leave that window open. During market hours it:
- scans for **day-trade breakouts every 5 min** → sends BUY/target/stop alerts → logs them as paper trades
- sends a **3:10 PM square-off reminder**
- checks quality stocks for **dips every 15 min**, and **news every 30 min** (warns you about bad news on stocks you're watching)
- at **4:15 PM** sends an evening report: swing plan, how today's paper trades ended, dips
- answers your messages: `/day`, `/plan 2500`, `/top`, `/dips`, `/paper`, `/news`, `/help` (only from your own chat)

## Use it from your phone 📱

Your phone talks to the app through your own **Telegram bot**. The bot runs on a computer that stays on, and you just chat
with it. Setup is in *Telegram setup* above (5 minutes, free).

**Send `/start`** and a button menu appears at the bottom of the chat:

```
[ 💼 Wallet ] [ 📊 Picks ] [ ⚡ Day scan ]
[ 📉 Dips ]   [ 📰 News ]  [ 📒 Paper results ]
[ 🧪 Strategy Lab ]        [ ❓ Help ]
```

| You want to... | On your phone |
|---|---|
| See today's strategy picks | tap **📊 Picks** → tap **🛒 Paper-buy CUPID (hold)** → **✅ Yes, buy** |
| Day trade | tap **⚡ Day scan** (or wait: breakouts arrive by themselves every 5 min) → **🛒 Paper-buy** → **✅ Yes** |
| Buy any stock | type `/buy SBIN 1500` (day trade) or `/buy SBIN 1500 hold` |
| See money, locked amount, live P&L | tap **💼 Wallet** |
| Get out of a trade | tap **📤 Exit #1 SBIN** under the wallet → **✅ Yes, exit** → "You made ₹X" |
| Know when to get out | automatic: 🎯 target / 🛑 stop / 💡 half-way / ⏰ 3:15 messages, each with an **Exit now** button |
| Monthly keep/sell review | automatic on the last trading day of the month, with **Sell** buttons |
| Check a company | type `/check TCS` (Graham, Buffett, Lynch, QGLP, CAN SLIM) |
| Re-test the strategies | tap **🧪 Strategy Lab** (about 1 minute) |

Every buy or sell asks **"Yes / Cancel"** first, so a mis-tap does nothing. The bot only answers **your** chat id.

## Chat advisor (free) 💬

Talk to the bot in your own words: *"which stock looks good today?"*, *"why did TCS fall?"*, *"should I sell SBC?"*.
It greets you by name, checks the app's own tested picks, dips, checklists and your wallet, **searches Google live** for
the latest news, and answers in a few short lines with its sources. On weekdays at **8:45 AM** it sends a morning
message with the market mood and one idea for the day.

**Free:** it runs on Google Gemini's free tier. Get a key at **aistudio.google.com/apikey** (no card), paste it in `.env`
after `GEMINI_API_KEY=`, test it with menu **A**, then restart the bot. The free tier has a daily limit, and the app caps you
at 40 questions/day (`advisor.max_questions_per_day`). Google may use free-tier chats to improve its products; the advisor
only sees your paper wallet and stock questions. Type **forget** to clear its memory. It never promises profit.

## Keep the bot running 24/7 (free)

**Option A: your PC.** Menu → **13**, and leave the window open. The PC must be on during market hours. To start it at
login, press Win+R → `shell:startup` → paste a copy of the Desktop shortcut there.

**Option C: GitHub Actions + cron-job.org (what this app uses now, ₹0, no card, PC can be off).**
cron-job.org starts `.github/workflows/bot.yml`, which runs `python main.py tick`: one pass that answers Telegram and does whatever is due, then exits.

- **Schedule (cron-job.org, times in UTC):** `*/5 3-10 * * 1-5` (every 5 min, weekdays 8:30 AM - 3:55 PM IST) and `*/30 * * * *` (every 30 min, always).
- **Replies aren't instant:** up to about 5 min in market hours, 30 min otherwise. Buys and sells fill at the price when the run happens, and stop-loss alerts can be about 5 min late.
- **Your data** (prices, paper wallet) lives in the Actions cache, saved at the end of every run. GitHub deletes caches unused for 7 days. If the triggers stop that long, the bot restarts from the `seed` release with a fresh paper wallet.
- **Secrets** are in GitHub → Settings → Secrets and variables → Actions: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and optionally `GEMINI_API_KEY` and the four `ANGEL_` keys. Your chats and wallet are kept out of the public run logs.
- **The access token on cron-job.org expires after a year.** When it does, make a new one (only the stock-app repo, Actions: read and write) and update the `Authorization` header on both jobs.
- **Don't run menu 13 on the PC at the same time:** two bots fight over Telegram messages.
- **Pause it:** disable both jobs on cron-job.org.
- GitHub's terms discourage using Actions as an always-on server, so GitHub could limit this. Option B is the fallback.

**Option B: free cloud server, so your PC can be off (₹0).** Oracle Cloud's *Always Free* tier gives you a Linux
server free forever. About 20 minutes, once.

1. **Sign up** at **cloud.oracle.com** → *Sign up*. You need to do this part yourself.
   - It asks for a debit or credit card **only to check you're a real person**. You may see a small temporary hold
     that is refunded. Always Free resources are never charged.
   - **Home region:** pick **India West (Mumbai)** or **India South (Hyderabad)**. It can't be changed later.
   - Stay on the free account. **Don't click "Upgrade to Pay As You Go"**: that's the only way to ever get billed.
2. **Create the server:** menu ☰ → *Compute* → *Instances* → **Create instance**:
   - **Image:** *Change image* → **Ubuntu** → *Canonical Ubuntu 24.04*.
   - **Shape:** *Change shape* → **Ampere** → **VM.Standard.A1.Flex**, set **1 OCPU and 6 GB memory**. It is marked
     "Always Free-eligible". Don't use the tiny E2.1.Micro: 1 GB is too little for the monthly Strategy Lab.
   - **SSH keys:** *Generate a key pair for me* → **Save private key**, which goes to your Downloads folder.
   - Leave the rest and click **Create**. When it shows RUNNING, copy the **Public IP address**.
   - *"Out of capacity"?* Free Ampere servers are popular. Try the other *Availability domain*, or try again in a
     few hours.
3. **Put the bot on it:** double-click **`deploy\deploy_to_server.bat`** on your PC. Paste the **IP** and the **key file
   path** when it asks. It packs the app with your data and `.env`, uploads it, installs everything, starts the bot as a
   service, and stops the bot on your PC. Telegram then says **"🤖 Stock bot is running"**.

It now runs day and night, restarts itself if it crashes, and comes back after a server reboot. Updates, the monthly
Strategy Lab and the weekly re-tests all run there. **You can switch your PC off.**

**Later:**
- **Code changed on the PC?** Run `deploy\deploy_to_server.bat -CodeOnly`. It sends only the code and keeps the
  server's wallet, data and `.env`. It remembers the IP and key.
- **Changed `.env` (e.g. Angel One or Gemini keys)?** Run `deploy\deploy_to_server.bat` again (a full deploy). ⚠️ This
  replaces the server's data with the PC's, so paper trades made since the last deploy are lost. Or edit it on the
  server: `ssh -i KEY ubuntu@IP`, then `nano stock-app/.env`, then `sudo systemctl restart stockbot`.
- **Only ONE place runs the bot.** After deploying, menu 13 on the PC refuses to start it, because two copies would
  fight over Telegram. Everything else in the PC menu still works, but on the PC's own copy of the data. Your live
  wallet is on the server and in Telegram.
- **Bring it back to the PC:** on the server run `sudo systemctl disable --now stockbot`, then delete
  `data\RUNS_ON_SERVER` on the PC.
- **Angel One + cloud:** Angel's SmartAPI may ask for your server's IP address in the app settings (a "static IP").
  The Oracle public IP is fixed, so put that one in.

## Reading a plan

```
#1 AARTIIND (Aarti Industries Limited) - Basic Materials - score 61/100
   Buy 2 share(s) at ~Rs 503.00 (limit order; don't pay above Rs 508.00)
   Target:    Rs 556.80 (+10.7%) -> expected profit Rs 89.44 after charges
   Stop-loss: Rs 476.10 (-5.35%) -> maximum loss Rs 71.95 incl. charges
```

- **Target and stop** come from the stock's typical daily move (ATR): the stop is 1.5× below, the target 3× above.
- **Profit and loss** are after STT, exchange fees, SEBI fees, stamp duty, GST and the DP charge.
- **"No stock passed every check today"** is a normal answer, and it tells you why.

## About the backtest (be aware)

- It uses **technical rules only**. Free sources only have *today's* P/E, debt and news, and using those for
  2016 would be cheating, so fundamentals and news aren't tested.
- It only includes stocks listed **today**. Companies that collapsed are missing, which flatters the results.
- Trades buy at the **next day's open**. If a day touches both stop and target, it assumes the stop hit first.
- It compares against the Nifty 50 *price* index. A real index fund also gets ~1.2%/yr in dividends.
- An early test on 80 stocks lost money against the Nifty. Run it on the full universe and tune `config.yaml`
  (`backtest`, `planner`, `scoring`). Be careful not to tune until the past looks perfect ("overfitting");
  that is exactly why paper trading comes next.

## Dip alerts

The app only watches **quality** stocks: profitable, market cap ≥ ₹5,000 cr and not over-borrowed. A fall of 5%+ (in 1 or 3 days) is labelled:
- 🟢 **OPPORTUNITY** when the whole market is falling (Nifty −2% or more, or many stocks down together) and the stock fell roughly with it
- ⚠️ **WARNING** when it fell much more than the market, or when there's negative company news

## News

There are 6 free RSS feeds (NSE announcements, Economic Times ×2, Moneycontrol, Livemint, Business Standard), checked every 30 minutes.
Each item is linked to stocks by company name and tagged positive, negative or neutral with a **free keyword method**.
It's rough: it can miss things and mislabel some items. Recent news changes the score: +5 for good, −15 for bad.
*(Optional, paid: `news.ai_tagging: true` plus `ANTHROPIC_API_KEY` in `.env` uses Claude for better tagging. Off by default.)*

## Changing the rules

Everything is in [`config.yaml`](config.yaml). Change it and re-run, with no code changes:
filters (penny/illiquid guards), scoring points, planner (stop/target/min profit), backtest settings,
dip thresholds, news feeds, alert times, paper-trading rules, and **charges**. Check the charges against your broker's
charges page once you choose one.

## Command line (same as the menu)

```powershell
.\.venv\Scripts\python.exe main.py                      # the menu
.\.venv\Scripts\python.exe main.py dt-watch | dt-scan | dt-backtest | dt-paper
.\.venv\Scripts\python.exe main.py daily | plan --budget 1500 | scan | backtest | paper | dips | news | update | run
```

## Project layout

```
run.bat                 double-click menu
main.py                 command line
config.yaml             all rules, filters, charges, alert settings
.env                    secrets (Telegram token) - never share
screener/
  universe.py           NSE stock list           prices.py      Yahoo prices -> SQLite
  fundamentals.py       P/E, debt, growth         indicators.py  SMA, RSI, MACD, ATR...
  scoring.py            filters + rule scoring    charges.py     Indian delivery charges
  planner.py            budget -> 1-2 trades      backtest.py    history replay vs Nifty
  trading.py            buy/sell mechanics shared by backtest + paper trading
  dips.py               dip/crash detection       news.py        RSS + tagging
  telegram.py           send/receive messages     bot.py         always-on loop
  paper.py              swing paper trading       pipeline.py    shared steps
  intraday.py           DAY TRADING: 5-min data, ORB signals, backtest, live scan, paper trades
  ui.py                 colours, tables, progress bars (rich)
  wallet.py             paper wallet: lock money, live P&L, exit alerts, auto square-off
  desk.py               the live Trading Desk screen
  deploy/               free cloud server setup (pack, upload, run as a service)
  live.py               live prices: Angel One SmartAPI (real-time) or Yahoo fallback
  strategies.py         Strategy Lab: 7 investor strategies, monthly backtests, walk-forward test
  gurus.py              Graham / Buffett / Lynch / QGLP / CAN SLIM checklists, Magic Formula
  lab_ui.py             Strategy Lab, Strategy picks and stock-check screens
data/market.db          local cache (safe to delete; it re-downloads)
output/                 daily CSVs (ranked list, plans, backtest trades)
```

## Data sources & limits

- NSE public stock list (`EQUITY_L.csv`), downloaded at most once a day.
- Yahoo Finance via `yfinance`: free and unofficial. Prices during market hours are about 15 minutes late, and it's
  sometimes slow or rate-limited (lower `data.batch_size` if that happens). Free fundamentals are missing for
  some small companies; those stocks lose points instead of being guessed.
- Real-money auto-trading is **not built**. If it's ever added, it will only go through an official broker API, and only
  after you switch it on yourself.
