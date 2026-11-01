"""Indian delivery trade charges. All rates come from the `charges` section of config.yaml."""
from dataclasses import dataclass


@dataclass
class Charges:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0
    dp: float = 0.0

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst + self.dp


def side_charges(value: float, side: str, c: dict) -> Charges:
    """Charges for one order. side is 'buy' or 'sell'. value = price x quantity."""
    brokerage = value * c["brokerage_pct"] / 100
    brokerage = min(max(brokerage, c["brokerage_min_per_order"]), c["brokerage_max_per_order"])
    # Delivery charges STT on both sides (stt_pct); intraday only on the sell side (stt_sell_pct)
    stt_pct = c.get(f"stt_{side}_pct", c.get("stt_pct", 0))
    ch = Charges(
        brokerage=brokerage,
        stt=round(value * stt_pct / 100),  # STT is rounded to the nearest rupee
        exchange=value * c["exchange_txn_pct"] / 100,
        sebi=value * c["sebi_fee_pct"] / 100,
        stamp=value * c["stamp_duty_buy_pct"] / 100 if side == "buy" else 0.0,
        dp=c.get("dp_charge_per_sell", 0) if side == "sell" else 0.0,
    )
    ch.gst = (ch.brokerage + ch.exchange + ch.sebi) * c["gst_pct"] / 100
    return ch


def round_trip_cost(buy_price: float, sell_price: float, qty: int, c: dict) -> float:
    return side_charges(buy_price * qty, "buy", c).total + side_charges(sell_price * qty, "sell", c).total


def net_pnl(buy_price: float, sell_price: float, qty: int, c: dict) -> float:
    """Profit (or loss, if negative) in Rs after every charge."""
    return (sell_price - buy_price) * qty - round_trip_cost(buy_price, sell_price, qty, c)
