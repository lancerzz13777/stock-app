"""Phase 5: the LIVE PAPER FUND. Paper money only - it never places a real order.

Every HORIZON trading days (~monthly) the evening job re-runs the quant engine and re-balances a paper
portfolio to its picks, using the portfolio recipe that won on the choosing years. Every evening it values the
fund at the closing prices against the Nifty.

Alpha decay check: at each rebalance the full ranking of every eligible stock is stored. A month later the
real returns are known, so the LIVE information coefficient (IC) can be compared with the backtest's. If live IC
stays near zero while the backtest said ~0.06, the edge has gone (or never existed).

Fills: at that evening's official NSE close, with every delivery charge. shortcut: a real investor would
buy at the next morning's price, so the fund is slightly flattered; switch to next-day fills if it matters.
"""
import json
import math

import pandas as pd

from . import quant, tgfmt
from .charges import side_charges
from .db import get_state, set_state


def ensure(con):
    con.executescript("""
        CREATE TABLE IF NOT EXISTS fund_positions (symbol TEXT PRIMARY KEY, qty INTEGER, cost REAL);
        CREATE TABLE IF NOT EXISTS fund_trades (date TEXT, symbol TEXT, side TEXT, qty INTEGER, price REAL, charges REAL);
        CREATE TABLE IF NOT EXISTS fund_nav (date TEXT PRIMARY KEY, nav REAL, cash REAL, nifty REAL);
        CREATE TABLE IF NOT EXISTS fund_scores (date TEXT, symbol TEXT, score REAL, close REAL, PRIMARY KEY (date, symbol));
        CREATE TABLE IF NOT EXISTS fund_ic (date TEXT PRIMARY KEY, ic REAL, n INTEGER);
    """)


def _days(con) -> list[str]:
    return [d for (d,) in con.execute("SELECT date FROM prices WHERE symbol='^NSEI' ORDER BY date")]


def _last_closes(con, day: str, since: str | None = None) -> dict[str, float]:
    """Each stock's last close on or before `day` (a suspended or delisted stock keeps its last price)."""
    since = since or (pd.Timestamp(day) - pd.Timedelta(days=45)).date().isoformat()
    return {s: c for s, c, _ in con.execute(
        "SELECT symbol, close, MAX(date) FROM prices WHERE date BETWEEN ? AND ? AND close > 0 GROUP BY symbol",
        (since, day))}


def _cash(con, cfg) -> float:
    c = get_state(con, "fund_cash")
    return float(c) if c is not None else float(cfg["fund"]["capital"])


def _positions(con) -> dict[str, tuple[int, float]]:
    return {s: (q, c) for s, q, c in con.execute("SELECT symbol, qty, cost FROM fund_positions")}


def due(con, day: str) -> bool:
    last = get_state(con, "fund_rebalanced")
    days = _days(con)
    return last is None or (last in days and day in days and days.index(day) - days.index(last) >= quant.HORIZON)


def rebalance(con, cfg: dict, r: dict, day: str) -> dict:
    """Moves the paper fund to the quant engine's target. Returns what was sold and bought."""
    ch, held = cfg["charges"], _positions(con)
    px = _last_closes(con, day)
    sp = r["spec"]
    w = quant.target(r["latest"], held, sp["n"], cfg["lab"]["hold_buffer"],
                     r["vol_latest"] if sp["calmer"] else None, r["sectors"], sp["per_sector"])
    cash, sold, bought = _cash(con, cfg), [], []
    for s, (q, cost) in held.items():
        if s not in w.index:
            px.setdefault(s, cost)  # no price at all in a month: sold at what it cost (rare; flagged in the trade log)
            value = q * px[s]
            fee = side_charges(value, "sell", ch).total
            cash += value - fee
            con.execute("DELETE FROM fund_positions WHERE symbol=?", (s,))
            con.execute("INSERT INTO fund_trades VALUES (?,?,?,?,?,?)", (day, s, "SELL", q, px[s], fee))
            sold.append(s)
    nav = cash + sum(q * px[s] for s, (q, _) in held.items() if s in w.index)
    # shortcut: kept stocks are not resized (fewer charges, same as the backtest); new ones get their weight of NAV
    for s, wt in w.items():
        if s in held or s not in px:
            continue
        q = math.floor(min(nav * wt, cash) / (px[s] * 1.002))  # leave room for the buy charges
        if q <= 0:
            continue
        fee = side_charges(q * px[s], "buy", ch).total
        cash -= q * px[s] + fee
        con.execute("INSERT INTO fund_positions VALUES (?,?,?)", (s, q, px[s]))
        con.execute("INSERT INTO fund_trades VALUES (?,?,?,?,?,?)", (day, s, "BUY", q, px[s], fee))
        bought.append(s)
    con.executemany("INSERT OR REPLACE INTO fund_scores VALUES (?,?,?,?)",
                    [(day, s, float(v), px[s]) for s, v in r["latest"].items() if s in px])
    set_state(con, "fund_cash", cash)
    set_state(con, "fund_rebalanced", day)
    set_state(con, "fund_backtest", json.dumps({"ic": (r["learned_test"] if r["use_learned"] else r["combo_test"])["ic"],
                                                "learning_mix": r["use_learned"], "variant": r["best"],
                                                "test_cagr": r["port"]["test"].get("cagr"), "dsr": r["dsr"]}))
    con.commit()
    return {"sold": sold, "bought": bought, "kept": [s for s in held if s in w.index]}


def mark(con, cfg: dict, day: str) -> None:
    """Values the fund at the day's closes and checks the IC of every ranking that is now a month old."""
    days = [d for d in _days(con) if d <= day]
    px = _last_closes(con, day)
    nav = _cash(con, cfg) + sum(q * px.get(s, c) for s, (q, c) in _positions(con).items())
    nifty = con.execute("SELECT close FROM prices WHERE symbol='^NSEI' AND date=?", (day,)).fetchone()
    con.execute("INSERT OR REPLACE INTO fund_nav VALUES (?,?,?,?)", (day, nav, _cash(con, cfg), nifty and nifty[0]))
    for (d,) in con.execute("SELECT DISTINCT date FROM fund_scores WHERE date NOT IN (SELECT date FROM fund_ic)").fetchall():
        if d not in days or days.index(d) + quant.HORIZON >= len(days):
            continue  # its month isn't over yet
        later = _last_closes(con, days[days.index(d) + quant.HORIZON], d)
        df = pd.read_sql_query("SELECT symbol, score, close FROM fund_scores WHERE date=?", con, params=(d,))
        df["ret"] = df["symbol"].map(later) / df["close"] - 1
        df = df.dropna()
        if len(df) >= quant.MIN_STOCKS:
            con.execute("INSERT OR REPLACE INTO fund_ic VALUES (?,?,?)",
                        (d, df["score"].rank().corr(df["ret"].rank()), len(df)))
    con.commit()


def summary(con, cfg: dict) -> dict:
    ensure(con)
    nav = pd.read_sql_query("SELECT date, nav, nifty FROM fund_nav ORDER BY date", con)
    start = float(cfg["fund"]["capital"])
    days = _days(con)
    px = _last_closes(con, days[-1]) if days else {}
    hold = [{"symbol": s, "qty": q, "cost": c, "price": px.get(s, c), "pnl_pct": (px.get(s, c) / c - 1) * 100}
            for s, (q, c) in sorted(_positions(con).items())]
    ics = pd.read_sql_query("SELECT date, ic, n FROM fund_ic ORDER BY date", con)
    st = quant.ic_stats(ics["ic"]) if len(ics) else {"ic": None, "t": None, "n": 0}
    nifty0 = nav["nifty"].dropna().iloc[0] if nav["nifty"].notna().any() else None
    return {
        "start": start, "nav": nav["nav"].iloc[-1] if len(nav) else start,
        "fund_pct": (nav["nav"].iloc[-1] / start - 1) * 100 if len(nav) else 0.0,
        "nifty_pct": (nav["nifty"].dropna().iloc[-1] / nifty0 - 1) * 100 if nifty0 else None,
        "since": nav["date"].iloc[0] if len(nav) else None, "rebalanced": get_state(con, "fund_rebalanced"),
        "curve": [{"date": d, "fund": v / start * 100, "nifty": (n / nifty0 * 100) if nifty0 and n else None}
                  for d, v, n in nav.itertuples(index=False)],
        "holdings": hold, "cash": _cash(con, cfg),
        "live_ic": st["ic"], "live_t": st["t"], "ic_months": st["n"], "ic_history": ics.to_dict("records"),
        "backtest": json.loads(get_state(con, "fund_backtest") or "null"),
    }


def evening(con, cfg: dict, day: str) -> str | None:
    """Called by the bot after the evening price update. Returns a Telegram message, or None."""
    if not cfg.get("fund", {}).get("enabled"):
        return None
    ensure(con)
    moved = None
    if due(con, day):
        r = quant.research(con, cfg)  # ~2 minutes on the laptop, once a month
        moved = rebalance(con, cfg, r, day)
        set_state(con, "quant_picks", json.dumps({"at": pd.Timestamp.now().isoformat(), "picks": r["picks"]}))
    mark(con, cfg, day)
    s = summary(con, cfg)
    line = (f"📈 <b>Paper fund</b>: ₹{s['nav']:,.0f} ({s['fund_pct']:+.1f}%)"
            + (f" vs Nifty {s['nifty_pct']:+.1f}%" if s["nifty_pct"] is not None else "") + f" since {s['since']}")
    if s["live_ic"] is not None and s["backtest"]:
        line += f"\nLive IC {s['live_ic']:+.3f} over {s['ic_months']} month(s) vs backtest {s['backtest']['ic']:+.3f}"
    if moved:
        line = (f"🔄 <b>Paper fund rebalanced</b> ({s['backtest']['variant']})\n"
                f"Sold: {', '.join(moved['sold']) or 'none'}\nBought: {', '.join(moved['bought']) or 'none'}\n"
                f"Kept: {len(moved['kept'])}\n" + line)
    return line
