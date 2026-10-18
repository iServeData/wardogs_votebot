#!/usr/bin/env bash
# Guided setup for the game servers the bot serves: add, modify, remove, test.
#   sudo bash deploy/manage_servers.sh
# Uses the installed bot's own code (so it always matches the running version) and its
# configuration rules. Passwords are typed hidden and stored as encrypted systemd credentials.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo: sudo bash $0"; exit 1; }
APP=/opt/wardogs-votebot
if [[ ! -f "$APP/votebot/manage.py" ]]; then
  echo "The installed bot is older than this tool. Update it first:"
  echo "  sudo bash $(cd "$(dirname "$0")" && pwd)/update.sh"
  exit 1
fi
cd "$APP"
exec "$APP/venv/bin/python" -E -s -B -m votebot.manage "$@"
