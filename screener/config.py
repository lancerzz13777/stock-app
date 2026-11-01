"""Loads config.yaml and .env."""
import logging
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


# yfinance logs every failed ticker loudly; our code already handles missing data
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def _private_yfinance_cache() -> None:
    """yfinance keeps a small SQLite cache. Two windows (e.g. the bot + the menu) sharing it can
    corrupt it ("database disk image is malformed"), so each window gets its own folder."""
    import os
    import shutil
    import time

    import yfinance as yf

    base = ROOT / "data" / "yf-cache"
    base.mkdir(parents=True, exist_ok=True)
    for old in base.iterdir():  # clean up folders from windows closed over a day ago
        if old.is_dir() and time.time() - old.stat().st_mtime > 86400:
            shutil.rmtree(old, ignore_errors=True)
    mine = base / str(os.getpid())
    mine.mkdir(exist_ok=True)
    yf.set_tz_cache_location(str(mine))


_private_yfinance_cache()


def load_config(path: str | Path = ROOT / "config.yaml") -> dict:
    load_dotenv(ROOT / ".env")  # secrets become os.environ values; never stored in code
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # Make the DB path relative to the project folder, not wherever you ran the command from
    cfg["data"]["db_path"] = str(ROOT / cfg["data"]["db_path"])
    return cfg
