"""Phase 5: the LIVE PAPER FUND. Paper money only - it never places a real order.

Every HORIZON trading days (~monthly) the evening job re-runs the quant engine and re-balances a paper
portfolio to its picks, using the portfolio recipe that won on the choosing years. Every evening it values the
fund at the closing prices against the Nifty.

Alpha decay check: at each rebalance the full ranking of every eligible stock is stored. A month later the
real returns are known, so the LIVE information coefficient (IC) can be compared with the backtest's. If live IC
stays near zero while the backtest said ~0.06, the edge has gone (or never existed).

Where the ranking runs: the quant engine needs more memory than the 1.8 GB laptop server has, so the PC ranks
(`python main.py push-plan`, scheduled weekly) and sends a small "plan" (today's ranking + how to build the
portfolio) to the server. The server only trades the plan; it never runs the engine itself.

Fills: at that evening's official NSE close, with every delivery charge. shortcut: a real investor would
buy at the next morning's price, so the fund is slightly flattered; switch to next-day fills if it matters.
"""
import json
import math
import subprocess
import sys
from datetime import date

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


PLAN_MAX_AGE_DAYS = 10  # a plan ranked more than this many days before the re-balance is too old to trade


def make_plan(r: dict) -> dict:
    """The small, JSON-safe part of a quant.research() result the fund needs (about 100 KB)."""
    num = lambda x: None if x is None or x != x else float(x)
    return {"at": pd.Timestamp.now().isoformat(timespec="seconds"), "as_of": r["as_of"], "picks": r["picks"],
            "latest": {s: float(v) for s, v in r["latest"].items()},
            "vol": {s: float(v) for s, v in r["vol_latest"].dropna().items()},
            "sectors": {s: r["sectors"][s] for s in r["latest"].index if s in r["sectors"]}, "spec": r["spec"],
            "backtest": {"ic": num((r["learned_test"] if r["use_learned"] else r["combo_test"])["ic"]),
                         "learning_mix": bool(r["use_learned"]), "variant": r["best"],
                         "test_cagr": num(r["port"]["test"].get("cagr")), "dsr": num(r["dsr"])}}


def rebalance(con, cfg: dict, plan: dict, day: str) -> dict:
    """Moves the paper fund to the plan's target. Returns what was sold and bought."""
    ch, held = cfg["charges"], _positions(con)
    px = _last_closes(con, day)
    sp = plan["spec"]
    latest = pd.Series(plan["latest"], dtype=float).sort_values(ascending=False)
    w = quant.target(latest, held, sp["n"], cfg["lab"]["hold_buffer"],
                     pd.Series(plan["vol"], dtype=float) if sp["calmer"] else None, plan["sectors"], sp["per_sector"])
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
                    [(day, s, float(v), px[s]) for s, v in latest.items() if s in px])
    set_state(con, "fund_cash", cash)
    set_state(con, "fund_rebalanced", day)
    set_state(con, "fund_backtest", json.dumps(plan["backtest"] | {"ranked": plan["as_of"]}))
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
    moved, waiting = None, None
    if due(con, day):
        plan = json.loads(get_state(con, "fund_plan") or "null")
        if plan and plan["as_of"] >= (pd.Timestamp(day) - pd.Timedelta(days=PLAN_MAX_AGE_DAYS)).date().isoformat():
            moved = rebalance(con, cfg, plan, day)
        else:
            waiting = plan["as_of"] if plan else "never"
    mark(con, cfg, day)
    s = summary(con, cfg)
    line = (f"📈 <b>Paper fund</b>: ₹{s['nav']:,.0f} ({s['fund_pct']:+.1f}%)"
            + (f" vs Nifty {s['nifty_pct']:+.1f}%" if s["nifty_pct"] is not None else "") + f" since {s['since']}")
    if s["live_ic"] is not None and s["backtest"]:
        line += f"\nLive IC {s['live_ic']:+.3f} over {s['ic_months']} month(s) vs backtest {s['backtest']['ic']:+.3f}"
    if waiting:
        line += (f"\n⏳ Re-balance is due, waiting for fresh picks from the PC (last ranked: {waiting}). "
                 "They come when the PC is on in the evening; or run push-plan.bat on the PC.")
    if moved:
        line = (f"🔄 <b>Paper fund rebalanced</b> ({s['backtest']['variant']})\n"
                f"Sold: {', '.join(moved['sold']) or 'none'}\nBought: {', '.join(moved['bought']) or 'none'}\n"
                f"Kept: {len(moved['kept'])}\n" + line)
    return line


def _ssh(cfg: dict, cmd: str, data: str | None = None) -> str:
    host = cfg["fund"]["server"]
    return subprocess.run(["ssh", "-o", "ConnectTimeout=20", host, f"cd stock-app && .venv/bin/python -m screener.fund {cmd}"],
                          input=data, capture_output=True, text=True, timeout=300, check=True).stdout.strip()


BACKUP_TABLES = ["agent_trades", "fund_positions", "fund_trades", "fund_nav", "fund_scores", "fund_ic", "wallet",
                 "wallet_positions", "wallet_log", "paper_trades", "intraday_paper", "app_state", "bot_state", "alerts_sent",
                 "advisor_chat", "fii_dii", "deals", "insider", "events"]  # can't be downloaded again; prices can


def backup(con, path) -> int:
    """Copies the tables that can't be re-downloaded into a small separate SQLite file."""
    import sqlite3
    from pathlib import Path
    Path(path).unlink(missing_ok=True)
    out, n = sqlite3.connect(path), 0
    have = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in BACKUP_TABLES:
        if t in have:
            df = pd.read_sql_query(f"SELECT * FROM {t}", con)
            df.to_sql(t, out, index=False)
            n += len(df)
    out.commit()
    out.close()
    return n


def health(con) -> list[str]:
    """Run ON THE SERVER: what looks wrong with the bot right now (empty list = all fine)."""
    from .intraday import IST
    now = pd.Timestamp.now(tz=IST)
    g = lambda k: (con.execute("SELECT value FROM bot_state WHERE key=?", (k,)).fetchone()
                   or con.execute("SELECT value FROM app_state WHERE key=?", (k,)).fetchone() or [None])[0]
    bad = []
    hb = g("heartbeat")
    if not hb or pd.Timestamp(hb) < now - pd.Timedelta(minutes=20):
        bad.append(f"the bot's main loop is not running (last sign of life: {hb or 'never'})")
    done = g("daily_done")
    if now.weekday() < 5 and now.hour >= 20 and (not done or done < (now - pd.Timedelta(days=4)).date().isoformat()):
        bad.append(f"no evening update since {done}")
    last_cycle = g("agents_last_cycle")
    if done == now.date().isoformat() and con.execute("SELECT 1 FROM prices WHERE symbol='^NSEI' AND date=?",
                                                      (done,)).fetchone():
        if not last_cycle or last_cycle[:10] != done:  # a trading day, and the agents never ran
            bad.append(f"the trading agents didn't run today (last run {last_cycle})")
    return bad


def check_server(cfg: dict) -> list[str]:
    """PC side: asks the server how it is; problems (or no answer) go to Telegram."""
    from . import telegram, ui
    try:
        report = json.loads(_ssh(cfg, "health"))
    except Exception as e:
        report = [f"the laptop server didn't answer ({type(e).__name__}): is it on and on Wi-Fi?"]
    if report:
        telegram.send("🚨 <b>Stock bot needs a look</b>\n" + "\n".join(f"• {tgfmt.esc(x)}" for x in report)
                      + "\n<i>Checked from your PC.</i>", rich=True)
        for x in report:
            ui.warn(x)
    else:
        ui.ok("Server health: all fine.")
    return report


def pull_backup(cfg: dict) -> None:
    """PC side: the server writes a fresh backup; the PC keeps a copy for each of the last 14 days."""
    from .config import ROOT
    from . import ui
    folder = ROOT / "data" / "server-backups"
    folder.mkdir(parents=True, exist_ok=True)
    rows = _ssh(cfg, "backup")
    target = folder / f"server-{date.today().isoformat()}.sqlite"
    subprocess.run(["scp", "-q", "-o", "ConnectTimeout=20", f"{cfg['fund']['server']}:stock-app/data/backup.sqlite",
                    str(target)], check=True, timeout=300)
    for old in sorted(folder.glob("server-*.sqlite"))[:-14]:
        old.unlink()
    ui.ok(f"Backup copied to this PC: {target.name} ({rows} rows)")


def push(cfg: dict, con, force: bool = False) -> None:
    """PC side, every evening: checks the server is healthy, copies its backup, and (weekly) brings the PC's data
    up to date, ranks every stock and runs the Strategy Lab on the full history, then sends both to the server.
    The ranking is skipped if the server's plan is less than 6 days old (unless force)."""
    from . import lab_ui, pipeline, pit, strategies, ui
    for step in (check_server, pull_backup):
        try:
            step(cfg)
        except Exception as e:  # one failed step must not stop the others
            ui.warn(f"{step.__name__}: {type(e).__name__}: {str(e)[:120]}")
    have = _ssh(cfg, "age")
    if not force and have != "none" and pd.Timestamp(have) >= pd.Timestamp.now() - pd.Timedelta(days=6):
        ui.ok(f"The server's plan is fresh (ranked {have}); nothing to do.")
        return
    pipeline.update_all(cfg, con)
    if pit.coverage(con)[1]:
        ui.info(f"    full history: {pit.fill(con, cfg['lab']['years'])} new day(s)")
    with ui.console.status("[cyan]Ranking every stock (a few minutes)..."):
        r = quant.research(con, cfg)
    plan = make_plan(r)
    with ui.console.status("[cyan]Strategy Lab on the full history (a few minutes)..."):
        lab_ui.save_champion(con, strategies.run_lab(con, cfg), cfg)
    plan["lab"] = {k: get_state(con, k) for k in ("lab_champion", "lab_line")}
    ui.ok(_ssh(cfg, "receive", json.dumps(plan)))
    ui.info(f"Top {len(plan['picks'])}: {', '.join(plan['picks'])}")


def _receive(con) -> str:
    plan = json.load(sys.stdin)
    set_state(con, "fund_plan", json.dumps(plan))
    set_state(con, "quant_picks", json.dumps({"at": plan["at"], "picks": plan["picks"]}))  # the Quant Bot uses these
    for k, v in (plan.get("lab") or {}).items():  # the PC's honest Strategy Lab result (full history)
        if v:
            set_state(con, k, v)
    return f"Server got the plan ranked {plan['as_of']} ({len(plan['latest'])} stocks)."


if __name__ == "__main__":  # run ON THE SERVER by push(): "age" or "receive" (plan JSON on stdin)
    from .config import load_config
    from .db import connect
    _con = connect(load_config()["data"]["db_path"])
    if sys.argv[1] == "age":
        _p = json.loads(get_state(_con, "fund_plan") or "null")
        print(_p["at"] if _p else "none")
    elif sys.argv[1] == "receive":
        print(_receive(_con))
    elif sys.argv[1] == "health":
        print(json.dumps(health(_con)))
    elif sys.argv[1] == "backup":
        from .config import ROOT
        print(backup(_con, ROOT / "data" / "backup.sqlite"))
