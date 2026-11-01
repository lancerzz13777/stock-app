"""Stage 7: the always-on loop. Leave it running (PC or a free cloud VM) and it will:

- During market hours (Mon-Fri 9:15-15:30 IST): scan for DAY-TRADE breakouts every 5 min,
  log them as paper trades, remind you to square off at 3:10, check for dips every 15 min
- All the time: read news every 30 min, warn about bad news on stocks you're watching
- Every weekday at 16:15 IST: update data, send today's budget plan, update paper trades
- Answer your Telegram commands: /plan 2000, /top, /dips, /paper, /news, /help

Your taps are answered right away: long jobs (evening update, scans, Strategy Lab) run on
background workers so they never block commands. Everything is also written to data/bot.log.

It never buys or sells anything.
"""
import json
import os
import socket
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from .config import ROOT
from .db import connect

from . import advisor, dips, intraday, news, paper, pipeline, prices, telegram, tgfmt, wallet

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN, MARKET_CLOSE = dtime(9, 15), dtime(15, 30)

STATE = """
CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS alerts_sent (key TEXT PRIMARY KEY, sent_at TEXT);
"""

HELP = (
    "📱 Tap the buttons below, or type:\n\n"
    "PAPER WALLET (fake money, real prices)\n"
    "/buy SBIN 1500 - day trade (sell by 3:15)\n"
    "/buy SBIN 1500 hold - strategy position (monthly review)\n"
    "/buy SBIN 1500 stop 960 target 1000 - your own levels\n"
    "/exit 1 · /exit SBIN · /exit all\n"
    "/wallet - money, open trades, Exit buttons\n\n"
    "STRATEGY (tested on 10 years of NSE)\n"
    "/picks - what the winning strategy selects today\n"
    "/lab - re-run the Strategy Lab (~1 min)\n"
    "/check TCS - Graham/Buffett/Lynch/QGLP/CAN SLIM checklists\n\n"
    "MARKET\n"
    "/day - day-trade breakout scan (market hours)\n"
    "/dips - sudden falls in quality stocks\n"
    "/news - negative news on stocks\n"
    "/paper - paper trading results\n"
    "/help - this message"
)


LOG_FILE = ROOT / "data" / "bot.log"

# Commands that can take more than a couple of seconds - answered on a background worker
SLOW_COMMANDS = {"/lab", "/day", "/picks", "/dips", "/top", "/plan", "/check", "/paper", "/ask", "/demat"}


def log(text: str) -> None:
    line = f"[{datetime.now(IST):%Y-%m-%d %H:%M:%S}] {text}"
    # A public GitHub repo's run logs can be read by anyone: keep your chats and wallet out of them
    if not (os.environ.get("BOT_PUBLIC_LOGS") and text.startswith(("YOU>", "BOT>"))):
        print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def market_is_open(now: datetime) -> bool:
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


class Bot:
    def __init__(self, cfg, con):
        self.cfg = cfg
        self._local = threading.local()
        self._local.con = con                       # the main thread's connection
        self.jobs = ThreadPoolExecutor(1, thread_name_prefix="jobs")    # scheduled work
        self.slow = ThreadPoolExecutor(2, thread_name_prefix="slow")    # slow commands (2 at once)
        self.fast = ThreadPoolExecutor(1, thread_name_prefix="wallet")  # exit alerts - never wait behind a scan
        self._running: dict = {}
        self.run_once = False                       # True in tick(): everything runs right here, one by one
        con.executescript(STATE)
        paper._ensure(con)
        news._ensure(con)
        self.last_dip_scan = None
        self.last_dt_scan = None
        self.last_wallet_check = None
        wallet.ensure(con, cfg)
        intraday.ensure(con)
        self.last_news_scan = None
        self._trading_day: dict[str, bool] = {}

    @property
    def con(self):
        """Each thread gets its own SQLite connection (they can't be shared between threads)."""
        c = getattr(self._local, "con", None)
        if c is None:
            c = self._local.con = connect(self.cfg["data"]["db_path"])
        return c

    def _submit(self, pool, key: str, fn, *args):
        """Run fn in the background unless the same job is still running."""
        fut = self._running.get(key)
        if fut is not None and not fut.done():
            return False

        def safe():
            try:
                fn(*args)
            except Exception as e:
                log(f"ERROR in {key}: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                if key.startswith("/"):
                    self.say(f"⚠️ Something went wrong: {type(e).__name__}: {str(e)[:150]}")
        self._running[key] = pool.submit(safe)
        return True

    # --- small helpers -------------------------------------------------
    def state_get(self, key):
        row = self.con.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def state_set(self, key, value):
        self.con.execute("INSERT OR REPLACE INTO bot_state VALUES (?, ?)", (key, str(value)))
        self.con.commit()

    def first_time(self, key) -> bool:
        """True the first time we see this alert key - stops repeat alerts."""
        if self.con.execute("SELECT 1 FROM alerts_sent WHERE key=?", (key,)).fetchone():
            return False
        self.con.execute("INSERT INTO alerts_sent VALUES (?, ?)", (key, datetime.now(IST).isoformat()))
        self.con.commit()
        return True

    def say(self, text, buttons=None, menu=False, rich=False):
        log("BOT> " + text.replace("\n", " | ")[:300])
        telegram.send(text, buttons=buttons, menu=menu, rich=rich)

    def watched_symbols(self) -> set[str]:
        rows = self.con.execute("SELECT symbol FROM paper_trades WHERE status IN ('pending','open')").fetchall()
        picks = (self.state_get("todays_picks") or "").split(",")
        held = {p["symbol"] for p in wallet.open_positions(self.con)}
        return {r[0] for r in rows} | {p for p in picks if p} | held

    # --- commands ------------------------------------------------------
    # Plain words work too: "buy sbin 1500", "sell sbin", "sell all", "dips", "wallet"...
    WORDS = {"buy": "/buy", "sell": "/exit", "exit": "/exit", "wallet": "/wallet", "picks": "/picks",
             "pick": "/picks", "dip": "/dips", "dips": "/dips", "check": "/check", "day": "/day", "news": "/news",
             "paper": "/paper", "lab": "/lab", "help": "/help", "start": "/start", "hi": "/start", "hello": "/start",
             "cancel": "/cancel", "undo": "/cancel", "forget": "/forget", "ask": "/ask",
             "demat": "/demat", "portfolio": "/demat", "holdings": "/demat", "angel": "/demat"}

    def handle(self, text: str):
        """Called for every message/tap. Slow commands are passed to a background worker."""
        parts = text.split()
        if not parts:
            return
        cmd = parts[0].lower().split("@")[0]
        cmd = self.WORDS.get(cmd, cmd)
        if not cmd.startswith("/"):  # an ordinary sentence -> ask the advisor
            parts, cmd = ["/ask"] + parts, "/ask"
            text = "/ask " + text
        if threading.current_thread() is threading.main_thread():
            log(f"YOU> {text}")
        if cmd in SLOW_COMMANDS and threading.current_thread() is threading.main_thread() and not self.run_once:
            if not self._submit(self.slow, cmd, self.handle, text):
                self.say(f"⏳ Still working on your last {cmd.strip('/')} - one moment.")
            elif cmd in ("/picks", "/dips", "/check", "/paper", "/top", "/plan", "/demat"):
                self.say("⏳ One moment...")
            elif cmd == "/ask":
                telegram.typing()
            return
        self._handle(parts, cmd)

    def _handle(self, parts, cmd):
        try:
            fn = {
                "/start": lambda: self.say(tgfmt.HELP, menu=True, rich=True),
                "/help": lambda: self.say(tgfmt.HELP, menu=True, rich=True),
                "/buy": lambda: self.cmd_buy(parts), "/buyask": lambda: self.cmd_buy(parts),
                "/exit": lambda: self.cmd_exit(parts), "/exitask": lambda: self.cmd_exit(parts),
                "/wallet": self.cmd_wallet, "/picks": self.cmd_picks, "/lab": self.cmd_lab,
                "/check": lambda: self.cmd_check(parts), "/day": self.cmd_day, "/top": self.cmd_top,
                "/plan": lambda: self.cmd_plan(parts), "/dips": self.cmd_dips, "/paper": self.cmd_paper,
                "/news": self.cmd_news, "/noop": lambda: self.say("👍 OK"), "/cancel": self.cmd_cancel,
                "/ask": lambda: self.cmd_ask(parts), "/forget": self.cmd_forget, "/demat": self.cmd_demat,
            }.get(cmd)
            if fn:
                fn()
            else:
                self.say("🤔 I didn't get that.\n\n" + tgfmt.HELP, menu=True, rich=True)
        except wallet.WalletError as e:
            self.say(f"❌ {e}")
        except Exception as e:  # never let one bad command stop the bot
            log(f"ERROR in {cmd}: {traceback.format_exc()}")
            self.say(f"⚠️ Something went wrong: {type(e).__name__}: {str(e)[:150]}")

    def _sell_buttons(self, p):
        return [[(f"💰 Sell #{p['id']} {p['symbol']} now", f"/exit {p['id']}")]]

    def _symbols(self) -> set[str]:
        if not hasattr(self, "_sym_cache"):
            self._sym_cache = {r[0] for r in self.con.execute("SELECT symbol FROM universe")}
        return self._sym_cache

    def _parse_trade(self, parts):
        """Understands 'buy SBIN 1500', 'buy me the stocks for SBC of 1500', 'buy 1500 of TCS hold'..."""
        words = [w.strip(",.!?₹").replace("₹", "") for w in parts[1:]]
        syms = self._symbols()
        sym = next((w.upper() for w in words if w.upper() in syms), None)
        nums = [float(w.replace(",", "")) for w in words if w.replace(",", "").replace(".", "", 1).isdigit()]
        low = [w.lower() for w in words]
        extra = {}
        for key in ("stop", "target"):
            if key in low and low.index(key) + 1 < len(low):
                try:
                    extra[key] = float(words[low.index(key) + 1].replace(",", ""))
                except ValueError:
                    pass
        amount = next((n for n in nums if n not in extra.values()), None)
        mode = "hold" if any(w in low for w in ("hold", "keep", "long", "invest")) else "day"
        shares = amount is not None and any(w in low for w in ("share", "shares", "qty", "quantity"))
        return sym, amount, mode, extra, shares

    def cmd_buy(self, parts):
        """buy SBIN 1500 [hold] [stop 960] [target 1000] - executes immediately (paper money)."""
        sym, amount, mode, extra, shares = self._parse_trade(parts)
        if not sym:
            return self.say("🤔 Which stock? Type it like: <b>buy SBIN 1500</b>  or  <b>buy SBIN 1500 hold</b>", rich=True)
        amount = amount or self.cfg["intraday"]["budget"]
        if not shares:
            # "buy 2 SBIN": fewer rupees than one share costs means a number of shares
            q = wallet.live_prices([sym]).get(sym)
            shares = bool(q and amount < q[0] and amount == int(amount))
        if shares:
            p = wallet.buy(self.con, self.cfg, sym, None, mode=mode, qty=int(amount), **extra)
        else:
            p = wallet.buy(self.con, self.cfg, sym, amount, mode=mode, **extra)
        if p["status"] == "queued":
            return self.say(tgfmt.queued(p, wallet.cash(self.con), wallet.next_open(), wallet.market_open()),
                            rich=True, buttons=[[("↩️ Cancel order", "/cancel")]])
        self.say(tgfmt.bought(p, wallet.cash(self.con)), rich=True, buttons=self._sell_buttons(p))

    def cmd_exit(self, parts):
        """sell SBIN / sell 2 / sell all / 'sell my SBC' - executes immediately at the live price."""
        opens = wallet.open_positions(self.con)
        if not opens:
            return self.say("No open trades to sell.")
        low = [w.strip(",.!?#").lower() for w in parts[1:]]
        if "all" in low or "everything" in low:
            targets = opens
        else:
            targets = [p for p in opens if p["symbol"].lower() in low or str(p["id"]) in low]
            # "sell" / "sell it" with one trade open sells that one - but never a different stock than named
            named = any(w.upper() in self._symbols() or w.isdigit() for w in low)
            if not targets and len(opens) == 1 and not named:
                targets = opens
            if not targets:
                names = ", ".join(f"{p['symbol']} (#{p['id']})" for p in opens)
                return self.say(f"🤔 Sell which one? You hold: {names}. Type <b>sell SBIN</b> or <b>sell all</b>.",
                                rich=True, buttons=[[(f"💰 Sell {p['symbol']}", f"/exit {p['id']}")] for p in opens])
        px = wallet.live_prices([p["symbol"] for p in targets]) if wallet.market_open() else {}
        for pos in targets:
            q = px.get(pos["symbol"])
            if not wallet.is_fresh(q):  # market closed or a holiday: sell at the next live price, like a broker
                wallet.queue_sell(self.con, pos["id"])
                self.say(tgfmt.sell_queued(pos, wallet.next_open(), wallet.market_open()), rich=True,
                         buttons=[[("↩️ Cancel", "/cancel")]])
                continue
            c = wallet.sell(self.con, self.cfg, pos["id"], q[0])
            self.say(tgfmt.sold(pos, c, wallet.cash(self.con)), rich=True)

    def cmd_ask(self, parts):
        question = " ".join(parts[1:]).strip()
        if not question:
            return self.say("💬 Ask me anything, e.g. <b>which stock looks good today?</b>", rich=True)
        telegram.typing()
        try:
            self.say(advisor.ask(self.con, self.cfg, question), rich=True)
        except advisor.AdvisorError as e:
            self.say(f"💬 {tgfmt.esc(e)}", rich=True)

    def cmd_demat(self):
        """Your real Angel One holdings - read-only."""
        from . import live
        if not live.angel_configured():
            return self.say("🏦 <b>Demat not connected yet</b>\nOn the PC: menu → <b>L</b> (Live data setup). Fill the 4 "
                            "ANGEL_ lines in .env, test, then restart the bot.\n<i>The app will only READ your "
                            "holdings and live prices. It never places real orders.</i>", rich=True)
        try:
            pf = live.angel_portfolio()
        except Exception as e:
            return self.say(f"🏦 Couldn't read your Angel One account: {tgfmt.esc(str(e)[:150])}", rich=True)
        d = self.picks_data()
        keep = {p["symbol"] for p in d[1]["picks"]} if d and d[1]["invest"] else None
        self.say(tgfmt.demat(pf, keep), rich=True)

    def cmd_forget(self):
        advisor.forget(self.con)
        self.say("🧹 Chat memory cleared.")

    def morning_brief(self):
        telegram.typing()
        try:
            self.say(advisor.morning_brief(self.con, self.cfg), rich=True,
                     buttons=[[("📊 Picks", "/picks"), ("💼 Wallet", "/wallet")]])
        except advisor.AdvisorError as e:
            log(f"Morning brief skipped: {e}")

    def cmd_cancel(self):
        done = wallet.cancel_queued(self.con)
        if not done:
            return self.say("Nothing to cancel - no orders are waiting for the market to open.")
        self.say("↩️ <b>Cancelled</b>: " + ", ".join(("buy " if d["status"] == "queued" else "sell ")
                                                    + tgfmt.esc(d["symbol"]) for d in done)
                 + f"\nFree cash {tgfmt.rs(wallet.cash(self.con))}", rich=True)

    def cmd_wallet(self):
        opens = wallet.open_positions(self.con)
        px = wallet.live_prices([p["symbol"] for p in opens]) if opens else {}
        s = wallet.summary(self.con, self.cfg, px)
        buttons = [[(f"💰 Sell #{p['id']} {p['symbol']}", f"/exit {p['id']}")] for p in opens]
        if len(opens) > 1:
            buttons.append([("💰 Sell ALL", "/exit all")])
        self.say(tgfmt.wallet(s, opens, px, lambda p, price: wallet.unrealised(p, price, self.cfg)),
                 rich=True, buttons=buttons or None)

    def picks_data(self):
        """(champion, current picks, holdings to keep, holdings to sell, affordable picks) or None."""
        import json
        from . import strategies
        from .db import get_state
        c = get_state(self.con, "lab_champion")
        if not c:
            return None
        c = json.loads(c)
        cp = strategies.current_picks(self.con, self.cfg, c["key"], c["market_filter"])
        keep_zone = {p["symbol"] for p in cp["picks"]} if cp["invest"] else set()
        holds = [p for p in wallet.open_positions(self.con) if p.get("mode") == "hold" and not p.get("stop")]
        budget = self.cfg["alerts"]["default_budget"]
        each = budget / (2 if budget >= 2 * self.cfg["planner"]["min_trade_value"] else 1)
        affordable = [{**p, "qty": int(each // (p["price"] * 1.002))} for p in cp["picks"]
                      if p["in_top"] and p["price"] * 1.01 <= each]
        return c, cp, [h for h in holds if h["symbol"] in keep_zone], \
            [h for h in holds if h["symbol"] not in keep_zone], affordable, each

    def cmd_picks(self, review: bool = False):
        d = self.picks_data()
        if not d:
            return self.say("🧪 Run the Strategy Lab first — tap <b>🧪 Strategy Lab</b>.", rich=True)
        c, cp, keep, sell, affordable, each = d
        buttons = [[(f"💰 Sell {h['symbol']} (dropped out)", f"/exit {h['id']}")] for h in sell]
        buttons += [[(f"🛒 Buy {p['symbol']} · {p['qty']} × ₹{p['price']:,.0f}", f"/buy {p['symbol']} {each:g} hold")]
                    for p in affordable[:3]]
        self.say(tgfmt.picks(c, cp, keep, sell, affordable, review), rich=True, buttons=buttons or None)

    def cmd_lab(self):
        from . import lab_ui, strategies
        self.say("🧪 Testing the strategies on ~2,300 stocks… about a minute.")
        out = strategies.run_lab(self.con, self.cfg)
        lab_ui.save_champion(self.con, out, self.cfg)
        c, b = out["champion"], out["bench"]
        rows = out["table"].sort_values("oos_cagr", ascending=False).head(5)
        lines = ["🧪 <b>STRATEGY LAB</b>", "<i>Return per year on years the strategy never saw:</i>",
                 tgfmt.table([(r["name"][:24], f"{r['oos_cagr']:+.0f}%") for _, r in rows.iterrows()]
                             + [("Nifty 50 (just hold)", f"{b['oos_cagr']:+.0f}%")]),
                 f"🏆 <b>Winner: {tgfmt.esc(c['name'])}</b>",
                 ("✅ It held up on unseen years." if c["oos_beat_nifty"] else "❌ It did NOT beat the Nifty."),
                 f"<i>Biggest fall along the way: {c['all_max_dd']:.0f}%</i>"]
        self.say("\n".join(lines), rich=True, buttons=[[("📊 Show today's picks", "/picks")]])

    def cmd_check(self, parts):
        from . import fundamentals, gurus
        if len(parts) < 2:
            return self.say("✍️ Type it like: <b>check TCS</b>", rich=True)
        sym = self._parse_trade(parts)[0] or parts[1].upper().strip(",.!?")
        fund = fundamentals.load_fundamentals(self.con)
        if sym not in fund.index:
            return self.say(f"No data for {sym} (only actively traded stocks are covered).")
        r = fund.loc[sym].to_dict()
        rows = []
        for name, (passed, judged, _) in gurus.score(r).items():
            if judged:
                bar = "●" * passed + "○" * (judged - passed)
                rows.append((name, f"{bar}  {passed}/{judged}"))
        val = []
        if r.get("pe") == r.get("pe") and r.get("pe"):
            val.append(f"P/E {r['pe']:.0f}")
        if r.get("roe") == r.get("roe") and r.get("roe") is not None:
            val.append(f"ROE {r['roe'] * 100:.0f}%")
        if r.get("debt_to_equity") == r.get("debt_to_equity") and r.get("debt_to_equity") is not None:
            val.append(f"D/E {r['debt_to_equity']:.2f}")
        self.say(f"🔎 <b>{tgfmt.esc(sym)}</b> · <i>{tgfmt.esc(r.get('name') or '')}</i>\n"
                 + (f"<i>{' · '.join(val)}</i>\n" if val else "") + tgfmt.table(rows)
                 + "<i>Investor checklists on today's numbers (not backtested).</i>", rich=True,
                 buttons=[[(f"🛒 Buy {sym} (day)", f"/buy {sym}"), (f"🛒 Buy {sym} (hold)", f"/buy {sym} {self.cfg['alerts']['default_budget']:g} hold")]])

    def cmd_day(self):
        if not market_is_open(datetime.now(IST)):
            return self.say("🌙 Market closed. Day-trade scans run 9:30 AM – 1:30 PM on weekdays.")
        self.say("⚡ Scanning the 100 most-traded stocks…")
        res = intraday.live_scan(self.con, self.cfg, self.cfg["intraday"]["budget"])
        if not res["signals"]:
            return self.say(f"😴 <b>No breakout right now.</b>\n<i>{tgfmt.esc(self._dt_age(res))}</i>", rich=True)
        best = sorted(res["signals"], key=lambda s: -s["profit"] / max(s["loss"], 0.01))[0]
        warn = "" if self.dt_working() else \
            "\n⚠️ <b>This setup LOST money in this week's re-test</b> - paper trade it only."
        self.say(tgfmt.day_signal(best, self._dt_age(res)) + warn, rich=True,
                 buttons=[[(f"🛒 Buy {best['symbol']} now", f"/buy {best['symbol']} {self.cfg['intraday']['budget']:g}")]])

    def cmd_top(self):
        ranked = pipeline.build_ranked(self.cfg, self.con, quiet=True).head(10)
        self.say("Old swing rules - top 10:\n" + "\n".join(f"{s}: {r.score}/100, Rs {r.close:,.2f}" for s, r in ranked.iterrows()))

    def cmd_plan(self, parts):
        budget = float(parts[1].replace(",", "")) if len(parts) > 1 else self.cfg["alerts"]["default_budget"]
        _, msg = pipeline.make_plan(self.cfg, pipeline.build_ranked(self.cfg, self.con, quiet=True), budget)
        self.say(msg)

    def cmd_dips(self):
        live = market_is_open(datetime.now(IST))
        res = dips.best_dips(self.con, self.cfg, live=live)
        budget = self.cfg["alerts"]["default_budget"]
        buttons = [[(f"🛒 Buy {d['symbol']} · {d['qty']} × ₹{d['price']:,.0f}",
                     f"/buy {d['symbol']} {budget:g} hold stop {d['stop']} target {d['target']}")]
                   for d in res["best"] if d["qty"] >= 1]
        self.say(tgfmt.dips(res), rich=True, buttons=buttons or None)

    def cmd_paper(self):
        s = wallet.summary(self.con, self.cfg, {})
        rows = [("Trades closed", str(len(s["closed"]))), ("All-time", tgfmt.signed(s["all_time_pnl"]))]
        if s["win_rate"] is not None:
            rows.append(("Win rate", f"{s['win_rate']:.0f}%"))
        dt = intraday.paper_trades(self.con)
        dtc = dt[dt.status == "closed"] if len(dt) else dt
        if len(dtc):
            rows += [("Day signals", f"{len(dtc)} closed"), ("Signals total", tgfmt.signed(dtc.net_pnl.sum())),
                     ("Signal wins", f"{(dtc.net_pnl > 0).mean() * 100:.0f}%")]
        last = s["closed"].tail(5)
        recent = "\n".join(f"{tgfmt.dot(r.net_pnl)} {tgfmt.esc(r.symbol)}  {tgfmt.signed(r.net_pnl)}"
                           for r in last.itertuples()) if len(last) else "<i>No closed trades yet.</i>"
        self.say("📒 <b>PAPER RESULTS</b>\n" + tgfmt.table(rows) + "<b>Last trades</b>\n" + recent, rich=True)

    def cmd_news(self):
        items = news.recent_items(self.con, days=2, sentiment="negative")[:8]
        if not items:
            return self.say("📰 No negative stock news in the last 2 days.")
        self.say("📰 <b>BAD NEWS · last 2 days</b>\n" + "\n".join(
            f"🔻 <b>{tgfmt.esc(i['symbols'][:20])}</b> — {tgfmt.esc(i['title'][:90])}" for i in items), rich=True)

    @staticmethod
    def _dt_age(res) -> str:
        d = res["delay_min"]
        if d is None:
            return "No candles yet today."
        return f"Data as of {res['data_time']:%H:%M} ({d:.0f} min old)" + (" — STALE, check live price!" if d > 15 else "")

    def dt_working(self) -> bool:
        """False while the latest weekly re-test of the day-trade setup LOST money."""
        from .db import get_state
        dt = json.loads(get_state(self.con, "dt_bt") or "{}")
        return dt.get("good", True)

    def dt_scan(self):
        day = datetime.now(IST).date().isoformat()
        res = intraday.live_scan(self.con, self.cfg, self.cfg["intraday"]["budget"])
        alerts_on = self.dt_working()
        for s in res["signals"]:
            if intraday.trades_today(self.con, day) >= self.cfg["intraday"]["max_trades_per_day"]:
                break
            # Paused? The signal is still paper-tested quietly, so we know when the setup works again
            if self.first_time(f"dt:{day}:{s['symbol']}") and intraday.paper_log(self.con, s, day) and alerts_on:
                self.say(tgfmt.day_signal(s, self._dt_age(res)), rich=True,
                         buttons=[[(f"🛒 Buy {s['symbol']} now", f"/buy {s['symbol']} {self.cfg['intraday']['budget']:g}")]])

    def square_off_reminder(self):
        day = datetime.now(IST).date().isoformat()
        opens = [p for p in wallet.open_positions(self.con) if p.get("mode") != "hold"]
        if (opens or intraday.trades_today(self.con, day)) and self.first_time(f"sq:{day}"):
            self.say("⏰ <b>3:10 PM — sell all day trades now.</b>\n<i>Brokers auto-close at ~3:20 with an extra fee.</i>",
                     rich=True, buttons=[[(f"💰 Sell #{p['id']} {p['symbol']}", f"/exit {p['id']}")] for p in opens] or None)

    def wallet_check(self):
        """Watches paper-wallet positions: fills night orders at 9:15, exit alerts, auto square-off."""
        for kind, p in wallet.fill_queued(self.con, self.cfg):
            if kind == "bought":
                self.say("🔔 <b>Market open — your order filled</b>\n" + tgfmt.bought(p, wallet.cash(self.con)),
                         rich=True, buttons=self._sell_buttons(p))
            elif kind == "sold":
                self.say("🔔 <b>Market open — your sell order filled</b>\n" + tgfmt.sold(p, p, wallet.cash(self.con)),
                         rich=True)
            else:
                self.say(f"↩️ Order for <b>{tgfmt.esc(p['symbol'])}</b> cancelled: the price at open was too high "
                         "for the amount. Money returned.", rich=True)
        opens = wallet.open_positions(self.con)
        if not opens:
            return
        px = wallet.live_prices([p["symbol"] for p in opens])
        for c in wallet.auto_square_off(self.con, self.cfg, px):
            self.say(f"🔒 <b>{tgfmt.esc(c['symbol'])}</b> was auto-sold at {tgfmt.rs(c['exit'])}: "
                     f"<b>{tgfmt.signed(c['net_pnl'])}</b>\n<i>Sell before 3:15 yourself — brokers charge extra.</i>", rich=True)
        now = datetime.now(IST)
        for p in wallet.open_positions(self.con):
            q = px.get(p["symbol"])
            if wallet.is_fresh(q, now):
                for level, msg in wallet.new_alerts(self.con, p, q[0], now, self.cfg):
                    self.say(tgfmt.exit_alert(msg, p), rich=True,
                             buttons=self._sell_buttons(p) + [[("Keep it", "/noop")]] if level != "info" else None)

    def dip_scan(self):
        """During market hours: tell me only about the BEST dip, once per stock per day."""
        res = dips.best_dips(self.con, self.cfg, live=True)
        day = datetime.now(IST).date().isoformat()
        res["best"] = [d for d in res["best"] if self.first_time(f"bestdip:{day}:{d['symbol']}")]
        if res["best"]:
            budget = self.cfg["alerts"]["default_budget"]
            self.say(tgfmt.dips(res), rich=True, buttons=[[(f"🛒 Buy {d['symbol']} · {d['qty']} × ₹{d['price']:,.0f}",
                     f"/buy {d['symbol']} {budget:g} hold stop {d['stop']} target {d['target']}")] for d in res["best"]])

    def news_scan(self):
        news.fetch_news(self.con, self.cfg)
        watched = self.watched_symbols()
        for item in news.recent_items(self.con, days=1, sentiment="negative"):
            hit = watched & set(item["symbols"].split(","))
            if hit and self.first_time(f"news:{item['id']}"):
                self.say(f"⚠️ <b>Bad news on {tgfmt.esc(', '.join(sorted(hit)))}</b>\n{tgfmt.esc(item['title'])}\n"
                         f"<i>{tgfmt.esc(item['link'])}</i>", rich=True)

    def daily_job(self):
        pipeline.update_all(self.cfg, self.con)
        today = datetime.now(IST).date().isoformat()
        latest = self.con.execute("SELECT MAX(date) FROM prices WHERE symbol=?", (prices.NIFTY_SYMBOL,)).fetchone()[0]
        if latest != today:
            self.say(f"🌙 No new prices today (last: {latest}) — market holiday?")
            return
        events = paper.update(self.con, self.cfg) + intraday.paper_resolve(self.con, self.cfg)
        import pandas as pd
        d = pd.Timestamp(today)
        review = d == d + pd.offsets.BMonthEnd(0)
        self.say("🌆 <b>EVENING REPORT</b> · " + d.strftime("%a %d %b"), rich=True)
        self.refresh_strategies(review)
        self.cmd_picks(review=review and self.first_time(f"review:{today}"))
        res = dips.best_dips(self.con, self.cfg)
        if res["best"]:
            self.cmd_dips()
        if events:
            self.say("📒 <b>Paper trades today</b>\n" + "\n".join(tgfmt.esc(e) for e in events), rich=True)

    # --- keeping the strategies up to date --------------------------------
    @staticmethod
    def _age_days(iso: str | None) -> float:
        if not iso:
            return 1e9
        try:
            then = datetime.fromisoformat(iso)
        except ValueError:
            return 1e9
        return (datetime.now(then.tzinfo) - then).total_seconds() / 86400

    def refresh_strategies(self, review_day: bool = False):
        """Re-chooses the strategies on the newest data, so the advice follows the market:
        - Strategy Lab: every month-end review (or if the champion is >35 days old). The test window
          moves forward each time (lab.split_date: auto).
        - Day-trade setup: re-tested on the last 60 days every week. Losing -> alerts pause."""
        from . import lab_ui, strategies
        from .db import get_state
        if self.cfg["lab"].get("auto_rerun", True):
            old = json.loads(get_state(self.con, "lab_champion") or "null")
            if not old or review_day or self._age_days(old.get("updated")) > 35:
                log("Re-running the Strategy Lab on the latest data")
                out = strategies.run_lab(self.con, self.cfg)
                lab_ui.save_champion(self.con, out, self.cfg)
                c, b = out["champion"], out["bench"]
                same = old and old["key"] == c["key"] and old["market_filter"] == bool(c["market_filter"])
                verdict = ("✅ still beats the Nifty on unseen years" if c["oos_beat_nifty"]
                           else "❌ does NOT beat the Nifty on unseen years — a Nifty index fund is the honest choice")
                if same:
                    self.say(f"🧪 <b>Strategies re-checked</b> on data up to today: <b>{tgfmt.esc(c['name'])}</b> is "
                             f"still the best ({c['oos_cagr']:+.1f}%/yr unseen vs Nifty {b['oos_cagr']:+.1f}%).\n"
                             f"<i>{verdict}</i>", rich=True)
                else:
                    self.say(f"🔄 <b>STRATEGY CHANGED</b>\nThe market changed, so the Lab picked a new winner: "
                             f"<b>{tgfmt.esc(c['name'])}</b> ({c['oos_cagr']:+.1f}%/yr unseen vs Nifty "
                             f"{b['oos_cagr']:+.1f}%, falls up to {c['all_max_dd']:.0f}%)"
                             + (f"\n<i>Before: {tgfmt.esc(old['name'])}</i>" if old else "")
                             + f"\n<i>{verdict}</i>\nPicks below use the new strategy. Holdings it no longer likes "
                               "are marked SELL.", rich=True)
        ic = self.cfg["intraday"]
        dt = json.loads(get_state(self.con, "dt_bt") or "{}")
        if self._age_days(dt.get("updated")) >= ic.get("retest_days", 7):
            log("Re-testing the day-trade setup on the last 60 days")
            res = intraday.backtest(self.con, self.cfg)
            if res["trades"].empty:
                return
            new = intraday.save_result(self.con, res["summary"])
            was = dt.get("good", True)
            if was and not new["good"]:
                self.say(f"⏸ <b>Day-trade alerts PAUSED</b>\nThe breakout setup made {tgfmt.esc(new['line'])} in the "
                         "latest re-test. I'll keep paper-testing it quietly and switch alerts back on when it "
                         "works again. (/day still shows signals, with a warning.)", rich=True)
            elif new["good"] and not was:
                self.say(f"▶️ <b>Day-trade alerts back ON</b>\nThe breakout setup made {tgfmt.esc(new['line'])} "
                         "in the latest re-test.", rich=True)

    def trading_today(self, now) -> bool:
        """Market hours AND the NSE is actually trading (a holiday has no prices from today)."""
        if not market_is_open(now):
            return False
        day = now.date().isoformat()
        if day in self._trading_day:
            return self._trading_day[day]
        if now.time() < dtime(9, 25):
            return True  # too early to tell; the wallet itself waits for today's prices
        try:
            q = wallet.live_prices(["RELIANCE", "HDFCBANK", "SBIN", "INFY"])
        except Exception as e:
            log(f"Holiday check failed ({e}) - assuming the market is open")
            return True
        ok = any(wallet.is_fresh(v, now) for v in q.values())
        self._trading_day[day] = ok
        if not ok:
            log(f"No prices from today ({day}) - market holiday, scans paused")
        return ok

    def catch_up(self):
        """PC was off at the evening update? Fetch what was missed so picks and dips use fresh data."""
        log("Catching up on missed data (PC was off at the evening update)")
        pipeline.update_all(self.cfg, self.con)
        self.refresh_strategies()
        log("Catch-up finished")

    def _needs_catch_up(self, now) -> bool:
        latest = self.con.execute("SELECT MAX(date) FROM prices WHERE symbol=?", (prices.NIFTY_SYMBOL,)).fetchone()[0]
        if not latest:
            return False
        last_session = now.date() - timedelta(days=1)
        while last_session.weekday() >= 5:
            last_session -= timedelta(days=1)
        return latest < last_session.isoformat()

    # --- one pass, for GitHub Actions --------------------------------------
    def _job(self, name: str, fn) -> None:
        """One failed job must not stop the others in this pass."""
        try:
            fn()
        except Exception as e:
            log(f"ERROR in {name}: {type(e).__name__}: {e}\n{traceback.format_exc()}")

    def _tick_due(self, name: str, minutes: float, now) -> bool:
        """Like _due, but remembered between runs. One minute of slack, since runs never start exactly on time."""
        last = self.state_get(f"last_{name}")
        if last and now - datetime.fromisoformat(last) < timedelta(minutes=minutes - 1):
            return False
        self.state_set(f"last_{name}", now.isoformat())
        return True

    def tick(self):
        """Does everything that is due right now, then exits. A scheduler starts this every few minutes:
        answers waiting Telegram messages, then runs the same jobs as the always-on loop in run()."""
        self.run_once = True
        now = datetime.now(IST)
        a = self.cfg["alerts"]
        log("Tick started")
        if not self.state_get("actions_announced"):
            self.say("🤖 <b>Stock bot now runs on GitHub Actions.</b>\nYour PC can be off. Replies are no longer instant: "
                     "I answer on my next run, up to 5 minutes in market hours and 30 minutes otherwise.",
                     menu=True, rich=True)
            self.state_set("actions_announced", now.isoformat())
        if telegram.configured():
            offset = int(self.state_get("tg_offset") or 0)
            cmds, new_offset = telegram.get_commands(offset, wait_seconds=0)
            self.state_set("tg_offset", new_offset)  # saved first, so a crash can't repeat a buy or sell
            for c in cmds:
                self._job(f"command {c!r}", lambda c=c: self.handle(c))

        if market_is_open(now):
            self._job("wallet_check", self.wallet_check)
        if dtime(15, 10) <= now.time() <= MARKET_CLOSE and self.trading_today(now):
            self._job("square_off", self.square_off_reminder)
        if now.time() >= dtime(9, 30) and self.trading_today(now):
            if self._tick_due("dt_scan", self.cfg["intraday"]["scan_minutes"], now):
                self._job("dt_scan", self.dt_scan)
            if self._tick_due("dip_scan", a["dip_scan_minutes"], now):
                self._job("dip_scan", self.dip_scan)
        brief = self.cfg.get("advisor", {}).get("morning_brief", "off")
        if (advisor.configured() and brief not in ("off", "", None) and now.weekday() < 5
                and dtime(*map(int, str(brief).split(":"))) <= now.time() < MARKET_CLOSE
                and self.state_get("brief_done") != now.date().isoformat()):
            self.state_set("brief_done", now.date().isoformat())
            self._job("morning_brief", self.morning_brief)
        if self._tick_due("news_scan", a["news_scan_minutes"], now):
            self._job("news_scan", self.news_scan)
        report_time = dtime(*map(int, a["daily_report_time"].split(":")))
        if now.weekday() < 5 and now.time() >= report_time and self.state_get("daily_done") != now.date().isoformat():
            self.state_set("daily_done", now.date().isoformat())
            self._job("daily_job", self.daily_job)
        elif (not market_is_open(now) and self.state_get("catch_up_done") != now.date().isoformat()
              and self._needs_catch_up(now)):
            self.state_set("catch_up_done", now.date().isoformat())
            self._job("catch_up", self.catch_up)
        log("Tick finished")

    # --- main loop -----------------------------------------------------
    def run(self):
        # Only one bot at a time (two would fight over your Telegram messages)
        marker = ROOT / "data" / "RUNS_ON_SERVER"
        if os.name == "nt" and marker.exists():
            print(f"The bot runs on your cloud server ({marker.read_text().strip()}).\n"
                  "Not starting it on this PC too. To move it back here, delete data\\RUNS_ON_SERVER "
                  "and stop it on the server (sudo systemctl disable --now stockbot).")
            return
        try:
            self._lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._lock.bind(("127.0.0.1", 47613))
        except OSError:
            print("The stock bot is already running (another window). Close it first.")
            log("Second copy refused - the bot is already running")
            return
        a = self.cfg["alerts"]
        report_time = dtime(*map(int, a["daily_report_time"].split(":")))
        if not telegram.configured():
            log("Telegram is not set up (.env) - alerts will only be printed here.")
        log("Bot starting. Log file: " + str(LOG_FILE))
        # Only announce a start after a long gap (restarts for updates stay quiet)
        last = self.state_get("last_start")
        now0 = datetime.now(IST)
        if not last or (now0 - datetime.fromisoformat(last)).total_seconds() > 6 * 3600:
            self.say("🤖 Stock bot is running. Tap a button below (or type <b>help</b>).", menu=True, rich=True)
        self.state_set("last_start", now0.isoformat())
        offset = int(self.state_get("tg_offset") or 0)
        if not market_is_open(now0) and self._needs_catch_up(now0):
            self._submit(self.jobs, "catch_up", self.catch_up)
        while True:
            try:
                now = datetime.now(IST)
                if telegram.configured():
                    # Short wait so scheduled jobs are checked often; replies are still instant
                    cmds, offset = telegram.get_commands(offset, wait_seconds=10)
                    self.state_set("tg_offset", offset)
                    for c in cmds:
                        try:
                            self.handle(c)
                        except Exception as e:
                            log(f"ERROR handling {c!r}: {e}\n{traceback.format_exc()}")
                else:
                    time.sleep(10)

                # Scheduled jobs run on the background worker - they never block your taps
                if market_is_open(now) and self._due(self.last_wallet_check, 1, now):
                    if self._submit(self.fast, "wallet_check", self.wallet_check):
                        self.last_wallet_check = now
                scans = now.time() >= dtime(9, 30) and self.trading_today(now)
                if scans and self._due(self.last_dt_scan, self.cfg["intraday"]["scan_minutes"], now):
                    if self._submit(self.jobs, "dt_scan", self.dt_scan):
                        self.last_dt_scan = now
                if dtime(15, 10) <= now.time() <= MARKET_CLOSE and self.trading_today(now):
                    self._submit(self.fast, "square_off", self.square_off_reminder)
                if scans and self._due(self.last_dip_scan, a["dip_scan_minutes"], now):
                    if self._submit(self.jobs, "dip_scan", self.dip_scan):
                        self.last_dip_scan = now
                brief = self.cfg.get("advisor", {}).get("morning_brief", "off")
                if (advisor.configured() and brief not in ("off", "", None) and now.weekday() < 5
                        and now.time() >= dtime(*map(int, str(brief).split(":")))
                        and now.time() < MARKET_CLOSE and self.state_get("brief_done") != now.date().isoformat()):
                    if self._submit(self.jobs, "morning_brief", self.morning_brief):
                        self.state_set("brief_done", now.date().isoformat())
                if self._due(self.last_news_scan, a["news_scan_minutes"], now):
                    if self._submit(self.jobs, "news_scan", self.news_scan):
                        self.last_news_scan = now
                if now.weekday() < 5 and now.time() >= report_time and self.state_get("daily_done") != now.date().isoformat():
                    if self._submit(self.jobs, "daily_job", self.daily_job):
                        self.state_set("daily_done", now.date().isoformat())
                        log("Evening job started in the background (data update + report)")
            except KeyboardInterrupt:
                log("Bot stopped.")
                return
            except Exception:
                log("ERROR in main loop:\n" + traceback.format_exc())
                time.sleep(30)

    @staticmethod
    def _due(last, minutes, now) -> bool:
        return last is None or now - last >= timedelta(minutes=minutes)
