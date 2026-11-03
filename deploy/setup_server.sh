#!/usr/bin/env bash
# One-time setup on a free Ubuntu cloud server (Oracle Cloud Always Free).
# deploy_to_server.bat runs this for you. By hand, from inside the stock-app folder:  bash deploy/setup_server.sh
set -e
cd "$(dirname "$0")/.."

echo "== Installing Python =="
sudo apt-get update -y -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3 python3-venv python3-pip tzdata

# Extra safety memory for the monthly Strategy Lab (uses ~1-2 GB for a minute)
if ! swapon --show | grep -q /swapfile; then
  echo "== Adding 2 GB swap =="
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

# A laptop used as a server: never sleep, keep running with the lid closed
if [ -d /sys/class/power_supply ] || [ -f /etc/systemd/logind.conf ]; then
  echo "== Keeping this machine awake =="
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1
  sudo mkdir -p /etc/systemd/logind.conf.d
  printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n' \
    | sudo tee /etc/systemd/logind.conf.d/stockbot-lid.conf >/dev/null
  sudo systemctl kill -s HUP systemd-logind   # reload the lid setting without logging anyone out
fi

echo "== Creating the Python environment (a few minutes) =="
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ ! -f .env ]; then
  echo "!! No .env file found. Copy yours from your PC (it has your Telegram token)."
  exit 1
fi
chmod 600 .env
mkdir -p data

# Say hello on Telegram when it starts here (the bot stays quiet on quick restarts)
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/market.db'); c.execute(\"CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)\"); c.execute(\"DELETE FROM bot_state WHERE key='last_start'\"); c.commit()"

echo "== Installing the bot as a service (starts on boot, restarts if it crashes) =="
sed "s|__DIR__|$PWD|g; s|__USER__|$USER|g" deploy/stockbot.service | sudo tee /etc/systemd/system/stockbot.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable stockbot >/dev/null 2>&1
sudo systemctl restart stockbot
sleep 8
systemctl is-active stockbot

echo
echo "Done. The bot is running - Telegram should say 'Stock bot is running'."
echo "Useful commands:"
echo "  sudo systemctl status stockbot      # is it running?"
echo "  tail -f data/bot.log                # live log (Ctrl+C to leave)"
echo "  sudo systemctl restart stockbot     # after changing config.yaml or .env"
