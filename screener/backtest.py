"""Stage 4: replay the strategy over past years and compare it with simply holding the Nifty.

What is tested: the price filters, the TECHNICAL scoring rules, the budget planner
(shares, stop, target), real charges, and the trade mechanics in trading.py.

What is NOT tested (be aware):
- Fundamentals and news: free sources only give today's values, and using them for
  2016 would be cheating (look-ahead bias). So the backtest uses technical rules only.
- Survivorship bias: only stocks listed TODAY are included. Companies that went bust
  are missing, which makes results look somewhat better than reality.
"""
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .charges import net_pnl, side_charges
from .indicators import add_indicators
from .planner import plan_trade
from .scoring import TECHNICAL_RULES, max_points, points_from_hits, price_filter_mask, technical_hits
from .trading import check_exit, entry_price

from .prices import NIFTY_SYMBOL as NIFTY


def _load_history(con, years: int) -> dict[str, pd.DataFrame]:
    since = (date.today() - timedelta(days=int(365.25 * years) + 400)).isoformat()  # +400d warm-up
    df = pd.read_sql_query("SELECT * FROM prices WHERE date >= ? ORDER BY symbol, date", con, params=(since,))
    return {s: g.set_index("date").drop(columns="symbol") for s, g in df.groupby("symbol")}


def _max_drawdown(equity: pd.Series) -> float:
    return float(((equity / equity.cummax()) - 1).min() * 100)


def _cagr(start: float, end: float, days: int) -> float:
    years = days / 365.25
    return ((end / start) ** (1 / years) - 1) * 100 if years > 0 and start > 0 and end > 0 else 0.0


def run_backtest(con, cfg: dict, sectors: dict[str, str]) -> dict:
    bt = cfg["backtest"]
    rules = cfg["scoring"]["rules"]
    tech_max = max_points(rules, TECHNICAL_RULES)
    history = _load_history(con, bt["years"])
    nifty = history.pop(NIFTY, None)
    if nifty is None or nifty.empty:
        raise RuntimeError("No Nifty data. Run:  python main.py update")

    start = (date.today() - timedelta(days=int(365.25 * bt["years"]))).isoformat()
    print(f"  Computing indicators for {len(history)} stocks...")
    bars, signals = {}, []
    for sym, df in history.items():
        if len(df) < 260:
            continue
        f = add_indicators(df)
        f["score"] = (points_from_hits(technical_hits(f, rules), rules) / tech_max * 100).clip(lower=0)
        ok = price_filter_mask(f, cfg) & f["sma200"].notna() & (f["score"] >= bt["min_tech_score"])
        ok &= f.index >= start
        if ok.any():
            s = f.loc[ok, ["close", "atr", "score", "avg_turnover20"]].copy()
            s["symbol"] = sym
            signals.append(s)
            bars[sym] = df[["open", "high", "low", "close"]]
    if not signals:
        raise RuntimeError("No signals at all - check the data or lower backtest.min_tech_score")
    sig = pd.concat(signals).sort_values(["score", "avg_turnover20"], ascending=False)
    by_day = {d: g for d, g in sig.groupby(level=0)}

    days = [d for d in nifty.index if d >= start]
    cash = float(bt["capital"])
    open_pos, pending, trades, equity = [], [], [], []
    charges_cfg = cfg["charges"]
    print(f"  Simulating {len(days)} trading days ({days[0]} to {days[-1]})...")

    for d in days:
        # 1) Yesterday's signals -> buy at today's open
        for plan in pending:
            bar = bars[plan["symbol"]].loc[d] if d in bars[plan["symbol"]].index else None
            if bar is None:
                continue
            price = entry_price(plan, bar)
            if price is None:
                continue
            cost = price * plan["qty"] + side_charges(price * plan["qty"], "buy", charges_cfg).total
            if cost > cash:
                continue
            cash -= cost
            open_pos.append({**plan, "entry_date": d, "entry": price, "days": 0})
        pending = []

        # 2) Exits (not on the day we bought)
        still_open = []
        for pos in open_pos:
            b = bars[pos["symbol"]]
            if d not in b.index or pos["entry_date"] == d:
                still_open.append(pos)
                continue
            pos["days"] += 1
            ex = check_exit(pos, b.loc[d], pos["days"], bt["max_hold_days"])
            if ex is None:
                still_open.append(pos)
                continue
            px, why = ex
            sell_value = px * pos["qty"]
            cash += sell_value - side_charges(sell_value, "sell", charges_cfg).total
            pnl = net_pnl(pos["entry"], px, pos["qty"], charges_cfg)
            trades.append({"symbol": pos["symbol"], "entry_date": pos["entry_date"], "exit_date": d,
                           "qty": pos["qty"], "entry": pos["entry"], "exit": round(px, 2),
                           "target": pos["target"], "stop": pos["stop"], "reason": why,
                           "days": pos["days"], "net_pnl": round(pnl, 2), "score": pos["score"]})
        open_pos = still_open

        # 3) Mark to market
        held = sum(bars[p["symbol"]].loc[:d]["close"].iloc[-1] * p["qty"] for p in open_pos)
        equity.append((d, cash + held))

        # 4) Today's close -> plan tomorrow's buys
        slots = bt["max_positions"] - len(open_pos)
        if slots > 0 and d in by_day:
            held_syms = {p["symbol"] for p in open_pos}
            for _, row in by_day[d].iterrows():
                if row["symbol"] in held_syms:
                    continue
                r = pd.Series({"close": row["close"], "atr": row["atr"], "score": row["score"],
                               "reasons": "", "name": row["symbol"],
                               "sector": sectors.get(row["symbol"], "Unknown")}, name=row["symbol"])
                plan, _ = plan_trade(r, min(bt["trade_budget"], cash), cfg)
                if plan is None:
                    continue
                pending.append({"symbol": plan.symbol, "qty": plan.qty, "target": plan.target,
                                "stop": plan.stop, "dont_buy_above": plan.dont_buy_above,
                                "score": plan.score})
                held_syms.add(plan.symbol)
                if len(pending) == slots:
                    break

    eq = pd.Series(dict(equity))
    eq.index = pd.to_datetime(eq.index)
    nifty_close = nifty["close"].loc[days[0]:]
    nifty_close.index = pd.to_datetime(nifty_close.index)
    nifty_eq = nifty_close / nifty_close.iloc[0] * bt["capital"]
    return _summarize(pd.DataFrame(trades), eq, nifty_eq, bt)


def _summarize(trades: pd.DataFrame, eq: pd.Series, nifty_eq: pd.Series, bt: dict) -> dict:
    days = (eq.index[-1] - eq.index[0]).days
    s = {
        "period": f"{eq.index[0].date()} to {eq.index[-1].date()}",
        "trades": len(trades),
        "capital": bt["capital"],
        "final_equity": round(eq.iloc[-1], 2),
        "total_return_pct": round((eq.iloc[-1] / bt["capital"] - 1) * 100, 2),
        "cagr_pct": round(_cagr(bt["capital"], eq.iloc[-1], days), 2),
        "max_drawdown_pct": round(_max_drawdown(eq), 2),
        "nifty_final": round(nifty_eq.iloc[-1], 2),
        "nifty_return_pct": round((nifty_eq.iloc[-1] / bt["capital"] - 1) * 100, 2),
        "nifty_cagr_pct": round(_cagr(bt["capital"], nifty_eq.iloc[-1], days), 2),
        "nifty_max_drawdown_pct": round(_max_drawdown(nifty_eq), 2),
    }
    if len(trades):
        wins, losses = trades[trades.net_pnl > 0], trades[trades.net_pnl <= 0]
        s.update({
            "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
            "avg_win": round(wins.net_pnl.mean(), 2) if len(wins) else 0.0,
            "avg_loss": round(losses.net_pnl.mean(), 2) if len(losses) else 0.0,
            "avg_per_trade": round(trades.net_pnl.mean(), 2),
            "avg_days_held": round(trades.days.mean(), 1),
            "exit_reasons": trades.reason.value_counts().to_dict(),
        })
    yearly = pd.DataFrame({
        "strategy_%": eq.groupby(eq.index.year).agg(lambda x: (x.iloc[-1] / x.iloc[0] - 1) * 100),
        "nifty_%": nifty_eq.groupby(nifty_eq.index.year).agg(lambda x: (x.iloc[-1] / x.iloc[0] - 1) * 100),
    }).round(1)
    return {"summary": s, "trades": trades, "equity": eq, "nifty": nifty_eq, "yearly": yearly}


def verdict(s: dict) -> list[str]:
    out = []
    if s["trades"] < 30:
        out.append(f"Only {s['trades']} trades - too few to trust either way. Treat as NOT proven.")
    if s["cagr_pct"] > s["nifty_cagr_pct"]:
        out.append(f"Strategy beat buy-and-hold Nifty ({s['cagr_pct']}% vs {s['nifty_cagr_pct']}% a year).")
        out.append("Next: paper-trade it for 2-3 months (python main.py paper) before real money.")
    else:
        out.append(f"Strategy did NOT beat simply holding a Nifty index fund "
                   f"({s['cagr_pct']}% vs {s['nifty_cagr_pct']}% a year).")
        out.append("Honest takeaway: a Nifty 50 index fund SIP would have done better with zero effort.")
    if np.isfinite(s["max_drawdown_pct"]) and s["max_drawdown_pct"] < s["nifty_max_drawdown_pct"]:
        out.append(f"Its worst fall ({s['max_drawdown_pct']}%) was deeper than the Nifty's "
                   f"({s['nifty_max_drawdown_pct']}%).")
    return out
