"""Daily price history (OHLCV) from Yahoo Finance via yfinance, cached in SQLite.

First run downloads the full history; later runs only fetch the last few days.
Prices are split/dividend adjusted. If a stock had a split since our last download,
its old stored prices no longer line up, so we detect that and re-download it fully.
"""
import time
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from . import ui


NIFTY_SYMBOL = "^NSEI"


def _yahoo(symbol: str) -> str:
    return symbol if symbol.startswith("^") else f"{symbol}.NS"  # "^NSEI" = Nifty 50 index


def _fetch(symbols: list[str], **kwargs) -> dict[str, pd.DataFrame]:
    """Downloads a batch. Returns {symbol: DataFrame[date, open, high, low, close, volume]}."""
    raw = yf.download(
        [_yahoo(s) for s in symbols],
        group_by="ticker",
        auto_adjust=True,
        threads=True,
        progress=False,
        **kwargs,
    )
    out = {}
    if raw is None or raw.empty:
        return out
    for s in symbols:
        t = _yahoo(s)
        try:
            df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
        except KeyError:
            continue
        df = df.dropna(subset=["Close"])
        if df.empty:
            continue
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        df.index = pd.to_datetime(df.index).strftime("%Y-%m-%d")
        out[s] = df
    return out


def _save(con, symbol: str, df: pd.DataFrame, replace_all: bool = False) -> None:
    if replace_all:
        con.execute("DELETE FROM prices WHERE symbol = ?", (symbol,))
    rows = [(symbol, d, *map(float, r)) for d, r in zip(df.index, df.itertuples(index=False))]
    con.executemany("INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def _batches(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def update_prices(con, symbols: list[str], cfg: dict) -> None:
    years = cfg["data"]["history_years"]
    batch_size = cfg["data"]["batch_size"]
    pause = cfg["data"]["pause_seconds"]

    last_dates = dict(con.execute("SELECT symbol, MAX(date) FROM prices GROUP BY symbol").fetchall())
    new = [s for s in symbols if s not in last_dates]
    # Always re-read the last week: replaces a half-day candle saved during market hours, and fills
    # days Yahoo had missing earlier (it sometimes publishes a day's candle late)
    stale = [s for s in symbols if s in last_dates]
    refetch = []

    with ui.progress() as bar:
        # 1) Full history for stocks we've never downloaded
        if new:
            task = bar.add_task(f"{years}y history (first run)", total=len(new))
            for batch in _batches(new, batch_size):
                for s, df in _fetch(batch, period=f"{years}y", interval="1d").items():
                    _save(con, s, df, replace_all=True)
                con.commit()
                bar.advance(task, len(batch))
                time.sleep(pause)

        # 2) Recent days for stocks we already have
        if stale:
            task = bar.add_task("latest prices", total=len(stale))
            for batch in _batches(stale, batch_size):
                start = min(last_dates[s] for s in batch)
                start = (date.fromisoformat(start) - timedelta(days=7)).isoformat()
                for s, df in _fetch(batch, start=start, interval="1d").items():
                    if _adjustment_changed(con, s, df):
                        refetch.append(s)
                    else:
                        _save(con, s, df)
                con.commit()
                bar.advance(task, len(batch))
                time.sleep(pause)

        # 3) Splits/bonuses/dividends changed the adjusted history -> download it again
        if refetch:
            task = bar.add_task("re-download after splits/dividends", total=len(refetch))
            for batch in _batches(refetch, batch_size):
                for s, df in _fetch(batch, period=f"{years}y", interval="1d").items():
                    _save(con, s, df, replace_all=True)
                con.commit()
                bar.advance(task, len(batch))
                time.sleep(pause)


def _adjustment_changed(con, symbol: str, new_df: pd.DataFrame) -> bool:
    """True if the overlapping day's close differs by >0.5% from what we stored."""
    for d, close in zip(new_df.index, new_df["close"]):
        row = con.execute("SELECT close FROM prices WHERE symbol=? AND date=?", (symbol, d)).fetchone()
        if row and row[0]:
            return abs(close / row[0] - 1) > 0.005
    return False


def load_recent_prices(con, days: int = 420) -> dict[str, pd.DataFrame]:
    """Loads the last `days` calendar days of prices for every symbol, for scoring."""
    since = (date.today() - timedelta(days=days)).isoformat()
    df = pd.read_sql_query(
        "SELECT * FROM prices WHERE date >= ? ORDER BY symbol, date", con, params=(since,)
    )
    return {s: g.set_index("date").drop(columns="symbol") for s, g in df.groupby("symbol")}
