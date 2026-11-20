"""Delivery %: the share of each day's traded shares that buyers actually took into their demat
(the rest was intraday churn). High or rising delivery is read as investors buying to hold.
Free from NSE's full bhavcopy, one file per trading day, from 14 Nov 2019 (earlier files don't exist)."""
import io
import time
from datetime import date

import pandas as pd
import requests

from . import ui

URL = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{:%d%m%Y}.csv"
START = "2019-11-14"


def ensure(con):
    con.execute("CREATE TABLE IF NOT EXISTS delivery (symbol TEXT, date TEXT, pct REAL, PRIMARY KEY (symbol, date))")


def fetch(day: date) -> pd.DataFrame | None:
    """EQ-series delivery % for one day; None if NSE has no file (holiday / not published)."""
    from .universe import HEADERS
    r = requests.get(URL.format(day), headers=HEADERS, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    df = pd.read_csv(io.BytesIO(r.content), skipinitialspace=True)
    df.columns = df.columns.str.strip()
    df = df[df["SERIES"].str.strip() == "EQ"]  # BE/BZ trade-for-trade stocks are 100% delivery by rule
    return pd.DataFrame({"symbol": df["SYMBOL"].str.strip(), "date": day.isoformat(),
                         "pct": pd.to_numeric(df["DELIV_PER"], errors="coerce")}).dropna()


def fill(con) -> int:
    """Downloads every trading day we don't have yet (the trading days are the Nifty's). Returns days added."""
    ensure(con)
    have = {r[0] for r in con.execute("SELECT DISTINCT date FROM delivery")}
    want = [d for (d,) in con.execute("SELECT date FROM prices WHERE symbol='^NSEI' AND date >= ? ORDER BY date",
                                      (START,)) if d not in have]
    added = 0
    with ui.progress() as bar:
        task = bar.add_task("delivery % (NSE)", total=len(want))
        for d in want:
            try:
                df = fetch(date.fromisoformat(d))
            except Exception as e:  # NSE hiccup: that day is retried next run
                ui.warn(f"delivery {d}: {str(e)[:80]}")
                df = None
            if df is not None:
                con.executemany("INSERT OR REPLACE INTO delivery VALUES (?, ?, ?)", df.itertuples(index=False, name=None))
                added += 1
                if added % 20 == 0:
                    con.commit()
            bar.advance(task)
            time.sleep(0.2)  # be polite to NSE
    con.commit()
    return added


def wide(con, since: str) -> pd.DataFrame:
    """Delivery % as a wide table (rows = dates, columns = symbols)."""
    ensure(con)
    # in chunks: millions of rows as text would need ~600 MB at once on the small laptop server
    parts = [ch.pivot(index="date", columns="symbol", values="pct").astype("float32") for ch in pd.read_sql_query(
        "SELECT symbol, date, pct FROM delivery WHERE date >= ? ORDER BY date", con, params=(since,), chunksize=500_000)]
    w = pd.concat(parts).groupby(level=0).first() if parts else pd.DataFrame(dtype="float32")
    w.index = pd.to_datetime(w.index)
    return w
