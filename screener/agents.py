"""TRADING AGENTS: robot day-traders, each with its own PAPER account for the day (Breakout Rs 30,000, Quant Rs 10,000).

Each agent watches the 100 most-traded NSE stocks on 5-minute candles and follows one strategy.
It buys only with its own day's money (no leverage, never more), uses a stop-loss on every trade
and closes everything by 3:15 PM. The next day starts again from the same amount. P&L is after intraday charges.

The same code runs on past candles (a backtest) and on live candles (the day's trading). An agent
only ever sees candles that have closed, so a restart just replays the day so far and nothing is lost.
Paper only: nothing here can place a real order.
"""
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta

import pandas as pd

from . import intraday, ui
from .db import get_state, set_state
from .intraday import IST

SLIPPAGE = 0.0005       # 0.05%: we pay a little worse than the candle close on every fill
SQUARE_OFF = dtime(15, 15)

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_trades (
    day TEXT, agent TEXT, symbol TEXT, qty INTEGER, entry REAL, entry_time TEXT,
    exit REAL, exit_time TEXT, reason TEXT, pnl REAL, mode TEXT,
    PRIMARY KEY (day, agent, symbol, entry_time, mode)
);
"""


@dataclass
class Agent:
    key: str
    name: str
    idea: str
    tested: str          # what testing backs it, said plainly
    per_trade: float     # Rs per trade
    max_trades: int
    start: dtime
    end: dtime           # no new trades after this
    cap: float = 10_000  # Rs it may have in trades at once: its whole day's money


AGENTS = [  # trading now
    Agent("breakout", "Breakout Bot", "Buys a break above the 9:15-9:30 range on strong volume, above VWAP; target 2x risk",
          "the app's opening-range setup, backtested on 60 days", 10_000, 3, dtime(9, 30), dtime(13, 30), 30_000),
    Agent("quant", "Quant Bot", "At 9:30 buys the quant engine's top picks that trade above VWAP; stop 1.5%, out by 3:15",
          "quant score tested monthly, not intraday", 2500, 4, dtime(9, 30), dtime(10, 0)),
]
RETIRED = [  # 24 Nov 2026: lost money in their own ~80-day backtests; kept so their past trades still show
    Agent("bounce", "Bounce Bot", "Buys a stock down 2%+ today and 1% under VWAP when a strong green candle appears; "
          "target = back to VWAP", "retired 24 Nov 2026: lost in its backtest", 3300, 3, dtime(10, 0), dtime(14, 0)),
    Agent("rider", "Trend Rider", "Buys a stock up 1.5-6% making new day highs on strong volume; "
          "trailing stop 1% below its best price", "retired 24 Nov 2026: lost in its backtest", 3300, 3, dtime(10, 0), dtime(13, 0)),
]
BY_KEY = {a.key: a for a in AGENTS + RETIRED}


def ensure(con):
    con.executescript(SCHEMA)
    if "context" not in [r[1] for r in con.execute("PRAGMA table_info(agent_trades)")]:
        con.execute("ALTER TABLE agent_trades ADD COLUMN context TEXT")  # the conditions at entry, for learning


# ---------------------------------------------------------------- learning from losses
# Every trade records the conditions when it was bought. Each evening the agent looks for a condition under which
# it keeps losing; if that pattern holds in BOTH the older and the newer half of its trades (so it isn't one bad
# week), it stops buying under that condition. Its trade size also follows its record: steady losses shrink it.
FEATURES = {"time": "time of day", "market": "the market", "move": "the stock's move today", "volume": "volume"}


def context(r: pd.Series, t, breadth: float) -> dict:
    """The conditions at the moment of buying, in buckets a person can read."""
    hm = t.hour * 60 + t.minute
    chg = r["chg"] * 100
    return {"time": "9:30-10:30" if hm < 630 else "10:30-12:00" if hm < 720 else "after 12:00",
            "market": "market up" if breadth >= 0.55 else "market down" if breadth <= 0.45 else "market flat",
            "move": "down 2%+" if chg <= -2 else "down 0-2%" if chg < 0 else "up 0-2%" if chg < 2 else "up 2%+",
            "volume": "volume 2x+ normal" if r["volume"] >= 2 * r["vol_avg"] else "volume below 2x"}


def _t(x: pd.Series) -> float:
    return x.mean() / x.std() * math.sqrt(len(x)) if len(x) > 2 and x.std() > 0 else 0.0


def learn(con, today: str, lookback_days: int = 90) -> dict:
    """Lessons per agent from its trades before `today` (live and backtest): trade size, conditions to avoid,
    and plain-English reasons for its losses."""
    ensure(con)
    since = (pd.Timestamp(today) - pd.Timedelta(days=lookback_days)).date().isoformat()
    df = pd.read_sql_query("SELECT day, agent, reason, pnl, context FROM agent_trades WHERE pnl IS NOT NULL "
                           "AND day >= ? AND day < ?", con, params=(since, today))
    out = {"made": today}
    for a in AGENTS:
        g = df[df["agent"] == a.key]
        les = {"size": 1.0, "avoid": [], "why": [], "trades": len(g)}
        out[a.key] = les
        if len(g) < 10:
            les["why"].append(f"Only {len(g)} trades so far: too few to learn from yet (needs 10).")
            continue
        les["size"] = round(min(1.5, max(0.25, 1 + _t(g["pnl"]) / 4)), 2)  # steady losers shrink to 25%, winners grow to 150%
        losers = g[g["pnl"] < 0]
        if len(losers):
            top = losers["reason"].value_counts()
            les["why"].append(f"{len(losers)} of {len(g)} trades lost; most losses ended by '{top.index[0]}' "
                              f"({top.iloc[0] / len(losers):.0%} of them).")
        ctx = g[g["context"].notna() & (g["context"] != "null")]
        if len(ctx) >= 16:
            c = pd.DataFrame([json.loads(x) for x in ctx["context"]], index=ctx.index)
            mid = ctx["day"].sort_values().iloc[len(ctx) // 2]
            found = []
            for f in FEATURES:
                for bucket, idx in c.groupby(f).groups.items():
                    sub = ctx.loc[idx]
                    old, new = sub[sub["day"] < mid]["pnl"], sub[sub["day"] >= mid]["pnl"]
                    if (len(sub) >= 8 and len(old) >= 4 and len(new) >= 4 and old.mean() < 0 and new.mean() < 0
                            and _t(sub["pnl"]) <= -1.5):
                        found.append((sub["pnl"].mean(), f, bucket, len(sub), (sub["pnl"] > 0).mean()))
            for avg, f, bucket, n, win in sorted(found)[:2]:  # at most 2 rules, the worst first
                les["avoid"].append([f, bucket])
                les["why"].append(f"Loses when {FEATURES[f]} is '{bucket}' ({n} trades, {win:.0%} won, "
                                  f"₹{avg:+,.0f} a trade, in older AND newer trades), so it now skips those.")
        if les["size"] < 1:
            les["why"].append(f"Its record is negative overall, so its trade size is cut to {les['size']:.0%}.")
        elif les["size"] > 1:
            les["why"].append(f"Its record is positive overall, so its trade size is raised to {les['size']:.0%}.")
    return out


def lessons_for(con, day: str) -> dict:
    """The lessons in force on `day` (made for this day or earlier, so a replay of today never changes)."""
    q = json.loads(get_state(con, "agents_lessons") or "null")
    return q if q and q["made"] <= day else {}


# ---------------------------------------------------------------- the strategies
def prepare(bars: dict[str, pd.DataFrame], day) -> dict[str, pd.DataFrame]:
    """Today's candles per stock with the columns the agents read. bars hold yesterday too (for volume/close)."""
    out = {}
    for s, df in bars.items():
        df = intraday.add_columns(df)
        before = df[df.index.date < day]
        today = df[df.index.date == day].copy()
        if before.empty or len(today) < 4:
            continue
        today["chg"] = today["close"] / before["close"].iloc[-1] - 1
        today["or_high"] = today["high"].iloc[:3].max()
        today["or_low"] = today["low"].iloc[:3].min()
        today["day_high"] = today["high"].cummax()
        today["day_low"] = today["low"].cummin()
        today["strong"] = today["volume"] >= 1.5 * today["vol_avg"]
        today["prev_close"] = today["close"].shift(1)
        out[s] = today
    return out


def _bounded(entry, dist, lo, hi):
    return min(max(dist, entry * lo), entry * hi)


def wants(agent: Agent, r: pd.Series, picks: set) -> dict | None:
    """Entry plan if this agent buys on this (closed) candle: {stop, target or None, trail or None}."""
    c = r["close"]
    if agent.key == "breakout":
        if r["strong"] and c > r["or_high"] >= r["prev_close"] and c > r["vwap"]:
            risk = _bounded(c, c - r["or_low"], 0.003, 0.01)
            return {"stop": c - risk, "target": c + 2 * risk, "trail": None}
    elif agent.key == "bounce":
        if r["chg"] <= -0.02 and c < r["vwap"] * 0.99 and c > r["open"] and r["volume"] >= 1.2 * r["vol_avg"]:
            risk = _bounded(c, c - r["day_low"] * 0.997, 0.005, 0.015)
            return {"stop": c - risk, "target": r["vwap"], "trail": None}
    elif agent.key == "rider":
        if 0.015 <= r["chg"] <= 0.06 and r["strong"] and c > r["vwap"] and c >= r["day_high"] * 0.998:
            return {"stop": c * 0.99, "target": None, "trail": 0.01}
    elif agent.key == "quant":
        if r.name in picks and c > r["vwap"]:
            return {"stop": c * 0.985, "target": None, "trail": None}
    return None


def run_day(agent: Agent, day_bars: dict[str, pd.DataFrame], picks: set, cfg: dict, lessons: dict | None = None) -> list[dict]:
    """Plays one agent through one day of closed candles. Returns its trades (open ones have exit None).
    lessons: this agent's {size, avoid} from learn(); None = the plain rules (the backtest uses those)."""
    size = (lessons or {}).get("size", 1.0)
    avoid = {tuple(x) for x in (lessons or {}).get("avoid", [])}
    c = cfg["charges_intraday"]
    times = sorted({t for df in day_bars.values() for t in df.index})
    cash, open_, done = agent.cap, {}, []
    rows = {s: df.assign(symbol=s) for s, df in day_bars.items()}
    # share of stocks up on the day at each candle: "the market"
    breadth = pd.DataFrame({s: df["chg"] > 0 for s, df in day_bars.items()}).mean(axis=1) if day_bars else pd.Series(dtype=float)

    def close(s, price, why, t):
        p = open_.pop(s)
        price = price * (1 - SLIPPAGE)
        pnl = intraday.pnl("LONG", p["entry"], price, p["qty"], c)
        done.append({**p, "exit": round(price, 2), "exit_time": t.strftime("%H:%M"), "reason": why, "pnl": round(pnl, 2)})
        return p["qty"] * p["entry"] + pnl

    for t in times:
        # 1) exits on this candle for positions bought earlier
        for s in list(open_):
            df = rows[s]
            if t not in df.index:
                continue
            b, p = df.loc[t], open_[s]
            if t.time() >= SQUARE_OFF:
                cash += close(s, b["open"], "closed 3:15", t)
            elif b["open"] <= p["stop"]:
                cash += close(s, b["open"], "stop (gap)", t)
            elif b["low"] <= p["stop"]:
                cash += close(s, p["stop"], "stop", t)
            elif p["target"] and b["high"] >= p["target"]:
                cash += close(s, p["target"], "target", t)
            elif p["trail"]:
                p["stop"] = max(p["stop"], b["high"] * (1 - p["trail"]))
        # 2) entries on this candle's close
        if not (agent.start <= t.time() <= agent.end) or len(done) + len(open_) >= agent.max_trades:
            continue
        cands = []
        for s, df in rows.items():
            if s in open_ or t not in df.index or any(d["symbol"] == s for d in done):
                continue
            r = df.loc[t].copy()
            r.name = s
            plan = wants(agent, r, picks)
            if plan:
                plan["context"] = context(r, t, breadth.get(t, 0.5))
                if any((f, b) in avoid for f, b in plan["context"].items()):
                    continue  # a condition this agent has learned it loses in
                cands.append((abs(r["chg"]) if agent.key != "breakout" else r["volume"] / r["vol_avg"], s, r, plan))
        for _, s, r, plan in sorted(cands, key=lambda x: (x[1] in picks, x[0]), reverse=True):  # quant picks first
            if len(done) + len(open_) >= agent.max_trades:
                break
            entry = r["close"] * (1 + SLIPPAGE)
            qty = math.floor(min(agent.per_trade * size, cash) / entry)
            if qty < 1:
                continue
            cash -= qty * entry
            open_[s] = {"symbol": s, "qty": qty, "entry": round(entry, 2), "entry_time": t.strftime("%H:%M"),
                        "stop": round(plan["stop"], 2), "target": round(plan["target"], 2) if plan["target"] else None,
                        "trail": plan["trail"], "context": plan["context"]}
    return done + [{**p, "exit": None, "exit_time": None, "reason": "open", "pnl": None} for p in open_.values()]


# ---------------------------------------------------------------- results
def save(con, day: str, agent: str, trades: list[dict], mode: str) -> None:
    ensure(con)
    con.execute("DELETE FROM agent_trades WHERE day=? AND agent=? AND mode=?", (day, agent, mode))
    con.executemany("INSERT OR REPLACE INTO agent_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(day, agent, t["symbol"], t["qty"], t["entry"], t["entry_time"], t["exit"], t["exit_time"],
                      t["reason"], t["pnl"], mode, json.dumps(t.get("context"))) for t in trades])
    con.commit()


def leaderboard(con, mode: str, day: str | None = None) -> pd.DataFrame:
    ensure(con)
    q = "SELECT * FROM agent_trades WHERE mode=? AND pnl IS NOT NULL" + (" AND day=?" if day else "")
    df = pd.read_sql_query(q, con, params=(mode, day) if day else (mode,))
    rows = []
    for a in AGENTS + [r for r in RETIRED if (df["agent"] == r.key).any()]:
        g = df[df["agent"] == a.key]
        days = g.groupby("day")["pnl"].sum()
        rows.append({"agent": a.name, "key": a.key, "pnl": g["pnl"].sum(), "trades": len(g),
                     "win_rate": (g["pnl"] > 0).mean() * 100 if len(g) else float("nan"),
                     "days": len(days), "green_days": int((days > 0).sum()),
                     "best_day": days.max() if len(days) else float("nan"),
                     "worst_day": days.min() if len(days) else float("nan")})
    return pd.DataFrame(rows).sort_values("pnl", ascending=False)


# ---------------------------------------------------------------- backtest on stored candles
def backtest(con, cfg: dict, refresh: bool = True) -> pd.DataFrame:
    """Every agent except Quant (its picks are today's, so the past would be cheating) on ~60 past days."""
    symbols = intraday.liquid_universe(con, cfg)
    if refresh:
        with ui.console.status("[cyan]Downloading ~60 days of 5-minute candles (free, Yahoo)..."):
            intraday.save_bars(con, intraday.fetch_bars(symbols, cfg, period=f"{cfg['intraday']['backtest_days']}d"))
    bars = intraday.load_bars(con, symbols)
    days = sorted({d for df in bars.values() for d in df.index.date})
    ensure(con)
    con.execute("DELETE FROM agent_trades WHERE mode='backtest'")
    for i, day in enumerate(days[1:], 1):
        window = {s: df[(df.index.date == days[i - 1]) | (df.index.date == day)] for s, df in bars.items()}
        today = prepare(window, day)
        for a in AGENTS:
            if a.key != "quant":
                save(con, day.isoformat(), a.key, [t for t in run_day(a, today, set(), cfg) if t["pnl"] is not None],
                     "backtest")
    return leaderboard(con, "backtest")


# ---------------------------------------------------------------- live (paper) trading
def _msg_trade(a: Agent, t: dict, opened: bool) -> str:
    if opened:
        tgt = f", target ₹{t['target']:,.2f}" if t["target"] else (", trailing stop" if t["trail"] else "")
        return (f"🤖 <b>{a.name}</b> bought {t['qty']} <b>{t['symbol']}</b> @ ₹{t['entry']:,.2f} ({t['entry_time']}) "
                f"- stop ₹{t['stop']:,.2f}{tgt}")
    icon = "✅" if t["pnl"] > 0 else "❌"
    return (f"{icon} <b>{a.name}</b> sold {t['symbol']} @ ₹{t['exit']:,.2f} ({t['exit_time']}, {t['reason']}): "
            f"<b>₹{t['pnl']:+,.2f}</b> after charges")


def board_text(con, day: str) -> str:
    lb = leaderboard(con, "live", day)
    lines = [f"🏁 <b>Agents' paper results {day}</b> (paper, after charges)"]
    for i, r in enumerate(lb.itertuples(), 1):
        lines.append(f"{i}. {r.agent}: <b>₹{r.pnl:+,.2f}</b> ({r.trades} trades"
                     + (f", {r.win_rate:.0f}% won)" if r.trades else ")"))
    tot = leaderboard(con, "live")
    if tot["days"].max() > 1:
        lines.append("\nAll days so far: " + ", ".join(f"{r.agent} ₹{r.pnl:+,.0f}" for r in tot.itertuples()))
    q = json.loads(get_state(con, "agents_lessons") or "null")
    if q:
        lines.append("\n🧠 <b>What they learned</b> (used from the next trading day)")
        lines += [f"<b>{a.name}</b>: " + " ".join(q[a.key]["why"]) for a in AGENTS if q.get(a.key, {}).get("why")]
    lines.append("<i>Paper money only. One day proves nothing; watch weeks.</i>")
    return "\n".join(lines)


def live_cycle(con, cfg: dict, picks: set, now: datetime) -> list[str]:
    """Replays today so far for every agent on the latest closed candles; returns messages for new events."""
    from . import live
    symbols = sorted(set(intraday.liquid_universe(con, cfg)) | picks)
    data = live.candles(symbols, cfg, days=2)
    naive = now.replace(tzinfo=None)
    data = {s: df[df.index + pd.Timedelta(minutes=5) <= naive] for s, df in data.items()}  # closed candles only
    today = prepare(data, now.date())
    msgs, day = [], now.date().isoformat()
    sent_key = f"agents_sent:{day}"  # kept in the database: a restart mid-day doesn't resend old trades
    announced = {tuple(k) for k in json.loads(get_state(con, sent_key) or "[]")}
    lessons = lessons_for(con, day)
    for a in AGENTS:
        trades = run_day(a, today, picks, cfg, lessons.get(a.key))
        save(con, day, a.key, trades, "live")
        for t in trades:
            for opened in (True, False):
                if not opened and t["pnl"] is None:
                    continue
                key = (a.key, t["symbol"], t["entry_time"], opened)
                if key not in announced:
                    announced.add(key)
                    msgs.append(_msg_trade(a, t, opened))
    set_state(con, sent_key, json.dumps(sorted(announced)))
    set_state(con, "agents_last_cycle", now.isoformat())
    return msgs


def quant_picks(con, cfg: dict) -> set:
    """The quant engine's current picks; recomputed if older than 7 days (takes ~2 minutes)."""
    s = get_state(con, "quant_picks")
    q = json.loads(s) if s else None
    if not q or datetime.fromisoformat(q["at"]) < datetime.now() - timedelta(days=7):
        import sys
        if sys.platform != "win32":  # the small Linux server can't rank stocks: the PC pushes picks (push-plan)
            return set(q["picks"]) if q else set()
        from . import quant
        with ui.console.status("[cyan]Quant Bot: refreshing its picks (about 2 minutes)..."):
            r = quant.research(con, cfg)
        q = {"at": datetime.now().isoformat(), "picks": r["picks"]}
        set_state(con, "quant_picks", json.dumps(q))
    return set(q["picks"])


def run(cfg: dict, con) -> None:
    """Runs all agents during market hours, every trading day, until stopped (Ctrl+C)."""
    from . import telegram
    from .bot import market_is_open
    import sys
    from .config import ROOT
    marker = ROOT / "data" / "RUNS_ON_SERVER"
    if sys.platform == "win32" and marker.exists():
        print(f"The agents run inside the bot on your server ({marker.read_text().strip()}); not starting a second copy "
              "here (it would send every trade twice). Dashboard: python main.py web")
        return
    if sys.platform == "win32":  # keep the PC from idle-sleeping while this window is open (no settings changed)
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    picks, last_candle, reported = quant_picks(con, cfg), None, None  # before 9:15, not during the first scan
    ui.ok("Agents running (paper only). They trade 9:30-3:15 on market days; results go to Telegram. Ctrl+C stops.")
    telegram.send("🤖 <b>Trading agents switched on</b> (paper only): " + ", ".join(a.name for a in AGENTS)
                  + ". Paper money only, everything closed by 3:15.", rich=True)
    while True:
        now = datetime.now(IST)
        day = now.date().isoformat()
        if market_is_open(now) and now.time() >= dtime(9, 20):
            candle = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
            if candle != last_candle and now >= candle + timedelta(seconds=20):
                last_candle = candle
                try:
                    msgs = live_cycle(con, cfg, picks, now)
                    ui.info(f"{now:%H:%M} checked; {len(msgs)} new trade events")
                    for m in msgs:
                        ui.console.print(m)
                    if msgs:
                        telegram.send("\n\n".join(msgs), rich=True)
                except Exception as e:  # a bad data minute must not stop the day
                    ui.warn(f"{now:%H:%M} cycle failed: {type(e).__name__}: {str(e)[:150]}")
        elif now.time() >= dtime(15, 31) and reported != day and con.execute(
                "SELECT 1 FROM agent_trades WHERE mode='live' AND day=?", (day,)).fetchone():
            try:
                live_cycle(con, cfg, picks, now)  # final replay: every trade now closed at 3:15
            except Exception as e:
                ui.warn(f"final check failed: {e}")
            set_state(con, "agents_lessons", json.dumps(learn(con, (now + timedelta(days=1)).date().isoformat())))
            text = board_text(con, day)
            ui.console.print(text)
            telegram.send(text, rich=True)
            reported, picks = day, quant_picks(con, cfg)
        time.sleep(15)
