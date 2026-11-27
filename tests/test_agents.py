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
        self.assertLessEqual(sum(t["qty"] * t["entry"] for t in trades), agents.BY_KEY["quant"].cap)
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


class LiveMessages(unittest.TestCase):
    def test_live_record_is_never_wiped_and_refuses_hindsight_trades(self):
        import sqlite3
        con = sqlite3.connect(":memory:")
        t = lambda s, at, exit=None: {"symbol": s, "qty": 2, "entry": 100.0, "entry_time": at, "exit": exit,
                                      "exit_time": None if exit is None else "10:00", "reason": "open" if exit is None else "stop",
                                      "pnl": None if exit is None else -3.0, "context": {}}
        d = "2026-11-25"
        self.assertEqual(len(agents.save_live(con, d, "breakout", [t("A", "09:30")], "09:20")), 1)
        agents.save_live(con, d, "breakout", [], "09:40")                         # a broken replay finds nothing...
        self.assertEqual(con.execute("SELECT COUNT(*) FROM agent_trades").fetchone()[0], 1)  # ...the record stays
        got = agents.save_live(con, d, "breakout", [t("A", "09:30", 98.0), t("B", "09:30")], "10:45")
        self.assertEqual([x["symbol"] for x in got], ["A"])                      # B "bought at 9:30" seen at 11: refused
        agents.save_live(con, d, "breakout", [t("A", "09:30")], "10:50")          # revised candles can't reopen it
        self.assertEqual(con.execute("SELECT exit FROM agent_trades WHERE symbol='A'").fetchone()[0], 98.0)
        agents.save_live(con, d, "quant", [t("C", "09:30")], "09:20")
        today = {"C": candles([100.0] * 71 + [105.0] * 4, day=date(2026, 11, 25))}
        agents.close_leftovers(con, d, "quant", today, load_config())                # still open after 3:15: closed there
        exit_, reason, pnl = con.execute("SELECT exit, reason, pnl FROM agent_trades WHERE symbol='C'").fetchone()
        self.assertEqual(reason, "closed 3:15")
        self.assertAlmostEqual(exit_, 105 * (1 - agents.SLIPPAGE), places=2)
        self.assertGreater(pnl, 0)

    def test_each_trade_is_announced_once_even_after_a_restart(self):
        import os, tempfile
        from datetime import datetime
        from unittest import mock
        from screener import db, intraday, live
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        con = db.connect(os.path.join(d.name, "t.db"))
        self.addCleanup(con.close)
        bars = {"X": two_days([100.0 + 0.01 * k for k in range(75)])}
        at = lambda h, m: datetime(2026, 11, 20, h, m, tzinfo=intraday.IST)
        with mock.patch.object(live, "candles", lambda *a, **k: bars),                 mock.patch.object(intraday, "liquid_universe", lambda *a: ["X"]):
            bought = agents.live_cycle(con, load_config(), {"X"}, at(9, 36))
            closed = agents.live_cycle(con, load_config(), {"X"}, at(15, 31))
            again = agents.live_cycle(con, load_config(), {"X"}, at(15, 31))  # same as a restart: state is in the DB
        self.assertEqual(len(bought), 1)                                    # Quant Bot bought at 9:30
        self.assertEqual(len(closed), 1)                                    # and closed at 3:15
        self.assertEqual(again, [])


class Dashboard(unittest.TestCase):
    def test_totals_open_trades_and_valid_json(self):
        import json, os, tempfile
        from screener import db, web
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        con = db.connect(os.path.join(d.name, "t.db"))
        self.addCleanup(con.close)
        agents.save(con, "2026-11-20", "breakout", [
            {"symbol": "A", "qty": 10, "entry": 100.0, "entry_time": "09:30", "exit": 102.0, "exit_time": "10:00",
             "reason": "target", "pnl": 18.5},
            {"symbol": "B", "qty": 5, "entry": 200.0, "entry_time": "10:05", "exit": None, "exit_time": None,
             "reason": "open", "pnl": None}], "live")
        out = json.loads(json.dumps(web.data(con), allow_nan=False))       # NaN would make the page fail
        self.assertEqual(out["day"], "2026-11-20")
        self.assertEqual(len(out["trades"]), 2)
        self.assertEqual(sum(a["pnl"] for a in out["all_time"]), 18.5)    # the open trade isn't counted yet


class Learning(unittest.TestCase):
    def test_a_loss_pattern_seen_in_old_and_new_trades_is_avoided_and_explained(self):
        import json, os, tempfile
        from screener import db
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        con = db.connect(os.path.join(d.name, "t.db"))
        self.addCleanup(con.close)
        agents.ensure(con)
        rows = []
        for i in range(30):  # 30 days: wins when the market is up, losses when it is down
            day = f"2026-10-{i + 1:02d}"
            for mkt, pnl in (("market up", 40.0), ("market down", -60.0)):
                ctx = {"time": "9:30-10:30", "market": mkt, "move": "up 0-2%", "volume": "volume below 2x"}
                rows.append((day, "breakout", f"S{mkt[7]}", 1, 100, "10:00", 101, "11:00",
                             "stop" if pnl < 0 else "target", pnl + i % 3, "backtest", json.dumps(ctx)))
        con.executemany("INSERT INTO agent_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        les = agents.learn(con, "2026-11-05")["breakout"]
        self.assertIn(["market", "market down"], les["avoid"])
        self.assertNotIn(["market", "market up"], les["avoid"])
        self.assertTrue(any("market down" in w for w in les["why"]))
        self.assertTrue(any("'stop'" in w for w in les["why"]))
        self.assertEqual(agents.learn(con, "2026-10-01")["breakout"]["trades"], 0)  # never learns from the future


if __name__ == "__main__":
    unittest.main()
