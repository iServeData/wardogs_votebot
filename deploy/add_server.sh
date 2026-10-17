#!/usr/bin/env bash
# Stores the RCON password for one more game server and lets the bot read it.
#   sudo bash deploy/add_server.sh <id>        e.g. sudo bash deploy/add_server.sh eu
# <id> must match the "id" of the server in servers.json (lowercase letters, digits, _).
# Creates /etc/credstore.encrypted/votebot_rcon_password_<id> (encrypted for this machine) and a
# drop-in /etc/systemd/system/wardogs-votebot.service.d/rcon_<id>.conf that loads it as the
# credential "rcon_password_<id>". The main unit file is not edited. Nothing is restarted.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
ID="${1:-}"
[[ "$ID" =~ ^[a-z0-9_]{1,32}$ ]] || { echo "usage: sudo bash deploy/add_server.sh <id>   (lowercase letters, digits, _)"; exit 1; }
CRED="rcon_password_$ID"
DROP=/etc/systemd/system/wardogs-votebot.service.d
read -r -s -p "Paste the RCON password for server '$ID' (input hidden): " PWVAL; echo
[[ -n "$PWVAL" ]] || { echo "empty, nothing stored"; exit 1; }
install -d -m 755 "$DROP"
SDVER=$(systemctl --version | awk 'NR==1{print $2}')
if command -v systemd-creds &>/dev/null && [[ "${SDVER:-0}" -ge 250 ]]; then
  install -d -m 700 /etc/credstore.encrypted
  printf '%s' "$PWVAL" | systemd-creds encrypt --name="$CRED" - "/etc/credstore.encrypted/votebot_$CRED"
  chmod 600 "/etc/credstore.encrypted/votebot_$CRED"
  LINE="LoadCredentialEncrypted=$CRED:/etc/credstore.encrypted/votebot_$CRED"
else
  install -d -m 700 /etc/credstore
  printf '%s' "$PWVAL" > "/etc/credstore/votebot_$CRED"; chmod 600 "/etc/credstore/votebot_$CRED"
  LINE="LoadCredential=$CRED:/etc/credstore/votebot_$CRED"
fi
unset PWVAL
printf '# RCON password for game server "%s" (see servers.json)\n[Service]\n%s\n' "$ID" "$LINE" > "$DROP/rcon_$ID.conf"
chmod 644 "$DROP/rcon_$ID.conf"
systemctl daemon-reload
echo "Stored. The bot will read it as credential '$CRED'."
echo "Next: add the server to /etc/wardogs-votebot/servers.json (id \"$ID\"), then:"
echo "  sudo systemctl restart wardogs-votebot"
