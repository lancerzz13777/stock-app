"""Buying, selling, holidays and run-once failures, on a throwaway database. Nothing is sent to Telegram.

Run with:  python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import bot as botmod, db, telegram, wallet
from screener.config import load_config
from screener.intraday import IST

OPEN = datetime(2026, 11, 2, 11, 0, tzinfo=IST)     # a Monday, market open
EVENING = datetime(2026, 11, 2, 17, 0, tzinfo=IST)
PRICES = {"SBIN": 970.0, "TCS": 2500.0}


class BotCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.cfg = load_config()
        self.cfg["data"]["db_path"] = os.path.join(self.dir.name, "t.db")
        self.cfg["wallet"]["starting_cash"] = 10_000_000  # enough to buy far too many shares if a command is misread
        self.now = OPEN
        self.quote_time = None                           # None = quotes are from "now"
        self.sent = []
        for target, value in [
            (telegram, {"send": lambda text, **k: self.sent.append(text), "typing": lambda *a: None,
                        "configured": lambda: False}),
            (wallet, {"_now": lambda: self.now, "live_prices": self.quotes}),
            (botmod, {"log": lambda text: None}),
        ]:
            p = mock.patch.multiple(target, **value)
            p.start()
            self.addCleanup(p.stop)
        self.con = db.connect(self.cfg["data"]["db_path"])
        self.addCleanup(self.dir.cleanup)
        self.addCleanup(self.con.close)
        self.bot = botmod.Bot(self.cfg, self.con)
        self.bot._sym_cache = set(PRICES)

    def quotes(self, symbols):
        return {s: (PRICES[s], self.quote_time or self.now) for s in symbols if s in PRICES}


class Buying(BotCase):
    def buy(self, text):
        self.bot.cmd_buy(text.split())
        return wallet.open_positions(self.con)[-1]

    def test_rupees_below_one_share_price_are_not_read_as_shares(self):
        # Fails with a share count of 1500: this rupee amount can't buy even 1 share
        self.bot._handle("/buy TCS 1500".split(), "/buy")
        self.assertEqual(wallet.open_positions(self.con), [])
        self.assertIn("can't buy even 1 share", self.sent[-1] if self.sent else "")

    def test_small_number_means_shares(self):
        self.assertEqual(self.buy("/buy 2 SBIN")["qty"], 2)

    def test_the_word_shares_means_shares(self):
        self.assertEqual(self.buy("/buy 150 shares of SBIN")["qty"], 150)

    def test_amount_buys_what_it_can_afford(self):
        self.assertEqual(self.buy("/buy SBIN 2000")["qty"], 2)


class Selling(BotCase):
    def test_sell_closes_every_trade_of_that_stock_and_returns_money(self):
        start = wallet.cash(self.con)
        self.bot.cmd_buy("/buy SBIN 1000".split())
        self.bot.cmd_buy("/buy SBIN 1000".split())
        self.bot.cmd_exit("/exit SBIN".split())
        self.assertEqual(wallet.open_positions(self.con), [])
        self.assertLess(wallet.cash(self.con), start)            # charges were paid
        self.assertGreater(wallet.cash(self.con), start - 50)

    def test_never_sells_a_different_stock_than_named(self):
        self.bot.cmd_buy("/buy SBIN 1000".split())
        self.bot.cmd_exit("/exit TCS".split())
        self.assertEqual(len(wallet.open_positions(self.con)), 1)


class Holiday(BotCase):
    def test_old_prices_do_not_fill_orders(self):
        self.quote_time = datetime(2026, 10, 30, 15, 29, tzinfo=IST)
        self.bot.cmd_buy("/buy SBIN 2000".split())
        self.assertEqual(len(wallet.queued_orders(self.con)), 1)
        wallet.fill_queued(self.con, self.cfg)
        self.assertEqual(wallet.open_positions(self.con), [])

        self.quote_time = None
        wallet.fill_queued(self.con, self.cfg)
        self.assertEqual(len(wallet.open_positions(self.con)), 1)


class RunOnce(BotCase):
    def tick(self):
        self.now = EVENING
        with mock.patch.object(botmod, "datetime", mock.Mock(wraps=datetime, now=lambda tz=None: EVENING,
                                                             fromisoformat=datetime.fromisoformat)), \
                mock.patch.object(self.bot, "news_scan"):
            self.bot.tick()

    def boom(self):
        raise RuntimeError("Yahoo said no")

    def test_failed_job_fails_the_run_and_the_evening_job_retries(self):
        with mock.patch.object(self.bot, "daily_job", self.boom), self.assertRaises(SystemExit) as e:
            self.tick()
        self.assertEqual(e.exception.code, 1)
        self.assertNotEqual(self.bot.state_get("daily_done"), EVENING.date().isoformat())

        self.bot.job_failed = False  # a real next run is a new process
        done = mock.Mock()
        with mock.patch.object(self.bot, "daily_job", done):
            self.tick()
        done.assert_called_once()
        self.assertEqual(self.bot.state_get("daily_done"), EVENING.date().isoformat())


if __name__ == "__main__":
    unittest.main()
