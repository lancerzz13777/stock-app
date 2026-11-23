"""Phase 8 research: does insider buying predict next month's return? Same data, filters and forward returns as quant.research.
Run on the PC (needs the full history and python main.py flows-backfill):  python research/insider_signal.py"""
import sys
sys.path.insert(0, '.')
import numpy as np
import pandas as pd
from screener import quant, strategies, pit
from screener.config import load_config
from screener.db import connect

cfg = load_config(); con = connect(cfg["data"]["db_path"]); lab = cfg["lab"]
split = pd.Timestamp(strategies.split_date(cfg))
w = pit.load_wide(con, lab["years"])
w["close"].pop("^NSEI")
f = strategies.features(w)
c, fl = f["close"], cfg["filters"]
mask = (c >= fl["min_price"]) & (f["turnover60"] >= fl["min_avg_turnover_cr"] * 1e7) & f["ret_12"].notna()
fwd = (w["close"].ffill().shift(-quant.HORIZON) / c - 1).clip(-0.9, 3)
dates = c.index[260::quant.HORIZON]
ins = pd.read_sql_query("SELECT symbol, date, disclosed, person, mode, side, value FROM insider", con)
ins = ins[(ins.disclosed >= "2019-01-01") & (ins.disclosed <= "2026-12-31") & ins.value.notna()]
ins["disclosed"] = pd.to_datetime(ins.disclosed)
first, last = ins.disclosed.min() + pd.Timedelta(days=90), pd.Timestamp("2026-02-28")  # full 90-day window of data
dates = dates[(dates >= first) & (dates <= last)]
mask, fwd, turn = mask.loc[dates], fwd.loc[dates], f["turnover60"].loc[dates]
del f, w

INSIDERS = {"Promoters", "Promoter Group", "Director", "Key Managerial Personnel"}
mkt = ins[ins["mode"].isin(["Market Purchase", "Market Sale"])]
sign = np.where(mkt.side == "Buy", 1.0, np.where(mkt.side == "Sell", -1.0, 0))
mkt = mkt.assign(signed=sign * mkt.value)


def signal(rows, how):
    out = pd.DataFrame(0.0, index=dates, columns=c.columns)
    for d in dates:
        r = rows[(rows.disclosed <= d) & (rows.disclosed > d - pd.Timedelta(days=90))]
        if how == "net":
            g = r.groupby("symbol")["signed"].sum()
        else:  # buys only, count of distinct insider buy filings
            g = r[r.signed > 0].groupby("symbol").size().astype(float)
        g = g.reindex(c.columns).dropna()
        out.loc[d, g.index] = g / turn.loc[d, g.index].replace(0, np.nan) if how == "net" else g
    return out.fillna(0.0)


tests = {"net buying by promoters/directors (value / daily turnover)": signal(mkt[mkt.person.isin(INSIDERS)], "net"),
         "net buying by all insiders incl. employees": signal(mkt, "net"),
         "number of promoter/director buy filings": signal(mkt[mkt.person.isin(INSIDERS)], "count")}
print(f"dates {dates[0].date()}..{dates[-1].date()} ({len(dates)} months), split {split.date()}")
for name, s in tests.items():
    ic = quant.rank_ic(s, fwd, mask).reindex(dates)
    tr, te = quant.ic_stats(ic[ic.index <= split]), quant.ic_stats(ic[ic.index > split])
    hit = (s > 0) & mask
    excess = (fwd.where(hit).mean(axis=1) - fwd.where(mask).mean(axis=1)) * 100
    neg = (s < 0) & mask
    exneg = (fwd.where(neg).mean(axis=1) - fwd.where(mask).mean(axis=1)) * 100
    print(f"\n{name}\n  IC train {tr['ic']:+.4f} (t {tr['t']:+.2f}, n {tr['n']}) | test {te['ic']:+.4f} (t {te['t']:+.2f}, n {te['n']})")
    for part, m in (("train", excess.index <= split), ("test", excess.index > split)):
        e, en = excess[m].dropna(), exneg[m].dropna()
        print(f"  {part}: insider-BOUGHT stocks beat the rest by {e.mean():+.2f}%/month (t {e.mean() / e.std() * np.sqrt(len(e)):+.2f}, "
              f"avg {hit[m].sum(axis=1).mean():.0f} stocks)"
              + (f" | insider-SOLD {en.mean():+.2f}%/month (t {en.mean() / en.std() * np.sqrt(len(en)):+.2f}, avg {neg[m].sum(axis=1).mean():.0f})" if len(en) > 2 else ""))
