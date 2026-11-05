"""The main steps (update data, rank stocks, plan trades), shared by the command line and the bot."""
from datetime import date

import pandas as pd

from . import fundamentals, news, prices, scoring, ui, universe
from .config import ROOT
from .planner import plan_budget

OUTPUT = ROOT / "output"

DISCLAIMER = (
    "Screening ideas from a rules-based program - NOT advice, NOT a guarantee. "
    "Check the backtest (python main.py backtest) and paper-trade before using real money. "
    "Stop-losses can be skipped by gap-downs, so a real loss can exceed 'max loss'."
)


def update_all(cfg, con, limit=None):
    ui.console.print("[bold]1/4[/] NSE stock list")
    symbols = universe.refresh_universe(con, cfg["data"]["universe"])
    if limit:
        symbols = symbols[:limit]
    ui.info(f"    {len(symbols):,} stocks")

    ui.console.print("[bold]2/4[/] Daily prices")
    days, adjusted = prices.fill_from_nse(con, symbols)  # official closes first: one file per day
    ui.info(f"    official NSE closes for {days} recent trading days; {len(adjusted)} stocks to re-download")
    prices.update_prices(con, symbols + [prices.NIFTY_SYMBOL], cfg, refetch=adjusted, nse_ok=days > 0)
    if adjusted:  # a re-download replaces the whole history, so put NSE's newer closes back (files are cached)
        prices.fill_from_nse(con, symbols)

    ui.console.print("[bold]3/4[/] Fundamentals (only stocks passing the price/liquidity filters)")
    snap = scoring.build_snapshots(prices.load_recent_prices(con))
    snap = snap[snap.index.isin(symbols)]
    liquid = scoring.price_filters(snap, cfg)
    ui.info(f"    {len(liquid):,} of {len(snap):,} stocks pass")
    fundamentals.update_fundamentals(con, list(liquid.index), cfg)

    ui.console.print("[bold]4/4[/] News")
    with ui.console.status("reading feeds..."):
        n = news.fetch_news(con, cfg)
    ui.info(f"    {n} new items")


def build_ranked(cfg, con, quiet=False) -> pd.DataFrame:
    snap = scoring.build_snapshots(prices.load_recent_prices(con))
    if snap.empty:
        raise SystemExit("No price data yet. Run:  python main.py update")
    liquid = scoring.price_filters(snap, cfg)
    merged = liquid.join(fundamentals.load_fundamentals(con), how="left")
    merged = merged.join(news.news_counts(con, cfg["news"]["lookback_days"]), how="left")
    merged[["news_positive", "news_negative"]] = merged[["news_positive", "news_negative"]].fillna(0)
    merged["name"] = merged["name"].fillna(pd.Series(merged.index, index=merged.index))
    merged["sector"] = merged["sector"].fillna("Unknown")
    candidates = scoring.fundamental_filters(merged, cfg)
    if not quiet:
        ui.info(f"{len(snap):,} stocks with data -> {len(liquid):,} pass price/liquidity "
                f"-> {len(candidates):,} pass market-cap filter (penny/illiquid removed)")
    return scoring.score_all(candidates, cfg)


def save_csv(df: pd.DataFrame, name: str, **kwargs) -> str:
    OUTPUT.mkdir(exist_ok=True)
    path = OUTPUT / f"{name}_{date.today().isoformat()}.csv"
    df.to_csv(path, **kwargs)
    return str(path)


def make_plan(cfg, ranked: pd.DataFrame, budget: float):
    """Returns (plans, text) - text is ready for the console or Telegram."""
    plans, notes = plan_budget(ranked, budget, cfg)
    lines = [f"TRADE PLAN for Rs {budget:,.0f}  (prices as of {ranked['last_date'].max()})"]
    lines += [f"- {n}" for n in notes]
    for i, p in enumerate(plans, 1):
        lines += [
            "",
            f"#{i} {p.symbol} ({p.name}) - {p.sector} - score {p.score}/100",
            f"   Why: {p.reasons}",
            f"   Buy {p.qty} share(s) at ~Rs {p.buy:,.2f} (limit order; don't pay above Rs {p.dont_buy_above:,.2f})",
            f"   Invested:  Rs {p.invested:,.2f}",
            f"   Target:    Rs {p.target:,.2f} (+{p.target_pct}%) -> expected profit Rs {p.net_profit:,.2f} after charges",
            f"   Stop-loss: Rs {p.stop:,.2f} (-{p.stop_pct}%) -> maximum loss Rs {p.max_loss:,.2f} incl. charges",
            f"   Charges (round trip): Rs {p.charges:,.2f} | Reward/risk {p.reward_risk} | "
            f"Typical daily move {p.atr_pct}%",
        ]
    if plans:
        lines += ["", f"If all targets hit: +Rs {sum(p.net_profit for p in plans):,.2f}. "
                      f"If all stops hit: -Rs {sum(p.max_loss for p in plans):,.2f}."]
    lines += ["", DISCLAIMER]
    return plans, "\n".join(lines)
