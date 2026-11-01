"""Famous investors' checklists, applied to TODAY's fundamentals.

These are summaries of published rules, simplified to what free data offers. They could NOT be
backtested (free sources have no history of P/E, ROE, etc.), so treat them as a quality check on a
stock, not as proof that it will rise.
"""
import math

import pandas as pd


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def _fin(r) -> bool:
    return r.get("sector") == "Financial Services"


def _check(cond, text):
    """(True/False/None, text). None = not enough data to judge."""
    return (None if cond is None else bool(cond)), text  # bool(): numpy True/False -> plain Python


def graham(r: dict) -> list:
    """Benjamin Graham, 'The Intelligent Investor' - the defensive investor's rules."""
    pe, pb, eps, bv, cr, de = (r.get(k) for k in ("pe", "pb", "eps", "book_value", "current_ratio", "debt_to_equity"))
    price = r.get("price") or r.get("close")
    gnum = math.sqrt(22.5 * eps * bv) if _ok(eps) and _ok(bv) and eps > 0 and bv > 0 else None
    return [
        _check(pe <= 15 if _ok(pe) and pe > 0 else (False if _ok(eps) and eps <= 0 else None), "P/E at most 15"),
        _check(pe * pb <= 22.5 if _ok(pe) and _ok(pb) and pe > 0 else None, "P/E x P/B at most 22.5"),
        _check(price <= gnum if gnum and _ok(price) else None,
               f"price below Graham number" + (f" (₹{gnum:,.0f})" if gnum else "")),
        _check(None if _fin(r) else (cr >= 2 if _ok(cr) else None), "current ratio at least 2 (not for banks)"),
        _check((r.get("div_yield") or 0) > 0 if _ok(r.get("div_yield")) else None, "pays a dividend"),
        _check(eps > 0 if _ok(eps) else None, "profitable"),
    ]


def buffett(r: dict) -> list:
    """Warren Buffett's business tests, as described in 'The Warren Buffett Way' (Hagstrom)."""
    roe, de, pm, eg, fcf = (r.get(k) for k in ("roe", "debt_to_equity", "profit_margin", "earnings_growth", "free_cashflow"))
    return [
        _check(roe >= 0.15 if _ok(roe) else None, "return on equity at least 15%"),
        _check(None if _fin(r) else (de <= 0.5 if _ok(de) else None), "low debt (D/E at most 0.5)"),
        _check(pm >= 0.10 if _ok(pm) else None, "profit margin at least 10%"),
        _check(eg > 0 if _ok(eg) else None, "earnings growing"),
        _check(fcf > 0 if _ok(fcf) else None, "positive free cash flow"),
    ]


def lynch(r: dict) -> list:
    """Peter Lynch, 'One Up on Wall Street' - growth at a reasonable price."""
    peg, eg, de = r.get("peg"), r.get("earnings_growth"), r.get("debt_to_equity")
    return [
        _check(peg <= 1 if _ok(peg) and peg > 0 else None, "PEG ratio at most 1 (growth not overpriced)"),
        _check(0.10 <= eg <= 0.50 if _ok(eg) else None, "earnings growth 10-50% (fast but sustainable)"),
        _check(None if _fin(r) else (de <= 0.8 if _ok(de) else None), "manageable debt"),
        _check((r.get("inst_held") or 0) < 0.6 if _ok(r.get("inst_held")) else None,
               "not over-owned by institutions (room to be discovered)"),
    ]


def qglp(r: dict) -> list:
    """Raamdeo Agrawal (Motilal Oswal), 'The Joys of Compounding' - Quality, Growth, Longevity, Price."""
    roe, eg, rg, pe, peg = (r.get(k) for k in ("roe", "earnings_growth", "revenue_growth", "pe", "peg"))
    return [
        _check(roe >= 0.20 if _ok(roe) else None, "Quality: ROE at least 20%"),
        _check(eg >= 0.15 if _ok(eg) else None, "Growth: earnings growth at least 15%"),
        _check(rg >= 0.10 if _ok(rg) else None, "Longevity: revenue still growing 10%+"),
        _check(peg <= 1.5 if _ok(peg) and peg > 0 else (pe <= 30 if _ok(pe) and pe > 0 else None),
               "Price: PEG at most 1.5 (or P/E at most 30)"),
    ]


def canslim_fund(r: dict) -> list:
    """William O'Neil, 'How to Make Money in Stocks' - the C, I and N/L parts that need fundamentals."""
    eg, inst = r.get("earnings_growth"), r.get("inst_held")
    return [
        _check(eg >= 0.25 if _ok(eg) else None, "C: current quarterly earnings up 25%+"),
        _check(0.05 <= inst <= 0.85 if _ok(inst) else None, "I: some institutional ownership (5-85%)"),
        _check(r.get("rs") >= 80 if _ok(r.get("rs")) else None, "L: a leader (relative strength 80+)"),
    ]


CHECKLISTS = [
    ("Graham", "The Intelligent Investor", graham),
    ("Buffett", "The Warren Buffett Way", buffett),
    ("Lynch", "One Up on Wall Street", lynch),
    ("QGLP", "Raamdeo Agrawal, The Joys of Compounding", qglp),
    ("CAN SLIM", "O'Neil, How to Make Money in Stocks", canslim_fund),
]


def score(r: dict) -> dict:
    """{name: (passed, judged, [(ok, text), ...])} for each checklist."""
    out = {}
    for name, _, fn in CHECKLISTS:
        checks = fn(r)
        judged = [c for c in checks if c[0] is not None]
        out[name] = (sum(1 for c in judged if c[0]), len(judged), checks)
    return out


def magic_formula(fund: pd.DataFrame, min_mcap_cr: float = 1000) -> pd.Series:
    """Joel Greenblatt, 'The Little Book That Beats the Market': rank by cheapness (earnings yield,
    here 1/(EV/EBITDA)) + quality (return on capital, here ROA). Banks and utilities excluded.
    Returns the combined rank (1 = best)."""
    f = fund[(fund["market_cap"] >= min_mcap_cr * 1e7) & ~fund["sector"].isin(["Financial Services", "Utilities"])]
    f = f[(f["ev_ebitda"] > 0) & f["roa"].notna()]
    ey_rank = (1 / f["ev_ebitda"]).rank(ascending=False)
    roc_rank = f["roa"].rank(ascending=False)
    return (ey_rank + roc_rank).rank().sort_values()
