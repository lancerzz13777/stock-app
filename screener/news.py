"""Stage 6: read business-news RSS feeds and NSE announcements, link items to stocks,
and tag each as positive / negative / neutral.

Tagging is FREE by default (a keyword tagger - rough, but costs nothing).
Optionally, set news.ai_tagging: true and put ANTHROPIC_API_KEY in .env to use Claude
(paid per use, roughly a few rupees a day). Off by default.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

import feedparser
import pandas as pd
import requests

HEADERS = {"User-Agent": "Mozilla/5.0 (personal stock screener; RSS reader)"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    id TEXT PRIMARY KEY,
    published TEXT,
    source TEXT,
    title TEXT,
    summary TEXT,
    link TEXT,
    symbols TEXT,          -- comma separated NSE symbols mentioned
    sentiment TEXT,        -- positive / negative / neutral
    tagged_by TEXT,        -- keywords / claude
    fetched_at TEXT
);
"""

POSITIVE = [
    "surge", "surges", "jumps", "soars", "rallies", "rally", "record high", "record profit", "beats estimate",
    "beat estimates", "bags order", "wins order", "order win", "bagged", "upgrade", "upgraded", "buyback",
    "dividend", "bonus issue", "profit rises", "profit jumps", "profit up", "net profit rises", "revenue rises",
    "strong results", "approval", "approves", "expansion", "outperform", "raises target", "stake buy",
    "debt-free", "turnaround", "all-time high",
]
NEGATIVE = [
    "falls", "slumps", "plunges", "tanks", "crashes", "sinks", "tumbles", "fraud", "raid", "penalty", "fined",
    "downgrade", "downgraded", "loss widens", "profit falls", "profit drops", "profit declines", "net loss",
    "resigns", "resignation", "default", "defaults", "probe", "investigation", "sebi order", "ban", "banned",
    "lawsuit", "recall", "fire at", "strike", "pledge", "insolvency", "nclt", "misses estimate", "miss estimates",
    "cuts target", "warning", "weak results", "scam", "arrested", "show cause", "write-off", "lower circuit",
    "bankruptcy", "auditor quits", "accounting irregularities",
]
# Two-word name starts beginning with these are too generic ("india limited", "global health")
COMMON_WORDS = {"india", "indian", "national", "global", "united", "general", "international", "new"}
FUND_RE = re.compile(r"mutual fund|\bMF\b|exchange traded fund|\bETF\b|index fund|interval fund|"
                     r"fixed maturity plan|\bFMP\b|\bNAV\b", re.IGNORECASE)


def _ensure(con):
    con.executescript(SCHEMA)


def _clean_name(name: str) -> str:
    n = re.sub(r"\b(limited|ltd\.?|ltd)\b", "", str(name).lower())
    return re.sub(r"[^a-z0-9& ]", " ", n).strip()


def build_matcher(con):
    """Returns a function(text) -> list of symbols mentioned in the text."""
    rows = con.execute(
        "SELECT u.symbol, COALESCE(f.name, u.name) FROM universe u LEFT JOIN fundamentals f USING(symbol)"
    ).fetchall()
    # Match the full name, or its first two words ("tata motors", "lemon tree") when no other
    # company starts the same way. Single words ("asset", "silver") caused too many false matches.
    names, prefixes = {}, {}
    for sym, name in rows:
        words = _clean_name(name).split()
        if len(" ".join(words)) >= 6:
            names[" ".join(words)] = sym
        if len(words) >= 2 and len(words[0]) >= 3 and words[0] not in COMMON_WORDS | {"the"}:
            prefixes.setdefault(" ".join(words[:2]), set()).add(sym)
    unique_prefix = {p: next(iter(s)) for p, s in prefixes.items() if len(s) == 1}
    symbols = {s for s, _ in rows if len(s) >= 3}

    def match(text: str) -> list[str]:
        if FUND_RE.search(text):  # mutual fund / ETF notices are not about a company
            return []
        low = " " + " ".join(re.sub(r"[^a-z0-9& ]", " ", text.lower()).split()) + " "
        found = {sym for n, sym in names.items() if f" {n} " in low}
        found |= {sym for p, sym in unique_prefix.items() if f" {p} " in low}
        # Symbols only when written in CAPITALS as a separate word (e.g. "TCS", "INFY")
        found |= {t for t in re.findall(r"\b[A-Z][A-Z0-9&]{2,}\b", text) if t in symbols}
        return sorted(found)

    return match


_POS_RE = [re.compile(rf"\b{re.escape(k)}\b") for k in POSITIVE]
_NEG_RE = [re.compile(rf"\b{re.escape(k)}\b") for k in NEGATIVE]


def keyword_sentiment(text: str) -> str:
    low = text.lower()
    pos = sum(1 for k in _POS_RE if k.search(low))  # whole words only ("ban" must not match "bank")
    neg = sum(1 for k in _NEG_RE if k.search(low))
    if neg > pos:
        return "negative"
    if pos > neg:
        return "positive"
    return "neutral"


def _published(entry) -> str:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def fetch_news(con, cfg: dict) -> int:
    """Downloads all feeds, stores new items. Returns how many new items were stored."""
    _ensure(con)
    match = build_matcher(con)
    new = 0
    for feed in cfg["news"]["feeds"]:
        try:
            r = requests.get(feed["url"], headers=HEADERS, timeout=20)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
        except Exception as e:
            print(f"  ! {feed['name']}: {type(e).__name__}")
            continue
        for e in parsed.entries:
            title = (e.get("title") or "").strip()
            summary = re.sub(r"<[^>]+>", " ", e.get("summary") or "")[:500].strip()
            link = e.get("link") or title
            subject = re.search(r"SUBJECT:\s*(.+)$", summary)
            if subject and subject.group(1).strip() not in title:  # NSE titles are just the company name
                title = f"{title}: {subject.group(1).strip()}"
            item_id = hashlib.sha1(link.encode()).hexdigest()[:16]
            if con.execute("SELECT 1 FROM news WHERE id=?", (item_id,)).fetchone():
                continue
            syms = match(f"{title} {summary}")
            con.execute(
                "INSERT INTO news VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (item_id, _published(e), feed["name"], title, summary, link, ",".join(syms),
                 keyword_sentiment(f"{title} {summary}"), "keywords", datetime.now(timezone.utc).isoformat()),
            )
            new += 1
    con.commit()
    if cfg["news"].get("ai_tagging"):
        _claude_tag(con, cfg)
    return new


def _claude_tag(con, cfg: dict) -> None:
    """Optional, PAID: re-tag recent stock-linked items with Claude. Off unless ai_tagging: true."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("  ! news.ai_tagging is on but ANTHROPIC_API_KEY is not set in .env - using keywords only")
        return
    try:
        import anthropic
    except ImportError:
        print("  ! Run  pip install anthropic  to use AI tagging - using keywords only")
        return
    rows = con.execute(
        "SELECT id, title, summary, symbols FROM news WHERE tagged_by='keywords' AND symbols != '' "
        "ORDER BY published DESC LIMIT 40"
    ).fetchall()
    if not rows:
        return
    items = [{"id": i, "headline": t, "summary": s[:300], "stocks": sy} for i, t, s, sy in rows]
    client = anthropic.Anthropic()
    try:
        resp = client.beta.messages.create(
            model=cfg["news"].get("ai_model", "claude-opus-5-5"),
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": "low",
                "format": {
                    "type": "json_schema",
                    "schema": {
                        "type": "object",
                        "properties": {"items": {"type": "array", "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                            },
                            "required": ["id", "sentiment"],
                            "additionalProperties": False,
                        }}},
                        "required": ["items"],
                        "additionalProperties": False,
                    },
                },
            },
            messages=[{"role": "user", "content": (
                "For each Indian business news item, judge whether it is positive, negative or neutral "
                "for the share price of the listed stock(s) over the next few weeks. Routine filings "
                "(board meeting dates, trading window closures, investor meets) are neutral.\n\n"
                + json.dumps(items, ensure_ascii=False)
            )}],
        )
    except anthropic.APIError as e:
        print(f"  ! Claude tagging failed ({type(e).__name__}) - keeping keyword tags")
        return
    if resp.stop_reason != "end_turn":
        return
    text = next((b.text for b in resp.content if b.type == "text"), "")
    for it in json.loads(text).get("items", []):
        con.execute("UPDATE news SET sentiment=?, tagged_by='claude' WHERE id=?", (it["sentiment"], it["id"]))
    con.commit()


def news_counts(con, days: int) -> pd.DataFrame:
    """Per symbol: number of positive / negative items in the last `days` days."""
    _ensure(con)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = con.execute("SELECT symbols, sentiment FROM news WHERE published >= ? AND symbols != ''",
                       (since,)).fetchall()
    counts = {}
    for syms, sent in rows:
        for s in syms.split(","):
            c = counts.setdefault(s, {"news_positive": 0, "news_negative": 0})
            if sent in ("positive", "negative"):
                c[f"news_{sent}"] += 1
    return pd.DataFrame.from_dict(counts, orient="index", columns=["news_positive", "news_negative"])


def recent_items(con, days: int = 2, symbol: str | None = None, sentiment: str | None = None) -> list[dict]:
    _ensure(con)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q, args = "SELECT * FROM news WHERE published >= ? AND symbols != ''", [since]
    if sentiment:
        q += " AND sentiment = ?"
        args.append(sentiment)
    q += " ORDER BY published DESC"
    cols = [c[1] for c in con.execute("PRAGMA table_info(news)")]
    items = [dict(zip(cols, r)) for r in con.execute(q, args)]
    if symbol:
        items = [i for i in items if symbol in i["symbols"].split(",")]
    return items
