"""Buying, selling, holidays and run-once failures, on a throwaway database. Nothing is sent to Telegram.

Run with:  python -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screener import bot as botmod, db, telegram, wallet
from screener.config import load_config
from screener.intraday import IST

OPEN = datetime(2026, 11, 2, 11, 0, tzinfo=IST)     # a Monday, market open
EVENING = datetime(2026, 11, 2, 19, 0, tzinfo=IST)  # after the 18:30 report time
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
    def tick(self, at):
        self.now = at
        with mock.patch.object(botmod, "datetime", mock.Mock(wraps=datetime, now=lambda tz=None: at,
                                                             fromisoformat=datetime.fromisoformat)), \
                mock.patch.object(self.bot, "news_scan"):
            self.bot.tick()

    def boom(self):
        raise RuntimeError("Yahoo said no")

    def test_failed_job_fails_the_run_and_the_evening_job_retries(self):
        with mock.patch.object(self.bot, "daily_job", self.boom), self.assertRaises(SystemExit) as e:
            self.tick(EVENING)
        self.assertEqual(e.exception.code, 1)

        self.bot.job_failed = False  # a real next run is a new process
        done = mock.Mock()
        with mock.patch.object(self.bot, "daily_job", done):
            self.tick(EVENING + timedelta(minutes=10))   # too soon: waits 30 minutes between tries
            done.assert_not_called()
            self.tick(EVENING + timedelta(minutes=31))
        done.assert_called_once()


class EveningJob(BotCase):
    def run_job(self, at, nse):
        self.now = at
        with mock.patch.object(botmod, "datetime", mock.Mock(wraps=datetime, now=lambda tz=None: at)), \
                mock.patch.object(botmod.prices, "nse_day", lambda day: nse), \
                mock.patch.object(botmod.pipeline, "update_all") as update:
            self.bot.daily_job()
        return update

    def test_waits_for_nse_closes_then_calls_it_a_holiday_late_at_night(self):
        update = self.run_job(EVENING, None)
        update.assert_not_called()
        self.assertIsNone(self.bot.state_get("daily_done"))     # retried later
        self.run_job(EVENING.replace(hour=22, minute=5), None)
        self.assertEqual(self.bot.state_get("daily_done"), EVENING.date().isoformat())
        self.assertIn("holiday", self.sent[-1])


class NseFiles(unittest.TestCase):
    def test_reads_official_closes_and_the_nifty(self):
        import io, zipfile
        from screener import prices
        bhav = ("TradDt,TckrSymb,SctySrs,OpnPric,HghPric,LwPric,ClsPric,PrvsClsgPric,TtlTradgVol\n"
                "2026-11-05,SBIN,EQ,990,1009.6,987.2,1006.4,979,25273153\n"
                "2026-11-05,SOMEBOND,GS,100,100,100,100,100,5\n")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("bhav.csv", bhav)
        index = ("Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,Closing Index Value\n"
                 "Nifty 50,05-11-2026,24378.05,24579.9,24296.3,24536.6\n")
        replies = iter([mock.Mock(status_code=200, ok=True, content=buf.getvalue(), raise_for_status=lambda: None),
                        mock.Mock(status_code=200, ok=True, content=index.encode())])
        with mock.patch.object(prices.requests, "get", lambda *a, **k: next(replies)):
            df = prices.nse_day(datetime(2026, 11, 5).date())
        prices._nse_days.clear()
        self.assertEqual(sorted(df["symbol"]), ["SBIN", "^NSEI"])               # bonds dropped
        self.assertEqual(df.set_index("symbol").loc["^NSEI", "close"], 24536.6)
        self.assertEqual(df.set_index("symbol").loc["SBIN", "close"], 1006.4)
        self.assertEqual(set(df["date"]), {"2026-11-05"})
        self.assertEqual(df.set_index("symbol").loc["SBIN", "prev"], 979)


class SplitCheck(BotCase):
    def test_a_split_marks_the_stock_for_a_fresh_history_and_saves_nse_closes(self):
        import pandas as pd
        from datetime import date
        from screener import prices
        day = date.today()
        prev = (day - timedelta(days=1)).isoformat()
        self.con.executemany("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)", [
            ("^NSEI", prev, 1, 1, 1, 24000, 0), ("SBIN", prev, 1, 1, 1, 1000, 1), ("TCS", prev, 1, 1, 1, 2500, 1)])
        nse = pd.DataFrame({"symbol": ["SBIN", "TCS", "^NSEI"], "date": day.isoformat(), "open": 1.0, "high": 1.0,
                            "low": 1.0, "close": [505.0, 2510.0, 24100.0], "volume": 1.0,
                            "prev": [500.0, 2500.0, float("nan")]})   # SBIN split 1:2, TCS a normal day
        with mock.patch.object(prices, "nse_day", lambda d: nse if d == day else None):
            found, adjusted = prices.fill_from_nse(self.con, ["SBIN", "TCS"], days=0)
        self.assertEqual((found, adjusted), (1, {"SBIN"}))
        self.assertEqual(self.con.execute("SELECT close FROM prices WHERE symbol='TCS' AND date=?",
                                          (day.isoformat(),)).fetchone()[0], 2510.0)


class PriceTables(BotCase):
    def test_fast_tables_match_the_old_four_pivots(self):
        import pandas as pd
        from screener import strategies
        rows = [(s, d, 1.0 + i, 2.0 + i, 0.5 + i, 1.5 + i, 100.0 * i)
                for i, (s, d) in enumerate([("A", "2026-11-03"), ("A", "2026-11-04"), ("B", "2026-11-04")])]
        self.con.executemany("INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
        new = strategies.load_wide(self.con, 1)
        df = pd.read_sql_query("SELECT symbol, date, high, low, close, volume FROM prices", self.con)
        for c in ("close", "high", "low", "volume"):
            old = df.pivot(index="date", columns="symbol", values=c).sort_index()
            old.index = pd.to_datetime(old.index)
            pd.testing.assert_frame_equal(new[c].astype("float64"), old, check_names=False)


if __name__ == "__main__":
    unittest.main()
