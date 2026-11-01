"""Local SQLite cache for the stock list, daily prices and fundamentals."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
    symbol TEXT PRIMARY KEY,
    name TEXT,
    source TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol TEXT PRIMARY KEY,
    fetched_at TEXT,
    name TEXT,
    sector TEXT,
    industry TEXT,
    market_cap REAL,
    pe REAL,
    debt_to_equity REAL,
    earnings_growth REAL,
    revenue_growth REAL
);
CREATE TABLE IF NOT EXISTS app_state (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # timeout: wait up to 60s if another window (e.g. the bot) is writing, instead of crashing
    con = sqlite3.connect(path, timeout=60)
    con.execute("PRAGMA journal_mode=WAL")  # lets one window read while another writes
    con.executescript(SCHEMA)
    return con


def get_state(con, key: str, default=None):
    row = con.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(con, key: str, value) -> None:
    con.execute("INSERT OR REPLACE INTO app_state VALUES (?, ?)", (key, str(value)))
    con.commit()
