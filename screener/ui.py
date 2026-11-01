"""Terminal look & feel: colours, tables, panels, progress bars (using the free 'rich' library)."""
import math

import pandas as pd
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeRemainingColumn
from rich.table import Table
from rich.text import Text

console = Console(highlight=False)

GREEN, RED, YELLOW, DIM, ACCENT = "green", "red", "yellow", "grey62", "cyan"


def rs(x: float, sign: bool = False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"₹{x:+,.2f}" if sign else f"₹{x:,.2f}"


def money(x: float) -> Text:
    """Profit green, loss red."""
    return Text(rs(x, sign=True), style=GREEN if x > 0 else RED if x < 0 else "")


def pct(x: float, sign: bool = True) -> Text:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return Text("-")
    return Text(f"{x:+.2f}%" if sign else f"{x:.2f}%", style=GREEN if x > 0 else RED if x < 0 else "")


def title(text: str, subtitle: str = "") -> None:
    console.print()
    console.rule(f"[bold {ACCENT}]{text}[/]" + (f"  [{DIM}]{subtitle}[/]" if subtitle else ""))


def info(text: str) -> None:
    console.print(f"[{DIM}]{text}[/]")


def warn(text: str) -> None:
    console.print(f"[{YELLOW}]⚠ {text}[/]")


def error(text: str) -> None:
    console.print(f"[bold {RED}]✖ {text}[/]")


def ok(text: str) -> None:
    console.print(f"[{GREEN}]✔ {text}[/]")


def disclaimer(extra: str = "") -> None:
    console.print(Panel(
        "Ideas from a rules-based program — [bold]not advice, not a guarantee[/]. Stop-losses can be skipped "
        "by sudden gaps, so a real loss can exceed 'max loss'. " + extra,
        border_style=DIM, box=box.ROUNDED))


def table(columns: list[tuple[str, str]], rows: list[list], title_: str = "", caption: str = "") -> Table:
    """columns: [(header, justify)]; cells may be str or rich Text."""
    t = Table(title=title_, caption=caption, box=box.SIMPLE_HEAVY, header_style=f"bold {ACCENT}",
              title_style="bold", caption_style=DIM, pad_edge=False)
    for name, justify in columns:
        t.add_column(name, justify=justify, overflow="fold")
    for r in rows:
        t.add_row(*[c if isinstance(c, Text) else str(c) for c in r])
    return t


def score_text(score: float) -> Text:
    style = GREEN if score >= 60 else YELLOW if score >= 45 else DIM
    return Text(f"{score:.0f}", style=f"bold {style}")


def verdict_panel(lines: list[str], good: bool, heading: str = "VERDICT") -> Panel:
    body = "\n".join(f"• {l}" for l in lines)
    return Panel(body, title=f"[bold]{heading}[/]", border_style=GREEN if good else RED, box=box.HEAVY)


def trade_card(n: int, p, kind: str = "SWING") -> Panel:
    """A planner TradePlan as a coloured card."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style=DIM)
    grid.add_column()
    grid.add_row("Buy", f"[bold]{p.qty}[/] share(s) at ~[bold]{rs(p.buy)}[/]  "
                        f"[{DIM}](limit order, don't pay above {rs(p.dont_buy_above)})[/]")
    grid.add_row("Invested", rs(p.invested))
    grid.add_row("Target", Text.assemble((f"{rs(p.target)}  (+{p.target_pct}%)  → profit ", ""),
                                         (rs(p.net_profit, True), f"bold {GREEN}"), " after charges"))
    grid.add_row("Stop-loss", Text.assemble((f"{rs(p.stop)}  (-{p.stop_pct}%)  → max loss ", ""),
                                            (rs(-p.max_loss, True), f"bold {RED}"), " incl. charges"))
    grid.add_row("Charges", f"{rs(p.charges)} round trip   ·   reward/risk {p.reward_risk}   ·   "
                            f"moves ~{p.atr_pct}%/day")
    grid.add_row("Why", Text(p.reasons, style=DIM))
    return Panel(grid, title=f"[bold]#{n}  {p.symbol}[/]  [{DIM}]{p.name} · {p.sector}[/]",
                 subtitle=f"score [bold]{p.score}[/]/100 · {kind}", border_style=ACCENT, box=box.ROUNDED)


def progress() -> Progress:
    return Progress(TextColumn("  {task.description}"), BarColumn(bar_width=30), MofNCompleteColumn(),
                    TimeRemainingColumn(), console=console, transient=False)


def df_rows(df: pd.DataFrame, cols: list[str]) -> list[list]:
    return [[r[c] for c in cols] for _, r in df.iterrows()]


__all__ = ["console", "Group", "Panel", "Table", "Text", "box"]
