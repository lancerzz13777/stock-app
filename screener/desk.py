"""The Trading Desk: a live screen for paper day trading with the wallet.

Keys:  B buy   X exit   A exit all   D add fake money   H history   Q back to menu
Prices refresh every `wallet.price_refresh_seconds`. Exit alerts beep, show on screen, and go to Telegram.
"""
import time
from datetime import datetime

from rich.live import Live
from rich.prompt import Prompt

from . import intraday, telegram, ui, wallet
from .ui import Group, Panel, Table, Text, box, console

try:
    import msvcrt  # Windows: lets the screen refresh while waiting for a key
except ImportError:  # other systems: refresh after each command instead
    msvcrt = None


def _now() -> datetime:
    return datetime.now(intraday.IST)


def _key() -> str | None:
    if msvcrt and msvcrt.kbhit():
        return msvcrt.getwch().lower()
    return None


def _ask(question: str, default: str = "") -> str:
    try:
        return Prompt.ask(question, default=default, show_default=bool(default), console=console).strip().lstrip("﻿")
    except EOFError:
        return default


def _yes(question: str) -> bool:
    return _ask(f"{question} (y/n)", "y").lower().startswith("y")


class Desk:
    def __init__(self, cfg, con):
        self.cfg, self.con = cfg, con
        wallet.ensure(con, cfg)
        self.prices: dict = {}
        self.alerts: list[tuple[str, str, str]] = []   # (time, level, message)
        self.last_fetch: datetime | None = None
        from . import live
        self.source = live.source(cfg)
        w = cfg["wallet"]
        self.every = w["price_refresh_seconds_live"] if self.source.live else w["price_refresh_seconds"]

    # -------------------------------------------------------------- data
    def refresh(self):
        opens = wallet.open_positions(self.con)
        if opens:
            self.prices.update(wallet.live_prices([p["symbol"] for p in opens]))
        self.last_fetch = _now()
        for p in wallet.auto_square_off(self.con, self.cfg, self.prices):
            self.alert("exit", f"🔒 {p['symbol']} was auto-squared-off at ₹{p['exit']:,.2f} → ₹{p['net_pnl']:+,.2f}. "
                               "Real brokers charge extra for this - exit yourself before 3:15.")
        now = _now()
        for p in wallet.open_positions(self.con):
            q = self.prices.get(p["symbol"])
            if q:
                for level, msg in wallet.new_alerts(self.con, p, q[0], now, self.cfg):
                    self.alert(level, msg)

    def alert(self, level: str, msg: str):
        self.alerts.append((_now().strftime("%H:%M:%S"), level, msg))
        if level in ("exit", "warn"):
            console.bell()
        telegram.send(msg)

    # -------------------------------------------------------------- screen
    def render(self) -> Group:
        s = wallet.summary(self.con, self.cfg, self.prices)
        now = _now()
        top = Table.grid(expand=True, padding=(0, 3))
        for _ in range(5):
            top.add_column(justify="center")
        top.add_row(*[Text(h, style=ui.DIM) for h in ("Free cash", "Locked in trades", "Open P&L", "Total value",
                                                       "Booked today")])
        top.add_row(Text(ui.rs(s["cash"]), style="bold"), Text(ui.rs(s["locked"]), style="bold yellow"),
                    ui.money(s["open_pnl"]), Text(ui.rs(s["total"]), style="bold"),
                    Text.assemble(ui.money(s["today_pnl"]), (f"  ({s['today_trades']} trades)", ui.DIM)))
        mkt = "● MARKET OPEN" if wallet.market_open(now) else "● market closed"
        age = ""
        if self.prices:
            newest = max(t for _, t in self.prices.values())
            mins = (now - newest).total_seconds() / 60
            if not wallet.market_open(now):
                age = f" · last prices {newest:%d %b %H:%M}"
            else:
                age = f" · prices from {newest:%H:%M} ({mins:.0f} min old)" if mins >= 1 else f" · prices from {newest:%H:%M}"
        left = max(0, self.every - int((now - self.last_fetch).total_seconds())) if self.last_fetch else 0
        sub = f"{mkt} · {now:%H:%M:%S}{age} · refresh in {left}s"
        src = f"[bold green]● {self.source.name}[/]" if self.source.live else f"[yellow]● {self.source.name}[/]"
        sub = f"{src} · " + sub
        parts = [Panel(top, title="[bold cyan]💼 PAPER TRADING DESK[/]  [grey62]fake money · real prices[/]",
                       subtitle=f"[grey62]{sub}[/]", box=box.DOUBLE, border_style="cyan")]

        opens = s["open"]
        if opens:
            rows = []
            for p in opens:
                q = self.prices.get(p["symbol"])
                price = q[0] if q else None
                advice = Text("waiting for price", style=ui.DIM)
                pl = Text("-")
                if price is not None:
                    pl = ui.money(wallet.unrealised(p, price, self.cfg))
                    adv = wallet.exit_advice(p, price, now, self.cfg)
                    exit_now = [m for _, lvl, m in adv if lvl == "exit"]
                    if exit_now:
                        word = "TARGET HIT → EXIT" if "target" in exit_now[0] else "STOP HIT → EXIT" if "stop" in exit_now[0] \
                            else "EXIT NOW (3:15)"
                        advice = Text(word, style=f"bold {ui.GREEN if 'TARGET' in word else ui.RED}")
                    elif any(lvl == "warn" for _, lvl, _ in adv):
                        advice = Text("exit soon (3:15)", style=f"bold {ui.YELLOW}")
                    elif adv:
                        advice = Text("move stop to buy price", style=ui.YELLOW)
                    elif p.get("mode") == "hold":
                        advice = Text(f"hold · review {self._review_date()}", style=ui.ACCENT)
                    else:
                        advice = Text("hold", style=ui.DIM)
                rows.append([f"#{p['id']}", Text(p["symbol"], style="bold"),
                             "HOLD" if p.get("mode") == "hold" else "DAY", p["qty"], ui.rs(p["entry"]),
                             ui.rs(price) if price else "-", Text(ui.rs(p["stop"]) if p["stop"] else "-", style=ui.RED),
                             Text(ui.rs(p["target"]) if p["target"] else "-", style=ui.GREEN), ui.rs(p["locked"]), pl, advice])
            parts.append(ui.table([("#", "right"), ("Stock", "left"), ("Type", "left"), ("Qty", "right"),
                                   ("Bought", "right"), ("Now", "right"), ("Stop", "right"), ("Target", "right"),
                                   ("Locked", "right"), ("P&L if you exit now", "right"), ("What to do", "left")],
                                  rows, title_="Open positions"))
        else:
            parts.append(Text("\n  No open positions. Press B to paper-buy a stock.\n", style=ui.DIM))

        if self.alerts:
            style = {"exit": f"bold {ui.RED}", "warn": ui.YELLOW, "info": ui.ACCENT}
            parts.append(Panel(Group(*[Text(f"{t}  {m}", style=style.get(l, "")) for t, l, m in self.alerts[-6:]]),
                               title="Alerts", border_style=ui.YELLOW, box=box.ROUNDED))
        keys = "[bold cyan]B[/] buy   [bold cyan]X[/] exit   [bold cyan]A[/] exit all   [bold cyan]D[/] add money   " \
               "[bold cyan]H[/] history   [bold cyan]R[/] refresh   [bold cyan]Q[/] back"
        parts.append(Text.from_markup(keys))
        return Group(*parts)

    # -------------------------------------------------------------- actions
    def do_buy(self):
        hint = ""
        sig = self.con.execute("SELECT symbol, signal_time FROM intraday_paper WHERE day=? ORDER BY id DESC LIMIT 1",
                               (_now().date().isoformat(),)).fetchone() if self._has_table("intraday_paper") else None
        if sig:
            hint = sig[0]
            ui.info(f"Latest scanner signal today: {sig[0]} (breakout at {sig[1]})")
        sym = _ask("[bold]Stock symbol[/] (e.g. SBIN, TCS, RELIANCE)", hint).upper()
        if not sym:
            return
        amount = float(_ask("[bold]How much to invest?[/] ₹", f"{self.cfg['intraday']['budget']:g}").replace(",", "") or 0)
        mode = "hold" if _ask("[bold]D[/] = day trade (sell by 3:15)  ·  [bold]H[/] = hold (Strategy picks, reviewed "
                              "monthly)", "D").lower().startswith("h") else "day"
        with console.status(f"getting live price for {sym}..."):
            q = wallet.live_prices([sym]).get(sym)
        if not q:
            ui.error(f"No price for {sym}. Check the NSE symbol.")
            return
        price, ts = q
        lev = self.cfg["intraday"]["leverage"] if mode == "day" else 1
        qty = int(amount * lev // price)
        if qty < 1:
            ui.error(f"₹{amount:,.0f} can't buy 1 share of {sym} at ₹{price:,.2f}.")
            return
        w = self.cfg["wallet"]
        ui.info(f"{sym} last price ₹{price:,.2f} (at {ts:%H:%M}). ₹{amount:,.0f} buys {qty} share(s) = "
                f"₹{qty * price / lev:,.2f} will be LOCKED.")
        fake = {"side": "LONG", "entry": price, "qty": qty, "mode": mode}
        if mode == "day":
            stop = round(price * (1 - w["default_stop_pct"] / 100), 2)
            target = round(price * (1 + w["default_target_pct"] / 100), 2)
            stop = float(_ask("Stop-loss (sell if it falls to)", f"{stop:.2f}"))
            target = float(_ask("Target (sell when it reaches)", f"{target:.2f}"))
            console.print(Text.assemble("If target hits: ", ui.money(wallet.unrealised(fake, target, self.cfg)),
                                        "    If stop hits: ", ui.money(wallet.unrealised(fake, stop, self.cfg)),
                                        ("   (after charges)", ui.DIM)))
        else:
            stop = target = None
            console.print(Text.assemble("Held like the Strategy Lab tested it: reviewed at each month end. A 10% fall "
                                        "would be ", ui.money(wallet.unrealised(fake, price * 0.9, self.cfg)),
                                        (" (delivery charges)", ui.DIM)))
        if not _yes(f"Paper-BUY {qty} {sym} at ~₹{price:,.2f}?"):
            return
        try:
            p = wallet.buy(self.con, self.cfg, sym, amount, stop=stop, target=target, price=price, mode=mode)
        except wallet.WalletError as e:
            ui.error(str(e))
            time.sleep(2.5)
            return
        self.prices[sym] = q
        ui.ok(f"Bought #{p['id']}: {p['qty']} {sym} @ ₹{p['entry']:,.2f}. ₹{p['locked']:,.2f} locked.")
        levels = f"Stop {p['stop']} · Target {p['target']}" if mode == "day" else "HOLD - reviewed at month end"
        telegram.send(f"📥 Paper BUY #{p['id']}: {p['qty']} {sym} @ Rs {p['entry']:,.2f}\n"
                      f"{levels} · Rs {p['locked']:,.2f} locked\nI'll tell you when to exit.")
        time.sleep(1.5)

    def _close(self, p: dict, ask: bool = True):
        with console.status(f"getting live price for {p['symbol']}..."):
            q = wallet.live_prices([p["symbol"]]).get(p["symbol"])
        if not q:
            ui.error("Couldn't get a live price - try again.")
            return
        result = wallet.unrealised(p, q[0], self.cfg)
        console.print(Text.assemble(f"Exit #{p['id']} {p['symbol']} at ₹{q[0]:,.2f} → ", ui.money(result), " after charges"))
        if ask and not _yes("Confirm exit?"):
            return
        c = wallet.sell(self.con, self.cfg, p["id"], q[0])
        back = p["locked"] + c["net_pnl"]
        good = c["net_pnl"] > 0
        console.print(Panel(
            Text.assemble(("You made " if good else "You lost ", "bold"), ui.money(c["net_pnl"]),
                          f" on {p['symbol']} ({ui.rs(p['entry'])} → {ui.rs(c['exit'])}, {p['qty']} share(s)).\n",
                          (f"{ui.rs(back)} released back to your free cash.", ui.DIM)),
            border_style=ui.GREEN if good else ui.RED, box=box.HEAVY))
        telegram.send(f"📤 Paper EXIT #{p['id']} {p['symbol']} @ Rs {c['exit']:,.2f}: {c['net_pnl']:+,.2f} after charges")
        time.sleep(2)

    def do_exit(self):
        opens = wallet.open_positions(self.con)
        if not opens:
            ui.warn("No open positions.")
            time.sleep(1.5)
            return
        ref = _ask("Exit which? (number or symbol)", str(opens[0]["id"]) if len(opens) == 1 else "")
        try:
            self._close(wallet.find_open(self.con, ref))
        except wallet.WalletError as e:
            ui.error(str(e))
            time.sleep(2)

    def do_exit_all(self):
        opens = wallet.open_positions(self.con)
        if opens and _yes(f"Exit all {len(opens)} positions at live prices?"):
            for p in opens:
                self._close(p, ask=False)

    def do_deposit(self):
        amt = _ask("Add how much fake money? ₹", "5000").replace(",", "")
        try:
            ui.ok(f"Free cash now {ui.rs(wallet.deposit(self.con, self.cfg, float(amt)))}")
        except (ValueError, wallet.WalletError) as e:
            ui.error(str(e))
        time.sleep(1.5)

    def do_history(self):
        s = wallet.summary(self.con, self.cfg, self.prices)
        closed = s["closed"].tail(20)
        ui.title("Trade history", "paper wallet")
        if len(closed):
            console.print(ui.table(
                [("#", "right"), ("Stock", "left"), ("Qty", "right"), ("Bought", "right"), ("Sold", "right"),
                 ("Opened", "left"), ("Closed", "left"), ("How", "left"), ("Result", "right")],
                [[r.id, r.symbol, r.qty, ui.rs(r.entry), ui.rs(r.exit), r.opened_at[5:16], r.closed_at[5:16], r.reason,
                  ui.money(r.net_pnl)] for r in closed.itertuples()]))
            console.print(Text.assemble(
                f"{len(s['closed'])} trades · win rate {s['win_rate']:.0f}% · all-time ", ui.money(s["all_time_pnl"]),
                (f"  (fake money added: {ui.rs(s['deposited'])})", ui.DIM)))
        else:
            ui.info("No closed trades yet.")
        _ask(f"[{ui.DIM}]Press Enter to go back[/]")

    @staticmethod
    def _review_date() -> str:
        import pandas as pd
        d = pd.Timestamp(_now().date())
        return (d + pd.offsets.BMonthEnd(0)).strftime("%d %b")

    def _has_table(self, name: str) -> bool:
        return bool(self.con.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())

    # -------------------------------------------------------------- loop
    def run(self):
        actions = {"b": self.do_buy, "x": self.do_exit, "a": self.do_exit_all, "d": self.do_deposit,
                   "h": self.do_history}
        with console.status("loading prices..."):
            self.refresh()
        while True:
            console.clear()
            key = None
            if msvcrt:
                with Live(self.render(), console=console, refresh_per_second=2, transient=True) as live:
                    while key is None:
                        if (_now() - self.last_fetch).total_seconds() >= self.every:
                            self.refresh()
                        live.update(self.render())
                        time.sleep(0.3)
                        key = _key()
            else:
                console.print(self.render())
                key = _ask("Key").lower()[:1] or "r"
            console.print(self.render())
            if key == "q":
                return
            if key == "r":
                self.refresh()
            elif key in actions:
                try:
                    actions[key]()
                except (ValueError, wallet.WalletError) as e:
                    ui.error(str(e))
                    time.sleep(2)


def run(cfg, con):
    Desk(cfg, con).run()
