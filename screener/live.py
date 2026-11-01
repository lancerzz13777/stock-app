"""Where live prices come from.

- Angel One SmartAPI (free with an Angel One account): REAL-TIME prices and 5-minute candles
  straight from NSE through Angel's official API. Used automatically when the 4 ANGEL_* keys
  are filled in .env.
- Yahoo Finance (free, no account): fallback. Can be several minutes late.

Both return the same shapes, so the rest of the app doesn't care which one is used:
    quotes(["SBIN", ...])            -> {"SBIN": (price, time_of_price_IST), ...}
    candles(["SBIN", ...], days=5)   -> {"SBIN": DataFrame[open, high, low, close, volume]} (5-min, IST)
"""
import json
import os
import time
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from . import ui
from .config import ROOT

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENTS_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
INSTRUMENTS_FILE = ROOT / "data" / "angel_instruments.json"
ANGEL_KEYS = ("ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_MPIN", "ANGEL_TOTP_SECRET")


# ====================================================================== Yahoo (fallback)
class YahooSource:
    name = "Yahoo (free, can be minutes late)"
    live = False

    def quotes(self, symbols: list[str]) -> dict:
        import yfinance as yf
        symbols = sorted(set(symbols))
        if not symbols:
            return {}
        raw = yf.download([f"{s}.NS" for s in symbols], period="1d", interval="1m", group_by="ticker",
                          auto_adjust=False, progress=False, threads=True)
        out = {}
        if raw is None or raw.empty:
            return out
        for s in symbols:
            try:
                df = raw[f"{s}.NS"] if isinstance(raw.columns, pd.MultiIndex) else raw
            except KeyError:
                continue
            df = df.dropna(subset=["Close"])
            if df.empty:
                continue
            ts = pd.Timestamp(df.index[-1])
            ts = (ts.tz_localize("UTC") if ts.tz is None else ts).tz_convert(IST).to_pydatetime()
            out[s] = (round(float(df["Close"].iloc[-1]), 2), ts)
        return out

    def candles(self, symbols: list[str], cfg: dict, days: int = 5) -> dict:
        from .intraday import fetch_bars
        return fetch_bars(symbols, cfg, period=f"{days}d")


# ====================================================================== Angel One (real-time)
class AngelSource:
    name = "Angel One (LIVE, real-time)"
    live = True
    CANDLE_GAP = 0.35   # seconds between candle requests (Angel allows ~3/second)

    def __init__(self):
        self.api = None
        self.tokens: dict[str, str] = {}
        self.logged_in_at: datetime | None = None

    # -------------------------------------------------- setup
    def login(self) -> None:
        import pyotp
        from SmartApi import SmartConnect

        api = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
        totp = pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"].replace(" ", "")).now()
        res = api.generateSession(os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_MPIN"], totp)
        if not res or not res.get("status"):
            raise RuntimeError(f"Angel One login failed: {(res or {}).get('message', 'no response')}")
        self.api = api
        self.logged_in_at = datetime.now(IST)

    def _ensure(self) -> None:
        # Sessions end at midnight; log in again each new day
        if self.api is None or self.logged_in_at.date() != datetime.now(IST).date():
            self.login()
        if not self.tokens:
            self.tokens = load_instruments()

    def token(self, symbol: str) -> str | None:
        return self.tokens.get(symbol.upper())

    # -------------------------------------------------- data
    def quotes(self, symbols: list[str]) -> dict:
        self._ensure()
        want = {self.token(s): s for s in set(symbols) if self.token(s)}
        out = {}
        toks = list(want)
        for i in range(0, len(toks), 50):  # 50 instruments per request
            res = self.api.getMarketData("LTP", {"NSE": toks[i:i + 50]})
            if not res or not res.get("status"):
                raise RuntimeError(f"Angel quote failed: {(res or {}).get('message')}")
            for q in (res.get("data") or {}).get("fetched", []):
                sym = want.get(str(q.get("symbolToken")))
                if sym and q.get("ltp") is not None:
                    out[sym] = (round(float(q["ltp"]), 2), _parse_time(q.get("exchFeedTime")))
        return out

    def candles(self, symbols: list[str], cfg: dict, days: int = 5) -> dict:
        self._ensure()
        now = datetime.now(IST)
        frm = (now - timedelta(days=days + 3)).strftime("%Y-%m-%d 09:15")
        to = now.strftime("%Y-%m-%d %H:%M")
        out = {}
        for s in symbols:
            tok = self.token(s)
            if not tok:
                continue
            res = self.api.getCandleData({"exchange": "NSE", "symboltoken": tok, "interval": "FIVE_MINUTE",
                                          "fromdate": frm, "todate": to})
            time.sleep(self.CANDLE_GAP)
            rows = (res or {}).get("data") or []
            if not rows:
                continue
            df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
            idx = pd.to_datetime(df.pop("ts"))
            df.index = (idx.dt.tz_convert(IST) if idx.dt.tz is not None else idx).dt.tz_localize(None)
            df = df[(df.index.time >= dtime(9, 15)) & (df.index.time < dtime(15, 30))].astype(float)
            out[s] = df
        return out


def _parse_time(s) -> datetime:
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(s)[:20], fmt).replace(tzinfo=IST)
        except (ValueError, TypeError):
            pass
    return datetime.now(IST)


def load_instruments() -> dict[str, str]:
    """NSE symbol -> Angel token, from Angel's public instrument list (downloaded once a day)."""
    fresh = INSTRUMENTS_FILE.exists() and \
        datetime.fromtimestamp(INSTRUMENTS_FILE.stat().st_mtime).date() == datetime.now().date()
    if not fresh:
        r = requests.get(INSTRUMENTS_URL, timeout=60)
        r.raise_for_status()
        INSTRUMENTS_FILE.parent.mkdir(exist_ok=True)
        INSTRUMENTS_FILE.write_bytes(r.content)
    data = json.loads(INSTRUMENTS_FILE.read_text(encoding="utf-8"))
    out = {}
    for row in data:
        if row.get("exch_seg") == "NSE" and str(row.get("symbol", "")).endswith("-EQ"):
            out[row["symbol"][:-3]] = str(row["token"])
    return out


# ====================================================================== choosing a source
_source = None
_note = ""
_fallback_at = 0.0   # when we last fell back to Yahoo although Angel One is set up


def angel_configured() -> bool:
    return all(os.environ.get(k) for k in ANGEL_KEYS)


def source(cfg: dict | None = None):
    """The active data source (Angel One if set up and working, else Yahoo). Cached per window."""
    global _source, _note, _fallback_at
    if _source is not None:
        # Fell back to Yahoo because Angel was down? Try Angel again every 15 minutes.
        if not (isinstance(_source, YahooSource) and _fallback_at and time.time() - _fallback_at > 900):
            return _source
    choice = ((cfg or {}).get("data", {}) or {}).get("live_source", "auto")
    if choice != "yahoo" and angel_configured():
        try:
            a = AngelSource()
            a._ensure()
            _source, _note, _fallback_at = a, "", 0.0
            return _source
        except Exception as e:  # wrong keys, network, Angel down...
            _note = f"Angel One not available ({str(e)[:120]}) - using Yahoo"
            _fallback_at = time.time()
            ui.warn(_note)
    elif choice != "yahoo":
        _note = "Angel One not set up - using Yahoo (delayed). Menu → L to set up live data."
    _source = YahooSource()
    return _source


def note() -> str:
    return _note


def quotes(symbols: list[str], cfg: dict | None = None) -> dict:
    """Live prices with automatic fallback to Yahoo if Angel has a hiccup."""
    src = source(cfg)
    try:
        return src.quotes(symbols)
    except Exception as e:
        if isinstance(src, YahooSource):
            raise
        ui.warn(f"Angel quote failed ({str(e)[:80]}) - using Yahoo for this refresh")
        _relogin_next_time()
        return YahooSource().quotes(symbols)


def candles(symbols: list[str], cfg: dict, days: int = 5) -> dict:
    src = source(cfg)
    try:
        return src.candles(symbols, cfg, days)
    except Exception as e:
        if isinstance(src, YahooSource):
            raise
        ui.warn(f"Angel candles failed ({str(e)[:80]}) - using Yahoo for this scan")
        _relogin_next_time()
        return YahooSource().candles(symbols, cfg, days)


def angel_portfolio() -> dict:
    """Your REAL Angel One demat holdings and available cash. READ-ONLY: nothing is ever bought or sold.
    {"holdings": [{symbol, qty, avg, ltp, pnl, pnl_pct}], "cash": float, "net": float}"""
    if not angel_configured():
        raise RuntimeError("Angel One is not connected yet")
    src = _source if isinstance(_source, AngelSource) and _source.api else AngelSource()
    if src.api is None or src.logged_in_at.date() != datetime.now(IST).date():
        src.login()
    h = src.api.holding() or {}
    if not h.get("status", True):
        _relogin_next_time()
        raise RuntimeError(f"Angel holdings failed: {h.get('message')}")
    holdings = []
    for r in h.get("data") or []:
        qty = int(float(r.get("quantity") or 0)) + int(float(r.get("t1quantity") or 0))
        if qty <= 0:
            continue
        avg, ltp = float(r.get("averageprice") or 0), float(r.get("ltp") or 0)
        holdings.append({"symbol": str(r.get("tradingsymbol", "")).removesuffix("-EQ"), "qty": qty, "avg": avg,
                         "ltp": ltp, "pnl": (ltp - avg) * qty, "pnl_pct": (ltp / avg - 1) * 100 if avg else 0.0})
    f = (src.api.rmsLimit() or {}).get("data") or {}
    return {"holdings": sorted(holdings, key=lambda x: -x["ltp"] * x["qty"]),
            "cash": float(f.get("availablecash") or 0), "net": float(f.get("net") or 0)}


def _relogin_next_time():
    if isinstance(_source, AngelSource):
        _source.api = None
