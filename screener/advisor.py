"""Chat advisor - FREE: Google Gemini (free tier) with live Google Search.

How an answer is made:
1. The app gathers its OWN facts for your question: your paper wallet, what the tested strategy
   picks today, the best dips, investor checklists and live prices for any stock you mention.
2. Gemini reads those facts plus your recent chat, searches Google for the latest news, and
   replies in plain language, greeting you by name.

Costs nothing: a free key from https://aistudio.google.com/apikey (no card). The free tier has a
daily limit; when it's reached the bot says so and the rest of the app keeps working.
Note: on the free tier Google may use conversations to improve its products. The advisor only
ever sees your paper wallet and stock questions.
"""
import html
import json
import os
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
API = "https://generativelanguage.googleapis.com/v1beta"

SCHEMA = """
CREATE TABLE IF NOT EXISTS advisor_chat (ts TEXT, role TEXT, text TEXT);
"""

SYSTEM = """You are the personal stock research assistant of {name}, a beginner investor in India.
Talk to {name} like a friendly, knowledgeable mentor: greet them by name, be warm and direct.

About {name}: trades NSE stocks with small amounts (about Rs {budget} per trade), currently practising with
PAPER money (fake money, real prices) in this app. Wants clear, quick answers - no jargon walls.

How to answer:
- Use the APP FACTS below first: they come from this app's own tested strategy (risk-adjusted momentum,
  which beat the Nifty on years it was never tuned on), its dip ranking and investor checklists.
- Use Google Search for anything recent: news, results, why a stock moved, today's market.
- When suggesting a stock, give ONE clear idea: the name, the price, why (2-3 short reasons), the risk,
  and a simple plan (how many shares for Rs {budget}, where to cut the loss). If nothing looks good, say so.
- Never promise profit or say a stock "will" go up. Say what the evidence suggests and what could go wrong.
- If asked to buy or sell, tell {name} to type e.g. "buy SBIN 1500" or "sell SBIN" (the app executes it in
  the paper wallet).
- Keep it SHORT: under 180 words unless asked for detail. Use short lines and "•" bullets.
- Formatting: plain text only, with **bold** for stock names and key numbers. No tables, no headings, no links.
- Prices are in Rupees (₹). Today is {today} (IST). Market hours: Mon-Fri 9:15 AM - 3:30 PM.
"""


class AdvisorError(Exception):
    pass


def configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY"))


def ensure(con):
    con.executescript(SCHEMA)


# ---------------------------------------------------------------- Gemini (REST, no extra library)
def _key() -> str:
    return os.environ["GEMINI_API_KEY"].strip()


def pick_model(con, cfg) -> str:
    """Use the configured model; if Google retired it, pick the newest free 'flash' model automatically."""
    from .db import get_state, set_state
    wanted = cfg.get("advisor", {}).get("model", "auto")
    if wanted != "auto":
        return wanted
    cached = get_state(con, "gemini_model")
    if cached:
        return cached
    r = requests.get(f"{API}/models", params={"key": _key(), "pageSize": 200}, timeout=30)
    if r.status_code in (400, 401, 403):
        raise AdvisorError("Google rejected the API key. Check GEMINI_API_KEY in .env.")
    r.raise_for_status()
    names = [m["name"].split("/")[-1] for m in r.json().get("models", [])
             if "generateContent" in m.get("supportedGenerationMethods", [])]
    flash = [n for n in names if "flash" in n and not any(x in n for x in ("lite", "image", "tts", "live", "audio",
                                                                           "thinking", "exp", "preview", "8b"))]
    if not flash:
        flash = [n for n in names if "flash" in n] or names
    if not flash:
        raise AdvisorError("No Gemini model available for this key.")

    def version(n):  # gemini-2.5-flash -> (2, 5)
        m = re.search(r"(\d+)(?:\.(\d+))?", n)
        return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)
    model = sorted(flash, key=lambda n: (version(n), "latest" not in n))[-1]
    set_state(con, "gemini_model", model)
    return model


def _generate(con, cfg, system: str, contents: list[dict]) -> tuple[str, list[dict]]:
    """One Gemini call with Google Search. Returns (text, sources[{title, uri}])."""
    model = pick_model(con, cfg)
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1200},
    }
    for attempt in range(2):
        r = requests.post(f"{API}/models/{model}:generateContent", params={"key": _key()}, json=body, timeout=90)
        if r.status_code == 404 and attempt == 0:  # model retired - pick another and retry
            from .db import set_state
            set_state(con, "gemini_model", "")
            cfg = {**cfg, "advisor": {**cfg.get("advisor", {}), "model": "auto"}}
            model = pick_model(con, cfg)
            continue
        if r.status_code == 429:
            raise AdvisorError("Today's free Gemini limit is used up - it resets tomorrow. Everything else keeps working.")
        if r.status_code in (400, 401, 403):
            msg = (r.json().get("error") or {}).get("message", r.text[:200]) if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]
            raise AdvisorError(f"Google said: {msg}")
        r.raise_for_status()
        break
    data = r.json()
    cand = (data.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []))
    if not text.strip():
        raise AdvisorError("No answer came back (it may have been blocked). Try asking another way.")
    sources = []
    for ch in (cand.get("groundingMetadata") or {}).get("groundingChunks", []):
        web = ch.get("web") or {}
        if web.get("uri"):
            sources.append({"title": web.get("title") or "source", "uri": web["uri"]})
    return text.strip(), sources


# ---------------------------------------------------------------- app facts
_cache: dict = {}


def _cached(key: str, minutes: int, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < minutes * 60:
        return hit[1]
    val = fn()
    _cache[key] = (time.time(), val)
    return val


def app_facts(con, cfg, question: str) -> str:
    from . import dips, fundamentals, gurus, strategies, wallet
    from .db import get_state
    lines = []
    # Wallet
    try:
        opens = wallet.open_positions(con)
        px = wallet.live_prices([p["symbol"] for p in opens]) if opens else {}
        s = wallet.summary(con, cfg, px)
        lines.append(f"PAPER WALLET: free cash Rs {s['cash']:,.0f}, locked Rs {s['locked']:,.0f}, "
                     f"open P&L Rs {s['open_pnl']:+,.0f}, all-time P&L Rs {s['all_time_pnl']:+,.0f}.")
        for p in opens:
            q = px.get(p["symbol"])
            pl = wallet.unrealised(p, q[0], cfg) if q else 0
            lines.append(f"  holds {p['qty']} {p['symbol']} bought at {p['entry']}, now {q[0] if q else '?'}, "
                         f"P&L Rs {pl:+,.0f}, {'hold' if p.get('mode') == 'hold' else 'day trade'}"
                         + (f", stop {p['stop']}, target {p['target']}" if p.get("stop") else ""))
        for q in s.get("queued", []):
            lines.append(f"  order waiting for market open: buy {q['symbol']} with Rs {q['locked']:,.0f}")
    except Exception as e:
        lines.append(f"(wallet unavailable: {e})")
    # Strategy picks
    champ = get_state(con, "lab_champion")
    if champ:
        c = json.loads(champ)
        try:
            cp = _cached("picks", 30, lambda: strategies.current_picks(con, cfg, c["key"], c["market_filter"]))
            lines.append(f"TESTED STRATEGY: {c['name']} returned {c['oos_cagr']:+.1f}%/yr on unseen years "
                         f"(Nifty {c['nifty_oos']:+.1f}%), worst fall {c['max_dd']:.0f}%. Holds top 10, reviewed monthly; "
                         f"next review {cp['next_review']}.")
            if not cp["invest"]:
                lines.append("  Strategy says: stay in CASH (market downtrend).")
            else:
                top = [f"{p['symbol']} Rs {p['price']:,.0f} (1y {p['ret12']:+.0f}%)" for p in cp["picks"] if p["in_top"]]
                lines.append("  Today's top 10: " + "; ".join(top))
        except Exception as e:
            lines.append(f"(strategy picks unavailable: {e})")
    # Best dips
    try:
        res = _cached("dips", 30, lambda: dips.best_dips(con, cfg))
        if res["best"]:
            lines.append("BEST DIPS (quality stocks that fell with the market): " + "; ".join(
                f"{d['symbol']} Rs {d['price']:,.0f} fell {d['fall_pct']}% (target {d['target']}, stop {d['stop']})"
                for d in res["best"]))
        else:
            lines.append("BEST DIPS: none worth buying right now.")
    except Exception:
        pass
    # Stocks mentioned in the question
    syms = {r[0] for r in con.execute("SELECT symbol FROM universe")}
    words = {w.strip(",.?!").upper() for w in question.split()}
    mentioned = [w for w in words if w in syms][:3]
    if mentioned:
        fund = fundamentals.load_fundamentals(con)
        quotes = wallet.live_prices(mentioned)
        for sym in mentioned:
            q = quotes.get(sym)
            bits = [f"{sym}: live price Rs {q[0]:,.2f}" if q else f"{sym}: price unavailable"]
            if sym in fund.index:
                r = fund.loc[sym].to_dict()
                bits.append(f"{r.get('name')}, sector {r.get('sector')}")
                for k, label in (("pe", "P/E"), ("roe", "ROE"), ("debt_to_equity", "D/E")):
                    v = r.get(k)
                    if v == v and v is not None:
                        bits.append(f"{label} {v * 100:.0f}%" if k == "roe" else f"{label} {v:.1f}")
                bits.append("checklists " + ", ".join(f"{n} {p}/{j}" for n, (p, j, _) in gurus.score(r).items() if j))
            lines.append("STOCK " + "; ".join(bits))
    return "\n".join(lines)


# ---------------------------------------------------------------- chat
def _history(con, n: int = 8) -> list[dict]:
    rows = con.execute("SELECT role, text FROM advisor_chat ORDER BY rowid DESC LIMIT ?", (n,)).fetchall()[::-1]
    out = []
    for r, text in rows:  # Gemini wants user/model turns to alternate and start with the user
        role = "user" if r == "user" else "model"
        if out and out[-1]["role"] == role:
            out[-1]["parts"][0]["text"] += "\n\n" + text
        elif out or role == "user":
            out.append({"role": role, "parts": [{"text": text}]})
    if out and out[-1]["role"] == "user":  # the new question is added after this
        out.pop()
    return out


def _remember(con, role: str, text: str):
    con.execute("INSERT INTO advisor_chat VALUES (?, ?, ?)", (datetime.now(IST).isoformat(), role, text))
    con.commit()


def _system(cfg) -> str:
    a = cfg.get("advisor", {})
    return SYSTEM.format(name=a.get("your_name", "friend"), budget=f"{cfg['alerts']['default_budget']:,.0f}",
                         today=datetime.now(IST).strftime("%A %d %B %Y, %H:%M"))


def _to_telegram(text: str, sources: list[dict]) -> str:
    """Model text -> safe Telegram HTML (escape everything, then turn **bold** into <b>)."""
    t = html.escape(text, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?m)^\s*[\*\-]\s+", "• ", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
    t = re.sub(r"(?m)^#+\s*", "", t)
    if sources:
        seen, srcs = set(), []
        for s in sources:
            title = s["title"].split(" - ")[0][:40]
            if title not in seen:
                seen.add(title)
                srcs.append(html.escape(title, quote=False))
        t += "\n\n<i>🔎 Sources: " + " · ".join(srcs[:3]) + "</i>"
    return t


def _check_quota(con, cfg):
    limit = cfg.get("advisor", {}).get("max_questions_per_day", 40)
    today = datetime.now(IST).date().isoformat()
    used = con.execute("SELECT COUNT(*) FROM advisor_chat WHERE role='user' AND ts >= ?", (today,)).fetchone()[0]
    if used >= limit:
        raise AdvisorError(f"You've asked {used} questions today (limit {limit}, set in config.yaml) - "
                           "this keeps you inside Google's free quota.")


def ask(con, cfg, question: str) -> str:
    """Answer a question. Returns Telegram-HTML text."""
    if not configured():
        raise AdvisorError("The advisor isn't set up yet - it needs a free Google Gemini key (no card). "
                           "On the PC: menu → A.")
    ensure(con)
    _check_quota(con, cfg)
    facts = app_facts(con, cfg, question)
    contents = _history(con) + [{"role": "user", "parts": [{"text": f"APP FACTS (live, from this app):\n{facts}\n\n"
                                                                       f"QUESTION: {question}"}]}]
    text, sources = _generate(con, cfg, _system(cfg), contents)
    _remember(con, "user", question)
    _remember(con, "model", text)
    return _to_telegram(text, sources)


def morning_brief(con, cfg) -> str:
    """The weekday morning greeting with one idea for the day."""
    ensure(con)
    facts = app_facts(con, cfg, "")
    prompt = (f"APP FACTS (live, from this app):\n{facts}\n\nWrite {cfg.get('advisor', {}).get('your_name', 'my')}'s "
              "morning message before the market opens: greet them, give the market mood in one line (search today's "
              "Indian market news: Nifty, global cues), then ONE stock idea worth watching today from the app facts "
              "(check its latest news with Google Search first; skip it if the news is bad) with price, why, risk and "
              "a plan for the budget. Mention anything important about their open positions. Under 150 words.")
    text, sources = _generate(con, cfg, _system(cfg), [{"role": "user", "parts": [{"text": prompt}]}])
    _remember(con, "model", text)
    return "☀️ " + _to_telegram(text, sources)


def forget(con) -> None:
    ensure(con)
    con.execute("DELETE FROM advisor_chat")
    con.commit()
