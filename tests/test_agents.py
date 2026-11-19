"""Trading agents on made-up candles.  Run with:  python -m unittest discover tests"""
import sys
import unittest
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import agents
from screener.config import load_config

DAY = date(2026, 11, 20)


def candles(prices, volume=1000, day=DAY):
    idx = pd.date_range(f"{day} 09:15", periods=len(prices), freq="5min")
    return pd.DataFrame({"open": [prices[0]] + list(prices[:-1]), "high": [p * 1.001 for p in prices], "low": [p * 0.999 for p in prices],
                         "close": prices, "volume": volume}, index=idx)


def two_days(today, volume_today=1000):
    y = candles([100.0] * 75, day=date(2026, 11, 19))
    t = candles(today, volume=volume_today)
    return pd.concat([y, t])


class Agents(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def run_agent(self, key, bars, picks=()):
        return agents.run_day(agents.BY_KEY[key], agents.prepare(bars, DAY), set(picks), self.cfg)

    def test_quant_bot_never_spends_more_than_10000_and_closes_at_315(self):
        bars = {f"S{i}": two_days([100.0 + 0.01 * k for k in range(75)]) for i in range(6)}
        trades = self.run_agent("quant", bars, picks=bars)
        self.assertEqual(len(trades), 4)                                    # its 4-trade limit
        self.assertLessEqual(sum(t["qty"] * t["entry"] for t in trades), agents.DAY_CAP)
        self.assertTrue(all(t["reason"] == "closed 3:15" and t["exit_time"] == "15:15" for t in trades))

    def test_stop_loss_sells_on_the_way_down(self):
        path = [100.0 + 0.01 * k for k in range(4)] + [100.05 - 0.2 * k for k in range(15)] + [97.0] * 56  # buy at 9:30, then a slow 3% fall
        trades = self.run_agent("quant", {"X": two_days(path)}, picks={"X"})
        self.assertEqual(trades[0]["reason"], "stop")
        self.assertLess(trades[0]["pnl"], 0)
        self.assertGreater(trades[0]["pnl"], -2500 * 0.02)                  # ~1.5% of Rs 2,500 plus charges

    def test_breakout_needs_volume_and_a_range_break(self):
        flat = two_days([100.0] * 75)
        self.assertEqual(self.run_agent("breakout", {"X": flat}), [])
        path = [100.0] * 3 + [100.0] * 3 + [101.0] + [103.0] * 68
        vol = [1000] * 6 + [5000] + [1000] * 68
        trades = self.run_agent("breakout", {"X": two_days(path, vol)})
        self.assertEqual(trades[0]["reason"], "target")
        self.assertGreater(trades[0]["pnl"], 0)


if __name__ == "__main__":
    unittest.main()
