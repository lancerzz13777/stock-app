"""Stage 8: paper trading - log suggested trades with fake money and track how they'd have done
on real prices. Uses the same buy/sell mechanics as the backtest (trading.py).
"""
from datetime import date

import pandas as pd

from .charges import net_pnl
from .trading import check_exit, entry_price

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT,           -- date the idea was generated (after that day's close)
    symbol TEXT,
    qty INTEGER,
    target REAL,
    stop REAL,
    dont_buy_above REAL,
    score INTEGER,
    status TEXT,            -- pending / open / closed / skipped
    entry_date TEXT, entry REAL,
    exit_date TEXT, exit REAL,
    reason TEXT,
    days INTEGER,
    net_pnl REAL
);
"""


def _ensure(con):
    con.executescript(SCHEMA)


def log_plans(con, plans, created: str | None = None) -> int:
    """Adds planner suggestions as pending paper trades (skips stocks already pending/open)."""
    _ensure(con)
    created = created or date.today().isoformat()
    added = 0
    for p in plans:
        busy = con.execute("SELECT 1 FROM paper_trades WHERE symbol=? AND status IN ('pending','open')",
                           (p.symbol,)).fetchone()
        if busy:
            continue
        con.execute(
            "INSERT INTO paper_trades (created, symbol, qty, target, stop, dont_buy_above, score, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
            (created, p.symbol, p.qty, p.target, p.stop, p.dont_buy_above, p.score),
        )
        added += 1
    con.commit()
    return added


def _bars_after(con, symbol: str, after: str) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT date, open, high, low, close FROM prices WHERE symbol=? AND date > ? ORDER BY date",
        con, params=(symbol, after)).set_index("date")


def update(con, cfg: dict) -> list[str]:
    """Fills pending trades at the next open and closes open ones on stop/target/time. Returns events."""
    _ensure(con)
    max_hold = cfg["paper"]["max_hold_days"]
    events = []
    cols = [c[1] for c in con.execute("PRAGMA table_info(paper_trades)")]
    for row in con.execute("SELECT * FROM paper_trades WHERE status IN ('pending','open')").fetchall():
        t = dict(zip(cols, row))
        if t["status"] == "pending":
            bars = _bars_after(con, t["symbol"], t["created"])
            if bars.empty:
                continue
            first_day, bar = bars.index[0], bars.iloc[0]
            price = entry_price(t, bar)
            if price is None:
                con.execute("UPDATE paper_trades SET status='skipped', entry_date=?, reason=? WHERE id=?",
                            (first_day, f"opened at {bar['open']:.2f}, outside buy range", t["id"]))
                events.append(f"Paper: skipped {t['symbol']} (opened at Rs {bar['open']:.2f}, outside buy range)")
                continue
            con.execute("UPDATE paper_trades SET status='open', entry_date=?, entry=? WHERE id=?",
                        (first_day, price, t["id"]))
            events.append(f"Paper: bought {t['qty']} {t['symbol']} at Rs {price:.2f}")
            t.update(status="open", entry_date=first_day, entry=price)

        # Open: walk through every day after entry until it exits
        for days_held, (d, bar) in enumerate(_bars_after(con, t["symbol"], t["entry_date"]).iterrows(), 1):
            ex = check_exit(t, bar, days_held, max_hold)
            if ex:
                px, why = ex
                pnl = net_pnl(t["entry"], px, t["qty"], cfg["charges"])
                con.execute("UPDATE paper_trades SET status='closed', exit_date=?, exit=?, reason=?, days=?, "
                            "net_pnl=? WHERE id=?", (d, px, why, days_held, round(pnl, 2), t["id"]))
                events.append(f"Paper: sold {t['symbol']} at Rs {px:.2f} ({why}) -> {pnl:+.2f} after charges")
                break
    con.commit()
    return events


def report(con, cfg: dict) -> str:
    _ensure(con)
    df = pd.read_sql_query("SELECT * FROM paper_trades ORDER BY id", con)
    if df.empty:
        return ("No paper trades yet. Run 'python main.py plan' and answer 'y' to log the ideas, "
                "or let the always-on bot log them each evening.")
    lines = []
    closed = df[df.status == "closed"]
    open_ = df[df.status == "open"]
    pending = df[df.status == "pending"]
    first = pd.to_datetime(df.created.min()).date()
    running_days = (date.today() - first).days

    lines.append(f"PAPER TRADING since {first} ({running_days} days)")
    lines.append(f"Closed: {len(closed)}  Open: {len(open_)}  Waiting to buy: {len(pending)}  "
                 f"Skipped: {(df.status == 'skipped').sum()}")
    if len(closed):
        wins = closed[closed.net_pnl > 0]
        lines.append(f"Win rate: {len(wins) / len(closed) * 100:.0f}%   Total P&L after charges: "
                     f"Rs {closed.net_pnl.sum():+.2f}   Avg per trade: Rs {closed.net_pnl.mean():+.2f}")
    for _, t in open_.iterrows():
        last = con.execute("SELECT close FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 1",
                           (t.symbol,)).fetchone()
        if last:
            unreal = net_pnl(t.entry, last[0], int(t.qty), cfg["charges"])
            lines.append(f"  open {t.symbol}: {t.qty} @ {t.entry:.2f}, now {last[0]:.2f}, "
                         f"target {t.target}, stop {t.stop} -> {unreal:+.2f} if sold now")
    for _, t in closed.tail(10).iterrows():
        lines.append(f"  {t.exit_date} {t.symbol}: {t.reason}, {t.net_pnl:+.2f}")

    p = cfg["paper"]
    ok = running_days >= p["trust_after_days"] and len(closed) >= p["trust_after_trades"] \
        and len(closed) and closed.net_pnl.sum() > 0
    lines.append("")
    if ok:
        lines.append("Paper results are positive over enough time and trades. If the backtest also beat "
                     "the Nifty, you could consider small real trades - still with a stop-loss every time.")
    else:
        lines.append(f"NOT proven yet: need {p['trust_after_days']}+ days, {p['trust_after_trades']}+ closed "
                     "trades and a positive total before using real money.")
    return "\n".join(lines)
