"""QUANT RESEARCH ENGINE: how quant funds test ideas, run on our free NSE daily prices.

1. Alphas: each turns prices into a score per stock per day (higher = expected to do better).
2. IC (information coefficient): how well a score ranks the NEXT month's returns (rank correlation,
   -1..+1). Real alphas sit around 0.02-0.08; a t-stat above 2 means it is probably not noise.
3. Combine: alpha weights are fitted ONLY on the choosing years, then judged on the unseen years.
4. Portfolio: hold the top N stocks by the combined score, after every charge, against the Nifty.
5. Luck check: Deflated Sharpe (Bailey & Lopez de Prado, 2014) = the chance the result is real
   after counting how many ideas were tried. Below 95% = could be luck.

Same data limits as the Strategy Lab: only stocks listed today (survivorship bias flatters results),
daily closes only, long-only (no shorting for retail delivery trades). Research only - it never trades.
"""
import math
from statistics import NormalDist

import numpy as np
import pandas as pd

from . import strategies
from .charges import side_charges

HALF_LIFE = 24  # months: in the learning mix, an IC from 2 years ago counts half as much as last month's
HORIZON = 21    # trading days ~ one month: the holding period every alpha is judged on
MIN_STOCKS = 50  # skip dates with fewer eligible stocks (early years)

# name -> (plain-English idea, where it comes from)
ALPHAS = {
    "momentum_12_1": ("Rose most over 12 months, skipping the last month", "Jegadeesh & Titman 1993"),
    "risk_adj_momentum": ("6/12-month return per unit of volatility", "NSE momentum index method"),
    "near_52w_high": ("Closest to its 52-week high", "George & Hwang 2004"),
    "trend_200d": ("Furthest above its 200-day average", "trend following"),
    "low_volatility": ("Smallest daily swings over a year", "Haugen & Baker; Ang et al. 2006"),
    "reversal_1w": ("Fell most last week (bounce-back)", "Lehmann 1990; Jegadeesh 1990"),
    "reversal_1m": ("Fell most last month (bounce-back)", "Jegadeesh 1990"),
    "volume_surge": ("Last week's volume vs its 3-month normal", "Gervais, Kaniel & Mingelgrin 2001"),
    "illiquidity": ("Price moves a lot per rupee traded", "Amihud 2002"),
    # Phase 3: India-only free data (NSE delivery %, from Nov 2019)
    "delivery_pct": ("High share of traded stock taken into demat (last week)", "NSE delivery data; Indian studies"),
    "delivery_surge": ("Delivery % last week vs its own 3-month normal", "NSE delivery data"),
}


def alphas(f: dict, volume: pd.DataFrame, deliv: pd.DataFrame, dates=None) -> dict[str, pd.DataFrame]:
    """Every alpha as a wide table (rows = days, columns = stocks). With `dates`, each one is cut to those rows
    the moment it is computed, so the 1.8 GB laptop never holds eleven full-size tables at once."""
    c = f["close"]
    ret = c.pct_change(fill_method=None)
    fns = {
        "momentum_12_1": lambda: f["ret_12_1"],
        "risk_adj_momentum": lambda: (f["ret_6"] + f["ret_12"]) / 2 / f["vol"],
        "near_52w_high": lambda: c / f["high52"],
        "trend_200d": lambda: c / f["sma200"] - 1,
        "low_volatility": lambda: -f["vol"],
        "reversal_1w": lambda: -(c / c.shift(5) - 1),
        "reversal_1m": lambda: -(c / c.shift(21) - 1),
        "volume_surge": lambda: volume.rolling(5).mean() / volume.rolling(60, min_periods=40).mean(),
        "illiquidity": lambda: (ret.abs() / (c * volume).replace(0, np.nan)).rolling(60, min_periods=40).mean(),
        "delivery_pct": lambda: deliv.rolling(5, min_periods=3).mean(),
        "delivery_surge": lambda: deliv.rolling(5, min_periods=3).mean() - deliv.rolling(60, min_periods=40).mean(),
    }
    return {k: (fn() if dates is None else fn().loc[dates].copy()) for k, fn in fns.items()}


def rank_ic(score: pd.DataFrame, fwd: pd.DataFrame, mask: pd.DataFrame) -> pd.Series:
    """Spearman rank correlation between score and forward return, one value per date."""
    ok = mask & score.notna() & fwd.notna()
    ic = score.where(ok).rank(axis=1).corrwith(fwd.where(ok).rank(axis=1), axis=1)
    return ic[ok.sum(axis=1) >= MIN_STOCKS]


def ic_stats(ic: pd.Series) -> dict:
    ic = ic.dropna()
    n = len(ic)
    sd = ic.std()
    return {"ic": ic.mean() if n else float("nan"), "t": ic.mean() / sd * math.sqrt(n) if n > 2 and sd > 0 else 0.0,
            "hit": (ic > 0).mean() * 100 if n else float("nan"), "n": n}


def learned_weights(ics: pd.DataFrame) -> pd.DataFrame:
    """Learning from results: the weight of each alpha on each date, from ONLY the ICs already known then
    (last month's result is known at this date), recent months counting more. An alpha needs a t-stat of 2+
    so far to count, so one lucky month can't switch it on and a few bad months fade it out."""
    known = ics.shift(1)
    n = known.notna().cumsum()
    t = known.expanding().mean() / known.expanding().std() * np.sqrt(n)
    return known.ewm(halflife=HALF_LIFE, min_periods=12).mean().where(t.abs() >= 2, 0.0)


def centred_rank(score: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    """Score -> -0.5..+0.5 rank within the day's eligible stocks; 0 (neutral) where missing."""
    return (score.where(mask).rank(axis=1, pct=True) - 0.5).fillna(0).where(mask)


def sector_rank(score: pd.DataFrame, mask: pd.DataFrame, sectors: dict, min_size: int = 5) -> pd.DataFrame:
    """Like centred_rank, but each stock is ranked only against its own sector (no bet on which sector wins).
    Stocks with no known sector, or in a sector with fewer than min_size eligible stocks that day, keep the market rank."""
    out = centred_rank(score, mask)
    groups = pd.Series({c: sectors.get(c) for c in score.columns}).dropna()
    for _, cols in groups.groupby(groups):
        cols = list(cols.index)
        s = score[cols].where(mask[cols])
        big = s.notna().sum(axis=1) >= min_size
        r = s.rank(axis=1, pct=True) - 0.5
        out.loc[big, cols] = r.loc[big].fillna(0).where(mask.loc[big, cols])
    return out


def target(s: pd.Series, held, n: int, buffer: int, vol_row: pd.Series | None = None,
           sectors: dict | None = None, per_sector: int | None = None) -> pd.Series:
    """Weights of the n stocks to hold, from scores sorted best first: held names still in the top
    n x buffer stay (fewer trades), then the best new ones. Shared by the backtest and the live fund."""
    zone = set(s.index[: n * buffer])
    new, count = [], {}
    for x in [x for x in held if x in zone] + list(s.index):
        sec = (sectors or {}).get(x) or x  # unknown sector: no cap
        if x in new or (per_sector and count.get(sec, 0) >= per_sector):
            continue
        new.append(x)
        count[sec] = count.get(sec, 0) + 1
        if len(new) == n:
            break
    w = pd.Series(1.0, index=new) if vol_row is None else (1 / vol_row.reindex(new)).fillna(0)
    return w / w.sum() if w.sum() > 0 else pd.Series(1 / len(new), index=new)


def portfolio(score: pd.DataFrame, fwd: pd.DataFrame, mask: pd.DataFrame, cfg: dict, n: int,
              vol: pd.DataFrame | None = None, sectors: dict | None = None, per_sector: int | None = None) -> pd.Series:
    """Holds the top n by score, re-picked every HORIZON days; keeps a stock while it is in the top
    n x hold_buffer (fewer trades). vol: size each stock by 1/volatility (calmer = bigger) instead of equal money.
    per_sector: at most this many stocks from one sector. Returns the rupee value at the END of each period."""
    lab, ch = cfg["lab"], cfg["charges"]
    eq, held, out = float(lab["capital"]), {}, {}
    dates = score.index
    for i, d in enumerate(dates[:-1]):
        s = score.loc[d].where(mask.loc[d]).dropna().sort_values(ascending=False)
        if len(s) < MIN_STOCKS or fwd.loc[d].notna().sum() < MIN_STOCKS:  # last, unfinished month: no result yet
            continue
        w = target(s, held, n, lab["hold_buffer"], None if vol is None else vol.loc[d], sectors, per_sector)
        new = list(w.index)
        # shortcut: charges only on names entering/leaving, priced on their slice; re-weighting kept names is free
        cost = (sum(side_charges(eq * v, "sell", ch).total for x, v in held.items() if x not in w.index)
                + sum(side_charges(eq * v, "buy", ch).total for x, v in w.items() if x not in held))
        r = (fwd.loc[d, new].fillna(0) * w).sum()  # shortcut: a stock that stops trading counts as flat
        eq = (eq - cost) * (1 + r)
        out[dates[i + 1]] = eq
        held = w.to_dict()
    return pd.Series(out, dtype=float)


def deflated_sharpe(r: pd.Series, trial_sharpes: list[float]) -> float:
    """Probability (0-1) the true Sharpe is above 0, after allowing for the number of ideas tried.
    r = per-period excess returns of the chosen portfolio; trial_sharpes = per-period Sharpe of every idea."""
    r = r.dropna()
    if len(r) < 10 or r.std() == 0:
        return float("nan")
    nd, n = NormalDist(), len(trial_sharpes)
    sr = r.mean() / r.std()
    g = 0.5772156649  # Euler-Mascheroni
    sr0 = (math.sqrt(np.var(trial_sharpes, ddof=1)) * ((1 - g) * nd.inv_cdf(1 - 1 / n)
                                                        + g * nd.inv_cdf(1 - 1 / (n * math.e)))) if n > 1 else 0.0
    skew, kurt = r.skew(), r.kurt() + 3  # pandas gives excess kurtosis
    den = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr * sr))
    return nd.cdf((sr - sr0) * math.sqrt(len(r) - 1) / den)


def research(con, cfg: dict, survivors_only: bool = False, sector_relative: bool = False) -> dict:
    """survivors_only=True forces Yahoo's listed-today history even when the full NSE history is stored."""
    lab = cfg["lab"]
    rf = lab["risk_free_pct"] / 100
    n = lab["positions"]
    split = pd.Timestamp(strategies.split_date(cfg))

    from . import pit
    full = not survivors_only and pit.coverage(con)[1] >= 240 * lab["years"]  # every NSE stock, delisted too
    w = pit.load_wide(con, lab["years"]) if full else strategies.load_wide(con, lab["years"])
    nifty = w["close"].pop("^NSEI").dropna()
    volume = w["volume"].drop(columns=["^NSEI"], errors="ignore")
    for k in ("high", "low"):
        w[k] = w[k].drop(columns=["^NSEI"], errors="ignore")
    f = strategies.features(w)
    c, fl = f["close"], cfg["filters"]
    mask = (c >= fl["min_price"]) & (f["turnover60"] >= fl["min_avg_turnover_cr"] * 1e7) & f["ret_12"].notna()
    # a stock that stops trading counts at its LAST price (bust stocks lose), not as flat
    fwd = (w["close"].ffill().shift(-HORIZON) / c - 1).clip(-0.9, 3)

    # judge only on non-overlapping monthly dates once a year of history exists; keep the latest day for picks
    dates = c.index[260::HORIZON]
    if dates[-1] != c.index[-1]:
        dates = dates.append(c.index[-1:])
    mask, fwd = mask.loc[dates], fwd.loc[dates]
    from . import delivery
    deliv = delivery.wide(con, str(c.index[0].date())).reindex(index=c.index, columns=c.columns)
    al = alphas(f, volume, deliv, dates)
    del deliv
    vol = f["vol"].loc[dates]
    del f, w, volume
    from .fundamentals import load_fundamentals
    sectors = load_fundamentals(con)["sector"].dropna().to_dict()

    train, test = dates <= split, dates > split
    rows, ranks, ics = [], {}, {}
    for k, a in al.items():
        ic = ics[k] = rank_ic(a, fwd, mask).reindex(dates)
        tr, te = ic_stats(ic[ic.index <= split]), ic_stats(ic[ic.index > split])
        ranks[k] = sector_rank(a, mask, sectors) if sector_relative else centred_rank(a, mask)
        top = a.where(mask).rank(axis=1, pct=True) >= 0.8
        excess = (fwd.where(top).mean(axis=1) - fwd.where(mask).mean(axis=1))[test] * 12 * 100
        turnover = 1 - (top & top.shift()).sum(axis=1) / top.sum(axis=1).replace(0, np.nan)
        rows.append({"alpha": k, "idea": ALPHAS[k][0], "source": ALPHAS[k][1],
                     "train_ic": tr["ic"], "train_t": tr["t"], "test_ic": te["ic"], "test_t": te["t"],
                     "test_hit": te["hit"], "test_top_excess": excess.mean(), "turnover": turnover.mean() * 100})
    table = pd.DataFrame(rows).set_index("alpha")

    # combined alpha: train-period mean IC as the weight, only for alphas with |t| >= 2 there
    weights = table["train_ic"].where(table["train_t"].abs() >= 2, 0.0)
    if weights.abs().sum() == 0:
        weights = table["train_ic"].fillna(0)  # nothing significant: fall back to plain IC weights, flagged in the report
    combo = sum(ranks[k] * weights[k] for k in al) / weights.abs().sum()
    combo_ic = rank_ic(combo, fwd, mask)
    table["weight"] = weights / weights.abs().sum()

    # the LEARNING mix: re-weighted every month from results known by then (walk-forward, no peeking)
    lw = learned_weights(pd.DataFrame(ics))
    learned = sum(ranks[k].mul(lw[k], axis=0) for k in al).div(lw.abs().sum(axis=1).replace(0, np.nan), axis=0)
    learned_ic = rank_ic(learned, fwd, mask)
    table["weight_now"] = lw.iloc[-1] / (lw.iloc[-1].abs().sum() or 1)

    # portfolios: every single alpha (the "trials") plus the combination, all after charges
    def ex_returns(eq):
        return (pd.concat([pd.Series({dates[0]: float(lab["capital"])}), eq]).pct_change().dropna() - rf / 12)
    trials = {k: portfolio(a, fwd, mask, cfg, n) for k, a in al.items()}
    # Phase 2: ways to turn the combined score into a portfolio; the best on the CHOOSING years is used
    specs = {f"Top {n}, equal money": {"n": n}, "Top 25, equal money": {"n": 25},
             "Top 25, calmer stocks bigger": {"n": 25, "vol": vol},
             "Top 25, calmer bigger, max 5 per sector": {"n": 25, "vol": vol, "sectors": sectors, "per_sector": 5}}
    variants = {name: portfolio(combo, fwd, mask, cfg, **sp) for name, sp in specs.items()}
    trial_sr = [(lambda r: r.mean() / r.std())(ex_returns(e)) for e in [*trials.values(), *variants.values()]]

    def period(series, part):
        s = series[series.index <= split] if part == "train" else series[series.index >= series.index[series.index <= split][-1]]
        return strategies.metrics(s, rf)

    vtable = pd.DataFrame({name: {**{f"train_{k}": v for k, v in period(e, "train").items()},
                                  **{f"test_{k}": v for k, v in period(e, "test").items()}}
                           for name, e in variants.items()}).T
    best = vtable["train_sharpe"].idxmax()
    eq = variants[best]
    nifty_eq = nifty.reindex(c.index).ffill().loc[eq.index]

    r_test = ex_returns(eq)
    r_test = r_test[r_test.index > split]
    lt, ft = ic_stats(learned_ic[learned_ic.index > split]), ic_stats(combo_ic[combo_ic.index > split])
    use_learned = lt["n"] > 0 and lt["ic"] >= ft["ic"]
    live = learned if use_learned else combo
    latest = live.iloc[-1].where(mask.iloc[-1]).dropna().sort_values(ascending=False)
    return {
        "table": table, "weights_fallback": bool((table["train_t"].abs() < 2).all()),
        "combo_train": ic_stats(combo_ic[combo_ic.index <= split]), "combo_test": ic_stats(combo_ic[combo_ic.index > split]),
        "equity": eq, "nifty": nifty_eq,
        "variants": vtable, "best": best,
        "port": {"train": period(eq, "train"), "test": period(eq, "test")},
        "bench": {"train": period(nifty_eq, "train"), "test": period(nifty_eq, "test")},
        "dsr": deflated_sharpe(r_test, trial_sr), "trials": len(trial_sr) + 1,  # +1: the learning mix was tried too
        "learned_test": lt, "use_learned": use_learned,
        "history": "every NSE stock, delisted ones too" if full else "only stocks listed today (survivorship bias)",
        "picks": list(latest.index[:specs[best]["n"]]), "as_of": str(c.index[-1].date()), "split": str(split.date()),
        "period": (str(dates[0].date()), str(dates[-1].date())), "n_test_months": int(test.sum()),
        # for the live fund (Phase 5): today's full ranking and how the chosen variant builds the portfolio
        "latest": latest, "vol_latest": vol.iloc[-1], "sectors": sectors,
        "spec": {"n": specs[best]["n"], "calmer": "vol" in specs[best], "per_sector": specs[best].get("per_sector")},
    }
