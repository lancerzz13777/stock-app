"""The list of stocks to scan, taken from NSE's public CSV files.

These are static files NSE publishes for download; we fetch them at most once a day
and cache them in SQLite, so we're not hammering NSE's servers.
"""
import io
from datetime import date

import pandas as pd
import requests

# NSE has served the archives from both hostnames; try the newer one first.
HOSTS = ["https://nsearchives.nseindia.com", "https://archives.nseindia.com"]
PATHS = {
    "all": "/content/equities/EQUITY_L.csv",
    "nifty500": "/content/indices/ind_nifty500list.csv",
}
HEADERS = {"User-Agent": "Mozilla/5.0 (personal stock screener)", "Accept": "text/csv,*/*"}


def _download_csv(kind: str) -> pd.DataFrame:
    last_err = None
    for host in HOSTS:
        try:
            r = requests.get(host + PATHS[kind], headers=HEADERS, timeout=30)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = [c.strip().upper() for c in df.columns]
            return df
        except Exception as e:  # try the next host
            last_err = e
    raise RuntimeError(f"Could not download NSE stock list: {last_err}")


def refresh_universe(con, kind: str) -> list[str]:
    """Returns the list of NSE symbols (e.g. 'RELIANCE'), refreshing the cache once a day."""
    today = date.today().isoformat()
    row = con.execute("SELECT updated_at, source FROM universe LIMIT 1").fetchone()
    if row and row[0] == today and row[1] == kind:
        return [r[0] for r in con.execute("SELECT symbol FROM universe ORDER BY symbol")]

    try:
        df = _download_csv(kind)
    except RuntimeError as e:
        cached = [r[0] for r in con.execute("SELECT symbol FROM universe ORDER BY symbol")]
        if cached:
            print(f"  ! {e}\n  ! Using the cached list of {len(cached)} stocks instead.")
            return cached
        raise

    # Only the normal "EQ" series. BE/BZ etc. are trade-to-trade / surveillance stocks.
    df = df[df["SERIES"].astype(str).str.strip() == "EQ"]
    name_col = "NAME OF COMPANY" if "NAME OF COMPANY" in df.columns else "COMPANY NAME"
    rows = [(s.strip(), str(n).strip(), kind, today) for s, n in zip(df["SYMBOL"], df[name_col])]

    con.execute("DELETE FROM universe")
    con.executemany("INSERT INTO universe VALUES (?, ?, ?, ?)", rows)
    con.commit()
    return sorted(r[0] for r in rows)
