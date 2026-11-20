"""Screens for the Strategy Lab, Strategy Picks and the investor checklists."""
import json
import math
from datetime import datetime

import pandas as pd
from rich.prompt import Prompt

from . import fundamentals, gurus, strategies, ui, wallet
from .db import get_state, set_state
from .ui import Group, Panel, Table, Text, box, console


def _ask(q: str, default: str = "") -> str:
    try:
        return Prompt.ask(q, default=default, show_default=bool(default), console=console).strip().lstrip("﻿")
    except EOFError:
        return default


def _pct(x, plus=True):
    return ui.pct(x) if x == x else Text("-")


def champion(con) -> dict | None:
    s = get_state(con, "lab_champion")
    return json.loads(s) if s else None


# ======================================================================== Strategy Lab
def lab(cfg, con):
    L = cfg["lab"]
    ui.title("Strategy Lab", "famous investors' strategies tested on 10 years of NSE data")
    ui.info(f"Each strategy picks its top {L['positions']} stocks every month-end and holds them for the month. "
            f"Winner is chosen on data up to {strategies.split_date(cfg)}, then checked on the years after.")
    with console.status("[cyan]Testing 12 strategy variants on ~2,300 stocks (about a minute)..."):
        out = strategies.run_lab(con, cfg)
    t, b, c = out["table"], out["bench"], out["champion"]
    split_year = int(strategies.split_date(cfg)[:4])
    first, last = out["period"]
    rows = []
    for _, r in t.sort_values("is_sharpe", ascending=False).iterrows():
        star = r["key"] == c["key"] and r["market_filter"] == c["market_filter"]
        rows.append([Text(("★ " if star else "  ") + r["name"], style="bold" if star else ""),
                     Text(r["source"], style=ui.DIM),
                     _pct(r["is_cagr"]), _pct(r["oos_cagr"]), Text(f"{r['all_max_dd']:.0f}%", style=ui.RED),
                     f"{r['oos_sharpe']:.2f}",
                     Text("✔", style=ui.GREEN) if r["oos_cagr"] > b["oos_cagr"] else Text("✖", style=ui.RED)])
    rows.append([Text("  Nifty 50 (just hold the index)", style="bold cyan"), Text("benchmark", style=ui.DIM),
                 _pct(b["is_cagr"]), _pct(b["oos_cagr"]), Text(f"{b['all_max_dd']:.0f}%", style=ui.RED),
                 f"{b['oos_sharpe']:.2f}", ""])
    console.print(ui.table(
        [("Strategy", "left"), ("From", "left"), (f"{first[:4]}-{split_year} /yr", "right"),
         (f"{split_year + 1}-{last[:4]} /yr (unseen)", "right"), ("Worst fall", "right"),
         ("Quality*", "right"), ("Beat Nifty?", "center")], rows,
        title_="Results after charges (sorted by how they did in the choosing period)",
        caption="* risk-adjusted return (Sharpe) in the unseen years: return per unit of ups-and-downs"))

    s = strategies.BY_KEY[c["key"]]
    lines = [
        f"[bold]{c['name']}[/] - {s.source}",
        f"Rule: {s.idea}" + (" Holds cash when the Nifty is below its 200-day average." if c["market_filter"] else ""),
        f"Chosen on {first[:4]}-{split_year}: [bold]{c['is_cagr']:+.1f}%/yr[/] (Nifty {b['is_cagr']:+.1f}%).",
        f"Then on the UNSEEN years {split_year + 1}-{last[:4]}: [bold]{c['oos_cagr']:+.1f}%/yr[/] "
        f"(Nifty {b['oos_cagr']:+.1f}%) → " + ("[bold green]it held up.[/]" if c["oos_beat_nifty"] else
                                               "[bold red]it did NOT beat the Nifty - don't trust it.[/]"),
        f"Price of that return: falls of up to [bold red]{c['all_max_dd']:.0f}%[/] along the way.",
    ]
    h = out["hindsight"]
    if h["key"] != c["key"] or h["market_filter"] != c["market_filter"]:
        lines.append(f"[grey62](Picking the best of the whole 10 years would have chosen '{h['name']}' - that's "
                     "hindsight, which is exactly the trap this test avoids.)[/]")
    console.print(Panel("\n".join(lines), title="★ CHAMPION", border_style=ui.GREEN if c["oos_beat_nifty"] else ui.RED,
                        box=box.HEAVY))

    budget = float(_ask("[bold]Your budget, to test the champion with YOUR money?[/] ₹",
                        f"{cfg['alerts']['default_budget']:g}").replace(",", "") or 1500)
    with console.status("[cyan]Re-running with your budget (whole shares, real charges)..."):
        sb = strategies.small_budget_check(out, cfg, budget)
    a = sb["all"]
    console.print(Panel(
        f"With ₹{budget:,.0f} the strategy can only hold [bold]{sb['n']}[/] stock(s) instead of {L['positions']}.\n"
        f"Result {first[:4]}-{last[:4]}: ₹{budget:,.0f} → [bold]₹{sb['final']:,.0f}[/] "
        f"({a['cagr']:+.1f}%/yr) after ₹{sb['charges']:,.0f} of charges, but with falls of up to "
        f"[bold red]{a['max_dd']:.0f}%[/].\n"
        "[grey62]With only 1-2 stocks, luck matters far more than with 10 - a few lucky picks can make or break it. "
        "Adding money over time to hold more stocks makes results more dependable.[/]",
        title="With your budget", border_style=ui.YELLOW, box=box.ROUNDED))
    ui.info("Caveats: only stocks listed today are tested (survivorship bias flatters results), Nifty is the price "
            "index (an index fund also earns ~1.2%/yr dividends), and past results never guarantee future ones.")

    save_champion(con, out, cfg)
    ui.ok("Saved. 'Strategy picks' (menu S) now uses this champion.")


def save_champion(con, out: dict, cfg) -> None:
    """Remember the Strategy Lab winner - used by Strategy picks, the status bar and the bot."""
    c, b = out["champion"], out["bench"]
    set_state(con, "lab_champion", json.dumps({
        "key": c["key"], "market_filter": bool(c["market_filter"]), "name": c["name"],
        "is_cagr": float(c["is_cagr"]), "oos_cagr": float(c["oos_cagr"]), "max_dd": float(c["all_max_dd"]),
        "nifty_oos": float(b["oos_cagr"]), "beat": bool(c["oos_beat_nifty"]),
        "date": datetime.now().strftime("%d %b %Y"), "updated": datetime.now().isoformat(timespec="seconds"),
        "split": strategies.split_date(cfg),
    }))
    set_state(con, "lab_line", json.dumps({"good": bool(c["oos_beat_nifty"]), "line":
              f"{c['name']}: {c['oos_cagr']:+.1f}%/yr on unseen years vs Nifty {b['oos_cagr']:+.1f}%"}))


# ======================================================================== Strategy Picks
def guru_summary(r: dict) -> Text:
    parts = []
    for name, (passed, judged, _) in gurus.score(r).items():
        if judged:
            style = ui.GREEN if passed / judged >= 0.75 else ui.YELLOW if passed / judged >= 0.5 else ui.DIM
            parts.append((f"{name} {passed}/{judged}", style))
            parts.append(("  ", ""))
    return Text.assemble(*parts) if parts else Text("no fundamentals yet", style=ui.DIM)


def picks(cfg, con):
    c = champion(con)
    if not c:
        ui.warn("Run the Strategy Lab first (menu Y) - it decides which strategy is trustworthy enough to use.")
        return
    ui.title("Strategy picks", f"{c['name']} - chosen by the Strategy Lab on {c['date']}")
    if not c["beat"]:
        console.print(Panel("[bold red]The champion did NOT beat the Nifty on unseen years.[/] The honest choice is a "
                            "Nifty 50 index fund. The picks below are for paper trading only.", border_style=ui.RED))
    with console.status("[cyan]Ranking stocks on the latest prices..."):
        cp = strategies.current_picks(con, cfg, c["key"], c["market_filter"])
        fund = fundamentals.load_fundamentals(con)
    console.print(Text.assemble(f"Prices as of {cp['date']} · Nifty is ",
                                ("above" if cp["nifty_above_200dma"] else "BELOW", ui.GREEN if cp["nifty_above_200dma"] else ui.RED),
                                " its 200-day average · next review: ", (str(cp["next_review"]), "bold")))
    if not cp["invest"]:
        console.print(Panel("[bold]The strategy says: stay in CASH this month[/] (the market is in a downtrend). "
                            "Keep money in a liquid fund / savings and check again at the next review.",
                            border_style=ui.YELLOW))
        return

    L = cfg["lab"]
    rows = []
    for p in cp["picks"]:
        r = {**(fund.loc[p["symbol"]].to_dict() if p["symbol"] in fund.index else {}), "close": p["price"]}
        rows.append([p["rank"], Text(p["symbol"], style="bold" if p["in_top"] else ui.DIM), ui.rs(p["price"]),
                     Text(p["why"], style=ui.DIM), guru_summary(r)])
    console.print(ui.table([("#", "right"), ("Stock", "left"), ("Price", "right"), ("Why it qualifies", "left"),
                            ("Investor checklists (today's fundamentals)", "left")], rows,
                           title_=f"Top {L['positions']} = buy zone · {L['positions'] + 1}-{L['positions'] * L['hold_buffer']} "
                                  "= keep if you already own it"))

    budget = float(_ask("[bold]How much are you investing this time?[/] ₹", f"{cfg['alerts']['default_budget']:g}")
                   .replace(",", "") or 1500)
    k = 2 if budget >= 2 * cfg["planner"]["min_trade_value"] else 1
    each = budget / k
    chosen = [p for p in cp["picks"] if p["in_top"] and p["price"] * 1.01 <= each][:k]
    if not chosen:
        ui.warn(f"None of today's top {L['positions']} costs under ₹{each:,.0f} per share. Options: a bigger budget, or "
                "wait for the next review.")
        return
    for i, p in enumerate(chosen, 1):
        qty = math.floor(each / (p["price"] * 1.002))
        val = qty * p["price"]
        fake = {"side": "LONG", "entry": p["price"], "qty": qty, "mode": "hold"}
        loss20 = wallet.unrealised(fake, p["price"] * 0.8, cfg)
        g = Table.grid(padding=(0, 2))
        g.add_column(style=ui.DIM)
        g.add_column()
        g.add_row("Buy", f"[bold]{qty}[/] share(s) at ~[bold]{ui.rs(p['price'])}[/] = {ui.rs(val)} (delivery, hold)")
        g.add_row("Why", p["why"])
        g.add_row("Hold until", f"the month-end review on [bold]{cp['next_review']}[/]. Then: keep it if it's still in "
                                f"the top {L['positions'] * L['hold_buffer']}, sell it if not. That's exactly what was tested.")
        g.add_row("Risk", Text.assemble("No fixed target - momentum rides winners. If it falls 20%, that's ",
                                        ui.money(loss20), f". The strategy's worst fall was {c['max_dd']:.0f}%."))
        console.print(Panel(g, title=f"[bold]#{i}  {p['symbol']}[/]  rank {p['rank']}", border_style="cyan", box=box.ROUNDED))
    console.print(Text(f"Tested result: {c['oos_cagr']:+.1f}%/yr on unseen years (Nifty {c['nifty_oos']:+.1f}%), "
                       f"with falls up to {c['max_dd']:.0f}%. Not a guarantee.", style=ui.DIM))
    ui.disclaimer("To practise: menu T (Trading Desk) during market hours → B → choose H (hold).")


# ======================================================================== Stock checker
def check_stock(cfg, con):
    sym = _ask("[bold]Stock symbol[/] (e.g. TCS, HDFCBANK)").upper()
    if not sym:
        return
    fund = fundamentals.load_fundamentals(con)
    if sym not in fund.index:
        ui.warn(f"No fundamentals for {sym} yet (only liquid stocks are downloaded). Check the symbol.")
        return
    r = fund.loc[sym].to_dict()
    ui.title(f"{sym} · {r.get('name') or ''}", r.get("sector") or "")
    w = strategies.load_wide(con, 2)
    for k in ("close", "high", "low", "volume"):
        w[k] = w[k].drop(columns=["^NSEI"], errors="ignore")
    f = strategies.features(w)
    if sym in f["close"].columns:
        rr = strategies.row(f, f["close"].index[-1]).loc[sym]
        r["rs"] = rr["rs"]
        r.setdefault("close", rr["close"])
        console.print(Text(f"Price ₹{rr['close']:,.2f} · 12-month return {rr['ret_12'] * 100:+.0f}% · relative strength "
                           f"{rr['rs']:.0f}/100 · {(1 - rr['close'] / rr['high52']) * 100:.0f}% below 52-week high",
                           style=ui.DIM))
        tt = strategies.rank_stocks("minervini", strategies.row(f, f["close"].index[-1]), cfg)
        console.print(Text.assemble("Minervini trend template: ", ("PASSES ✔", ui.GREEN) if sym in tt.index else ("fails ✖", ui.RED)))
    for name, src, fn in gurus.CHECKLISTS:
        checks = fn(r)
        lines = [Text.assemble(("✔ " if ok else "✖ " if ok is False else "? ", ui.GREEN if ok else ui.RED if ok is False else ui.DIM),
                               (txt, "" if ok is not None else ui.DIM)) for ok, txt in checks]
        passed = sum(1 for ok, _ in checks if ok)
        judged = sum(1 for ok, _ in checks if ok is not None)
        console.print(Panel(Group(*lines), title=f"[bold]{name}[/] {passed}/{judged}", subtitle=f"[grey62]{src}[/]",
                            border_style=ui.GREEN if judged and passed / judged >= 0.75 else ui.DIM, box=box.ROUNDED))
    mf = gurus.magic_formula(fund)
    if sym in mf.index:
        ui.info(f"Greenblatt Magic Formula rank: {int(mf[sym])} of {len(mf)} (lower = cheaper + better business)")
    ui.info("? = not enough free data to judge. These checklists use today's numbers and were NOT backtested.")


# ======================================================================== Quant research
def quant_report(cfg, con):
    from . import pipeline, quant
    ui.title("Quant Research", "alphas -> IC -> combined score -> portfolio -> luck check")
    with console.status("[cyan]Scoring ~2,300 stocks on 11 alphas over 10 years (a minute or two)..."):
        q = quant.research(con, cfg)
    t, split = q["table"], q["split"]
    ui.info(f"Monthly tests {q['period'][0]} to {q['period'][1]}. Weights chosen on data up to {split}; "
            f"judged on the {q['n_test_months']} unseen months after it.")

    def ic(x, tstat):
        style = ui.GREEN if tstat >= 2 else ui.RED if tstat <= -2 else ui.DIM
        return Text(f"{x:+.3f} (t {tstat:+.1f})", style=style)

    rows = [[Text(k, style="bold"), Text(r["idea"], style=ui.DIM), ic(r["train_ic"], r["train_t"]),
             ic(r["test_ic"], r["test_t"]), _pct(r["test_top_excess"]), f"{r['turnover']:.0f}%",
             f"{r['weight']:+.2f}" if r["weight"] else Text("0", style=ui.DIM)]
            for k, r in t.sort_values("test_t", ascending=False).iterrows()]
    ct, cs = q["combo_train"], q["combo_test"]
    rows.append([Text("COMBINED", style="bold cyan"), Text("weighted mix of the alphas above", style=ui.DIM),
                 ic(ct["ic"], ct["t"]), ic(cs["ic"], cs["t"]), "", "", ""])
    console.print(ui.table(
        [("Alpha", "left"), ("Idea", "left"), ("IC choosing years", "right"), ("IC unseen years", "right"),
         ("Top 20% vs all /yr", "right"), ("Monthly churn", "right"), ("Weight", "right")], rows,
        title_="Does each score predict next month's returns?",
        caption="IC = rank correlation with next month's return. Green = t >= 2 (probably real), red = reliably wrong way."))
    if q["weights_fallback"]:
        ui.warn("No alpha was significant in the choosing years, so the weights are plain ICs: treat the mix as weak.")

    def row(name, x, star=False):
        return [Text(("★ " if star else "  ") + name, style="bold" if star else ""), _pct(x.get("train_cagr", float("nan"))),
                _pct(x.get("test_cagr", float("nan"))), Text(f"{x.get('test_max_dd', float('nan')):.0f}%", style=ui.RED),
                f"{x.get('test_sharpe', float('nan')):.2f}"]
    b = {f"{part}_{k}": v for part in ("train", "test") for k, v in q["bench"][part].items()}
    rows = [row(name, r, name == q["best"]) for name, r in q["variants"].iterrows()] + [row("Nifty 50 (benchmark)", b)]
    console.print(ui.table([("Portfolio from the combined score (after all charges)", "left"), ("Choosing yrs /yr", "right"),
                            ("Unseen yrs /yr", "right"), ("Worst fall (unseen)", "right"), ("Sharpe (unseen)", "right")],
                           rows, caption="★ = best Sharpe in the choosing years; that one is used for picks"))
    dsr = q["dsr"]
    verdict = ("probably a real edge" if dsr >= 0.95 else "could easily be luck" if dsr == dsr else "not enough months")
    console.print(f"Luck check (Deflated Sharpe, {q['trials']} ideas tried): "
                  f"[bold]{dsr * 100:.0f}%[/] chance the unseen-years edge is real -> {verdict}." if dsr == dsr
                  else f"Luck check: {verdict}.")
    console.print(f"\nToday's top {len(q['picks'])} by combined score ({q['as_of']}): [bold]{', '.join(q['picks'])}[/]")
    ui.info(f"Research only, paper only. History used: {q['history']}.")
    path = pipeline.save_csv(t, "quant_alphas")
    ui.info(f"Saved: {path}")


# ======================================================================== Trading agents
def agents_backtest(cfg, con):
    from . import agents
    ui.title("Trading agents: backtest", "Rs 10,000 a day each, paper, after intraday charges")
    lb = agents.backtest(con, cfg)
    rows = [[Text(r["agent"], style="bold"), Text(agents.BY_KEY[r["key"]].idea, style=ui.DIM), ui.money(r["pnl"]),
             str(r["trades"]), f"{r['win_rate']:.0f}%" if r["trades"] else "-",
             f"{r['green_days']}/{r['days']}", ui.money(r["worst_day"]) if r["days"] else "-"]
            for _, r in lb.iterrows() if r["key"] != "quant"]
    console.print(ui.table([("Agent", "left"), ("Strategy", "left"), ("Total P&L", "right"), ("Trades", "right"),
                            ("Won", "right"), ("Green days", "right"), ("Worst day", "right")], rows,
                           caption="Quant Bot is skipped: its picks are today's, so testing it on the past would cheat."))
    ui.info("Past candles from Yahoo; fills at the candle close plus 0.05% slippage. Live results can be worse.")
