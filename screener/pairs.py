"""PAIRS TRADING RESEARCH (Phase 6, research only: nothing here trades).

The idea: two stocks that moved together for a year (e.g. two private banks) drift apart for a while, then
come back. When the gap gets unusually wide, buy the one that fell behind and SHORT the one that ran ahead;
close when the gap closes. It doesn't care whether the market goes up or down (market-neutral).

Method: the "distance" method of Gatev, Goetzmann & Rouwenhorst (2006, Review of Financial Studies):
1. Formation (1 year): scale each stock's price to start at 1; the pairs whose lines stayed closest
   (smallest sum of squared gaps) are chosen.
2. Trading (next 6 months): open when the gap is 2 standard deviations wider than in the formation year,
   close when the lines cross again, or at the end of the 6 months.
3. A new set of pairs is chosen every month; 6 overlapping sets run at once. Everything is walk-forward:
   pairs are picked only from the past.

Honest limits:
- Shorting a stock for weeks is only possible with stock futures in India, so only the most-traded stocks
  (a stand-in for the ~200 F&O stocks) are used. shortcut: the real F&O list changes over time; we use
  the top 200 by turnover at each formation date.
- Costs: FUTURES_COST_PCT per leg per side (STT + exchange + slippage). The futures premium and margin
  interest are not modelled.
- Capital: Rs 1 long + Rs 1 short per pair counts as Rs 1 committed (futures margin is ~20% a leg).
"""
from itertools import combinations

import numpy as np
import pandas as pd

from . import pit, strategies

FORM, TRADE, STEP = 252, 126, 21   # trading days: 1 year to choose, 6 months to trade, a new set every month
N_PAIRS = 20
TOP_LIQUID = 200
FUTURES_COST_PCT = 0.07            # % of a leg's value each time it is bought or sold


def choose(c: pd.DataFrame, n_pairs: int, sectors: dict | None) -> list[tuple[str, str, float]]:
    """The n closest pairs in a formation window (rows = days, no gaps). sectors=None: any two stocks."""
    norm = c / c.iloc[0]
    groups = {}
    for s in norm.columns:
        key = (sectors or {}).get(s) if sectors is not None else "all"
        if key:
            groups.setdefault(key, []).append(s)
    found = []
    for names in groups.values():
        if len(names) < 2:
            continue
        x = norm[names].to_numpy()
        sq = (x ** 2).sum(axis=0)
        ssd = sq[:, None] + sq[None, :] - 2 * x.T @ x   # sum of squared gaps for every pair at once
        i, j = np.triu_indices(len(names), 1)
        found += [(ssd[a, b], names[a], names[b]) for a, b in zip(i, j)]
    out = []
    for _, a, b in sorted(found)[:n_pairs]:
        out.append((a, b, float((norm[a] - norm[b]).std())))
    return out


def trade(c: pd.DataFrame, a: str, b: str, sd: float, open_z: float) -> tuple[pd.Series, list[dict]]:
    """Daily P&L (on Rs 1 a leg) of one pair over its trading window, and its trades."""
    pa, pb = (c[a] / c[a].iloc[0]).to_numpy(), (c[b] / c[b].iloc[0]).to_numpy()
    gap = pa - pb
    cost = 2 * FUTURES_COST_PCT / 100   # both legs, one side
    pnl, trades = np.zeros(len(c)), []
    side, start, qa, qb = 0, 0, 0.0, 0.0
    for t in range(1, len(c)):
        if side:
            pnl[t] += side * (qa * (pa[t] - pa[t - 1]) - qb * (pb[t] - pb[t - 1]))
            if np.sign(gap[t]) != np.sign(gap[start]) or t == len(c) - 1:  # crossed back, or the window ends
                pnl[t] -= cost
                trades.append({"days": t - start, "pnl": float(pnl[start:t + 1].sum())})
                side = 0
                continue
        if not side and t < len(c) - 1 and abs(gap[t]) > open_z * sd:
            side = -1 if gap[t] > 0 else 1   # a ran ahead: short a, buy b (side -1); and the other way round
            qa, qb, start = 1 / pa[t], 1 / pb[t], t
            pnl[t] -= cost
    return pd.Series(pnl, index=c.index), trades


def backtest(w: dict, mask_liquid: pd.DataFrame, sectors: dict | None, open_z: float) -> tuple[pd.Series, list[dict]]:
    """Return per day of the whole strategy (overlapping monthly sets, each Rs 1 per pair) and every trade."""
    c = w["close"]
    days = c.index
    books, trades = [], []
    for f0 in range(0, len(days) - FORM - 1, STEP):
        f1 = f0 + FORM
        names = mask_liquid.iloc[f1 - 1]
        names = names[names].index
        form = c.iloc[f0:f1][names].dropna(axis=1)        # full price history in the formation year only
        if form.shape[1] < 20:
            continue
        # a stock that stops trading keeps its last price: it can't escape a loss
        tr = c.iloc[f1 - 1:f1 + TRADE][form.columns].ffill()
        book = pd.Series(0.0, index=tr.index[1:])
        for a, b, sd in choose(form, N_PAIRS, sectors):
            p, t = trade(tr[[a, b]], a, b, sd, open_z)
            book += p.iloc[1:].to_numpy() / N_PAIRS
            trades += t
        books.append(book)
    daily = pd.concat(books, axis=1).mean(axis=1)  # each set gets an equal share of the money
    return daily, trades


def research(con, cfg: dict) -> dict:
    lab = cfg["lab"]
    rf = lab["risk_free_pct"] / 100
    split = pd.Timestamp(strategies.split_date(cfg))
    full = pit.coverage(con)[1] >= 240 * lab["years"]
    w = pit.load_wide(con, lab["years"]) if full else strategies.load_wide(con, lab["years"])
    nifty = w["close"].pop("^NSEI").dropna()
    w["volume"] = w["volume"].drop(columns=["^NSEI"], errors="ignore")
    turnover = (w["close"] * w["volume"]).rolling(60, min_periods=40).mean()
    liquid = turnover.rank(axis=1, ascending=False) <= TOP_LIQUID
    from .fundamentals import load_fundamentals
    sectors = load_fundamentals(con)["sector"].dropna().to_dict()

    variants = {"Same sector, open at 2 sd": (sectors, 2.0), "Any two stocks, open at 2 sd": (None, 2.0),
                "Same sector, open at 1.5 sd": (sectors, 1.5)}
    rows, curves, all_trades = {}, {}, {}
    for name, (sec, z) in variants.items():
        daily, trades = backtest(w, liquid, sec, z)
        eq = (1 + daily).cumprod()
        monthly = eq.resample("ME").last()
        curves[name], all_trades[name] = monthly, trades
        m = monthly.pct_change().dropna()
        tr, te = m[m.index <= split], m[m.index > split]
        t = pd.DataFrame(trades)
        rows[name] = {
            **{f"train_{k}": v for k, v in strategies.metrics(monthly[monthly.index <= split], rf).items()},
            **{f"test_{k}": v for k, v in strategies.metrics(monthly[monthly.index >= tr.index[-1]], rf).items()},
            "test_t": te.mean() / te.std() * np.sqrt(len(te)) if len(te) > 2 and te.std() > 0 else 0.0,
            "nifty_corr": m.corr(nifty.resample("ME").last().pct_change().reindex(m.index)),
            "trades_per_year": len(t) / max(1, (daily.index[-1] - daily.index[0]).days / 365.25),
            "won": (t["pnl"] > 0).mean() * 100 if len(t) else float("nan"),
            "hold_days": t["days"].mean() if len(t) else float("nan"),
        }
    table = pd.DataFrame(rows).T
    best = table["train_sharpe"].idxmax()  # chosen on the choosing years only
    return {"table": table, "best": best, "curves": curves, "split": str(split.date()),
            "history": "every NSE stock, delisted ones too" if full else "only stocks listed today (survivorship bias)",
            "period": (str(curves[best].index[0].date()), str(curves[best].index[-1].date())), "rf": lab["risk_free_pct"]}
