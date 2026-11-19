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
}


def alphas(f: dict, volume: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Every alpha as a wide table (rows = days, columns = stocks)."""
    c = f["close"]
    ret = c.pct_change(fill_method=None)
    return {
        "momentum_12_1": f["ret_12_1"],
        "risk_adj_momentum": (f["ret_6"] + f["ret_12"]) / 2 / f["vol"],
        "near_52w_high": c / f["high52"],
        "trend_200d": c / f["sma200"] - 1,
        "low_volatility": -f["vol"],
        "reversal_1w": -(c / c.shift(5) - 1),
        "reversal_1m": -(c / c.shift(21) - 1),
        "volume_surge": volume.rolling(5).mean() / volume.rolling(60, min_periods=40).mean(),
        "illiquidity": (ret.abs() / (c * volume).replace(0, np.nan)).rolling(60, min_periods=40).mean(),
    }


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


def centred_rank(score: pd.DataFrame, mask: pd.DataFrame) -> pd.DataFrame:
    """Score -> -0.5..+0.5 rank within the day's eligible stocks; 0 (neutral) where missing."""
    return (score.where(mask).rank(axis=1, pct=True) - 0.5).fillna(0).where(mask)


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
        zone = set(s.index[: n * lab["hold_buffer"]])
        new, count = [], {}
        for x in [x for x in held if x in zone] + list(s.index):
            sec = (sectors or {}).get(x) or x  # unknown sector: no cap
            if x in new or (per_sector and count.get(sec, 0) >= per_sector):
                continue
            new.append(x)
            count[sec] = count.get(sec, 0) + 1
            if len(new) == n:
                break
        w = pd.Series(1.0, index=new) if vol is None else (1 / vol.loc[d, new]).fillna(0)
        w = w / w.sum() if w.sum() > 0 else pd.Series(1 / len(new), index=new)
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


def research(con, cfg: dict) -> dict:
    lab = cfg["lab"]
    rf = lab["risk_free_pct"] / 100
    n = lab["positions"]
    split = pd.Timestamp(strategies.split_date(cfg))

    w = strategies.load_wide(con, lab["years"])
    nifty = w["close"].pop("^NSEI").dropna()
    volume = w["volume"].drop(columns=["^NSEI"], errors="ignore")
    for k in ("high", "low"):
        w[k] = w[k].drop(columns=["^NSEI"], errors="ignore")
    f = strategies.features(w)
    c, fl = f["close"], cfg["filters"]
    mask = (c >= fl["min_price"]) & (f["turnover60"] >= fl["min_avg_turnover_cr"] * 1e7) & f["ret_12"].notna()
    fwd = (c.shift(-HORIZON) / c - 1).clip(-0.9, 3)

    # judge only on non-overlapping monthly dates once a year of history exists; keep the latest day for picks
    dates = c.index[260::HORIZON]
    if dates[-1] != c.index[-1]:
        dates = dates.append(c.index[-1:])
    mask, fwd = mask.loc[dates], fwd.loc[dates]
    al = {k: v.loc[dates] for k, v in alphas(f, volume).items()}
    vol = f["vol"].loc[dates]
    del f, w, volume
    from .fundamentals import load_fundamentals
    sectors = load_fundamentals(con)["sector"].dropna().to_dict()

    train, test = dates <= split, dates > split
    rows, ranks = [], {}
    for k, a in al.items():
        ic = rank_ic(a, fwd, mask)
        tr, te = ic_stats(ic[ic.index <= split]), ic_stats(ic[ic.index > split])
        ranks[k] = centred_rank(a, mask)
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
        weights = table["train_ic"]  # nothing significant: fall back to plain IC weights, flagged in the report
    combo = sum(ranks[k] * weights[k] for k in al) / weights.abs().sum()
    combo_ic = rank_ic(combo, fwd, mask)
    table["weight"] = weights / weights.abs().sum()

    # portfolios: every single alpha (the "trials") plus the combination, all after charges
    def ex_returns(eq):
        return (pd.concat([pd.Series({dates[0]: float(lab["capital"])}), eq]).pct_change().dropna() - rf / 12)
    trials = {k: portfolio(a, fwd, mask, cfg, n) for k, a in al.items()}
    # Phase 2: ways to turn the combined score into a portfolio; the best on the CHOOSING years is used
    variants = {
        f"Top {n}, equal money": portfolio(combo, fwd, mask, cfg, n),
        "Top 25, equal money": portfolio(combo, fwd, mask, cfg, 25),
        "Top 25, calmer stocks bigger": portfolio(combo, fwd, mask, cfg, 25, vol=vol),
        "Top 25, calmer bigger, max 5 per sector": portfolio(combo, fwd, mask, cfg, 25, vol=vol, sectors=sectors, per_sector=5),
    }
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
    latest = combo.iloc[-1].where(mask.iloc[-1]).dropna().sort_values(ascending=False)
    return {
        "table": table, "weights_fallback": bool((table["train_t"].abs() < 2).all()),
        "combo_train": ic_stats(combo_ic[combo_ic.index <= split]), "combo_test": ic_stats(combo_ic[combo_ic.index > split]),
        "equity": eq, "nifty": nifty_eq,
        "variants": vtable, "best": best,
        "port": {"train": period(eq, "train"), "test": period(eq, "test")},
        "bench": {"train": period(nifty_eq, "train"), "test": period(nifty_eq, "test")},
        "dsr": deflated_sharpe(r_test, trial_sr), "trials": len(trial_sr),
        "picks": list(latest.index[:int(best.split()[1].rstrip(","))]), "as_of": str(c.index[-1].date()), "split": str(split.date()),
        "period": (str(dates[0].date()), str(dates[-1].date())), "n_test_months": int(test.sum()),
    }
