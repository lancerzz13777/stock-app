"""Basic fundamentals (P/E, debt/equity, market cap, growth) from Yahoo Finance.

Free data is patchy for smaller Indian companies - missing values are stored as NULL
and the scoring treats them cautiously. Cached for `fundamentals_max_age_days`.
"""
import time
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

from . import ui


def _num(v):
    try:
        v = float(v)
        return v if v == v else None  # drop NaN
    except (TypeError, ValueError):
        return None


# Extra fields for the investor checklists (Graham, Buffett, Lynch, Greenblatt, QGLP...)
EXTRA = {
    "pb": "priceToBook", "roe": "returnOnEquity", "roa": "returnOnAssets", "current_ratio": "currentRatio",
    "peg": "trailingPegRatio", "div_yield": "dividendYield", "eps": "trailingEps", "book_value": "bookValue",
    "ev_ebitda": "enterpriseToEbitda", "profit_margin": "profitMargins", "op_cashflow": "operatingCashflow",
    "free_cashflow": "freeCashflow", "net_income": "netIncomeToCommon", "inst_held": "heldPercentInstitutions",
    "price": "currentPrice",
}


def ensure_columns(con) -> None:
    have = {r[1] for r in con.execute("PRAGMA table_info(fundamentals)")}
    for col in EXTRA:
        if col not in have:
            con.execute(f"ALTER TABLE fundamentals ADD COLUMN {col} REAL")
    con.commit()


def update_fundamentals(con, symbols: list[str], cfg: dict) -> None:
    ensure_columns(con)
    max_age = cfg["data"]["fundamentals_max_age_days"]
    pause = cfg["data"]["pause_seconds"]
    cutoff = (date.today() - timedelta(days=max_age)).isoformat()
    # Rows with no data at all are failed downloads, not real answers - retry those
    con.execute("DELETE FROM fundamentals WHERE market_cap IS NULL AND pe IS NULL AND sector IS NULL")
    # Rows saved before the checklist fields existed (roe/pb/eps all empty) count as stale
    fresh = {r[0] for r in con.execute("SELECT symbol FROM fundamentals WHERE fetched_at >= ? AND "
                                       "NOT (roe IS NULL AND pb IS NULL AND eps IS NULL)", (cutoff,))}
    todo = [s for s in symbols if s not in fresh]
    if not todo:
        return
    ui.info(f"  Fetching P/E, debt, growth for {len(todo)} stocks (kept for {max_age} days)...")

    bar = ui.progress()
    bar.start()
    task = bar.add_task("fundamentals", total=len(todo))
    try:
        _fetch_loop(con, todo, pause, bar, task)
    finally:
        bar.stop()
        con.commit()


def _fetch_loop(con, todo, pause, bar, task):
    fails_in_a_row, backoffs = 0, 0
    for i, s in enumerate(todo, 1):
        bar.advance(task)
        try:
            info = yf.Ticker(f"{s}.NS").info or {}
        except Exception:
            info = {}
        if not (info.get("marketCap") or info.get("sector")):
            fails_in_a_row += 1
            if fails_in_a_row >= 5:  # Yahoo is rate-limiting us: wait, and give up after 3 waits
                backoffs += 1
                if backoffs > 3:
                    con.commit()
                    ui.warn(f"Yahoo keeps blocking requests. Got {i - 1 - fails_in_a_row}/{len(todo)}; "
                            "run the update again later - it continues where it stopped.")
                    return
                ui.warn(f"Yahoo is rate-limiting - waiting 2 minutes ({backoffs}/3)...")
                time.sleep(120)
                fails_in_a_row = 0
            continue
        fails_in_a_row = 0
        de = _num(info.get("debtToEquity"))
        record = {
            "symbol": s, "fetched_at": date.today().isoformat(),
            "name": info.get("longName") or info.get("shortName"), "sector": info.get("sector"),
            "industry": info.get("industry"), "market_cap": _num(info.get("marketCap")),
            "pe": _num(info.get("trailingPE")),
            "debt_to_equity": de / 100 if de is not None else None,  # Yahoo reports D/E as a percentage
            "earnings_growth": _num(info.get("earningsGrowth")), "revenue_growth": _num(info.get("revenueGrowth")),
            **{col: _num(info.get(key)) for col, key in EXTRA.items()},
        }
        # Yahoo gives dividend yield either as 0.012 or 1.2 depending on version - store as a fraction
        if record["div_yield"] is not None and record["div_yield"] > 1:
            record["div_yield"] /= 100
        cols = ", ".join(record)
        con.execute(f"INSERT OR REPLACE INTO fundamentals ({cols}) VALUES ({', '.join('?' * len(record))})",
                    list(record.values()))
        if i % 25 == 0:
            con.commit()
        time.sleep(pause)


def load_fundamentals(con) -> pd.DataFrame:
    ensure_columns(con)
    return pd.read_sql_query("SELECT * FROM fundamentals", con).set_index("symbol")
