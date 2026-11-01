"""Personal NSE stock screener. Double-click run.bat (or run `python main.py`) for the menu.

Command line (same features as the menu):
    python main.py dt-watch | dt-scan | dt-backtest | dt-paper          day trading
    python main.py daily | plan | scan | backtest | paper               swing (multi-day)
    python main.py dips | news | update | run | telegram-test
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
from rich.live import Live
from rich.prompt import Prompt

from screener import backtest, desk, dips, intraday, lab_ui, live, news, paper, pipeline, telegram, ui
from screener.bot import IST, Bot, market_is_open
from screener.config import ROOT, load_config
from screener.db import connect, get_state, set_state
from screener.planner import plan_budget
from screener.ui import Group, Panel, Table, Text, box, console


# ====================================================================== helpers
def ask_budget(default: float, what: str = "investing") -> float:
    while True:
        try:
            raw = Prompt.ask(f"[bold]How much are you {what} this time?[/] ₹", default=f"{default:g}", console=console)
        except EOFError:
            return float(default)
        try:
            value = float(raw.strip().lstrip("﻿").replace(",", "").replace("₹", ""))
            if value > 0:
                return value
        except ValueError:
            pass
        ui.warn("Please type an amount, e.g. 1500")


def ask_yes(question: str, default: bool = True) -> bool:
    try:
        raw = Prompt.ask(f"{question} (y/n)", default="y" if default else "n", console=console)
    except EOFError:
        return default
    return raw.strip().lstrip("\ufeff").lower().startswith("y")


def now_ist() -> datetime:
    return datetime.now(IST)


# ====================================================================== status + menu
def status_panel(cfg, con) -> Panel:
    last = con.execute("SELECT MAX(date) FROM prices WHERE symbol='^NSEI'").fetchone()[0] or "no data yet"
    n_stocks = con.execute("SELECT COUNT(DISTINCT symbol) FROM prices").fetchone()[0]
    n_fund = con.execute("SELECT COUNT(*) FROM fundamentals WHERE market_cap IS NOT NULL").fetchone()[0]
    now = now_ist()
    mkt = Text("● MARKET OPEN", style=f"bold {ui.GREEN}") if market_is_open(now) else Text("● market closed", style=ui.DIM)
    tg = Text("Telegram ✔", style=ui.GREEN) if telegram.configured() else Text("Telegram not set up", style=ui.YELLOW)
    data_src = Text("Live: Angel One ✔", style=ui.GREEN) if live.angel_configured() else \
        Text("Live: Yahoo, delayed (L)", style=ui.YELLOW)

    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(ratio=3)
    grid.add_column(ratio=1, justify="right")
    grid.add_row(Text.assemble(mkt, f"   {now:%a %d %b, %H:%M} IST"), tg)
    grid.add_row(Text(f"Prices up to {last} · {n_stocks:,} stocks · fundamentals for {n_fund:,}", style=ui.DIM), data_src)
    for key, label in (("lab_line", "Strategy Lab champion"), ("dt_bt", "Day-trade backtest"),
                       ("swing_bt", "Old swing rules")):
        s = get_state(con, key)
        if s:
            s = json.loads(s)
            grid.add_row(Text.assemble((f"{label}: ", ui.DIM), (s["line"], ui.GREEN if s["good"] else ui.RED)), "")
        else:
            grid.add_row(Text(f"{label}: not run yet", style=ui.DIM), "")
    return Panel(grid, title="[bold cyan]📈 NSE Stock Screener[/]", subtitle="[grey62]personal use · free data[/]",
                 box=box.DOUBLE, border_style="cyan")


MENU = [
    ("STRATEGIES  (famous investors' rules, tested on 10 years of NSE data)", [
        ("Y", "Strategy Lab", "test Momentum, Minervini, O'Neil, Darvas, Low-vol, Dual momentum"),
        ("S", "Strategy picks for my budget", "what the winning strategy selects today"),
        ("G", "Check a stock", "Graham, Buffett, Lynch, QGLP, CAN SLIM checklists"),
    ]),
    ("PRACTICE  (paper wallet: fake money, real live prices)", [
        ("T", "Trading desk", "buy → money locked → live P&L → exit alerts → exit"),
    ]),
    ("DAY TRADING  (buy & sell the same day)", [
        ("1", "Live day-trade scanner", "auto-refreshes every 5 min during market hours"),
        ("2", "Day-trade scan once", "now, or a review of today's session after 3:30 PM"),
        ("3", "Day-trade backtest", "last ~60 days of 5-minute candles"),
        ("4", "Day-trade paper results", "how the logged signals actually did"),
    ]),
    ("OLD SWING RULES  (kept for comparison)", [
        ("5", "Old swing plan (download first)", "these rules LOST to the Nifty - see S instead"),
        ("6", "Old swing plan (quick)", ""),
        ("7", "Ranked list of all stocks", ""),
        ("8", "Swing backtest", "10 years vs Nifty"),
        ("9", "Swing paper results", ""),
    ]),
    ("MARKET", [
        ("10", "Dips / crash check", "quality stocks that fell 5%+"),
        ("11", "Latest stock news", ""),
    ]),
    ("SETUP", [
        ("12", "Update all data", "prices, fundamentals, news"),
        ("13", "Start Telegram bot", "always-on alerts; keep the window open"),
        ("14", "Telegram setup / test", ""),
        ("15", "Open settings file", "config.yaml"),
        ("L", "Live data setup", "Angel One real-time prices (free with an account)"),
        ("A", "Chat advisor setup", "free Google Gemini key - ask the bot anything"),
        ("0", "Exit", ""),
    ]),
]


def menu_panel() -> Group:
    parts = []
    for heading, items in MENU:
        t = Table.grid(padding=(0, 2))
        t.add_column(justify="right", style="bold cyan", width=4)
        t.add_column(style="bold")
        t.add_column(style=ui.DIM)
        for key, name, hint in items:
            t.add_row(key, name, hint)
        parts.append(Text(heading, style="bold yellow"))
        parts.append(t)
        parts.append(Text(""))
    return Group(*parts)


def menu(cfg, con):
    actions = {
        "t": lambda: desk.run(cfg, con), "l": live_setup, "a": lambda: advisor_setup(cfg, con),
        "y": lambda: lab_ui.lab(cfg, con), "s": lambda: lab_ui.picks(cfg, con), "g": lambda: lab_ui.check_stock(cfg, con),
        "1": lambda: dt_watch(cfg, con), "2": lambda: dt_scan(cfg, con), "3": lambda: dt_backtest(cfg, con),
        "4": lambda: dt_paper(cfg, con), "5": lambda: swing_daily(cfg, con), "6": lambda: swing_plan(cfg, con),
        "7": lambda: swing_scan(cfg, con), "8": lambda: swing_backtest(cfg, con), "9": lambda: swing_paper(cfg, con),
        "10": lambda: show_dips(cfg, con), "11": lambda: show_news(cfg, con), "12": lambda: update(cfg, con),
        "13": lambda: Bot(cfg, con).run(), "14": telegram_test, "15": lambda: os.startfile(ROOT / "config.yaml"),
    }
    while True:
        console.clear()
        console.print(status_panel(cfg, con))
        console.print(menu_panel())
        try:
            choice = Prompt.ask("[bold]Choose a number[/]", console=console).strip().lstrip("﻿")
        except EOFError:
            return
        choice = choice.lower()
        if choice == "0":
            return
        if choice not in actions:
            continue
        try:
            actions[choice]()
        except KeyboardInterrupt:
            ui.warn("Stopped.")
        except SystemExit as e:
            ui.error(str(e))
        except Exception as e:  # keep the menu alive whatever happens
            ui.error(f"{type(e).__name__}: {e}")
        if choice not in ("15", "t"):
            Prompt.ask(f"\n[{ui.DIM}]Press Enter to go back to the menu[/]", default="", show_default=False,
                       console=console)


# ====================================================================== day trading
def render_dt_signal(sig: dict, logged: bool) -> Panel:
    buy = sig["direction"] == "LONG"
    action = "BUY" if buy else "SELL (short)"
    g = Table.grid(padding=(0, 2))
    g.add_column(style=ui.DIM)
    g.add_column()
    g.add_row("Action", Text(f"{action} {sig['qty']} share(s) at ~{ui.rs(sig['entry'])}",
                             style=f"bold {ui.GREEN if buy else ui.RED}"))
    g.add_row("Target", Text.assemble(f"{ui.rs(sig['target'])} → ", (ui.rs(sig["profit"], True), f"bold {ui.GREEN}"),
                                      " after charges"))
    g.add_row("Stop-loss", Text.assemble(f"{ui.rs(sig['stop'])} → ", (ui.rs(-sig["loss"], True), f"bold {ui.RED}"),
                                         " incl. charges"))
    g.add_row("Exit by", "3:15 PM at the latest — never hold a day trade overnight")
    if logged:
        g.add_row("", Text("logged as a paper trade · to practise it with your wallet: menu → T → B", style=ui.DIM))
    return Panel(g, title=f"[bold]{sig['symbol']}[/]  breakout at {sig['signal_time']}", border_style=ui.GREEN if buy else ui.RED,
                 box=box.ROUNDED)


def dt_dashboard(res: dict, alerts: list, next_scan: datetime, cfg, trades_done: int) -> Group:
    delay = res["delay_min"]
    if delay is None:
        fresh = Text("no candles yet today", style=ui.YELLOW)
    else:
        style = ui.GREEN if delay <= 6 else ui.YELLOW if delay <= 15 else f"bold {ui.RED}"
        fresh = Text(f"data as of {res['data_time']:%H:%M} ({delay:.0f} min old)"
                     + ("  STALE — prices may have moved" if delay > 15 else ""), style=style)
    left = max(0, int((next_scan - now_ist()).total_seconds()))
    head = Table.grid(expand=True)
    head.add_column()
    head.add_column(justify="right")
    head.add_row(Text.assemble(("LIVE DAY-TRADE SCANNER  ", "bold cyan"), fresh),
                 Text(f"{now_ist():%H:%M:%S} · next scan {left // 60}:{left % 60:02d} · "
                      f"trades today {trades_done}/{cfg['intraday']['max_trades_per_day']}", style=ui.DIM))

    st = sorted(res["status"], key=lambda r: -(r["vs_range_pct"] if r["vs_range_pct"] == r["vs_range_pct"] else -99))[:12]
    movers = ui.table([("Stock", "left"), ("Price", "right"), ("Day", "right"), ("vs 9:30 high", "right"), ("VWAP", "center")],
                      [[r["symbol"], ui.rs(r["price"]), ui.pct(r["chg_pct"]), ui.pct(r["vs_range_pct"]),
                        Text("above", style=ui.GREEN) if r["above_vwap"] else Text("below", style=ui.RED)] for r in st],
                      title_=f"Closest to a breakout (of {res['universe']} liquid stocks)")
    parts = [Panel(head, box=box.HEAVY, border_style="cyan"), movers]
    now = now_ist()
    sq = datetime.combine(now.date(), intraday._t(cfg["intraday"]["square_off"]), tzinfo=IST)
    if now >= sq - timedelta(minutes=5):
        parts.append(Panel("[bold]SQUARE-OFF TIME — close every day trade now.[/]", border_style=ui.RED, box=box.HEAVY))
    if alerts:
        parts.append(Text("Signals today", style="bold yellow"))
        parts += alerts[-4:]
    else:
        parts.append(Text("No breakout signals yet today. Waiting is part of the strategy.", style=ui.DIM))
    parts.append(Text("Ctrl+C to stop", style=ui.DIM))
    return Group(*parts)


def handle_new_signals(cfg, con, res, seen: set, panels: list) -> int:
    day = now_ist().date().isoformat()
    for sig in res["signals"]:
        key = f"{day}:{sig['symbol']}"
        if key in seen:
            continue
        seen.add(key)
        logged = False
        if intraday.trades_today(con, day) < cfg["intraday"]["max_trades_per_day"]:
            logged = intraday.paper_log(con, sig, day)
        panels.append(render_dt_signal(sig, logged))
        if logged:
            telegram.send(f"⚡ DAY TRADE {('BUY' if sig['direction'] == 'LONG' else 'SHORT')} {sig['symbol']} "
                          f"{sig['qty']} @ ~Rs {sig['entry']}\nTarget {sig['target']} (+Rs {sig['profit']:.2f})  "
                          f"Stop {sig['stop']} (-Rs {sig['loss']:.2f})\nExit by 3:15 PM. Data may be delayed.")
    return intraday.trades_today(con, day)


def dt_watch(cfg, con):
    if not market_is_open(now_ist()):
        ui.title("Live day-trade scanner")
        console.print(Panel("The market is closed. NSE trades [bold]Mon–Fri, 9:15 AM – 3:30 PM IST[/].\n"
                            "Start this a little before 9:30 AM (the first signals can appear right after).\n"
                            "For now, try option 2 to review today's session, or 3 for the backtest.",
                            border_style=ui.YELLOW))
        return
    budget = ask_budget(cfg["intraday"]["budget"], "day trading with")
    cfg["intraday"]["budget"] = budget
    seen, panels = set(), []
    every = timedelta(minutes=cfg["intraday"]["scan_minutes"])
    with console.status("[cyan]First scan of the liquid stocks..."):
        res = intraday.live_scan(con, cfg, budget)
    done = handle_new_signals(cfg, con, res, seen, panels)
    next_scan = now_ist() + every
    with Live(dt_dashboard(res, panels, next_scan, cfg, done), console=console, refresh_per_second=1,
              screen=False) as live:
        while market_is_open(now_ist()):
            if now_ist() >= next_scan:
                res = intraday.live_scan(con, cfg, budget)
                done = handle_new_signals(cfg, con, res, seen, panels)
                next_scan = now_ist() + every
            live.update(dt_dashboard(res, panels, next_scan, cfg, done))
            time.sleep(1)
    ui.info("Market closed - scanner stopped. Option 4 shows how today's paper trades ended (after 3:30 PM).")


def dt_scan(cfg, con):
    budget = ask_budget(cfg["intraday"]["budget"], "day trading with")
    cfg["intraday"]["budget"] = budget
    if market_is_open(now_ist()):
        ui.title("Day-trade scan", "live")
        with console.status("[cyan]Scanning liquid stocks..."):
            res = intraday.live_scan(con, cfg, budget)
        panels = []
        handle_new_signals(cfg, con, res, set(), panels)
        console.print(dt_dashboard(res, panels, now_ist(), cfg, intraday.trades_today(con, now_ist().date().isoformat())))
        return
    ui.title("Day-trade session review", "what the strategy would have done in the latest session")
    with console.status("[cyan]Downloading the latest 5-minute candles..."):
        syms = intraday.liquid_universe(con, cfg)
        data = live.candles(syms, cfg, days=5)
        intraday.save_bars(con, data)
    rows, day = [], None
    for s, df in data.items():
        df = intraday.add_columns(df)
        day = day or df.index[-1].date()
        last_day = df[df.index.date == df.index[-1].date()]
        t = intraday.trade_for_day(last_day, budget, cfg)
        if t:
            rows.append({"symbol": s, **t})
    rows.sort(key=lambda r: r["signal_time"])
    if not rows:
        ui.info("No breakout signals in the latest session.")
        return
    taken = rows[:cfg["intraday"]["max_trades_per_day"]]
    console.print(ui.table(
        [("Signal", "left"), ("Stock", "left"), ("Side", "left"), ("Entry", "right"), ("Qty", "right"),
         ("Stop", "right"), ("Target", "right"), ("Exit", "right"), ("How", "left"), ("Result", "right")],
        [[r["signal_time"], r["symbol"] + (" ★" if r in taken else ""), r["direction"], ui.rs(r["entry"]), r["qty"],
          ui.rs(r["stop"]), ui.rs(r["target"]), f"{ui.rs(r['exit'])} @{r['exit_time']}", r["reason"],
          ui.money(r["net_pnl"])] for r in rows],
        title_=f"All breakouts on {day} (₹{budget:,.0f} budget, leverage {cfg['intraday']['leverage']}x)",
        caption=f"★ = the first {len(taken)} (max per day) - the ones you'd actually have taken"))
    total = sum(r["net_pnl"] for r in taken)
    console.print(Text.assemble("Taking the first trades only: ", ui.money(total), " after charges"))


def dt_backtest(cfg, con):
    ic = cfg["intraday"]
    ui.title("Day-trade backtest", f"Opening Range Breakout · {ic['universe_size']} most liquid stocks · "
                                   f"₹{ic['budget']:,} per trade · leverage {ic['leverage']}x")
    with console.status("[cyan]Downloading ~60 days of 5-minute candles (1-3 min)..."):
        res = intraday.backtest(con, cfg)
    if res["trades"].empty:
        ui.warn("No trades found.")
        return
    s = res["summary"]
    t = ui.table([("", "left"), ("", "right")], [
        ["Period", f"{s['period']}  ({s['market_days']} trading days)"],
        ["Trades", f"{s['trades']}  (max {ic['max_trades_per_day']}/day)"],
        ["Win rate", f"{s['win_rate_pct']}%"],
        ["Average win / loss", Text.assemble(ui.money(s["avg_win"]), " / ", ui.money(s["avg_loss"]))],
        ["Average per trade (after charges)", ui.money(s["avg_per_trade"])],
        ["TOTAL after charges", ui.money(s["total_pnl"])],
        ["Return on budget", ui.pct(s["return_on_budget_pct"])],
        ["Nifty in same period", ui.pct(s.get("nifty_return_pct", float("nan")))],
        ["Green days", f"{s['green_days_pct']}%"],
        ["Best / worst day", Text.assemble(ui.money(s["best_day"]), " / ", ui.money(s["worst_day"]))],
        ["Worst losing streak (drawdown)", ui.money(s["max_drawdown"])],
        ["How trades ended", ", ".join(f"{k} {v}" for k, v in s["exit_reasons"].items())],
    ])
    console.print(t)

    daily = res["daily"].tail(20)
    scale = max(1.0, daily.abs().max())
    bars = ui.table([("Day", "left"), ("P&L", "right"), ("", "left")],
                    [[d, ui.money(v), Text("█" * max(1, int(abs(v) / scale * 25)), style=ui.GREEN if v > 0 else ui.RED)]
                     for d, v in daily.items()], title_="Last 20 trading days")
    console.print(bars)
    good = intraday.save_result(con, s)["good"]
    console.print(ui.verdict_panel(intraday.verdict(s), good))
    path = pipeline.save_csv(res["trades"], "daytrade_backtest", index=False)
    ui.info(f"Every trade saved to {path}")
    if ic["leverage"] == 1:
        ui.info("With leverage 1 each trade risks ~₹5-15, so profits are small too. Raising 'leverage' in "
                "config.yaml multiplies profits AND losses - only after months of paper trading.")


def dt_paper(cfg, con):
    ui.title("Day-trade paper trading")
    with console.status("[cyan]Checking finished sessions..."):
        events = intraday.paper_resolve(con, cfg)
    for e in events:
        ui.ok(e)
    df = intraday.paper_trades(con)
    if df.empty:
        console.print(Panel("No day-trade paper trades yet.\nRun option 1 (live scanner) during market hours, or start "
                            "the Telegram bot (13) - every signal is logged with fake money automatically.",
                            border_style=ui.YELLOW))
        return
    closed = df[df.status == "closed"]
    console.print(ui.table(
        [("Day", "left"), ("Stock", "left"), ("Side", "left"), ("In", "right"), ("Qty", "right"), ("Out", "right"),
         ("How", "left"), ("Result", "right"), ("Status", "left")],
        [[r.day, r.symbol, r.direction, f"{ui.rs(r.entry)} @{r.entry_time}" if r.entry == r.entry else "-",
          r.qty if r.qty == r.qty else "-", f"{ui.rs(r.exit)} @{r.exit_time}" if r.exit == r.exit else "-",
          r.reason or "-", ui.money(r.net_pnl) if r.net_pnl == r.net_pnl else Text("-"), r.status]
         for r in df.tail(25).itertuples()], title_="Latest paper trades (fake money, real prices)"))
    if len(closed):
        days = closed.day.nunique()
        total = closed.net_pnl.sum()
        console.print(Text.assemble(f"Closed {len(closed)} trades over {days} days · win rate "
                                    f"{(closed.net_pnl > 0).mean() * 100:.0f}% · total ", ui.money(total)))
        first = pd.to_datetime(df.day.min())
        proven = (datetime.now() - first).days >= 60 and len(closed) >= 30 and total > 0
        console.print(ui.verdict_panel(
            ["Positive over 60+ days and 30+ trades. If you go live, start with the smallest size and keep every "
             "stop-loss."] if proven else
            ["NOT proven yet: needs 60+ days, 30+ closed trades and a positive total before real money."],
            proven, "READY?"))


# ====================================================================== swing
def update(cfg, con, limit=None):
    ui.title("Updating data")
    pipeline.update_all(cfg, con, limit)
    ui.ok("Data is up to date.")


def swing_scan(cfg, con) -> pd.DataFrame:
    ranked = pipeline.build_ranked(cfg, con)
    n = cfg["scoring"]["top_n_report"]
    ui.title("Ranked stocks", f"prices as of {ranked['last_date'].max()}")
    console.print(ui.table(
        [("#", "right"), ("Stock", "left"), ("Score", "right"), ("Price", "right"), ("RSI", "right"),
         ("P/E", "right"), ("Sector", "left"), ("Why", "left")],
        [[i, s, ui.score_text(r.score), ui.rs(r.close), f"{r.rsi:.0f}",
          f"{r.pe:.1f}" if r.pe == r.pe else "-", r.sector, Text(r.reasons, style=ui.DIM)]
         for i, (s, r) in enumerate(ranked.head(n).iterrows(), 1)]))
    ui.info(f"Full list saved to {pipeline.save_csv(ranked, 'ranked', index_label='symbol')}")
    return ranked


def swing_plan(cfg, con, budget=None, ranked=None):
    ranked = pipeline.build_ranked(cfg, con, quiet=True) if ranked is None else ranked
    budget = budget or ask_budget(cfg["alerts"]["default_budget"])
    plans, notes = plan_budget(ranked, budget, cfg)
    ui.title(f"Swing plan for ₹{budget:,.0f}", f"prices as of {ranked['last_date'].max()}")
    s = get_state(con, "swing_bt")
    if s and not json.loads(s)["good"]:
        ui.warn("The swing strategy LOST to the Nifty in its 10-year backtest (option 8). "
                "Treat these as paper-trading ideas only.")
    for n in notes:
        ui.info(n)
    for i, p in enumerate(plans, 1):
        console.print(ui.trade_card(i, p))
    if plans:
        console.print(Text.assemble("If all targets hit: ", ui.money(sum(p.net_profit for p in plans)),
                                    "    If all stops hit: ", ui.money(-sum(p.max_loss for p in plans))))
        pipeline.save_csv(pd.DataFrame([p.__dict__ for p in plans]), "plan", index=False)
        if sys.stdin.isatty() and ask_yes("Log these as PAPER trades (fake money)?"):
            ui.ok(f"Logged {paper.log_plans(con, plans)} paper trade(s).")
    ui.disclaimer()


def swing_daily(cfg, con, budget=None):
    update(cfg, con)
    for e in paper.update(con, cfg):
        ui.ok(e)
    swing_plan(cfg, con, budget, pipeline.build_ranked(cfg, con))


def swing_backtest(cfg, con):
    bt = cfg["backtest"]
    ui.title("Swing backtest", f"{bt['years']} years · ₹{bt['capital']:,} capital · ₹{bt['trade_budget']:,} per trade · "
                               "technical rules only")
    sectors = dict(con.execute("SELECT symbol, sector FROM fundamentals").fetchall())
    with console.status("[cyan]Replaying 10 years of trading (about a minute)..."):
        res = backtest.run_backtest(con, cfg, sectors)
    s = res["summary"]
    console.print(ui.table(
        [("", "left"), ("Strategy", "right"), ("Nifty buy & hold", "right")],
        [["Final value", ui.rs(s["final_equity"]), ui.rs(s["nifty_final"])],
         ["Total return", ui.pct(s["total_return_pct"]), ui.pct(s["nifty_return_pct"])],
         ["Per year (CAGR)", ui.pct(s["cagr_pct"]), ui.pct(s["nifty_cagr_pct"])],
         ["Worst fall", ui.pct(s["max_drawdown_pct"]), ui.pct(s["nifty_max_drawdown_pct"])]],
        title_=f"{s['period']} · {s['trades']} trades · {s.get('win_rate_pct', 0)}% wins · "
               f"avg ₹{s.get('avg_per_trade', 0):+.2f}/trade"))
    y = res["yearly"]
    console.print(ui.table([("Year", "left"), ("Strategy", "right"), ("Nifty", "right")],
                           [[str(i), ui.pct(r["strategy_%"]), ui.pct(r["nifty_%"])] for i, r in y.iterrows()]))
    good = bool(s["cagr_pct"] > s["nifty_cagr_pct"] and s["trades"] >= 30)
    console.print(ui.verdict_panel(backtest.verdict(s), good))
    set_state(con, "swing_bt", json.dumps({"good": good, "line": (
        f"{s['cagr_pct']:+.1f}%/yr vs Nifty {s['nifty_cagr_pct']:+.1f}%/yr ({datetime.now():%d %b})")}))
    ui.info("Caveats: survivorship bias, price-index Nifty, fundamentals/news not tested. "
            f"Trades saved to {pipeline.save_csv(res['trades'], 'backtest_trades', index=False)}")


def swing_paper(cfg, con):
    ui.title("Swing paper trading")
    for e in paper.update(con, cfg):
        ui.ok(e)
    console.print(Panel(paper.report(con, cfg), box=box.ROUNDED, border_style="cyan"))


# ====================================================================== market
def show_dips(cfg, con):
    live = market_is_open(now_ist())
    ui.title("Dips & crashes", "live (delayed)" if live else "last close")
    with console.status("[cyan]Checking quality stocks..."):
        alerts, market = dips.find_dips(con, cfg, live=live)
    if "note" in market:
        ui.warn(market["note"])
        return
    console.print(Text.assemble("Nifty ", ui.pct(market["nifty_pct"]), f" · {market['breadth_pct']:.0f}% of quality "
                                "stocks fell sharply", (" · MARKET-WIDE SELL-OFF", f"bold {ui.RED}") if market["market_wide"] else ""))
    if not alerts:
        ui.ok("No quality stock had a sudden fall.")
        return
    console.print(ui.table(
        [("", "left"), ("Stock", "left"), ("Fall", "right"), ("Price", "right"), ("Why", "left")],
        [[Text("OPPORTUNITY", style=f"bold {ui.GREEN}") if a["kind"] == "OPPORTUNITY" else Text("WARNING", style=f"bold {ui.YELLOW}"),
          a["symbol"], Text(f"-{a['fall_pct']}%", style=ui.RED), ui.rs(a["price"]), a["why"]] for a in alerts]))
    ui.info("A dip is a chance to LOOK, not a signal to buy. Falling stocks can keep falling.")


def show_news(cfg, con):
    ui.title("Stock news", "free RSS feeds · keyword tagging (rough)")
    with console.status("[cyan]Reading news feeds..."):
        n = news.fetch_news(con, cfg)
    ui.info(f"{n} new items")
    items = news.recent_items(con, days=2)[:40]
    style = {"positive": ui.GREEN, "negative": ui.RED, "neutral": ui.DIM}
    console.print(ui.table([("", "center"), ("Stocks", "left"), ("Headline", "left"), ("Source", "left")],
                           [[Text({"positive": "▲", "negative": "▼", "neutral": "•"}[i["sentiment"]], style=style[i["sentiment"]]),
                             i["symbols"][:30], i["title"][:110], Text(i["source"], style=ui.DIM)] for i in items]))


def live_setup():
    ui.title("Live data setup", "Angel One SmartAPI - real-time NSE prices, free with an Angel One account")
    console.print(Panel(
        "[bold]1. Open an Angel One account[/] (free; PAN + Aadhaar + bank details) at angelone.in or in their app.\n"
        "[bold]2. Turn on TOTP:[/] smartapi.angelbroking.com/enable-totp → log in → you get a QR code AND a text key.\n"
        "   Scan the QR with Google Authenticator, and keep the text key - that's your [bold]ANGEL_TOTP_SECRET[/].\n"
        "[bold]3. Create an API key:[/] smartapi.angelbroking.com → sign up → 'Create an App' → type 'Trading APIs'\n"
        "   (redirect URL can be http://127.0.0.1) → copy the [bold]API Key[/].\n"
        "[bold]4. Fill these 4 lines in .env[/] (only you edit this file - never share it):\n"
        "   ANGEL_API_KEY=...      ANGEL_CLIENT_CODE=your Angel login ID\n"
        "   ANGEL_MPIN=your 4-digit PIN      ANGEL_TOTP_SECRET=the text key from step 2\n"
        "[bold]5. Choose this option again[/] to test. The app then uses live prices automatically.\n\n"
        "[bold]6. Restart the bot[/] (close its window, then menu 13). Telegram → [bold]🏦 My demat[/] shows your real holdings.\n\n"
        "[grey62]The app only READS prices and your holdings. It never places orders - auto-trading is not built.[/]",
        title="Steps", border_style="cyan"))
    rows = [[k, Text("✔ filled", style=ui.GREEN) if os.environ.get(k) else Text("✖ empty", style=ui.RED)]
            for k in live.ANGEL_KEYS]
    console.print(ui.table([("Setting in .env", "left"), ("", "left")], rows))
    if not live.angel_configured():
        ui.info("Currently using Yahoo (free, can be several minutes late).")
        if ask_yes("Open the .env file in Notepad now?"):
            os.startfile(ROOT / ".env")
        return
    a = live.AngelSource()
    try:
        with console.status("logging in to Angel One..."):
            a.login()
        ui.ok("Logged in to Angel One")
        with console.status("downloading Angel's instrument list (once a day)..."):
            a.tokens = live.load_instruments()
        ui.ok(f"{len(a.tokens):,} NSE stocks available")
        t0 = time.time()
        q = a.quotes(["SBIN", "RELIANCE", "TCS"])
        ms = (time.time() - t0) * 1000
        for s, (price, ts) in q.items():
            age = (now_ist() - ts).total_seconds()
            ui.ok(f"{s}: ₹{price:,.2f}  (exchange time {ts:%H:%M:%S}, {age:.0f}s ago)")
        ui.info(f"Quote request took {ms:.0f} ms")
        c = a.candles(["SBIN"], {}, days=1).get("SBIN")
        if c is not None and len(c):
            ui.ok(f"5-minute candles OK - latest {c.index[-1]:%d %b %H:%M}")
        console.print(Panel("[bold green]Live data is working.[/] The scanner, Trading Desk and bot now use Angel One.\n"
                            "Outside market hours prices are the last trade, so 'seconds ago' will be large - that's normal.",
                            border_style=ui.GREEN))
    except Exception as e:
        ui.error(f"Angel One test failed: {e}")
        ui.info("Check: API key, client code (your login ID, e.g. A123456), 4-digit MPIN, and the TOTP text key "
                "(not the 6-digit code). Your PC clock must also be correct for TOTP.")


def advisor_setup(cfg, con):
    from screener import advisor
    ui.title("Chat advisor setup", "FREE: Google Gemini + live Google Search")
    console.print(Panel(
        "[bold]1.[/] Open [bold]aistudio.google.com/apikey[/] and sign in with your Google account.\n"
        "[bold]2.[/] Click [bold]Create API key[/] and copy it. It's free - no card, no payment.\n"
        "[bold]3.[/] Paste it in [bold].env[/] after [bold]GEMINI_API_KEY=[/] and save.\n"
        "[bold]4.[/] Choose this option again to test, then restart the Telegram bot (menu 13).\n\n"
        "[grey62]Free tier: a daily limit (the app caps you at 40 questions/day to stay inside it). Google may use "
        "free-tier chats to improve its products - the advisor only sees your paper wallet and stock questions.[/]",
        title="Steps", border_style="cyan"))
    if not advisor.configured():
        if ask_yes("Open the .env file in Notepad now?"):
            os.startfile(ROOT / ".env")
        return
    try:
        with console.status("asking Gemini a test question (with Google Search)..."):
            model = advisor.pick_model(con, cfg)
            answer = advisor.ask(con, cfg, "In one short paragraph: how did the Indian stock market do today?")
        ui.ok(f"Working! Model: {model}")
        import html, re
        console.print(Panel(html.unescape(re.sub(r"</?\w+>", "", answer)), title="Test answer", border_style=ui.GREEN))
    except Exception as e:
        ui.error(f"Test failed: {e}")


def telegram_test():
    ui.title("Telegram setup")
    if not telegram._token():
        console.print(Panel(
            "1. In Telegram, open [bold]@BotFather[/] → send [bold]/newbot[/] → pick a name → copy the token.\n"
            "2. Open [bold]stock-app\\.env[/] in Notepad, paste it after [bold]TELEGRAM_BOT_TOKEN=[/], save.\n"
            "3. Send 'hi' to your new bot in Telegram.\n"
            "4. Come back here and choose this option again.", border_style=ui.YELLOW))
        if ask_yes("Open the .env file in Notepad now?"):
            os.startfile(ROOT / ".env")
        return
    if not telegram.chat_id():
        chats = telegram.discover_chat_id()
        if not chats:
            ui.warn("Token works. Now send 'hi' to your bot in Telegram, then choose this option again.")
            return
        for cid, name in chats:
            ui.ok(f"Found your chat: {name}. Add this line to .env:   TELEGRAM_CHAT_ID={cid}")
        if ask_yes("Open the .env file in Notepad now?"):
            os.startfile(ROOT / ".env")
        return
    if telegram.send("✅ Stock screener connected. Alerts will arrive here."):
        ui.ok("Test message sent - check Telegram!")
    else:
        ui.error("Sending failed - check the token and chat id in .env")


# ====================================================================== entry point
def main():
    cfg = load_config()
    con = connect(cfg["data"]["db_path"])
    try:
        if len(sys.argv) == 1:
            return menu(cfg, con)
        ap = argparse.ArgumentParser(description="Personal NSE stock screener")
        sub = ap.add_subparsers(dest="cmd", required=True)
        for name in ("lab", "picks", "check", "live-setup", "desk", "dt-watch", "dt-scan", "dt-backtest", "dt-paper", "scan", "backtest", "paper", "dips", "news",
                     "run", "telegram-test", "menu"):
            sub.add_parser(name)
        for name in ("update", "daily", "plan"):
            p = sub.add_parser(name)
            p.add_argument("--limit", type=int)
            p.add_argument("--budget", type=float)
        args = ap.parse_args()
        {
            "lab": lambda: lab_ui.lab(cfg, con), "picks": lambda: lab_ui.picks(cfg, con),
            "check": lambda: lab_ui.check_stock(cfg, con), "live-setup": live_setup, "desk": lambda: desk.run(cfg, con), "dt-watch": lambda: dt_watch(cfg, con), "dt-scan": lambda: dt_scan(cfg, con),
            "dt-backtest": lambda: dt_backtest(cfg, con), "dt-paper": lambda: dt_paper(cfg, con),
            "scan": lambda: swing_scan(cfg, con), "backtest": lambda: swing_backtest(cfg, con),
            "paper": lambda: swing_paper(cfg, con), "dips": lambda: show_dips(cfg, con),
            "news": lambda: show_news(cfg, con), "run": lambda: Bot(cfg, con).run(), "tick": lambda: Bot(cfg, con).tick(), "telegram-test": telegram_test,
            "menu": lambda: menu(cfg, con), "update": lambda: update(cfg, con, args.limit),
            "daily": lambda: swing_daily(cfg, con, args.budget), "plan": lambda: swing_plan(cfg, con, args.budget),
        }[args.cmd]()
    except KeyboardInterrupt:
        ui.warn("Stopped. Anything downloaded so far is saved.")
    finally:
        con.close()


if __name__ == "__main__":
    main()
