#!/usr/bin/env bash
# Updates an existing install to the code in this checkout, with a backup and automatic rollback.
#   sudo bash deploy/update.sh            (run from anywhere; uses the checkout this script is in)
#   sudo bash deploy/update.sh --check    (compare only: show versions and what would change)
# What it does: checks the new code compiles, stops the bot, backs up the code and the database
# to /var/backups/wardogs-votebot/<time>, installs the new code (root-owned, read-only to the bot),
# installs new libraries only if requirements.lock changed, starts the bot and watches the log.
# If the bot doesn't come up cleanly, the backup is put back and the old version restarted.
# It never edits the systemd unit, env file, options, credentials, firewall or anything else.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

SVC=wardogs-votebot
APP=/opt/wardogs-votebot
DATA=/var/lib/wardogs-votebot
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PY="$APP/venv/bin/python"

[[ -d "$APP/votebot" && -x "$PY" ]] || { echo "No install found in $APP. Use deploy/install.sh for a new install."; exit 1; }
[[ -f "$SRC/votebot/__init__.py" ]] || { echo "Can't find the new code next to this script ($SRC)."; exit 1; }

ver () { grep -oP '__version__ = "\K[^"]+' "$1/votebot/__init__.py" 2>/dev/null || echo "0.1.x"; }
OLD=$(ver "$APP"); NEW=$(ver "$SRC")
LOCK_CHANGED=no; cmp -s "$SRC/requirements.lock" "$APP/requirements.lock" 2>/dev/null || LOCK_CHANGED=yes
echo "Installed: $OLD   New: $NEW   Library changes: $LOCK_CHANGED"
if ! cmp -s "$SRC/deploy/wardogs-votebot.service" /etc/systemd/system/$SVC.service; then
  echo "Note: deploy/wardogs-votebot.service differs from the installed unit. It is NOT changed by this script."
fi
[[ "${1:-}" == "--check" ]] && { diff -rq -x __pycache__ "$APP/votebot" "$SRC/votebot" || true; exit 0; }

# 1) the new code must at least compile with the installed Python (nothing is written)
"$PY" -B - "$SRC/votebot" <<'PYEOF'
import pathlib, sys
for f in sorted(pathlib.Path(sys.argv[1]).glob("*.py")):
    compile(f.read_text(encoding="utf-8"), str(f), "exec")
print("compile check: ok")
PYEOF

# 2) stop and back up code + database (the database may be upgraded by the new version)
TS=$(date -u +%Y%m%d_%H%M%S)
BK=/var/backups/wardogs-votebot/$TS
install -d -o root -g root -m 700 "$BK"
WAS_ACTIVE=no; systemctl is-active --quiet $SVC && WAS_ACTIVE=yes
START=$(date +%s)
systemctl stop $SVC
# Nothing has changed yet: on any error here just start the old version again.
trap 'echo "!! Backup failed; nothing was changed. Starting the old version again."; systemctl start $SVC' ERR
cp -a "$APP/votebot" "$BK/"
[[ -f "$APP/requirements.lock" ]] && cp -a "$APP/requirements.lock" "$BK/"
install -d -m 700 "$BK/data"
for f in "$DATA"/votebot.db "$DATA"/votebot.db-wal "$DATA"/votebot.db-shm; do
  if [[ -f "$f" ]]; then cp -a "$f" "$BK/data/"; fi
done
echo "backup: $BK"

rollback () {
  echo "!! The new version did not start cleanly. Rolling back to $OLD."
  systemctl stop $SVC || true
  trap - ERR
  rm -rf "$APP/votebot"; cp -a "$BK/votebot" "$APP/votebot"
  if [[ -f "$BK/requirements.lock" ]]; then cp -a "$BK/requirements.lock" "$APP/requirements.lock"; fi
  if [[ $LOCK_CHANGED == yes && -f "$APP/requirements.lock" ]]; then
    "$APP/venv/bin/pip" install --quiet --require-hashes --no-deps -r "$APP/requirements.lock" || true
  fi
  rm -f "$DATA"/votebot.db "$DATA"/votebot.db-wal "$DATA"/votebot.db-shm
  cp -a "$BK/data/." "$DATA/"
  systemctl start $SVC
  echo "Rolled back. Recent log:"
  journalctl -u $SVC --since "@$START" --no-pager | tail -40
  exit 1
}

# 3) install the new code (from here on, any error rolls back)
trap rollback ERR
rm -rf "$APP/votebot"
cp -r "$SRC/votebot" "$APP/votebot"
cp "$SRC/requirements.lock" "$APP/requirements.lock"
find "$APP/votebot" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$APP/votebot" "$APP/requirements.lock"
find "$APP/votebot" -type d -exec chmod 755 {} +; find "$APP/votebot" -type f -exec chmod 644 {} +
chmod 644 "$APP/requirements.lock"
if [[ $LOCK_CHANGED == yes ]]; then
  echo "installing updated libraries (hash-checked)"
  "$APP/venv/bin/pip" install --quiet --require-hashes --no-deps -r "$APP/requirements.lock" || rollback
  chown -R root:root "$APP/venv"
fi

# 4) start and watch
trap - ERR
START=$(date +%s)
systemctl start $SVC
echo "waiting for the bot to load its vote options (up to 90 s)"
for _ in $(seq 1 45); do
  sleep 2
  LOG=$(journalctl -u $SVC --since "@$START" --no-pager -o cat 2>/dev/null || true)
  if grep -qE "Traceback|Configuration error|Refusing to run" <<<"$LOG" || systemctl is-failed --quiet $SVC; then
    rollback
  fi
  if grep -q "catalog loaded" <<<"$LOG"; then
    echo
    grep -E "starting vote bot|server .*rcon=|catalog loaded|synced" <<<"$LOG" | tail -8
    echo
    echo "Updated $OLD -> $NEW. Backup kept in $BK"
    exit 0
  fi
done
systemctl is-active --quiet $SVC || rollback
echo "The bot is running but hasn't logged 'catalog loaded' yet (game server slow or unreachable?)."
echo "Check: journalctl -u $SVC -n 50 --no-pager     Backup: $BK"
[[ $WAS_ACTIVE == yes ]] || echo "(It was stopped before the update.)"
