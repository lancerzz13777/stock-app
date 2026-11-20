"""Survivorship-free price history (Phase 4): every stock that traded on NSE each day, including the ones
that later delisted, went bust or merged - from NSE's own daily files, free.

Yahoo only knows stocks listed TODAY, so a backtest on it never holds a stock that later died: results look
better than reality ("survivorship bias"). These files fix that.

Prices are adjusted for splits and bonuses with NSE's own "previous close" (which NSE adjusts on the
ex-date): when it differs from the last close we stored, everything before that day is scaled by the ratio.
Limit: a stock that changed its symbol shows up as one stock ending and a new one starting.
"""
import io
import time
import zipfile
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from . import prices, ui

OLD_URL = "https://nsearchives.nseindia.com/content/historical/EQUITIES/{:%Y}/{}/cm{:%d}{}{:%Y}bhav.csv.zip"
NEW_FORMAT_FROM = date(2024, 7, 8)  # NSE switched file formats; later days come from prices.nse_day
SERIES = ["EQ", "BE", "BZ"]


def ensure(con):
    con.execute("CREATE TABLE IF NOT EXISTS pit_prices (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, "
                "close REAL, volume REAL, prev REAL, PRIMARY KEY (symbol, date))")


def day(d: date) -> pd.DataFrame | None:
    """One day's raw (unadjusted) prices for every equity, or None if NSE has no file."""
    if d >= NEW_FORMAT_FROM:
        df = prices.nse_day(d)
        return None if df is None else df[df["symbol"] != prices.NIFTY_SYMBOL]
    from .universe import HEADERS
    mon = d.strftime("%b").upper()
    r = requests.get(OLD_URL.format(d, mon, d, mon, d), headers=HEADERS, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    b = pd.read_csv(z.open(z.namelist()[0]))
    b = b[b["SERIES"].isin(SERIES)]
    return pd.DataFrame({"symbol": b["SYMBOL"], "date": d.isoformat(), "open": b["OPEN"], "high": b["HIGH"],
                         "low": b["LOW"], "close": b["CLOSE"], "volume": b["TOTTRDQTY"], "prev": b["PREVCLOSE"]})


def fill(con, years: int) -> int:
    """Downloads every trading day of the last `years` (+1 for the 52-week warm-up) we don't have yet."""
    ensure(con)
    since = (datetime.now() - timedelta(days=365 * (years + 1) + 60)).strftime("%Y-%m-%d")
    have = {r[0] for r in con.execute("SELECT DISTINCT date FROM pit_prices")}
    want = [d for (d,) in con.execute("SELECT date FROM prices WHERE symbol='^NSEI' AND date >= ? ORDER BY date",
                                      (since,)) if d not in have]
    added = 0
    with ui.progress() as bar:
        task = bar.add_task("NSE history incl. delisted stocks", total=len(want))
        for d in want:
            try:
                df = day(date.fromisoformat(d))
            except Exception as e:  # NSE hiccup: retried next run
                ui.warn(f"history {d}: {str(e)[:80]}")
                df = None
            if df is not None:
                cols = ["symbol", "date", "open", "high", "low", "close", "volume", "prev"]
                con.executemany("INSERT OR REPLACE INTO pit_prices VALUES (?,?,?,?,?,?,?,?)",
                                df[cols].itertuples(index=False, name=None))
                added += 1
                if added % 20 == 0:
                    con.commit()
            bar.advance(task)
            time.sleep(0.2)
    con.commit()
    return added


def coverage(con) -> tuple[str | None, int]:
    ensure(con)
    return con.execute("SELECT MIN(date), COUNT(DISTINCT date) FROM pit_prices").fetchone()


def adjust(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Split/bonus-adjusted wide tables from raw ones (needs 'prev'). Volume is scaled the other way."""
    close = raw["close"]
    last = close.ffill().shift(1)  # last close before each day (suspended days carried over)
    ratio = (raw["prev"] / last).where(lambda r: (r - 1).abs() > 0.005).clip(0.02, 50).fillna(1.0)
    # factor for day t = product of the ratios of every LATER day: scale history to today's share count
    factor = ratio.iloc[::-1].cumprod().iloc[::-1].shift(-1).fillna(1.0)
    out = {k: raw[k] * factor for k in ("close", "high", "low")}
    out["volume"] = raw["volume"] / factor
    return out


def load_wide(con, years: int) -> dict[str, pd.DataFrame]:
    """Same shape as strategies.load_wide (close/high/low/volume, rows = dates), but every stock ever traded."""
    since = (pd.Timestamp.today() - pd.DateOffset(years=years + 1, days=60)).strftime("%Y-%m-%d")
    raw = {}
    for col in ("close", "high", "low", "volume", "prev"):  # one column at a time: keeps peak memory low
        df = pd.read_sql_query(f"SELECT symbol, date, {col} FROM pit_prices WHERE date >= ?", con, params=(since,))
        w = df.pivot(index="date", columns="symbol", values=col).astype("float32")
        w.index = pd.to_datetime(w.index)
        raw[col] = w
        del df
    out = adjust(raw)
    nifty = pd.read_sql_query("SELECT date, close FROM prices WHERE symbol='^NSEI' AND date >= ?", con, params=(since,))
    out["close"]["^NSEI"] = pd.Series(nifty["close"].values, index=pd.to_datetime(nifty["date"]), dtype="float32")
    return out
