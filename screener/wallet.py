"""Paper wallet for day trading: fake money, real live prices.

- BUY: the money for the trade is LOCKED (taken out of your available cash).
- While open: live profit/loss, and alerts telling you when to get out
  (target reached, stop-loss hit, half-way -> protect your profit, square-off time).
- EXIT: the money is released back with the profit or loss AFTER all intraday charges.
- Still open at 3:20 PM? It is closed automatically, like a broker would.
"""
from datetime import datetime, time as dtime, timedelta

import pandas as pd

from .intraday import IST, TICK, pnl

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallet (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cash REAL,              -- available (not locked)
    deposited REAL,         -- total fake money ever added
    created TEXT
);
CREATE TABLE IF NOT EXISTS wallet_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT, symbol TEXT, side TEXT, qty INTEGER,
    entry REAL, locked REAL, stop REAL, target REAL,
    status TEXT,            -- open / closed
    closed_at TEXT, exit REAL, reason TEXT, net_pnl REAL,
    alerts_sent TEXT DEFAULT '',
    mode TEXT DEFAULT 'day'  -- day (closed by 3:20 PM) / hold (strategy position, reviewed monthly)
);
CREATE TABLE IF NOT EXISTS wallet_log (
    ts TEXT, kind TEXT, amount REAL, cash_after REAL, note TEXT
);
"""


class WalletError(Exception):
    """A message for the user (not enough cash, unknown stock, market closed...)."""


def _now() -> datetime:
    return datetime.now(IST)


def _t(s: str) -> dtime:
    return dtime(*map(int, s.split(":")))


def ensure(con, cfg: dict) -> None:
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(wallet_positions)")}
    if "mode" not in have:
        con.execute("ALTER TABLE wallet_positions ADD COLUMN mode TEXT DEFAULT 'day'")
    if "sell_queued" not in have:  # 1 = sell at the next market open
        con.execute("ALTER TABLE wallet_positions ADD COLUMN sell_queued INTEGER DEFAULT 0")
    if not con.execute("SELECT 1 FROM wallet WHERE id=1").fetchone():
        start = float(cfg["wallet"]["starting_cash"])
        con.execute("INSERT INTO wallet VALUES (1, ?, ?, ?)", (start, start, _now().isoformat()))
        _log(con, "deposit", start, start, "starting fake money")
        con.commit()


def _log(con, kind, amount, cash_after, note):
    con.execute("INSERT INTO wallet_log VALUES (?, ?, ?, ?, ?)",
                (_now().strftime("%Y-%m-%d %H:%M:%S"), kind, round(amount, 2), round(cash_after, 2), note))


def cash(con) -> float:
    return con.execute("SELECT cash FROM wallet WHERE id=1").fetchone()[0]


def deposit(con, cfg, amount: float) -> float:
    ensure(con, cfg)
    if amount <= 0:
        raise WalletError("Amount must be above 0")
    con.execute("UPDATE wallet SET cash = cash + ?, deposited = deposited + ? WHERE id=1", (amount, amount))
    _log(con, "deposit", amount, cash(con), "added fake money")
    con.commit()
    return cash(con)


def reset(con, cfg) -> None:
    con.executescript("DROP TABLE IF EXISTS wallet; DROP TABLE IF EXISTS wallet_positions; DROP TABLE IF EXISTS wallet_log;")
    ensure(con, cfg)


# ------------------------------------------------------------------ live prices
def live_prices(symbols: list[str]) -> dict[str, tuple[float, datetime]]:
    """Latest price per symbol: {symbol: (price, time of that price in IST)}.
    Real-time from Angel One when set up, otherwise Yahoo (can be minutes late)."""
    from . import live
    return live.quotes(symbols)


def market_open(now: datetime | None = None) -> bool:
    now = now or _now()
    return now.weekday() < 5 and dtime(9, 15) <= now.time() < dtime(15, 30)


def is_fresh(quote, now: datetime | None = None) -> bool:
    """True if a quote is from today's session. On a market holiday the only prices are from an
    earlier day - trading on those would be fake, so callers wait instead."""
    if not quote:
        return False
    now = now or _now()
    ts = quote[1]
    return ts.date() == now.date() and ts.time() >= dtime(9, 15)


# ------------------------------------------------------------------ trading
def buy(con, cfg: dict, symbol: str, amount: float, side: str = "LONG", stop: float | None = None,
        target: float | None = None, price: float | None = None, allow_closed: bool = False,
        mode: str = "day", qty: int | None = None) -> dict:
    """Opens a paper position and LOCKS the money. Returns the new position.
    Give either `amount` (rupees to spend) or `qty` (number of shares).
    mode 'day' = day trade (stop/target, closed by 3:20 PM). mode 'hold' = strategy position: full
    price paid (no leverage), delivery charges, reviewed at each month end."""
    ensure(con, cfg)
    symbol = symbol.upper().strip().removesuffix(".NS")
    now = _now()
    if qty is not None and qty < 1 or qty is None and (not amount or amount <= 0):
        raise WalletError("Amount must be above 0")
    if not allow_closed:
        if not market_open(now) or (mode == "day" and now.time() >= dtime(15, 0)):
            # Like a broker's after-market order: lock the money now, buy at the next market open
            return queue_buy(con, cfg, symbol, amount, mode, stop, target, qty)
    if price is None:
        quote = live_prices([symbol]).get(symbol)
        if not quote:
            raise WalletError(f"Couldn't get a price for {symbol}. Check the NSE symbol (e.g. SBIN, RELIANCE, TCS).")
        if not allow_closed and not is_fresh(quote, now):
            # No price from today yet (market holiday, or the first minute after 9:15) - fill when one arrives
            return queue_buy(con, cfg, symbol, amount, mode, stop, target, qty)
        price = quote[0]
    lev = cfg["intraday"]["leverage"] if mode == "day" else 1
    if qty is not None:
        amount = qty * price / lev
    qty = int(round(amount * lev / price, 6))
    if qty < 1:
        raise WalletError(f"₹{amount:,.0f} can't buy even 1 share of {symbol} at ₹{price:,.2f}.")
    locked = round(qty * price / lev, 2)
    available = cash(con)
    if locked > available:
        raise WalletError(f"Not enough free cash: need ₹{locked:,.2f}, available ₹{available:,.2f}. "
                          "Exit a position or add fake money.")
    w = cfg["wallet"]
    sign = 1 if side == "LONG" else -1
    if mode == "day":
        stop = stop or round(price * (1 - sign * w["default_stop_pct"] / 100) / TICK) * TICK
        target = target or round(price * (1 + sign * w["default_target_pct"] / 100) / TICK) * TICK
    if stop and sign * (price - stop) <= 0 or target and sign * (target - price) <= 0:
        raise WalletError("Stop must be on the losing side and target on the winning side of the price.")
    cur = con.execute(
        "INSERT INTO wallet_positions (opened_at, symbol, side, qty, entry, locked, stop, target, status, mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
        (now.strftime("%Y-%m-%d %H:%M:%S"), symbol, side, qty, round(price, 2), locked,
         round(stop, 2) if stop else None, round(target, 2) if target else None, mode))
    con.execute("UPDATE wallet SET cash = cash - ? WHERE id=1", (locked,))
    _log(con, "lock", -locked, cash(con), f"#{cur.lastrowid} {side} {qty} {symbol} @ {price:.2f}")
    con.commit()
    return position(con, cur.lastrowid)


# ------------------------------------------------------------------ orders placed while the market is closed
def next_open(now: datetime | None = None) -> datetime:
    now = now or _now()
    d = now
    if now.time() >= dtime(9, 15):
        d = now + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.replace(hour=9, minute=15, second=0, microsecond=0)


def queue_buy(con, cfg, symbol, amount, mode="day", stop=None, target=None, qty=None) -> dict:
    """Locks the money now; fill_queued() buys at the first live price after the market opens.
    For a number of shares (qty), 5% extra is locked in case the price opens higher; unused money comes back."""
    quote = live_prices([symbol]).get(symbol)  # also checks the symbol exists
    if not quote:
        raise WalletError(f"Couldn't find {symbol}. Check the NSE symbol (e.g. SBIN, RELIANCE, TCS).")
    if qty:
        lev = cfg["intraday"]["leverage"] if mode == "day" else 1
        amount = round(qty * quote[0] * 1.05 / lev, 2)
    if amount > cash(con):
        raise WalletError(f"Not enough free cash: need ₹{amount:,.2f}, available ₹{cash(con):,.2f}.")
    if int(amount // (quote[0] * 1.05)) < 1:
        raise WalletError(f"₹{amount:,.0f} is too little for even 1 share of {symbol} (last ₹{quote[0]:,.2f}).")
    cur = con.execute(
        "INSERT INTO wallet_positions (opened_at, symbol, side, qty, entry, locked, stop, target, status, mode) "
        "VALUES (?, ?, 'LONG', ?, ?, ?, ?, ?, 'queued', ?)",  # qty = shares wanted (0 = as many as the money buys)
        (_now().strftime("%Y-%m-%d %H:%M:%S"), symbol, qty or 0, quote[0], round(amount, 2), stop, target, mode))
    con.execute("UPDATE wallet SET cash = cash - ? WHERE id=1", (amount,))
    _log(con, "lock", -amount, cash(con), f"#{cur.lastrowid} queued buy {symbol} for {amount:.2f}")
    con.commit()
    return position(con, cur.lastrowid)


def queued_orders(con) -> list[dict]:
    ids = [r[0] for r in con.execute("SELECT id FROM wallet_positions WHERE status='queued' ORDER BY id")]
    return [position(con, i) for i in ids]


def queue_sell(con, pid: int) -> dict:
    con.execute("UPDATE wallet_positions SET sell_queued=1 WHERE id=? AND status='open'", (pid,))
    con.commit()
    return position(con, pid)


def cancel_queued(con) -> list[dict]:
    """Cancels queued buys (money back) and queued sells."""
    out = []
    for q in queued_orders(con):
        con.execute("UPDATE wallet_positions SET status='cancelled' WHERE id=?", (q["id"],))
        con.execute("UPDATE wallet SET cash = cash + ? WHERE id=1", (q["locked"],))
        _log(con, "release", q["locked"], cash(con), f"#{q['id']} queued buy {q['symbol']} cancelled")
        out.append(q)
    for p in open_positions(con):
        if p.get("sell_queued"):
            con.execute("UPDATE wallet_positions SET sell_queued=0 WHERE id=?", (p["id"],))
            out.append(p)
    con.commit()
    return out


def fill_queued(con, cfg, prices: dict | None = None) -> list[tuple[str, dict]]:
    """At market open: execute queued buys and sells. Returns [("bought"/"sold"/"cancelled", position)]."""
    if not market_open():
        return []
    events = []
    todo_buy, todo_sell = queued_orders(con), [p for p in open_positions(con) if p.get("sell_queued")]
    if not (todo_buy or todo_sell):
        return events
    syms = [q["symbol"] for q in todo_buy + todo_sell]
    prices = {**(prices or {}), **live_prices(syms)}
    for q in todo_buy:
        quote = prices.get(q["symbol"])
        if not is_fresh(quote):  # holiday / no trade yet today: keep waiting
            continue
        price = quote[0]
        lev = cfg["intraday"]["leverage"] if q["mode"] == "day" else 1
        qty = int(q["locked"] * lev // price)
        if q["qty"]:
            qty = min(qty, q["qty"])
        if qty < 1:
            con.execute("UPDATE wallet_positions SET status='cancelled', reason='price too high at open' WHERE id=?", (q["id"],))
            con.execute("UPDATE wallet SET cash = cash + ? WHERE id=1", (q["locked"],))
            events.append(("cancelled", q))
            continue
        used = round(qty * price / lev, 2)
        w, stop, target = cfg["wallet"], q["stop"], q["target"]
        if q["mode"] == "day":
            stop = stop or round(price * (1 - w["default_stop_pct"] / 100) / TICK) * TICK
            target = target or round(price * (1 + w["default_target_pct"] / 100) / TICK) * TICK
        con.execute("UPDATE wallet_positions SET status='open', qty=?, entry=?, locked=?, stop=?, target=?, opened_at=? "
                    "WHERE id=?", (qty, round(price, 2), used, round(stop, 2) if stop else None,
                                   round(target, 2) if target else None, _now().strftime("%Y-%m-%d %H:%M:%S"), q["id"]))
        con.execute("UPDATE wallet SET cash = cash + ? WHERE id=1", (round(q["locked"] - used, 2),))  # unused money back
        _log(con, "fill", -used, cash(con), f"#{q['id']} bought {qty} {q['symbol']} @ {price:.2f} at open")
        events.append(("bought", position(con, q["id"])))
    con.commit()
    for p in todo_sell:
        quote = prices.get(p["symbol"])
        if is_fresh(quote):
            events.append(("sold", {**p, **sell(con, cfg, p["id"], quote[0], reason="sold at market open")}))
    return events


def position(con, pid: int) -> dict:
    cols = [c[1] for c in con.execute("PRAGMA table_info(wallet_positions)")]
    row = con.execute("SELECT * FROM wallet_positions WHERE id=?", (pid,)).fetchone()
    return dict(zip(cols, row)) if row else {}


def open_positions(con) -> list[dict]:
    ids = [r[0] for r in con.execute("SELECT id FROM wallet_positions WHERE status='open' ORDER BY id")]
    return [position(con, i) for i in ids]


def find_open(con, ref: str) -> dict:
    """Find an open position by its number (#3 / 3) or stock symbol."""
    ref = ref.strip().lstrip("#").upper()
    for p in open_positions(con):
        if str(p["id"]) == ref or p["symbol"] == ref:
            return p
    raise WalletError(f"No open position '{ref}'.")


def unrealised(p: dict, price: float, cfg: dict) -> float:
    """Profit/loss if you exited now at `price`, after all charges."""
    charges = cfg["charges"] if p.get("mode") == "hold" else cfg["charges_intraday"]  # delivery vs intraday
    return pnl(p["side"], p["entry"], price, p["qty"], charges)


def sell(con, cfg: dict, pid: int, price: float | None = None, reason: str = "you exited") -> dict:
    """Closes a position and RELEASES the money plus profit (or minus loss)."""
    p = position(con, pid)
    if not p or p["status"] != "open":
        raise WalletError("That position is not open.")
    if price is None:
        quote = live_prices([p["symbol"]]).get(p["symbol"])
        if not quote:
            raise WalletError(f"Couldn't get a live price for {p['symbol']} - try again in a moment.")
        price = quote[0]
    result = round(unrealised(p, price, cfg), 2)
    back = round(p["locked"] + result, 2)
    con.execute("UPDATE wallet_positions SET status='closed', closed_at=?, exit=?, reason=?, net_pnl=? WHERE id=?",
                (_now().strftime("%Y-%m-%d %H:%M:%S"), round(price, 2), reason, result, pid))
    con.execute("UPDATE wallet SET cash = cash + ? WHERE id=1", (back,))
    _log(con, "release", back, cash(con), f"#{pid} {p['symbol']} exit @ {price:.2f}: {result:+.2f}")
    con.commit()
    return position(con, pid)


# ------------------------------------------------------------------ when to get out
def exit_advice(p: dict, price: float, now: datetime, cfg: dict) -> list[tuple[str, str, str]]:
    """[(key, level, message)] - level: 'exit' (act now), 'warn', 'info'. Keys are used to alert only once."""
    w = cfg["wallet"]
    long = p["side"] == "LONG"
    gain = unrealised(p, price, cfg)
    out = []
    hold = p.get("mode") == "hold"  # multi-day position: no half-way or 3:15 square-off alerts
    if hold and not (p.get("stop") or p.get("target")):
        return out  # pure strategy position - reviewed at month end only
    if p.get("target") and ((long and price >= p["target"]) or (not long and price <= p["target"])):
        out.append(("target", "exit", f"🎯 {p['symbol']} reached your target ₹{p['target']:,.2f}. "
                                       f"EXIT now to book ₹{gain:+,.2f} after charges."))
    elif p.get("stop") and ((long and price <= p["stop"]) or (not long and price >= p["stop"])):
        out.append(("stop", "exit", f"🛑 {p['symbol']} hit your stop-loss ₹{p['stop']:,.2f}. "
                                     f"EXIT now - loss ₹{gain:+,.2f}. Don't hope, it can fall further."))
    elif not hold:
        half = p["entry"] + (p["target"] - p["entry"]) / 2
        if (long and price >= half) or (not long and price <= half):
            out.append(("halfway", "info", f"💡 {p['symbol']} is half-way to target (₹{gain:+,.2f}). Consider moving "
                                           f"your stop up to your buy price ₹{p['entry']:,.2f} so this can't become a loss."))
    sq = _t(cfg["intraday"]["square_off"])
    if hold:
        return out
    if now.time() >= sq:
        out.append(("sq_now", "exit", f"⏰ 3:15 PM - EXIT {p['symbol']} NOW. Day trades must be closed today "
                                      f"(currently ₹{gain:+,.2f})."))
    elif now.time() >= _t(w["square_off_warning"]):
        mins = (datetime.combine(now.date(), sq) - now.replace(tzinfo=None)).seconds // 60
        out.append(("sq_warn", "warn", f"⏳ {mins} min to square-off. Plan your exit from {p['symbol']} "
                                       f"(currently ₹{gain:+,.2f})."))
    return out


def new_alerts(con, p: dict, price: float, now: datetime, cfg: dict) -> list[tuple[str, str]]:
    """Advice not sent before for this position (marks it as sent). [(level, message)]"""
    sent = set(filter(None, (p.get("alerts_sent") or "").split(",")))
    fresh = [(k, lvl, msg) for k, lvl, msg in exit_advice(p, price, now, cfg) if k not in sent]
    if fresh:
        sent |= {k for k, _, _ in fresh}
        con.execute("UPDATE wallet_positions SET alerts_sent=? WHERE id=?", (",".join(sorted(sent)), p["id"]))
        con.commit()
    return [(lvl, msg) for _, lvl, msg in fresh]


def auto_square_off(con, cfg: dict, prices: dict) -> list[dict]:
    """Closes positions still open after auto_square_off time (or from a previous day)."""
    now = _now()
    closed = []
    for p in open_positions(con):
        if p.get("mode") == "hold":
            continue
        old_day = p["opened_at"][:10] < now.date().isoformat()
        if old_day or now.time() >= _t(cfg["wallet"]["auto_square_off"]):
            q = prices.get(p["symbol"]) or live_prices([p["symbol"]]).get(p["symbol"])
            if is_fresh(q, now):
                closed.append(sell(con, cfg, p["id"], q[0], reason="auto square-off (you didn't exit in time)"))
    return closed


# ------------------------------------------------------------------ summary
def summary(con, cfg: dict, prices: dict) -> dict:
    ensure(con, cfg)
    opens = open_positions(con)
    queued = queued_orders(con)
    locked = sum(p["locked"] for p in opens) + sum(q["locked"] for q in queued)
    open_pnl = sum(unrealised(p, prices[p["symbol"]][0], cfg) for p in opens if p["symbol"] in prices)
    dep = con.execute("SELECT deposited FROM wallet WHERE id=1").fetchone()[0]
    today = _now().date().isoformat()
    closed = pd.read_sql_query("SELECT * FROM wallet_positions WHERE status='closed'", con)
    today_closed = closed[closed["closed_at"].str.startswith(today)] if len(closed) else closed
    free = cash(con)
    return {
        "cash": free, "locked": locked, "open_pnl": open_pnl, "total": free + locked + open_pnl,
        "deposited": dep, "all_time_pnl": free + locked + open_pnl - dep,
        "today_pnl": float(today_closed["net_pnl"].sum()) if len(today_closed) else 0.0,
        "today_trades": len(today_closed), "closed": closed, "open": opens, "queued": queued,
        "win_rate": float((closed["net_pnl"] > 0).mean() * 100) if len(closed) else None,
    }
