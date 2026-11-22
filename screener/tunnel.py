"""Dashboard from anywhere (free): a Cloudflare quick tunnel, no account, no router change.

Runs only if the `cloudflared` program is installed (in stock-app/bin/ or on the PATH); otherwise the
dashboard stays home-Wi-Fi only. The tunnel's web address changes each time it starts, so the bot
sends the new link (with the dashboard's secret key) to Telegram.
shortcut: quick tunnels have no uptime promise; a free named tunnel or Tailscale is the upgrade if it gets flaky.
"""
import re
import shutil
import subprocess
import time

from .config import ROOT

URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def program() -> str | None:
    local = ROOT / "bin" / "cloudflared"
    return str(local) if local.exists() else shutil.which("cloudflared")


def run(port: int, on_url, log) -> None:
    """Keeps a tunnel to 127.0.0.1:port open forever (run in a thread); on_url(url) each time it (re)starts."""
    exe = program()
    if not exe:
        log("Dashboard: home Wi-Fi only (install cloudflared to reach it from anywhere, see README)")
        return
    while True:
        p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        sent = False
        for line in p.stderr:  # cloudflared prints its address on stderr
            m = URL.search(line)
            if m and not sent:
                sent = True
                on_url(m.group(0))
        p.wait()
        log(f"Dashboard tunnel stopped (code {p.returncode}); restarting in 60 s")
        time.sleep(60)
