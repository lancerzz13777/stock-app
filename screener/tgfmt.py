"""Telegram message designs: short, bold headings, aligned numbers, clear green/red results.

All builders return Telegram-HTML strings (send with telegram.send(..., rich=True)).
"""
from .telegram import esc

LINE = "━━━━━━━━━━━━━━━━━━"


def rs(x: float) -> str:
    return f"₹{x:,.2f}"


def signed(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}₹{abs(x):,.2f}"


def dot(x: float) -> str:
    return "🟢" if x > 0 else "🔴" if x < 0 else "⚪"


def table(rows: list[tuple[str, str]]) -> str:
    """Two aligned columns inside a monospace block."""
    w = max(len(k) for k, _ in rows)
    return "<pre>" + "\n".join(f"{esc(k).ljust(w)}  {esc(v)}" for k, v in rows) + "</pre>"


# ------------------------------------------------------------------ wallet
def wallet(s: dict, opens: list[dict], prices: dict, unrealised) -> str:
    out = [f"💼 <b>PAPER WALLET</b>", table([
        ("Free cash", rs(s["cash"])),
        ("Locked", rs(s["locked"])),
        ("Open P&L", signed(s["open_pnl"])),
        ("Total", rs(s["total"])),
        ("Today", f"{signed(s['today_pnl'])}  ({s['today_trades']} trades)"),
        ("All-time", signed(s["all_time_pnl"])),
    ])]
    if s.get("queued"):
        out.append("<b>Orders waiting for 9:15 AM</b>")
        out += [f"🕘 <b>#{q['id']} {esc(q['symbol'])}</b> · buy with {rs(q['locked'])}" for q in s["queued"]]
    if opens:
        out.append("<b>Open trades</b>")
        for p in opens:
            q = prices.get(p["symbol"])
            pl = unrealised(p, q[0]) if q else 0.0
            kind = "HOLD" if p.get("mode") == "hold" else "DAY"
            now = rs(q[0]) if q else "?"
            lvl = []
            if p.get("stop"):
                lvl.append(f"stop {p['stop']:,.2f}")
            if p.get("target"):
                lvl.append(f"target {p['target']:,.2f}")
            if p.get("sell_queued"):
                kind += " · selling at 9:15"
            out.append(f"{dot(pl)} <b>#{p['id']} {esc(p['symbol'])}</b> · {kind}\n"
                       f"    {p['qty']} × {rs(p['entry'])} → {now}   <b>{signed(pl)}</b>"
                       + (f"\n    <i>{' · '.join(lvl)}</i>" if lvl else "\n    <i>reviewed at month end</i>"))
    elif not s.get("queued"):
        out.append("<i>No open trades. Try 📊 Picks or 📉 Dips.</i>")
    return "\n".join(out)


def bought(p: dict, cash: float) -> str:
    rows = [("Shares", f"{p['qty']} × {rs(p['entry'])}"), ("Locked", rs(p["locked"])), ("Free cash", rs(cash))]
    if p.get("stop"):
        rows.append(("Stop-loss", rs(p["stop"])))
    if p.get("target"):
        rows.append(("Target", rs(p["target"])))
    when = ("I'll tell you when to sell (by 3:15 PM at the latest)." if p.get("mode") != "hold"
            else "I'll tell you when to sell." if p.get("stop") else "Reviewed at month end.")
    return f"✅ <b>BOUGHT {esc(p['symbol'])}</b>  <i>#{p['id']} · paper</i>\n{table(rows)}<i>{when}</i>"


def queued(q: dict, cash: float, opens_at, market_hours: bool = False) -> str:
    when = ("There's no live price from today yet (market holiday?), so this buys <b>as soon as trading starts</b>."
            if market_hours else
            f"The market is closed, so this buys at the <b>next open ({opens_at:%a %d %b}, 9:15 AM)</b>.")
    return (f"🕘 <b>ORDER PLACED · {esc(q['symbol'])}</b>  <i>#{q['id']} · paper</i>\n{when}\n"
            + table([("Amount locked", rs(q["locked"])), ("Last price", rs(q["entry"])),
                     ("Type", "hold" if q.get("mode") == "hold" else "day trade"), ("Free cash", rs(cash))])
            + "\n<i>Unused money comes back after it fills. Type <b>cancel</b> to undo.</i>")


def sell_queued(p: dict, opens_at, market_hours: bool = False) -> str:
    when = ("Sells <b>as soon as there's a live price today</b>" if market_hours
            else f"Sells at the <b>next open ({opens_at:%a %d %b}, 9:15 AM)</b>")
    return (f"🕘 <b>SELL ORDER PLACED · {esc(p['symbol'])}</b>  <i>#{p['id']}</i>\n"
            f"{when}. Type <b>cancel</b> to undo.")


def sold(pos: dict, c: dict, cash: float) -> str:
    pl = c["net_pnl"]
    head = "🟢 <b>PROFIT</b>" if pl > 0 else "🔴 <b>LOSS</b>" if pl < 0 else "⚪ <b>BREAK-EVEN</b>"
    return (f"{head}  <b>{signed(pl)}</b>\n"
            f"Sold <b>{esc(pos['symbol'])}</b> <i>#{pos['id']}</i>\n" + table([
                ("Bought", rs(pos["entry"])), ("Sold", rs(c["exit"])), ("Shares", str(pos["qty"])),
                ("Result", f"{signed(pl)} after charges"), ("Back to cash", rs(pos["locked"] + pl)),
                ("Free cash", rs(cash))]))


# ------------------------------------------------------------------ dips
def dips(res: dict) -> str:
    m = res["market"]
    if "note" in m:
        return f"📉 <b>DIP CHECK</b>\n{esc(m['note'])}"
    mood = ("🔻 <b>market-wide sell-off</b>" if m["market_wide"] else "normal day")
    label = "Nifty" if m.get("source", "Nifty") == "Nifty" else "Market"
    out = [f"📉 <b>DIP CHECK</b> · {esc(m['date'])}",
           f"{label} today <b>{m['nifty_pct']:+.2f}%</b> · 3 days <b>{m.get('nifty_3d_pct', 0):+.2f}%</b>", mood, LINE]
    if not res["best"]:
        out.append("😴 <b>No dip worth buying right now.</b>")
        why = []
        if res["company_falls"]:
            why.append(f"{res['company_falls']} fell much more than the market (avoided)")
        if res["weak"]:
            why.append(f"{res['weak']} not strong enough")
        if why:
            out.append("<i>" + " · ".join(why) + "</i>")
        elif not m["market_wide"]:
            out.append("<i>Good dips usually come on days the whole market falls.</i>")
        return "\n".join(out)
    medals = ["🥇", "🥈"]
    for i, d in enumerate(res["best"]):
        checks = " · ".join(f"{n} {p}/{j}" for n, (p, j) in list(d["checklists"].items())[:3])
        out.append(f"{medals[i]} <b>{esc(d['symbol'])}</b>  {rs(d['price'])}\n"
                   f"<i>{esc(d['name'])}</i>\n" + table([
                       ("Fell", f"−{d['fall_pct']}% in {d.get('window', '1 day')}"),
                       ("Quality", f"{d['quality']}% of checks passed"),
                       ("Trend", "above 200-day avg ✔"),
                       ("Buy", f"{d['qty']} × {rs(d['price'])}"),
                       ("Target", f"{rs(d['target'])} (back to pre-fall)"),
                       ("Stop-loss", rs(d["stop"])),
                   ]) + f"\n<i>{esc(checks)}</i>")
    if res["company_falls"]:
        out.append(f"⚠️ <i>{res['company_falls']} other fall(s) were much worse than the market — skipped.</i>")
    out.append("<i>Dip rules are not backtested. A falling stock can fall further.</i>")
    return "\n".join(out)


# ------------------------------------------------------------------ picks
def picks(c: dict, cp: dict, holds_keep: list, holds_sell: list, affordable: list[dict], review: bool) -> str:
    title = "📅 <b>MONTH-END REVIEW</b>" if review else "📊 <b>STRATEGY PICKS</b>"
    out = [f"{title}", f"<i>{esc(c['name'])} · tested {c['oos_cagr']:+.1f}%/yr on unseen years "
                       f"(Nifty {c['nifty_oos']:+.1f}%) · falls up to {c['max_dd']:.0f}%</i>", LINE]
    if not cp["invest"]:
        out.append("💤 <b>Stay in CASH this month</b>\n<i>The market is in a downtrend (Nifty below its 200-day average).</i>")
        if holds_sell:
            out.append("Sell: " + ", ".join(f"<b>{esc(h['symbol'])}</b>" for h in holds_sell))
        return "\n".join(out)
    if holds_keep or holds_sell:
        out.append("<b>Your holdings</b>")
        out += [f"✅ KEEP <b>{esc(h['symbol'])}</b>" for h in holds_keep]
        out += [f"❌ SELL <b>{esc(h['symbol'])}</b> <i>(dropped out of top 20)</i>" for h in holds_sell]
        out.append(LINE)
    medals = ["🥇", "🥈", "🥉"]
    if affordable:
        out.append("<b>Best buys for your budget</b>")
        for i, p in enumerate(affordable[:3]):
            out.append(f"{medals[i]} <b>{esc(p['symbol'])}</b>  {rs(p['price'])}\n"
                       f"    buy {p['qty']} ≈ {rs(p['qty'] * p['price'])} · <i>1y {p['ret12']:+.0f}%</i>")
    rest = [p["symbol"] for p in cp["picks"] if p["in_top"] and p["symbol"] not in {a["symbol"] for a in affordable[:3]}]
    if rest:
        out.append(f"<i>Also in top 10: {esc(' · '.join(rest))}</i>")
    out.append(f"📅 Next review <b>{cp['next_review']:%d %b}</b> · hold until then")
    return "\n".join(out)


# ------------------------------------------------------------------ real demat (read-only)
def demat(pf: dict, strategy_keep: set | None = None) -> str:
    hs = pf["holdings"]
    value = sum(h["ltp"] * h["qty"] for h in hs)
    pnl = sum(h["pnl"] for h in hs)
    out = ["🏦 <b>MY DEMAT · Angel One</b>  <i>real account · read-only</i>", table([
        ("Holdings", rs(value)), ("Profit/loss", signed(pnl)), ("Cash to trade", rs(pf["cash"]))])]
    if not hs:
        out.append("<i>No shares in your demat yet.</i>")
    for h in hs[:15]:
        tag = " · ✅ strategy likes it" if strategy_keep is not None and h["symbol"] in strategy_keep else ""
        out.append(f"{dot(h['pnl'])} <b>{esc(h['symbol'])}</b>  {h['qty']} × {rs(h['ltp'])}\n"
                   f"    bought at {rs(h['avg'])} · <b>{signed(h['pnl'])}</b> ({h['pnl_pct']:+.1f}%){tag}")
    if len(hs) > 15:
        out.append(f"<i>…and {len(hs) - 15} more</i>")
    out.append("<i>Profit/loss before selling charges. This app never places real orders — buy and sell in the "
               "Angel One app.</i>")
    return "\n".join(out)


# ------------------------------------------------------------------ alerts & signals
def exit_alert(msg: str, p: dict) -> str:
    return f"🔔 <b>{esc(p['symbol'])}</b> <i>#{p['id']}</i>\n{esc(msg)}"


def day_signal(s: dict, age: str) -> str:
    side = "BUY" if s["direction"] == "LONG" else "SHORT"
    return (f"⚡ <b>DAY TRADE · {side} {esc(s['symbol'])}</b>\n" + table([
        ("Buy", f"{s['qty']} × {rs(s['entry'])}"), ("Target", f"{rs(s['target'])}  {signed(s['profit'])}"),
        ("Stop-loss", f"{rs(s['stop'])}  {signed(-s['loss'])}"), ("Exit by", "3:15 PM")])
        + f"<i>{esc(age)}</i>")


HELP = (
    "📱 <b>HOW TO USE</b>\n"
    "Tap a button below, or just type:\n\n"
    "<b>buy SBIN 1500</b> — day trade\n"
    "<b>buy SBIN 1500 hold</b> — keep for weeks\n"
    "<b>sell SBIN</b> · <b>sell 2</b> · <b>sell all</b>\n"
    "<b>cancel</b> — undo orders placed while the market is closed\n"
    "<b>check TCS</b> — investor checklists\n"
    "<b>demat</b> — your real Angel One holdings (read-only)\n\n"
    "💬 Or just <b>ask me anything</b> in your own words:\n"
    "<i>which stock looks good today? · why did TCS fall? · should I sell SBC?</i>\n\n"
    "💼 Wallet · 📊 Picks · 📉 Dips · ⚡ Day scan\n"
    "<i>All trades use paper (fake) money.</i>"
)
