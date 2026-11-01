"""Stage 3: turn a budget into 1-2 concrete, affordable trade plans."""
import math
from dataclasses import dataclass

import pandas as pd

from .charges import net_pnl, round_trip_cost

TICK = 0.05  # NSE price step


def _tick_down(p: float) -> float:
    return math.floor(p / TICK) * TICK


def _tick_up(p: float) -> float:
    return math.ceil(p / TICK) * TICK


@dataclass
class TradePlan:
    symbol: str
    name: str
    sector: str
    score: int
    reasons: str
    qty: int
    buy: float
    dont_buy_above: float
    target: float
    stop: float
    invested: float
    charges: float
    net_profit: float
    max_loss: float
    reward_risk: float
    atr_pct: float
    target_pct: float
    stop_pct: float


def _min_target_for_profit(buy: float, qty: int, want: float, c: dict) -> float:
    """Lowest sell price that leaves at least `want` Rs after charges (binary search)."""
    lo, hi = buy, buy * 2
    for _ in range(50):
        mid = (lo + hi) / 2
        if net_pnl(buy, mid, qty, c) >= want:
            hi = mid
        else:
            lo = mid
    return hi


def plan_trade(r: pd.Series, money: float, cfg: dict) -> tuple[TradePlan | None, str]:
    """Returns (plan, "") or (None, reason it was rejected)."""
    p, c = cfg["planner"], cfg["charges"]
    buy = round(r.close, 2)
    qty = int(money // (buy * (1 + p["buy_slippage_pct"] / 100)))
    if qty < 1:
        return None, "too expensive for this budget"

    atr_target = buy + p["target_atr_multiple"] * r.atr
    if (atr_target / buy - 1) * 100 > p["max_target_pct"]:
        return None, f"too volatile for steady small wins: moves ~{r.atr / buy * 100:.1f}%/day"
    profit_target = _min_target_for_profit(buy, qty, p["min_net_profit"], c)
    if (profit_target / buy - 1) * 100 > p["max_target_pct"]:
        return None, (f"charges too big for this trade size: needs "
                      f"+{(profit_target / buy - 1) * 100:.1f}% to net Rs {p['min_net_profit']}")
    target = _tick_up(max(atr_target, profit_target))
    target_pct = (target / buy - 1) * 100

    stop_dist = min(p["stop_atr_multiple"] * r.atr, buy * p["max_stop_pct"] / 100)
    stop = _tick_down(buy - stop_dist)

    profit = net_pnl(buy, target, qty, c)
    loss = -net_pnl(buy, stop, qty, c)
    rr = profit / loss if loss > 0 else float("inf")
    if rr < p["min_reward_risk"]:
        return None, f"reward/risk too low after charges: {rr:.2f} < {p['min_reward_risk']}"

    return TradePlan(
        # r.name is the row label (the symbol); r["name"] is the company name column
        symbol=r.name, name=r["name"] or r.name, sector=r["sector"] or "?",
        score=int(r.score), reasons=r.reasons, qty=qty, buy=buy,
        dont_buy_above=round(_tick_down(buy * (1 + p["buy_slippage_pct"] / 100)), 2),
        target=round(target, 2), stop=round(stop, 2), invested=round(buy * qty, 2),
        charges=round(round_trip_cost(buy, target, qty, c), 2),
        net_profit=round(profit, 2), max_loss=round(loss, 2), reward_risk=round(rr, 2),
        atr_pct=round(r.atr / buy * 100, 2), target_pct=round(target_pct, 2),
        stop_pct=round((1 - stop / buy) * 100, 2),
    ), ""


def _pick(ranked: pd.DataFrame, money: float, n: int, cfg: dict) -> list[TradePlan]:
    plans, sectors = [], set()
    for sym, r in ranked.iterrows():
        plan, _ = plan_trade(r, money, cfg)
        if plan is None or plan.sector in sectors:  # spread risk: different sectors
            continue
        plans.append(plan)
        if plan.sector not in ("Unknown", "?"):  # unknown sectors don't block each other
            sectors.add(plan.sector)
        if len(plans) == n:
            break
    return plans


def plan_budget(scored: pd.DataFrame, budget: float, cfg: dict) -> tuple[list[TradePlan], list[str]]:
    """Returns (1-2 plans, notes explaining the decision)."""
    p = cfg["planner"]
    notes = []
    good = scored[scored["score"] >= p["min_score"]]
    notes.append(f"{len(good)} stocks score >= {p['min_score']}; "
                 f"{(good['close'] <= budget).sum()} of them cost <= Rs {budget:,.0f} per share.")

    if budget < p["min_trade_value"]:
        notes.append(f"Budget is below Rs {p['min_trade_value']:,} - fixed charges (DP ~Rs "
                     f"{cfg['charges']['dp_charge_per_sell']}) would eat most of any profit. "
                     "Consider waiting and saving up.")

    plans = []
    if p["max_picks"] >= 2 and budget >= 2 * p["min_trade_value"]:
        plans = _pick(good, budget / 2, 2, cfg)
        if len(plans) < 2:
            plans = []
    if not plans:
        plans = _pick(good, budget, 1, cfg)

    if not plans:
        reasons = {}  # group by the part before ":" so similar reasons are counted together
        for _, r in good.head(30).iterrows():
            _, why = plan_trade(r, budget, cfg)
            kind, _, detail = why.partition(":")
            reasons.setdefault(kind, []).append(f"{r.name} ({detail.strip()})" if detail else r.name)
        notes.append("No stock passed every check today. Reasons:")
        for kind, items in sorted(reasons.items(), key=lambda x: -len(x[1])):
            notes.append(f"   {kind} - {len(items)} stock(s), e.g. {', '.join(items[:3])}")
        notes.append("That's a valid answer - no trade is better than a bad trade.")
    return plans, notes
