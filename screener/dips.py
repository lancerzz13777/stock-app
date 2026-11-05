"""Stage 5: watch QUALITY stocks for sudden 5-10% falls, and judge why they fell.

- Whole market falling (Nifty down a lot, or many stocks down together) and the stock
  fell roughly with it   -> possible buying chance (panic, not a company problem)
- Stock fell much more than the market, or has negative news -> WARNING, don't catch it
"""
import pandas as pd

from . import news, prices
from .scoring import CRORE


def quality_symbols(con, cfg: dict) -> list[str]:
    d = cfg["dips"]
    fund = pd.read_sql_query("SELECT * FROM fundamentals", con)
    ok = (
        (fund["market_cap"] >= d["min_market_cap_cr"] * CRORE)
        & (fund["pe"] > 0)
        & ((fund["sector"] == "Financial Services") | (fund["debt_to_equity"].fillna(99) <= d["max_debt_to_equity"]))
    )
    return sorted(fund.loc[ok, "symbol"])


def _changes_from_db(con, symbols: list[str]) -> pd.DataFrame:
    rows = {}
    data = prices.load_recent_prices(con, days=20)
    for s in symbols:
        df = data.get(s)
        if df is None or len(df) < 4:
            continue
        rows[s] = {"price": df["close"].iloc[-1], "prev": df["close"].iloc[-2],
                   "close_3d": df["close"].iloc[-4], "date": df.index[-1]}
    return pd.DataFrame.from_dict(rows, orient="index")


def _changes_live(symbols: list[str], cfg: dict) -> pd.DataFrame:
    """Today's (delayed) prices straight from Yahoo. NOT saved to the database."""
    rows = {}
    size = cfg["data"]["batch_size"]
    for i in range(0, len(symbols), size):
        for s, df in prices._fetch(symbols[i:i + size], period="10d", interval="1d").items():
            if len(df) >= 4:
                rows[s] = {"price": df["close"].iloc[-1], "prev": df["close"].iloc[-2],
                           "close_3d": df["close"].iloc[-4], "date": df.index[-1]}
    return pd.DataFrame.from_dict(rows, orient="index")


def find_dips(con, cfg: dict, live: bool = False) -> tuple[list[dict], dict]:
    """Returns (alerts, market) where market describes the overall day."""
    d = cfg["dips"]
    syms = quality_symbols(con, cfg)
    if not syms:
        return [], {"note": "No fundamentals yet - run an update first."}
    all_syms = syms + [prices.NIFTY_SYMBOL]
    ch = _changes_live(all_syms, cfg) if live else _changes_from_db(con, all_syms)
    if ch.empty:
        return [], {"note": "No price data."}
    ch["day_pct"] = (ch["price"] / ch["prev"] - 1) * 100
    ch["three_day_pct"] = (ch["price"] / ch["close_3d"] - 1) * 100

    stocks = ch.drop(index=prices.NIFTY_SYMBOL, errors="ignore")
    latest = stocks["date"].mode().iloc[0]
    stocks = stocks[stocks["date"] == latest]          # only stocks with today's price
    # The market's move over the SAME window as each stock: the Nifty if its data is current,
    # otherwise the median quality stock (Yahoo's Nifty sometimes lags a few days).
    nifty_ok = prices.NIFTY_SYMBOL in ch.index and ch.at[prices.NIFTY_SYMBOL, "date"] == latest
    # Use whichever fell more: the Nifty 50 (big companies) or the typical quality stock. A sell-off in
    # mid/small caps is still a market-wide move, not a problem with each company.
    med_1d, med_3d = float(stocks["day_pct"].median()), float(stocks["three_day_pct"].median())
    if nifty_ok:
        mkt_1d = min(float(ch.at[prices.NIFTY_SYMBOL, "day_pct"]), med_1d)
        mkt_3d = min(float(ch.at[prices.NIFTY_SYMBOL, "three_day_pct"]), med_3d)
    else:
        mkt_1d, mkt_3d = med_1d, med_3d
    breadth = float((stocks["day_pct"] <= -d["breadth_drop_pct"]).mean() * 100)
    market_wide = (mkt_1d <= -d["market_drop_pct"] or mkt_3d <= -1.5 * d["market_drop_pct"]
                   or breadth >= d["breadth_pct"])
    market = {"nifty_pct": round(mkt_1d, 2), "nifty_3d_pct": round(mkt_3d, 2), "breadth_pct": round(breadth, 1),
              "market_wide": market_wide, "date": str(latest),
              "source": "Market"}

    alerts = []
    for sym, r in stocks.iterrows():
        one_day = r["day_pct"] <= r["three_day_pct"]
        fall = -min(r["day_pct"], r["three_day_pct"])
        if fall < d["min_fall_pct"]:
            continue
        excess = fall + min(mkt_1d if one_day else mkt_3d, 0)  # how much worse than the market, same window
        bad_news = news.recent_items(con, days=3, symbol=sym, sentiment="negative")
        if bad_news:
            kind = "WARNING"
            why = f"negative company news: \"{bad_news[0]['title'][:100]}\""
        elif market_wide and excess <= d["max_excess_vs_market_pct"]:
            kind = "OPPORTUNITY"
            why = "fell with a market-wide sell-off (looks like panic, not a company problem)"
        else:
            kind = "WARNING"
            why = (f"fell {excess:.1f}% more than the market with no clear reason found - "
                   "check the news/announcements before even considering it")
        if fall > d["max_fall_pct"]:
            why += f". Big fall (>{d['max_fall_pct']}%) - extra caution"
        alerts.append({"symbol": sym, "kind": kind, "fall_pct": round(fall, 1), "price": round(r["price"], 2),
                       "why": why, "date": str(r["date"]), "window": "1 day" if one_day else "3 days"})
    alerts.sort(key=lambda a: (a["kind"] != "OPPORTUNITY", -a["fall_pct"]))
    return alerts, market


def format_dips(alerts: list[dict], market: dict) -> str:
    if "note" in market:
        return market["note"]
    head = (f"Market {market['date']}: Nifty {market['nifty_pct']:+.2f}%, "
            f"{market['breadth_pct']:.0f}% of quality stocks down sharply"
            f"{' -> MARKET-WIDE SELL-OFF' if market['market_wide'] else ''}")
    if not alerts:
        return head + "\nNo quality stock had a sudden fall today."
    lines = [head]
    for a in alerts:
        icon = "🟢" if a["kind"] == "OPPORTUNITY" else "⚠️"
        lines.append(f"{icon} {a['kind']}: {a['symbol']} -{a['fall_pct']}% to Rs {a['price']}: {a['why']}")
    lines.append("A dip is a chance to LOOK, not a signal to buy. Falling stocks can keep falling.")
    return "\n".join(lines)


def best_dips(con, cfg: dict, live: bool = False, budget: float | None = None) -> dict:
    """Ranks the falls and keeps only the best 1-2 to CONSIDER buying.

    A dip qualifies only if: the whole market fell (not a company problem), no negative news,
    the company passes at least half of the investor checklists, and it's still in a long-term
    uptrend (above its 200-day average). Plan: target = the price before the fall, stop = -8%.
    These dip rules were NOT backtested - treat them as a shortlist to look at, not a signal.
    """
    from . import fundamentals, gurus
    alerts, market = find_dips(con, cfg, live=live)
    out = {"market": market, "best": [], "company_falls": 0, "weak": 0}
    if "note" in market:
        return out
    budget = budget or cfg["alerts"]["default_budget"]
    stop_pct = cfg["dips"].get("stop_pct", 8)
    fund = fundamentals.load_fundamentals(con)
    ranked = []
    for a in alerts:
        if a["kind"] != "OPPORTUNITY":
            out["company_falls"] += 1
            continue
        r = fund.loc[a["symbol"]].to_dict() if a["symbol"] in fund.index else {}
        scores = [p / j for p, j, _ in gurus.score(r).values() if j]
        quality = sum(scores) / len(scores) if scores else 0
        n, sma200 = con.execute("SELECT COUNT(*), AVG(close) FROM (SELECT close FROM prices WHERE symbol=? "
                                "ORDER BY date DESC LIMIT 200)", (a["symbol"],)).fetchone()
        sma200 = sma200 if n >= 200 else None
        uptrend = bool(sma200 and a["price"] > sma200)
        if quality < 0.5 or not uptrend:
            out["weak"] += 1
            continue
        score = 0.6 * quality + 0.25 + (0.15 if a["fall_pct"] <= cfg["dips"]["max_fall_pct"] else 0)
        before = a["price"] / (1 - a["fall_pct"] / 100)
        qty = int(budget // a["price"])
        ranked.append({**a, "score": round(score * 100), "quality": round(quality * 100), "sma200": sma200,
                       "name": r.get("name") or a["symbol"], "target": round(before, 2),
                       "stop": round(a["price"] * (1 - stop_pct / 100), 2), "qty": qty,
                       "checklists": {n: (p, j) for n, (p, j, _) in gurus.score(r).items() if j}})
    ranked.sort(key=lambda x: -x["score"])
    out["best"] = ranked[:2]
    return out
