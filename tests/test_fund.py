"""Live paper fund (quant Phase 5) on a throwaway database.  Run with:  python -m unittest discover tests"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import db, fund
from screener.config import load_config

DAYS = [d.date().isoformat() for d in pd.bdate_range("2026-01-01", periods=50)]
STOCKS = [f"S{i:02d}" for i in range(60)]


class Fund(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.cfg = load_config()
        self.con = db.connect(os.path.join(self.dir.name, "t.db"))
        self.addCleanup(self.con.close)
        # stock i grows i/1000 a day: a higher number really does do better
        rows = [(s, d, 1, 1, 1, 100 * (1 + i / 1000) ** k, 1e6) for i, s in enumerate(STOCKS) for k, d in enumerate(DAYS)]
        rows += [("^NSEI", d, 1, 1, 1, 20000 + k, 1) for k, d in enumerate(DAYS)]
        self.con.executemany("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        fund.ensure(self.con)

    def research(self, best_first):
        """A plan as the PC sends it (through JSON, as on the real wire)."""
        import json
        latest = pd.Series(range(len(best_first), 0, -1), index=best_first, dtype=float)
        r = {"latest": latest, "vol_latest": pd.Series(0.02, index=best_first), "sectors": {}, "as_of": DAYS[0],
             "spec": {"n": 10, "calmer": False, "per_sector": None}, "picks": best_first[:10], "combo_test": {"ic": 0.06},
             "learned_test": {"ic": 0.07}, "use_learned": True, "best": "Top 10", "port": {"test": {}}, "dsr": float("nan")}
        return json.loads(json.dumps(fund.make_plan(r)))

    def test_server_waits_for_a_fresh_plan_from_the_pc(self):
        from screener.db import set_state
        import json
        msg = fund.evening(self.con, self.cfg, DAYS[0])
        self.assertIn("waiting for fresh picks", msg)
        self.assertEqual(fund._positions(self.con), {})
        set_state(self.con, "fund_plan", json.dumps(self.research(STOCKS[::-1])))
        self.assertIn("rebalanced", fund.evening(self.con, self.cfg, DAYS[1]))
        self.assertEqual(len(fund._positions(self.con)), 10)

    def test_buys_the_top_ten_pays_charges_and_waits_a_month(self):
        self.assertTrue(fund.due(self.con, DAYS[0]))
        fund.rebalance(self.con, self.cfg, self.research(STOCKS[::-1]), DAYS[0])
        held = fund._positions(self.con)
        self.assertEqual(sorted(held), sorted(STOCKS[-10:]))
        fund.mark(self.con, self.cfg, DAYS[0])
        nav = self.con.execute("SELECT nav FROM fund_nav").fetchone()[0]
        self.assertLess(nav, self.cfg["fund"]["capital"])           # charges were paid
        self.assertGreater(nav, self.cfg["fund"]["capital"] * 0.99)
        self.assertGreaterEqual(fund._cash(self.con, self.cfg), 0)
        self.assertFalse(fund.due(self.con, DAYS[20]))
        self.assertTrue(fund.due(self.con, DAYS[21]))

    def test_live_ic_is_measured_a_month_later_and_dropped_stocks_are_sold(self):
        fund.rebalance(self.con, self.cfg, self.research(STOCKS[::-1]), DAYS[0])
        fund.mark(self.con, self.cfg, DAYS[20])
        self.assertIsNone(self.con.execute("SELECT ic FROM fund_ic").fetchone())   # month not over yet
        fund.mark(self.con, self.cfg, DAYS[21])
        self.assertAlmostEqual(self.con.execute("SELECT ic FROM fund_ic").fetchone()[0], 1.0)  # ranking was perfect

        moved = fund.rebalance(self.con, self.cfg, self.research(STOCKS), DAYS[21])  # ranking flipped
        self.assertEqual(len(moved["sold"]), 10)
        self.assertEqual(sorted(fund._positions(self.con)), STOCKS[:10])
        s = fund.summary(self.con, self.cfg)
        self.assertEqual(len(s["holdings"]), 10)
        self.assertEqual(s["live_ic"], 1.0)


class ServerCare(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.con = db.connect(os.path.join(self.dir.name, "t.db"))
        self.addCleanup(self.con.close)

    def test_backup_keeps_the_tables_that_cant_be_downloaded_again(self):
        import sqlite3
        from screener import agents
        agents.ensure(self.con)
        self.con.execute("INSERT INTO agent_trades (day, agent, symbol, mode) VALUES ('2026-11-20', 'quant', 'SBIN', 'live')")
        self.con.execute("INSERT INTO prices VALUES ('SBIN', '2026-11-20', 1, 1, 1, 1, 1)")
        path = os.path.join(self.dir.name, "b.sqlite")
        fund.backup(self.con, path)
        b = sqlite3.connect(path)
        self.assertEqual(b.execute("SELECT symbol FROM agent_trades").fetchall(), [("SBIN",)])
        self.assertNotIn("prices", {r[0] for r in b.execute("SELECT name FROM sqlite_master")})  # re-downloadable
        b.close()

    def test_health_flags_a_dead_main_loop(self):
        from screener.intraday import IST
        self.con.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)")
        old = (pd.Timestamp.now(tz=IST) - pd.Timedelta(hours=2)).isoformat()
        self.con.execute("INSERT INTO bot_state VALUES ('heartbeat', ?)", (old,))
        self.assertTrue(any("main loop" in x for x in fund.health(self.con)))
        self.con.execute("UPDATE bot_state SET value=? WHERE key='heartbeat'", (pd.Timestamp.now(tz=IST).isoformat(),))
        self.assertFalse(any("main loop" in x for x in fund.health(self.con)))

    def test_dashboard_needs_the_key_only_from_the_internet(self):
        from screener import web
        k = web.key(self.con)
        self.assertEqual(web.key(self.con), k)                                    # made once, then kept
        net = {"Cf-Connecting-Ip": "1.2.3.4"}
        self.assertEqual(web.allowed({}, {}, k), (True, False))                   # home Wi-Fi
        self.assertEqual(web.allowed(net, {}, k), (False, False))                 # internet, no key
        self.assertEqual(web.allowed(net, {"k": ["wrong"]}, k), (False, False))
        self.assertEqual(web.allowed(net, {"k": [k]}, k), (True, True))           # Telegram link: sets the cookie
        self.assertEqual(web.allowed({**net, "Cookie": f"a=1; k={k}"}, {}, k), (True, False))

    def test_nse_dates_and_numbers_are_read(self):
        from screener import flows
        self.assertEqual(flows._day("20-NOV-2026"), "2026-11-20")
        self.assertEqual(flows._day("04-Oct-2025 18:13"), "2025-10-04")
        self.assertIsNone(flows._day("-"))
        self.assertEqual(flows._num("1,234.5"), 1234.5)
        self.assertIsNone(flows._num("-"))


if __name__ == "__main__":
    unittest.main()
