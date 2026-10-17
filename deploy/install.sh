#!/usr/bin/env bash
# Installs the bot on a Debian/Ubuntu server as a hardened systemd service.
# Run from the unpacked project folder:  sudo bash deploy/install.sh
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }

APP=/opt/wardogs-votebot
ETC=/etc/wardogs-votebot
SRC="$(cd "$(dirname "$0")/.." && pwd)"

PY=$(command -v python3.13 || command -v python3.12 || command -v python3.11 || true)
[[ -n "$PY" ]] || { apt-get update && apt-get install -y python3 python3-venv; PY=$(command -v python3); }
"$PY" -c 'import sys; assert sys.version_info >= (3,11), "need Python 3.11+"'
"$PY" -c 'import venv' 2>/dev/null || apt-get install -y python3-venv

id votebot &>/dev/null || useradd --system --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin votebot

install -d -o root -g root -m 755 "$APP"
rm -rf "$APP/votebot"
cp -r "$SRC/votebot" "$SRC/requirements.lock" "$APP/"
find "$APP/votebot" -name __pycache__ -prune -exec rm -rf {} +
chown -R root:root "$APP"; chmod -R go-w "$APP"
"$PY" -m venv "$APP/venv"
"$APP/venv/bin/pip" install --upgrade --quiet pip
"$APP/venv/bin/pip" install --quiet --require-hashes --no-deps -r "$APP/requirements.lock"
chown -R root:root "$APP/venv"   # the bot user cannot modify its own code or libraries

install -d -o root -g votebot -m 750 "$ETC"
[[ -f "$ETC/votebot.env" ]] || install -o root -g votebot -m 640 "$SRC/config/votebot.env.example" "$ETC/votebot.env"
[[ -f "$ETC/messages.txt" ]] || install -o root -g votebot -m 644 "$SRC/config/messages.txt" "$ETC/messages.txt"
[[ -f "$ETC/options.json" ]] || install -o root -g votebot -m 640 "$SRC/config/options.json.example" "$ETC/options.json"

install -o root -g root -m 644 "$SRC/deploy/wardogs-votebot.service" /etc/systemd/system/wardogs-votebot.service

# --- secrets as (encrypted, if supported) systemd credentials ---
store_secret () {  # $1 = file name, $2 = credential id used in the unit, $3 = prompt
  local name="$1" cred="$2" val
  read -r -s -p "$3: " val; echo
  [[ -n "$val" ]] || { echo "empty, skipped"; return; }
  local sdver; sdver=$(systemctl --version | awk 'NR==1{print $2}')
  if command -v systemd-creds &>/dev/null && [[ "${sdver:-0}" -ge 250 ]]; then
    install -d -m 700 /etc/credstore.encrypted
    printf '%s' "$val" | systemd-creds encrypt --name="$cred" - "/etc/credstore.encrypted/$name"
    chmod 600 "/etc/credstore.encrypted/$name"
  else
    install -d -m 700 /etc/credstore
    printf '%s' "$val" > "/etc/credstore/$name"; chmod 600 "/etc/credstore/$name"
    sed -i "s|^LoadCredentialEncrypted=|#LoadCredentialEncrypted=|; s|^#LoadCredential=|LoadCredential=|" \
        /etc/systemd/system/wardogs-votebot.service
  fi
  unset val
}
if [[ "${1:-}" != "--no-secrets" ]]; then
  store_secret votebot_discord_token discord_token "Paste the Discord bot token (input hidden)"
  store_secret votebot_rcon_password rcon_password "Paste the WARDOGS RCON password (input hidden)"
fi

systemctl daemon-reload
echo
echo "Installed. Next:"
echo "  1) edit $ETC/votebot.env  (DISCORD_GUILD_ID, RCON_BASE_URL, ...)"
echo "  2) sudo systemctl enable --now wardogs-votebot"
echo "  3) journalctl -u wardogs-votebot -f"
echo "  4) systemd-analyze security wardogs-votebot   (should be well under 2.0)"
echo "  Later updates: git pull, then sudo bash deploy/update.sh"
echo "  More servers: see README (servers.json + deploy/add_server.sh)"
