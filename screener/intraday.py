"""DAY TRADING: Opening Range Breakout (ORB) on 5-minute candles, all positions closed by 3:15 PM.

Strategy (all numbers in config.yaml -> intraday):
1. Opening range = high/low of the first 15 minutes (9:15-9:30).
2. LONG signal: a 5-min candle CLOSES above the range high, above VWAP, on volume >= 1.5x average.
   (SHORT is the mirror image, only if allow_short: true.)
3. Entry at the NEXT candle's open. Stop at the other side of the range, kept between 0.3% and 1%.
   Target = 2x the risk. If neither is hit, square off at 3:15 PM.
4. Max 2 trades a day. One trade per stock per day.

Free data limits: Yahoo gives ~60 days of 5-min history, and live candles can be several minutes
late. Every live signal shows how old its data is - if it's stale, the move may already be over.
"""
import math
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from .charges import side_charges

IST = ZoneInfo("Asia/Kolkata")
TICK = 0.05

SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_bars (
    symbol TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS intraday_paper (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT, symbol TEXT, direction TEXT,
    signal_time TEXT, entry_time TEXT, entry REAL, qty INTEGER,
    stop REAL, target REAL,
    exit_time TEXT, exit REAL, reason TEXT, net_pnl REAL,
    status TEXT                -- waiting / closed / skipped
);
"""


def ensure(con):
    con.executescript(SCHEMA)


def _t(s: str) -> dtime:
    return dtime(*map(int, s.split(":")))


# ---------------------------------------------------------------- data
def liquid_universe(con, cfg: dict) -> list[str]:
    """The most traded stocks (by 20-day average turnover) - day trading needs heavy volume."""
    ic = cfg["intraday"]
    df = pd.read_sql_query(
        "SELECT symbol, date, close, volume FROM prices WHERE date >= date('now', '-45 days') "
        "AND symbol NOT LIKE '^%'", con)
    if df.empty:
        return []
    df["turnover"] = df["close"] * df["volume"]
    g = df.sort_values("date").groupby("symbol").agg(turnover=("turnover", lambda x: x.tail(20).mean()),
                                                     close=("close", "last"))
    g = g[g["close"] >= ic["min_price"]]
    return list(g.sort_values("turnover", ascending=False).head(ic["universe_size"]).index)


def fetch_bars(symbols: list[str], cfg: dict, period: str) -> dict[str, pd.DataFrame]:
    """5-min candles in IST market hours. {symbol: DataFrame[open, high, low, close, volume]}."""
    out = {}
    size = cfg["data"]["batch_size"]
    for i in range(0, len(symbols), size):
        batch = symbols[i:i + size]
        raw = yf.download([f"{s}.NS" for s in batch], period=period, interval=cfg["intraday"]["interval"],
                          group_by="ticker", auto_adjust=False, prepost=False, threads=True, progress=False)
        if raw is None or raw.empty:
            continue
        for s in batch:
            try:
                df = raw[f"{s}.NS"] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            df = df.dropna(subset=["Close"]).rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
            if df.empty:
                continue
            idx = pd.to_datetime(df.index)
            idx = idx.tz_localize("UTC") if idx.tz is None else idx
            df.index = idx.tz_convert(IST).tz_localize(None)
            df = df[(df.index.time >= dtime(9, 15)) & (df.index.time < dtime(15, 30))]
            out[s] = df
    return out


def save_bars(con, data: dict[str, pd.DataFrame]) -> None:
    ensure(con)
    for s, df in data.items():
        rows = [(s, ts.strftime("%Y-%m-%d %H:%M"), *map(float, r)) for ts, r in zip(df.index, df.itertuples(index=False))]
        con.executemany("INSERT OR REPLACE INTO intraday_bars VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    con.commit()


def load_bars(con, symbols: list[str] | None = None) -> dict[str, pd.DataFrame]:
    ensure(con)
    df = pd.read_sql_query("SELECT * FROM intraday_bars ORDER BY symbol, ts", con)
    if symbols is not None:
        df = df[df["symbol"].isin(symbols)]
    out = {}
    for s, g in df.groupby("symbol"):
        g = g.drop(columns="symbol")
        g.index = pd.to_datetime(g.pop("ts"))
        out[s] = g
    return out


# ---------------------------------------------------------------- strategy
def add_columns(df: pd.DataFrame) -> pd.DataFrame:
    """VWAP (resets daily) and 20-candle average volume (across days)."""
    df = df.copy()
    day = df.index.date
    tp = (df["high"] + df["low"] + df["close"]) / 3
    pv = (tp * df["volume"]).groupby(day).cumsum()
    vol = df["volume"].groupby(day).cumsum()
    df["vwap"] = pv / vol.replace(0, float("nan"))
    df["vol_avg"] = df["volume"].rolling(20, min_periods=5).mean().shift(1)
    return df


def find_signal(day_df: pd.DataFrame, cfg: dict) -> dict | None:
    """First breakout of one day (needs add_columns already applied). None if no signal."""
    ic = cfg["intraday"]
    n_or = ic["opening_range_minutes"] // 5
    if len(day_df) <= n_or:
        return None
    or_high = day_df["high"].iloc[:n_or].max()
    or_low = day_df["low"].iloc[:n_or].min()
    first, last = _t(ic["first_entry"]), _t(ic["last_entry"])
    for i in range(n_or, len(day_df)):
        bar = day_df.iloc[i]
        t = day_df.index[i].time()
        if t < first or t > last:
            continue
        prev_close = day_df["close"].iloc[i - 1]
        strong = bar["vol_avg"] > 0 and bar["volume"] >= ic["volume_multiple"] * bar["vol_avg"]
        if not strong:
            continue
        if bar["close"] > or_high >= prev_close and bar["close"] > bar["vwap"]:
            return {"i": i, "direction": "LONG", "or_high": or_high, "or_low": or_low}
        if ic["allow_short"] and bar["close"] < or_low <= prev_close and bar["close"] < bar["vwap"]:
            return {"i": i, "direction": "SHORT", "or_high": or_high, "or_low": or_low}
    return None


def make_levels(entry: float, sig: dict, budget: float, cfg: dict) -> dict | None:
    """Stop, target and quantity for an entry price. None if we can't afford 1 share."""
    ic = cfg["intraday"]
    qty = int(budget * ic["leverage"] // entry)
    if qty < 1:
        return None
    if sig["direction"] == "LONG":
        dist = entry - sig["or_low"]
    else:
        dist = sig["or_high"] - entry
    dist = min(max(dist, entry * ic["min_stop_pct"] / 100), entry * ic["max_stop_pct"] / 100)
    sign = 1 if sig["direction"] == "LONG" else -1
    stop = entry - sign * dist
    target = entry + sign * dist * ic["reward_risk"]
    rnd = (lambda p: math.floor(p / TICK) * TICK) if sign == 1 else (lambda p: math.ceil(p / TICK) * TICK)
    return {"qty": qty, "stop": round(rnd(stop), 2), "target": round(target, 2)}


def pnl(direction: str, entry: float, exit_: float, qty: int, c: dict) -> float:
    """Net profit after intraday charges (long: buy then sell; short: sell then buy)."""
    buy, sell = (entry, exit_) if direction == "LONG" else (exit_, entry)
    return (sell - buy) * qty - side_charges(buy * qty, "buy", c).total - side_charges(sell * qty, "sell", c).total


def simulate(day_df: pd.DataFrame, start_i: int, direction: str, stop: float, target: float,
             cfg: dict) -> tuple[float, str, str]:
    """Walk candles from start_i: exit at stop/target (stop wins if both), else at square-off time."""
    sq = _t(cfg["intraday"]["square_off"])
    long = direction == "LONG"
    for i in range(start_i, len(day_df)):
        b = day_df.iloc[i]
        ts = day_df.index[i]
        if ts.time() >= sq:
            return b["open"], "square-off 3:15", ts.strftime("%H:%M")
        if (long and b["open"] <= stop) or (not long and b["open"] >= stop):
            return b["open"], "stop (gap)", ts.strftime("%H:%M")
        if (long and b["low"] <= stop) or (not long and b["high"] >= stop):
            return stop, "stop", ts.strftime("%H:%M")
        if (long and b["high"] >= target) or (not long and b["low"] <= target):
            return target, "target", ts.strftime("%H:%M")
    last = day_df.iloc[-1]
    return last["close"], "end of data", day_df.index[-1].strftime("%H:%M")


def trade_for_day(day_df: pd.DataFrame, budget: float, cfg: dict) -> dict | None:
    """The full ORB trade for one stock on one (complete) day, or None."""
    sig = find_signal(day_df, cfg)
    if sig is None or sig["i"] + 1 >= len(day_df):
        return None
    entry_i = sig["i"] + 1
    entry = day_df["open"].iloc[entry_i]
    lv = make_levels(entry, sig, budget, cfg)
    if lv is None:
        return None
    exit_, why, exit_time = simulate(day_df, entry_i, sig["direction"], lv["stop"], lv["target"], cfg)
    return {
        "direction": sig["direction"], "signal_time": day_df.index[sig["i"]].strftime("%H:%M"),
        "entry_time": day_df.index[entry_i].strftime("%H:%M"), "entry": round(entry, 2), **lv,
        "exit": round(exit_, 2), "exit_time": exit_time, "reason": why,
        "net_pnl": round(pnl(sig["direction"], entry, exit_, lv["qty"], cfg["charges_intraday"]), 2),
    }


# ---------------------------------------------------------------- backtest
def backtest(con, cfg: dict, refresh: bool = True, progress=None) -> dict:
    ic = cfg["intraday"]
    symbols = liquid_universe(con, cfg)
    if not symbols:
        raise RuntimeError("No daily data yet - run an update first.")
    if refresh:
        save_bars(con, fetch_bars(symbols, cfg, period=f"{ic['backtest_days']}d"))
    bars = load_bars(con, symbols)

    candidates = []
    for s, df in bars.items():
        df = add_columns(df)
        for day, ddf in df.groupby(df.index.date):
            t = trade_for_day(ddf, ic["budget"], cfg)
            if t:
                candidates.append({"day": str(day), "symbol": s, **t})
        if progress:
            progress()
    if not candidates:
        return {"trades": pd.DataFrame(), "symbols": len(bars)}

    # Take trades in time order, max N per day (you can only watch so many)
    cand = pd.DataFrame(candidates).sort_values(["day", "signal_time"])
    trades = cand.groupby("day").head(ic["max_trades_per_day"]).reset_index(drop=True)
    daily = trades.groupby("day")["net_pnl"].sum()
    cum = daily.cumsum()
    days_total = len({d for df in bars.values() for d in df.index.date})
    wins = trades[trades.net_pnl > 0]
    summary = {
        "symbols": len(bars),
        "period": f"{trades.day.min()} to {trades.day.max()}",
        "market_days": days_total,
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "avg_win": round(wins.net_pnl.mean(), 2) if len(wins) else 0.0,
        "avg_loss": round(trades[trades.net_pnl <= 0].net_pnl.mean(), 2) if len(wins) < len(trades) else 0.0,
        "avg_per_trade": round(trades.net_pnl.mean(), 2),
        "total_pnl": round(trades.net_pnl.sum(), 2),
        "best_day": round(daily.max(), 2),
        "worst_day": round(daily.min(), 2),
        "green_days_pct": round((daily > 0).mean() * 100, 1),
        "max_drawdown": round(float((cum - cum.cummax()).min()), 2),
        "exit_reasons": trades.reason.value_counts().to_dict(),
        "budget": ic["budget"], "leverage": ic["leverage"],
        "return_on_budget_pct": round(trades.net_pnl.sum() / ic["budget"] * 100, 2),
    }
    nifty = con.execute("SELECT date, close FROM prices WHERE symbol='^NSEI' AND date BETWEEN ? AND ? ORDER BY date",
                        (trades.day.min(), trades.day.max())).fetchall()
    if len(nifty) >= 2:
        summary["nifty_return_pct"] = round((nifty[-1][1] / nifty[0][1] - 1) * 100, 2)
    return {"summary": summary, "trades": trades, "daily": daily}


def save_result(con, s: dict) -> dict:
    """Remembers the latest backtest summary - shown in the menu, and used by the bot to pause
    day-trade alerts while the setup is losing money."""
    import json
    from .db import set_state
    good = bool(s["total_pnl"] > 0 and s["trades"] >= 30)
    sign = "+" if s["total_pnl"] >= 0 else "-"
    out = {"good": good, "total": float(s["total_pnl"]), "trades": int(s["trades"]),
           "win_rate": float(s["win_rate_pct"]), "days": int(s["market_days"]),
           "updated": datetime.now(IST).isoformat(timespec="seconds"),
           "line": f"{sign}₹{abs(s['total_pnl']):,.2f} over {s['market_days']} days, {s['win_rate_pct']}% wins "
                   f"({datetime.now(IST):%d %b})"}
    set_state(con, "dt_bt", json.dumps(out))
    return out


def verdict(s: dict) -> list[str]:
    out = []
    if s["trades"] < 30:
        out.append(f"Only {s['trades']} trades - far too few to judge. NOT proven.")
    out.append(f"Free data only covers ~{s['market_days']} trading days - one market mood. "
               "Treat any result as a hint, not proof.")
    if s["total_pnl"] <= 0:
        out.append(f"LOST money: Rs {s['total_pnl']:+,.2f} after charges. Doing nothing would have been better.")
    else:
        out.append(f"Made Rs {s['total_pnl']:+,.2f} after charges ({s['return_on_budget_pct']:+.1f}% on a "
                   f"Rs {s['budget']:,} budget).")
        if "nifty_return_pct" in s and s["return_on_budget_pct"] <= s["nifty_return_pct"]:
            out.append(f"...but just holding the Nifty returned {s['nifty_return_pct']:+.1f}% in the same period.")
    out.append("SEBI's study of FY2023 found about 7 in 10 individual intraday traders lost money. "
               "Paper-trade at least 2-3 months before using real money.")
    return out


# ---------------------------------------------------------------- live
def live_scan(con, cfg: dict, budget: float) -> dict:
    """Checks today's candles for fresh breakouts. Returns signals plus how old the data is."""
    ic = cfg["intraday"]
    from . import live
    symbols = liquid_universe(con, cfg)
    data = live.candles(symbols, cfg, days=2)  # today + yesterday (for the average volume) is all it uses
    now = datetime.now(IST).replace(tzinfo=None)
    today = now.date()
    signals, status, newest = [], [], None
    for s, df in data.items():
        df = add_columns(df)
        # Drop the candle that is still forming
        df = df[df.index + pd.Timedelta(minutes=5) <= now]
        tdf = df[df.index.date == today]
        if tdf.empty:
            continue
        newest = max(newest, tdf.index[-1]) if newest else tdf.index[-1]
        last = tdf.iloc[-1]
        n_or = ic["opening_range_minutes"] // 5
        or_high = tdf["high"].iloc[:n_or].max() if len(tdf) >= n_or else float("nan")
        status.append({"symbol": s, "price": last["close"],
                       "chg_pct": (last["close"] / df[df.index.date < today]["close"].iloc[-1] - 1) * 100
                       if (df.index.date < today).any() else 0.0,
                       "vs_range_pct": (last["close"] / or_high - 1) * 100 if or_high == or_high else float("nan"),
                       "above_vwap": last["close"] > last["vwap"]})
        sig = find_signal(tdf, cfg)
        # Only signals on the last 2 completed candles are "fresh"
        if sig and sig["i"] >= len(tdf) - 2:
            entry = last["close"]
            lv = make_levels(entry, sig, budget, cfg)
            if lv:
                win = pnl(sig["direction"], entry, lv["target"], lv["qty"], cfg["charges_intraday"])
                loss = -pnl(sig["direction"], entry, lv["stop"], lv["qty"], cfg["charges_intraday"])
                signals.append({"symbol": s, "direction": sig["direction"],
                                "signal_time": tdf.index[sig["i"]].strftime("%H:%M"),
                                "entry": round(entry, 2), **lv, "profit": round(win, 2), "loss": round(loss, 2)})
    save_bars(con, data)
    delay = (now - (newest + pd.Timedelta(minutes=5))).total_seconds() / 60 if newest is not None else None
    return {"signals": signals, "status": status, "data_time": newest, "delay_min": delay,
            "universe": len(symbols), "source": live.source(cfg).name}


# ---------------------------------------------------------------- paper trading
def paper_log(con, sig: dict, day: str) -> bool:
    """Records a live signal as a paper trade (resolved after the close). Respects max trades/day."""
    ensure(con)
    if con.execute("SELECT 1 FROM intraday_paper WHERE day=? AND symbol=?", (day, sig["symbol"])).fetchone():
        return False
    con.execute("INSERT INTO intraday_paper (day, symbol, direction, signal_time, status) VALUES (?, ?, ?, ?, 'waiting')",
                (day, sig["symbol"], sig["direction"], sig["signal_time"]))
    con.commit()
    return True


def trades_today(con, day: str) -> int:
    ensure(con)
    return con.execute("SELECT COUNT(*) FROM intraday_paper WHERE day=? AND status != 'skipped'", (day,)).fetchone()[0]


def paper_resolve(con, cfg: dict) -> list[str]:
    """After the close: replays each waiting paper trade on that day's real candles."""
    ensure(con)
    now = datetime.now(IST)
    rows = con.execute("SELECT id, day, symbol, signal_time FROM intraday_paper WHERE status='waiting'").fetchall()
    rows = [r for r in rows if r[1] < now.date().isoformat() or now.time() >= dtime(15, 30)]
    if not rows:
        return []
    from . import live
    syms = sorted({r[2] for r in rows})
    save_bars(con, live.candles(syms, cfg, days=5))
    bars = load_bars(con, syms)
    events = []
    for pid, day, sym, sig_time in rows:
        df = bars.get(sym)
        if df is None:
            continue
        ddf = add_columns(df)
        ddf = ddf[ddf.index.date == pd.Timestamp(day).date()]
        times = [t.strftime("%H:%M") for t in ddf.index]
        if sig_time not in times:
            continue
        i = times.index(sig_time) + 1
        if i >= len(ddf):
            continue
        sig = find_signal(ddf, cfg) or {}
        if not sig:
            con.execute("UPDATE intraday_paper SET status='skipped', reason='signal not confirmed on final data' "
                        "WHERE id=?", (pid,))
            continue
        entry = ddf["open"].iloc[i]
        lv = make_levels(entry, sig, cfg["intraday"]["budget"], cfg)
        if lv is None:
            continue
        exit_, why, exit_time = simulate(ddf, i, sig["direction"], lv["stop"], lv["target"], cfg)
        p = pnl(sig["direction"], entry, exit_, lv["qty"], cfg["charges_intraday"])
        con.execute("UPDATE intraday_paper SET entry_time=?, entry=?, qty=?, stop=?, target=?, exit_time=?, exit=?, "
                    "reason=?, net_pnl=?, status='closed' WHERE id=?",
                    (times[i], entry, lv["qty"], lv["stop"], lv["target"], exit_time, exit_, why, round(p, 2), pid))
        events.append(f"Day-trade paper: {sym} {sig['direction']} {lv['qty']} @ {entry:.2f} -> {exit_:.2f} "
                      f"({why}) = Rs {p:+.2f}")
    con.commit()
    return events


def paper_trades(con) -> pd.DataFrame:
    ensure(con)
    return pd.read_sql_query("SELECT * FROM intraday_paper ORDER BY day, signal_time", con)
