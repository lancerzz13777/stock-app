"""Shared trade mechanics used by both the backtest and paper trading, so they behave identically.

Rules (daily bars, conservative):
- Buy at the next day's OPEN, only if it's not above "don't buy above" and not already below the stop.
- If a day's range touches both stop and target, assume the STOP happened first.
- If the price gaps past the stop/target at the open, the exit is at the open (gaps hurt for real).
- After `max_hold_days` trading days, sell at the close.
"""


def entry_price(plan: dict, bar: dict) -> float | None:
    """Price we'd actually get at this bar's open, or None if we should skip the trade."""
    o = bar["open"]
    if o > plan["dont_buy_above"] or o <= plan["stop"]:
        return None
    return o


def check_exit(pos: dict, bar: dict, days_held: int, max_hold_days: int) -> tuple[float, str] | None:
    """Returns (exit_price, reason) if the position closes on this bar."""
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    if o <= pos["stop"]:
        return o, "stop (gap down)"
    if o >= pos["target"]:
        return o, "target (gap up)"
    if l <= pos["stop"]:
        return pos["stop"], "stop"
    if h >= pos["target"]:
        return pos["target"], "target"
    if days_held >= max_hold_days:
        return c, "time limit"
    return None
