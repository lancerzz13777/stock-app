"""Daily price history (OHLCV) from Yahoo Finance via yfinance, cached in SQLite.

First run downloads the full history; later runs only fetch the last few days.
Prices are split/dividend adjusted. If a stock had a split since our last download,
its old stored prices no longer line up, so we detect that and re-download it fully.
"""
import io
import time
import zipfile
from datetime import date, datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

from . import ui


NIFTY_SYMBOL = "^NSEI"
# NSE's free official end-of-day files (published around 6 PM). Yahoo is often a day late for NSE closes.
BHAVCOPY_URL = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{:%Y%m%d}_F_0000.csv.zip"
INDEX_URL = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{:%d%m%Y}.csv"
_nse_days: dict[date, pd.DataFrame] = {}


def nse_day(day: date) -> pd.DataFrame | None:
    """Official closes for one trading day: every equity plus the Nifty 50 (as ^NSEI).
    Columns symbol, date, open, high, low, close, volume, prev (NSE's previous close, which
    reflects splits and bonuses). None if NSE hasn't published that day."""
    if day in _nse_days:
        return _nse_days[day]
    from .universe import HEADERS
    r = requests.get(BHAVCOPY_URL.format(day), headers=HEADERS, timeout=30)
    if r.status_code == 404:  # holiday, weekend, or not published yet
        return None
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    b = pd.read_csv(z.open(z.namelist()[0]))
    b = b[b["SctySrs"].isin(["EQ", "BE", "BZ"])]
    out = pd.DataFrame({"symbol": b["TckrSymb"], "open": b["OpnPric"], "high": b["HghPric"], "low": b["LwPric"],
                        "close": b["ClsPric"], "volume": b["TtlTradgVol"], "prev": b["PrvsClsgPric"]})
    r = requests.get(INDEX_URL.format(day), headers=HEADERS, timeout=30)
    if r.ok:
        ix = pd.read_csv(io.BytesIO(r.content))
        n = ix[ix["Index Name"].str.strip() == "Nifty 50"]
        if len(n):
            n = n.iloc[0]
            # some days a few cells hold "-", which makes these columns text: read each value as a number
            out.loc[len(out)] = [NIFTY_SYMBOL, *(float(n[c]) for c in ("Open Index Value", "High Index Value",
                                 "Low Index Value", "Closing Index Value")), 0.0, float("nan")]
    out.insert(1, "date", day.isoformat())
    _nse_days[day] = out
    return out


def fill_from_nse(con, symbols: list[str], days: int = 10) -> tuple[int, set[str]]:
    """Writes NSE's official closes for the last `days` days (they replace Yahoo's for those days).
    Returns (trading days found, stocks with a split, bonus or big dividend: NSE's "previous close" no
    longer equals the close in NSE's file for the trading day before - their history must be downloaded again)."""
    from .intraday import IST
    want = set(symbols) | {NIFTY_SYMBOL}
    found, adjusted, before = 0, set(), None
    today = datetime.now(IST).date()  # the server's clock may be UTC
    for back in range(days, -1, -1):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:
            continue
        try:
            df = nse_day(d)
        except Exception as e:  # NSE down: Yahoo's prices stay
            ui.warn(f"NSE closes for {d}: {str(e)[:80]}")
            before = None       # can't compare the next day with a day we don't have
            continue
        if df is None:          # holiday or not published: the next day's "previous close" is still `before`'s
            continue
        found += 1
        rows = df[df["symbol"].isin(want)]
        if before is not None:
            last = dict(zip(before["symbol"], before["close"]))
            adjusted |= {s for s, p in zip(rows["symbol"], rows["prev"])
                         if last.get(s) and p > 0 and abs(last[s] / p - 1) > 0.005}
        before = df
        con.executemany("INSERT OR REPLACE INTO prices VALUES (?, ?, ?, ?, ?, ?, ?)",
                        rows.drop(columns="prev").itertuples(index=False, name=None))
    con.commit()
    return found, adjusted


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


def update_prices(con, symbols: list[str], cfg: dict, refetch=(), nse_ok: bool = False) -> None:
    """Yahoo fills what NSE's daily files can't: full history for new stocks, the re-download after a
    split (`refetch`), and any stock still behind. Without NSE's files (nse_ok False) it re-reads
    every stock's last days from Yahoo instead, which is much slower."""
    years = cfg["data"]["history_years"]
    batch_size = cfg["data"]["batch_size"]
    pause = cfg["data"]["pause_seconds"]

    last_dates = dict(con.execute("SELECT symbol, MAX(date) FROM prices GROUP BY symbol").fetchall())
    new = [s for s in symbols if s not in last_dates]
    upto = max(last_dates.values(), default="") if nse_ok else date.today().isoformat()
    refetch = [s for s in refetch if s in last_dates]
    stale = [s for s in symbols if s in last_dates and last_dates[s] < upto and s not in refetch]

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
