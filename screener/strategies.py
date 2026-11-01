"""STRATEGY LAB: famous investors' stock-selection rules, tested on 10 years of NSE prices.

Each strategy ranks stocks at the end of every month and holds the top N until the next month
(a stock is kept while it stays in the top N x hold_buffer, which cuts trading and charges).

Honest testing (walk-forward):
  - In-sample  (config lab.split_date and before): used to CHOOSE the champion strategy.
  - Out-of-sample (after): the champion is checked on years it was never chosen on.
  Only out-of-sample results tell you anything about the future. Even those are no guarantee.

Data limits: free sources only have TODAY's fundamentals, so only price-based rules can be
tested. And only stocks listed today are included (survivorship bias makes results look better).
"""
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .charges import side_charges


# =================================================================== the strategies
@dataclass
class Strategy:
    key: str
    name: str
    source: str          # book / research it comes from
    idea: str            # one-line plain-English rule
    market_filter: bool  # hold cash when Nifty is below its 200-day average ("don't fight the market")


STRATEGIES = [
    Strategy("momentum", "Momentum 12-1",
             "Jegadeesh & Titman (1993); basis of NSE's Nifty200 Momentum 30 index",
             "Buy the stocks that rose most over the last 12 months (ignoring the last month).", False),
    Strategy("nse_momentum", "Risk-adjusted momentum",
             "NSE Momentum index method (6 & 12-month return divided by volatility)",
             "Strongest steady risers: high 6/12-month returns with low day-to-day swings.", False),
    Strategy("minervini", "Minervini Trend Template",
             "Mark Minervini, 'Trade Like a Stock Market Wizard' (SEPA)",
             "Only stage-2 uptrends: price > 50 > 150 > 200-day averages, 200-day rising, near 52-week high, "
             "30%+ above 52-week low, relative strength in top 30%.", False),
    Strategy("canslim", "O'Neil CAN SLIM (price rules)",
             "William O'Neil, 'How to Make Money in Stocks'",
             "Leaders near new highs (within 15% of 52-week high), relative strength in top 20%, above 50-day "
             "average - and only when the market itself is in an uptrend.", True),
    Strategy("high52", "52-week-high breakouts",
             "George & Hwang (2004); Nicolas Darvas, 'How I Made $2,000,000 in the Stock Market'",
             "Buy stocks trading closest to their 52-week high, in a long-term uptrend.", False),
    Strategy("lowvol", "Low volatility",
             "Haugen & Baker; NSE's Nifty100 Low Volatility 30 index",
             "Buy the calmest stocks (smallest daily swings over a year).", False),
    Strategy("dual", "Dual momentum",
             "Gary Antonacci, 'Dual Momentum Investing'",
             "Momentum 12-1, but only when the Nifty beat a fixed deposit over the last 12 months; else cash.", False),
]
BY_KEY = {s.key: s for s in STRATEGIES}


# =================================================================== features
def load_wide(con, years: int) -> dict[str, pd.DataFrame]:
    """Daily close/high/low/volume as wide tables (rows = dates, columns = symbols)."""
    since = (pd.Timestamp.today() - pd.DateOffset(years=years + 1, days=60)).strftime("%Y-%m-%d")
    df = pd.read_sql_query("SELECT symbol, date, high, low, close, volume FROM prices WHERE date >= ?", con,
                           params=(since,))
    wide = {c: df.pivot(index="date", columns="symbol", values=c).sort_index() for c in ("close", "high", "low", "volume")}
    for k in wide:
        wide[k].index = pd.to_datetime(wide[k].index)
    return wide


def features(w: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    c, h, l, v = w["close"].ffill(limit=5), w["high"], w["low"], w["volume"]
    ret = c.pct_change(fill_method=None).clip(-0.5, 0.5)
    f = {
        "close": c,
        "sma50": c.rolling(50, min_periods=40).mean(),
        "sma150": c.rolling(150, min_periods=120).mean(),
        "sma200": c.rolling(200, min_periods=160).mean(),
        "high52": h.rolling(252, min_periods=200).max(),
        "low52": l.rolling(252, min_periods=200).min(),
        "ret_12_1": c.shift(21) / c.shift(252) - 1,
        "ret_12": c / c.shift(252) - 1,
        "ret_9": c / c.shift(189) - 1,
        "ret_6": c / c.shift(126) - 1,
        "ret_3": c / c.shift(63) - 1,
        "vol": ret.rolling(252, min_periods=200).std() * math.sqrt(252),
        "turnover60": (c * v).rolling(60, min_periods=40).mean(),
    }
    f["sma200_prev"] = f["sma200"].shift(21)
    # O'Neil-style relative strength: recent quarter weighted double, then percentile across stocks (0-100)
    rs_raw = 0.4 * f["ret_3"] + 0.2 * f["ret_6"] + 0.2 * f["ret_9"] + 0.2 * f["ret_12"]
    f["rs"] = rs_raw.rank(axis=1, pct=True) * 100
    return f


def row(f: dict, date) -> pd.DataFrame:
    """All features for one date: one row per stock."""
    return pd.DataFrame({k: v.loc[date] for k, v in f.items()})


def liquid(r: pd.DataFrame, cfg: dict) -> pd.Series:
    fl = cfg["filters"]
    return (r["close"] >= fl["min_price"]) & (r["turnover60"] >= fl["min_avg_turnover_cr"] * 1e7) & r["ret_12"].notna()


def rank_stocks(key: str, r: pd.DataFrame, cfg: dict) -> pd.Series:
    """Score for every eligible stock on this date (higher = better). Ineligible stocks are dropped."""
    ok = liquid(r, cfg)
    if key == "momentum" or key == "dual":
        score = r["ret_12_1"]
    elif key == "nse_momentum":
        score = ((r["ret_6"] + r["ret_12"]) / 2) / r["vol"]
    elif key == "minervini":
        ok &= (r["close"] > r["sma50"]) & (r["sma50"] > r["sma150"]) & (r["sma150"] > r["sma200"]) \
            & (r["sma200"] > r["sma200_prev"]) & (r["close"] >= 1.3 * r["low52"]) \
            & (r["close"] >= 0.75 * r["high52"]) & (r["rs"] >= 70)
        score = r["rs"]
    elif key == "canslim":
        ok &= (r["close"] >= 0.85 * r["high52"]) & (r["rs"] >= 80) & (r["close"] > r["sma50"])
        score = r["rs"]
    elif key == "high52":
        ok &= r["close"] > r["sma200"]
        score = r["close"] / r["high52"]
    elif key == "lowvol":
        score = -r["vol"]
    else:
        raise KeyError(key)
    return score[ok & score.notna()].sort_values(ascending=False)


def why(key: str, rr: pd.Series) -> str:
    """Plain-English reasons a stock qualifies today."""
    bits = [f"12-month return {rr['ret_12'] * 100:+.0f}%", f"relative strength {rr['rs']:.0f}/100"]
    if key in ("minervini", "canslim", "high52"):
        bits.append(f"{(1 - rr['close'] / rr['high52']) * 100:.0f}% below 52-week high")
    if key == "minervini":
        bits.append("price > 50 > 150 > 200-day avg, 200-day rising")
    if key in ("lowvol", "nse_momentum"):
        bits.append(f"volatility {rr['vol'] * 100:.0f}%/yr")
    return " · ".join(bits)


# =================================================================== market regime
def market_ok(nifty: pd.Series, date, key: str, rf: float) -> bool:
    n = nifty.loc[:date]
    if len(n) < 252:
        return True
    if key == "dual":
        return n.iloc[-1] / n.iloc[-252] - 1 > rf
    return n.iloc[-1] > n.tail(200).mean()


# =================================================================== backtest
def month_ends(dates: pd.DatetimeIndex) -> list:
    s = pd.Series(dates, index=dates)
    return list(s.groupby([dates.year, dates.month]).max())


def backtest(key: str, f: dict, nifty: pd.Series, cfg: dict, n: int | None = None, capital: float | None = None,
             whole_shares: bool = False, use_market_filter: bool | None = None) -> dict:
    lab = cfg["lab"]
    n = n or lab["positions"]
    capital = capital or lab["capital"]
    rf = lab["risk_free_pct"] / 100
    strat = BY_KEY[key]
    mfilter = strat.market_filter if use_market_filter is None else use_market_filter
    charges = cfg["charges"]
    dates = [d for d in month_ends(nifty.index) if d >= f["close"].index[0] + pd.Timedelta(days=380)]
    close = f["close"]

    cash, pos = capital, {}           # pos: symbol -> shares (float, or int if whole_shares)
    equity, holdings_log, trades, paid = [], [], 0, 0.0
    for i, d in enumerate(dates):
        px = close.loc[d]
        value = cash + sum(sh * px.get(s, np.nan) if not np.isnan(px.get(s, np.nan)) else 0 for s, sh in pos.items())
        equity.append((d, value))
        if i == len(dates) - 1:
            break
        invest = market_ok(nifty, d, key, rf) if (mfilter or key == "dual") else True
        ranked = rank_stocks(key, row(f, d), cfg) if invest else pd.Series(dtype=float)
        top = list(ranked.index[:n])
        keep_zone = set(ranked.index[: n * lab["hold_buffer"]])
        # Sell what fell out of the keep zone (or everything when the market filter says cash)
        for s in list(pos):
            if s not in keep_zone or not invest:
                p = px.get(s)
                if p is None or np.isnan(p):
                    continue
                val = pos.pop(s) * p
                fee = side_charges(val, "sell", charges).total
                cash += val - fee
                paid += fee
                trades += 1
        # Buy new names to fill up to n, equal money each
        new = [s for s in top if s not in pos][: max(0, n - len(pos))]
        if new and invest:
            alloc = cash / len(new)
            for s in new:
                p = px[s]
                if whole_shares:
                    sh = math.floor(alloc / (p * 1.002))
                    if sh < 1:
                        continue
                else:
                    sh = alloc / (p * 1.002)
                val = sh * p
                fee = side_charges(val, "buy", charges).total
                if val + fee > cash:
                    continue
                cash -= val + fee
                paid += fee
                pos[s] = sh
                trades += 1
        if not pos:
            cash *= (1 + rf) ** (1 / 12)  # idle money in a liquid fund
        holdings_log.append((d, sorted(pos)))

    eq = pd.Series(dict(equity))
    return {"key": key, "equity": eq, "trades": trades, "charges": paid, "holdings": holdings_log,
            "market_filter": mfilter}


def metrics(eq: pd.Series, rf: float) -> dict:
    if len(eq) < 3:
        return {}
    m = eq.pct_change().dropna()
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1 if years > 0 and eq.iloc[-1] > 0 else -1
    excess = m - rf / 12
    sharpe = excess.mean() / m.std() * math.sqrt(12) if m.std() > 0 else 0
    dd = (eq / eq.cummax() - 1).min()
    return {"cagr": cagr * 100, "max_dd": dd * 100, "sharpe": sharpe, "pos_months": (m > 0).mean() * 100,
            "total": (eq.iloc[-1] / eq.iloc[0] - 1) * 100}


def split(eq: pd.Series, date: str) -> tuple[pd.Series, pd.Series]:
    d = pd.Timestamp(date)
    return eq[eq.index <= d], eq[eq.index >= eq.index[eq.index <= d][-1]]


def split_date(cfg: dict) -> str:
    """Where the CHOOSING years end and the UNSEEN test years begin. 'auto' = N years before today, so
    every re-run chooses on more recent history and the champion keeps up with the market."""
    s = str(cfg["lab"].get("split_date", "auto"))
    if s == "auto":
        years = int(cfg["lab"].get("unseen_years", 4))
        return (pd.Timestamp.today() - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
    return s


def run_lab(con, cfg: dict, progress=None) -> dict:
    """Backtests every strategy (with and without the market filter) + benchmarks, walk-forward."""
    lab = cfg["lab"]
    rf = lab["risk_free_pct"] / 100
    w = load_wide(con, lab["years"])
    nifty = w["close"].pop("^NSEI").dropna()
    for k in ("high", "low", "volume"):
        w[k] = w[k].drop(columns=["^NSEI"], errors="ignore")
    f = features(w)
    rows = []
    variants = []
    for s in STRATEGIES:
        variants.append((s.key, s.market_filter))
        if not s.market_filter and s.key != "dual":
            variants.append((s.key, True))
    results = {}
    for key, mf in variants:
        res = backtest(key, f, nifty, cfg, use_market_filter=mf)
        results[(key, mf)] = res
        if progress:
            progress()

    # Benchmarks on the same month-end dates
    eq_dates = next(iter(results.values()))["equity"].index
    nifty_eq = nifty.reindex(eq_dates, method="ffill")
    nifty_eq = nifty_eq / nifty_eq.iloc[0] * lab["capital"]

    for (key, mf), res in results.items():
        s = BY_KEY[key]
        ins, oos = split(res["equity"], split_date(cfg))
        rows.append({
            "key": key, "market_filter": mf,
            "name": s.name + (" + market filter" if mf and not s.market_filter else ""),
            "source": s.source, "trades": res["trades"], "charges": res["charges"],
            **{f"is_{k}": v for k, v in metrics(ins, rf).items()},
            **{f"oos_{k}": v for k, v in metrics(oos, rf).items()},
            **{f"all_{k}": v for k, v in metrics(res["equity"], rf).items()},
        })
    ins, oos = split(nifty_eq, split_date(cfg))
    bench = {"name": "Nifty 50 (buy & hold)", **{f"is_{k}": v for k, v in metrics(ins, rf).items()},
             **{f"oos_{k}": v for k, v in metrics(oos, rf).items()},
             **{f"all_{k}": v for k, v in metrics(nifty_eq, rf).items()}}
    table = pd.DataFrame(rows)

    # Champion = best IN-SAMPLE risk-adjusted return among those that beat the Nifty in-sample.
    beat = table[table["is_cagr"] > bench["is_cagr"]]
    pool = beat if len(beat) else table
    champ = pool.sort_values("is_sharpe", ascending=False).iloc[0].to_dict()
    champ["oos_beat_nifty"] = bool(champ["oos_cagr"] > bench["oos_cagr"])
    best_hindsight = table.sort_values("all_cagr", ascending=False).iloc[0].to_dict()
    return {"table": table, "bench": bench, "champion": champ, "hindsight": best_hindsight,
            "results": results, "nifty_eq": nifty_eq, "features": f, "nifty": nifty,
            "period": (str(eq_dates[0].date()), str(eq_dates[-1].date()))}


def small_budget_check(lab_out: dict, cfg: dict, budget: float) -> dict:
    """The champion again, but with YOUR money: only 1-2 stocks, whole shares, real charges."""
    c = lab_out["champion"]
    n = 2 if budget >= 2 * cfg["planner"]["min_trade_value"] else 1
    res = backtest(c["key"], lab_out["features"], lab_out["nifty"], cfg, n=n, capital=budget, whole_shares=True,
                   use_market_filter=c["market_filter"])
    rf = cfg["lab"]["risk_free_pct"] / 100
    ins, oos = split(res["equity"], split_date(cfg))
    return {"n": n, "all": metrics(res["equity"], rf), "oos": metrics(oos, rf), "charges": res["charges"],
            "trades": res["trades"], "final": res["equity"].iloc[-1]}


def current_picks(con, cfg: dict, key: str, market_filter: bool) -> dict:
    """What the strategy holds RIGHT NOW (latest data), with reasons."""
    lab = cfg["lab"]
    w = load_wide(con, 2)
    nifty = w["close"].pop("^NSEI").dropna()
    for k in ("high", "low", "volume"):
        w[k] = w[k].drop(columns=["^NSEI"], errors="ignore")
    f = features(w)
    d = f["close"].index[-1]
    invest = market_ok(nifty, d, key, lab["risk_free_pct"] / 100) if (market_filter or key == "dual") else True
    r = row(f, d)
    ranked = rank_stocks(key, r, cfg) if invest else pd.Series(dtype=float)
    picks = []
    for rank, s in enumerate(ranked.index[: lab["positions"] * lab["hold_buffer"]], 1):
        picks.append({"rank": rank, "symbol": s, "price": float(r.at[s, "close"]), "why": why(key, r.loc[s]), "ret12": float(r.at[s, "ret_12"]) * 100,
                      "in_top": rank <= lab["positions"]})
    next_review = (d + pd.offsets.BMonthEnd(1)).date()  # strategies are reviewed at each month end
    return {"date": d.date(), "invest": invest, "picks": picks, "next_review": next_review,
            "nifty_above_200dma": bool(nifty.iloc[-1] > nifty.tail(200).mean())}
