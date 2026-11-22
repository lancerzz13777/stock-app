"""Who is buying: free NSE data collected every evening, kept for testing later.

- FII/DII: net buying by foreign and domestic institutions (whole market, per day).
- Bulk and block deals: big trades by named clients (per stock).
- Insider trades: promoters, directors and employees buying or selling their own company's shares.
- Results dates: when each company announces quarterly results (for "drift after results" tests).

NSE only gives FII/DII and bulk/block deals for the CURRENT day, with no free history, so collecting
them daily is the only way to build one; they become testable after a year or two. Insider trades
come with history (python main.py flows-backfill), so they can be tested sooner.
Each source fails on its own: one NSE hiccup doesn't stop the others or the evening update.
"""
import io
import time
from datetime import date, timedelta

import pandas as pd
import requests

from . import ui

API = "https://www.nseindia.com/api/"
CSV = "https://nsearchives.nseindia.com/content/equities/{}.csv"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fii_dii (date TEXT, category TEXT, buy REAL, sell REAL, net REAL, PRIMARY KEY (date, category));
CREATE TABLE IF NOT EXISTS deals (date TEXT, kind TEXT, symbol TEXT, client TEXT, side TEXT, qty REAL, price REAL,
    PRIMARY KEY (date, kind, symbol, client, side, qty, price));
CREATE TABLE IF NOT EXISTS insider (pid TEXT PRIMARY KEY, symbol TEXT, date TEXT, disclosed TEXT, person TEXT,
    mode TEXT, side TEXT, qty REAL, value REAL, before_pct REAL, after_pct REAL);
CREATE TABLE IF NOT EXISTS events (symbol TEXT, date TEXT, purpose TEXT, PRIMARY KEY (symbol, date, purpose));
"""


def ensure(con):
    con.executescript(SCHEMA)


def _get(url: str):
    from .universe import HEADERS
    r = requests.get(url, headers={**HEADERS, "Referer": "https://www.nseindia.com/"}, timeout=30)
    r.raise_for_status()
    return r


def _day(s: str) -> str | None:
    d = pd.to_datetime(str(s)[:11], format="%d-%b-%Y", errors="coerce")
    return None if pd.isna(d) else d.date().isoformat()


def _num(x) -> float | None:
    v = pd.to_numeric(str(x).replace(",", ""), errors="coerce")
    return None if pd.isna(v) else float(v)


def fii_dii() -> list[tuple]:
    return [(_day(r["date"]), r["category"], _num(r["buyValue"]), _num(r["sellValue"]), _num(r["netValue"]))
            for r in _get(API + "fiidiiTradeReact").json()]


def deals(kind: str) -> list[tuple]:
    df = pd.read_csv(io.BytesIO(_get(CSV.format(kind)).content))
    df.columns = df.columns.str.strip()
    return [(_day(r["Date"]), kind, str(r["Symbol"]).strip(), str(r["Client Name"]).strip(), str(r["Buy/Sell"]).strip(),
             _num(r["Quantity Traded"]), _num(r["Trade Price / Wght. Avg. Price"])) for _, r in df.iterrows()]


def insider(start: date, end: date) -> list[tuple]:
    url = f"{API}corporates-pit?index=equities&from_date={start:%d-%m-%Y}&to_date={end:%d-%m-%Y}"
    return [(str(r["pid"]), r["symbol"], _day(r.get("acqtoDt")), _day(r.get("intimDt")), r.get("personCategory"),
             r.get("acqMode"), r.get("tdpTransactionType"), _num(r.get("secAcq")), _num(r.get("secVal")),
             _num(r.get("befAcqSharesPer")), _num(r.get("afterAcqSharesPer"))) for r in _get(url).json().get("data", [])]


def events() -> list[tuple]:
    return [(r["symbol"], _day(r["date"]), r["purpose"]) for r in _get(API + "event-calendar?index=equities").json()]


SAVE = {"fii_dii": "INSERT OR REPLACE INTO fii_dii VALUES (?,?,?,?,?)",
        "deals": "INSERT OR REPLACE INTO deals VALUES (?,?,?,?,?,?,?)",
        "insider": "INSERT OR REPLACE INTO insider VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        "events": "INSERT OR REPLACE INTO events VALUES (?,?,?)"}


def collect(con) -> dict[str, int]:
    """Today's data from every source; returns rows saved per source (a failed source shows -1)."""
    ensure(con)
    today = date.today()
    jobs = {"FII/DII": ("fii_dii", fii_dii), "bulk deals": ("deals", lambda: deals("bulk")),
            "block deals": ("deals", lambda: deals("block")),
            "insider trades": ("insider", lambda: insider(today - timedelta(days=7), today)),  # late filings too
            "results dates": ("events", events)}
    out = {}
    for name, (table, fetch) in jobs.items():
        try:
            rows = [r for r in fetch() if r[1 if table == "events" else 0]]  # drop rows with no readable date
            con.executemany(SAVE[table], rows)
            con.commit()  # one source at a time: never hold the database while waiting on NSE
            out[name] = len(rows)
        except Exception as e:  # NSE hiccup: tomorrow's run gets it (except today-only sources)
            ui.warn(f"{name}: {type(e).__name__}: {str(e)[:80]}")
            out[name] = -1
    return out


def backfill_insider(con, since: str = "2019-01-01") -> int:
    """Insider trades history, one month per request (NSE allows date ranges). Safe to re-run."""
    ensure(con)
    start, rows = date.fromisoformat(since), 0
    months = pd.date_range(start, date.today(), freq="MS")
    with ui.progress() as bar:
        task = bar.add_task("insider trades history (NSE)", total=len(months))
        for m in months:
            end = min((m + pd.offsets.MonthEnd(0)).date(), date.today())
            try:
                got = insider(m.date(), end)
                con.executemany(SAVE["insider"], got)
                con.commit()
                rows += len(got)
            except Exception as e:
                ui.warn(f"insider {m:%b %Y}: {type(e).__name__}: {str(e)[:60]}")
            bar.advance(task)
            time.sleep(1)  # be polite to NSE
    return rows
