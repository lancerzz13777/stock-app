"""Technical indicators, computed from daily OHLCV.

add_indicators() returns a column per indicator for EVERY day (used by the backtest);
latest_snapshot() is just the last row (used by the daily scan). Same maths for both.
"""
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift()
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c, v = df["close"], df["volume"]
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    signal = macd.ewm(span=9, adjust=False).mean()
    hist = macd - signal
    out = df.copy()
    out["sma50"] = c.rolling(50).mean()
    out["sma200"] = c.rolling(200).mean()
    out["rsi"] = rsi(c)
    out["macd"] = macd
    out["macd_signal"] = signal
    out["macd_hist"] = hist
    out["macd_hist_prev"] = hist.shift()
    out["atr"] = atr(df)
    out["avg_volume20"] = v.rolling(20).mean()
    out["avg_turnover20"] = (c * v).rolling(20).mean()
    out["prev_close"] = c.shift()
    out["high20_prev"] = c.rolling(20).max().shift()  # highest close of the previous 20 days
    out["high52"] = df["high"].rolling(252, min_periods=50).max()
    out["low52"] = df["low"].rolling(252, min_periods=50).min()
    out["history_days"] = range(1, len(df) + 1)
    return out


def latest_snapshot(df: pd.DataFrame) -> dict:
    row = add_indicators(df).iloc[-1].to_dict()
    row["last_date"] = df.index[-1]
    return row
