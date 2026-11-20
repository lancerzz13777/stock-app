"""Read-only dashboard for the trading agents: http://<computer>:8050 (standard library only).

GET /       the page (screener/dashboard.html)
GET /api    everything the page shows, as JSON (?day=YYYY-MM-DD for an earlier day)
Nothing here can change data or place an order.
"""
import json
import math
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import agents, fund
from .db import connect, get_state
from .intraday import IST

PAGE = Path(__file__).with_name("dashboard.html")


def _clean(x):
    """NaN -> None so the JSON is valid."""
    if isinstance(x, float) and math.isnan(x):
        return None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    return x


def data(con, day: str | None = None, cfg: dict | None = None) -> dict:
    from .bot import market_is_open
    agents.ensure(con)
    now = datetime.now(IST)
    days = [r[0] for r in con.execute("SELECT DISTINCT day FROM agent_trades WHERE mode='live' ORDER BY day DESC")]
    day = day if day in days else (days[0] if days else now.date().isoformat())
    q = json.loads(get_state(con, "quant_picks") or "null") or {"picks": [], "at": None}
    picks = set(q["picks"])
    cols = ["agent", "symbol", "qty", "entry", "entry_time", "exit", "exit_time", "reason", "pnl"]
    trades = [dict(zip(cols, r)) | {"quant": r[1] in picks} for r in con.execute(
        f"SELECT {', '.join(cols)} FROM agent_trades WHERE mode='live' AND day=? ORDER BY entry_time, agent", (day,))]
    daily = [dict(zip(("day", "agent", "pnl", "trades"), r)) for r in con.execute(
        "SELECT day, agent, SUM(pnl), COUNT(*) FROM agent_trades WHERE mode='live' AND pnl IS NOT NULL "
        "GROUP BY day, agent ORDER BY day")]
    board = lambda mode, d=None: agents.leaderboard(con, mode, d).to_dict("records")
    return _clean({
        "now": now.isoformat(timespec="seconds"), "market_open": market_is_open(now) and now.weekday() < 5,
        "last_cycle": get_state(con, "agents_last_cycle"), "day": day, "days": days, "cap": agents.DAY_CAP,
        "agents": [{"key": a.key, "name": a.name, "idea": a.idea, "tested": a.tested, "per_trade": a.per_trade,
                    "max_trades": a.max_trades} for a in agents.AGENTS],
        "trades": trades, "daily": daily, "today": board("live", day), "all_time": board("live"),
        "backtest": board("backtest"), "quant": q, "fund": fund.summary(con, cfg) if cfg else None,
        "lessons": json.loads(get_state(con, "agents_lessons") or "null"),
    })


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api":
            con = connect(self.server.db_path)
            try:
                body = json.dumps(data(con, parse_qs(u.query).get("day", [None])[0], self.server.cfg)).encode()
            finally:
                con.close()
            self._send(body, "application/json")
        elif u.path in ("/", "/index.html"):
            self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
        else:
            self.send_error(404)

    def _send(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # quiet: the bot's log is for trading events
        pass


def serve(cfg: dict, host: str = "127.0.0.1", port: int = 8050) -> None:
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.db_path, srv.cfg = cfg["data"]["db_path"], cfg
    srv.serve_forever()
