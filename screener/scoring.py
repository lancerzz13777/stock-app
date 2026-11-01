"""Stage 2: filter out risky stocks, then score the rest using the rules in config.yaml.

Each rule is written once, as a True/False column over a table of stocks (or days), so the
daily scan and the backtest use exactly the same logic.
"""
import pandas as pd

from .indicators import latest_snapshot

CRORE = 1e7

TECHNICAL_RULES = [
    "trend_up", "above_200dma", "below_200dma", "rsi_healthy", "rsi_overbought", "rsi_oversold",
    "macd_bullish", "volume_spike", "breakout_20d", "near_52w_high", "far_below_52w_high",
]


def technical_hits(f: pd.DataFrame, rules: dict) -> dict[str, pd.Series]:
    """Rule name -> True/False per row. Needs only price data."""
    r = rules
    below_high = (1 - f["close"] / f["high52"]) * 100
    return {
        "trend_up": (f["close"] > f["sma50"]) & (f["sma50"] > f["sma200"]),
        "above_200dma": f["close"] > f["sma200"],
        "below_200dma": f["close"] <= f["sma200"],
        "rsi_healthy": f["rsi"].between(r["rsi_healthy"]["min"], r["rsi_healthy"]["max"]),
        "rsi_overbought": f["rsi"] > r["rsi_overbought"]["above"],
        "rsi_oversold": f["rsi"] < r["rsi_oversold"]["below"],
        "macd_bullish": (f["macd"] > f["macd_signal"]) & (f["macd_hist"] > f["macd_hist_prev"]),
        "volume_spike": (f["close"] > f["prev_close"])
                        & (f["volume"] >= r["volume_spike"]["multiple"] * f["avg_volume20"]),
        "breakout_20d": f["close"] > f["high20_prev"],
        "near_52w_high": below_high <= r["near_52w_high"]["within_pct"],
        "far_below_52w_high": below_high >= r["far_below_52w_high"]["below_pct"],
    }


def fundamental_hits(f: pd.DataFrame, rules: dict, sector_pe: dict) -> dict[str, pd.Series]:
    """Rules that need fundamentals and news (not available historically, so not backtested)."""
    r = rules
    pe = f["pe"]
    sec_med = f["sector"].map(sector_pe).fillna(sector_pe.get(None))
    de = f["debt_to_equity"]
    de_valid = de.notna() & (f["sector"] != "Financial Services")  # banks/NBFCs borrow by design
    eg, rg = f["earnings_growth"], f["revenue_growth"]
    hits = {
        "pe_below_sector": (pe > 0) & (pe < sec_med),
        "pe_too_high": pe > r["pe_too_high"]["above"],
        "pe_missing_or_loss": pe.isna() | (pe <= 0),
        "low_debt": de_valid & (de <= r["low_debt"]["max_de"]),
        "high_debt": de_valid & (de >= r["high_debt"]["min_de"]),
        "profit_growth": eg * 100 >= r["profit_growth"]["min_pct"],
        "profit_decline": eg < 0,
        "revenue_growth": rg * 100 >= r["revenue_growth"]["min_pct"],
        "large_cap": f["market_cap"] >= r["large_cap"]["min_cr"] * CRORE,
    }
    if "news_positive" in f:
        hits["news_positive"] = f["news_positive"] > 0
        hits["news_negative"] = f["news_negative"] > 0
    return hits


def _describe(name: str, row: pd.Series) -> str:
    """Human-readable reason for one rule hit."""
    g = row.get
    return {
        "trend_up": lambda: "uptrend (price > 50DMA > 200DMA)",
        "above_200dma": lambda: "above 200DMA",
        "below_200dma": lambda: "below 200DMA (long-term downtrend)",
        "rsi_healthy": lambda: f"RSI {g('rsi'):.0f} healthy",
        "rsi_overbought": lambda: f"RSI {g('rsi'):.0f} overbought",
        "rsi_oversold": lambda: f"RSI {g('rsi'):.0f} oversold / falling",
        "macd_bullish": lambda: "MACD bullish & strengthening",
        "volume_spike": lambda: f"volume {g('volume') / g('avg_volume20'):.1f}x avg on up day",
        "breakout_20d": lambda: "broke above 20-day high",
        "near_52w_high": lambda: f"{(1 - g('close') / g('high52')) * 100:.0f}% from 52w high",
        "far_below_52w_high": lambda: f"{(1 - g('close') / g('high52')) * 100:.0f}% below 52w high",
        "pe_below_sector": lambda: f"P/E {g('pe'):.0f} < sector {g('sector_pe', 0):.0f}",
        "pe_too_high": lambda: f"P/E {g('pe'):.0f} very high",
        "pe_missing_or_loss": lambda: "no P/E (loss-making or no data)",
        "low_debt": lambda: f"low debt (D/E {g('debt_to_equity'):.2f})",
        "high_debt": lambda: f"high debt (D/E {g('debt_to_equity'):.2f})",
        "profit_growth": lambda: f"profit growth {g('earnings_growth') * 100:.0f}%",
        "profit_decline": lambda: f"profit falling {g('earnings_growth') * 100:.0f}%",
        "revenue_growth": lambda: f"revenue growth {g('revenue_growth') * 100:.0f}%",
        "large_cap": lambda: "large cap",
        "news_positive": lambda: f"positive news ({g('news_positive'):.0f})",
        "news_negative": lambda: f"NEGATIVE news ({g('news_negative'):.0f}) - read before buying",
    }[name]()


def points_from_hits(hits: dict[str, pd.Series], rules: dict) -> pd.Series:
    total = None
    for name, mask in hits.items():
        rule = rules.get(name, {})
        if not rule.get("enabled", False):
            continue
        add = mask.fillna(False).astype(int) * rule["points"]
        total = add if total is None else total + add
    return total


def max_points(rules: dict, names=None) -> int:
    """Sum of every enabled positive rule - what a 'perfect' stock would score (= 100)."""
    return sum(r["points"] for n, r in rules.items()
               if r.get("enabled") and r["points"] > 0 and (names is None or n in names))


def build_snapshots(prices: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = {sym: latest_snapshot(df) for sym, df in prices.items()
            if len(df) >= 30 and not sym.startswith("^")}  # skip indices like ^NSEI
    return pd.DataFrame.from_dict(rows, orient="index")


def price_filter_mask(f: pd.DataFrame, cfg: dict) -> pd.Series:
    fl = cfg["filters"]
    return (
        (f["close"] >= fl["min_price"])
        & (f["avg_volume20"] >= fl["min_avg_volume"])
        & (f["avg_turnover20"] >= fl["min_avg_turnover_cr"] * CRORE)
        & (f["history_days"] >= fl["min_history_days"])
    )


def price_filters(snap: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Filters that only need price data (run before fetching fundamentals, to save time)."""
    # Drop stocks whose data stopped updating (suspended/delisted): last bar must be recent
    newest = pd.to_datetime(snap["last_date"]).max()
    recent = pd.to_datetime(snap["last_date"]) >= newest - pd.Timedelta(days=5)
    return snap[recent & price_filter_mask(snap, cfg)]


def fundamental_filters(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    f = cfg["filters"]
    mcap = df["market_cap"]
    if f["drop_if_market_cap_unknown"]:
        return df[mcap.notna() & (mcap >= f["min_market_cap_cr"] * CRORE)]
    return df[mcap.isna() | (mcap >= f["min_market_cap_cr"] * CRORE)]


def _sector_pe_medians(df: pd.DataFrame) -> dict:
    pos = df[df["pe"] > 0]
    overall = pos["pe"].median()
    med = {s: (g["pe"].median() if len(g) >= 3 else overall) for s, g in pos.groupby("sector")}
    med[None] = overall
    return med


def score_all(candidates: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    rules = cfg["scoring"]["rules"]
    out = candidates.copy()
    sector_pe = _sector_pe_medians(out)
    out["sector_pe"] = out["sector"].map(sector_pe).fillna(sector_pe[None])
    hits = {**technical_hits(out, rules), **fundamental_hits(out, rules, sector_pe)}
    out["points"] = points_from_hits(hits, rules)
    out["score"] = (out["points"] / max_points(rules) * 100).round().clip(lower=0).astype(int)

    reasons = []
    for sym, row in out.iterrows():
        parts = []
        for name, mask in hits.items():
            rule = rules.get(name, {})
            if rule.get("enabled") and bool(mask.get(sym, False)):
                p = rule["points"]
                parts.append(f"{'+' if p >= 0 else ''}{p} {_describe(name, row)}")
        reasons.append("; ".join(parts))
    out["reasons"] = reasons
    return out.sort_values(["score", "avg_turnover20"], ascending=False)
