"""Quant engine maths on made-up numbers.  Run with:  python -m unittest discover tests"""
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import quant
from screener.config import load_config

DATES = pd.bdate_range("2020-01-01", periods=12)
STOCKS = [f"S{i}" for i in range(60)]


def wide(values):
    return pd.DataFrame(values, index=DATES, columns=STOCKS)


class Quant(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(1)
        self.fwd = wide(rng.normal(0, 0.05, (12, 60)))
        self.mask = wide(True)

    def test_a_perfect_score_has_ic_one_and_noise_has_ic_near_zero(self):
        self.assertTrue(np.allclose(quant.rank_ic(self.fwd, self.fwd, self.mask), 1))
        noise = wide(np.random.default_rng(2).normal(size=(12, 60)))
        self.assertLess(abs(quant.ic_stats(quant.rank_ic(noise, self.fwd, self.mask))["ic"]), 0.15)

    def test_more_ideas_tried_means_less_trust(self):
        r = pd.Series(np.random.default_rng(3).normal(0.01, 0.04, 60))
        few, many = [0.0, 0.1], list(np.random.default_rng(4).normal(0, 0.15, 50))
        self.assertGreater(quant.deflated_sharpe(r, few), quant.deflated_sharpe(r, many))

    def test_flat_market_loses_only_the_charges(self):
        cfg = load_config()
        eq = quant.portfolio(wide(np.tile(np.arange(60.0), (12, 1))), wide(0.0), self.mask, cfg, 10)
        self.assertLess(eq.iloc[0], cfg["lab"]["capital"])        # paid to buy the first 10
        self.assertEqual(eq.iloc[-1], eq.iloc[0])                 # same 10 kept: no more charges


class FullHistory(unittest.TestCase):
    def test_split_is_found_from_the_jump_at_the_open(self):
        from screener import pit
        idx = pd.bdate_range("2024-01-01", periods=4)
        w = lambda v: pd.DataFrame({"X": v}, index=idx)
        raw = {"close": w([1000.0, 1010, 505, 510]), "high": w([1000.0, 1010, 505, 510]),
               "low": w([1000.0, 1010, 505, 510]), "volume": w([10.0, 10, 20, 20]),
               "open": w([1000.0, 1005, 505, 506])}         # 1:2 split on day 3: it opens at half the last close
        adj = pit.adjust(raw)
        self.assertEqual(adj["close"]["X"].tolist(), [500.0, 505.0, 505.0, 510.0])
        self.assertEqual(adj["volume"]["X"].tolist(), [20.0, 20.0, 20.0, 20.0])


class Delivery(unittest.TestCase):
    def test_reads_nse_full_bhavcopy_keeping_eq_only(self):
        from datetime import date
        from unittest import mock
        from screener import delivery
        csv = ("SYMBOL, SERIES, DATE1, CLOSE_PRICE, DELIV_QTY, DELIV_PER\n"
               "SBIN, EQ, 19-Nov-2026, 970.00, 100, 41.57\n"
               "TINYCO, BE, 19-Nov-2026, 12.00, 50, 100.00\n"      # trade-for-trade: always 100%, dropped
               "NEWCO, EQ, 19-Nov-2026, 50.00, -, -\n")            # no delivery figure, dropped
        reply = mock.Mock(status_code=200, content=csv.encode(), raise_for_status=lambda: None)
        with mock.patch.object(delivery.requests, "get", lambda *a, **k: reply):
            df = delivery.fetch(date(2026, 11, 19))
        self.assertEqual(df.values.tolist(), [["SBIN", "2026-11-19", 41.57]])


if __name__ == "__main__":
    unittest.main()
