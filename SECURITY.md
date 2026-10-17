# Security

## Reporting a problem

Please don't open a public issue for a security problem. Use GitHub's **Report a vulnerability**
(Security tab → Advisories) on this repository, or contact the maintainers privately. Include what you
found, how to reproduce it, and which version (`/voteadmin status` shows it).

## What the bot protects

- **Secrets**: the Discord token and each server's RCON password. They are loaded from systemd encrypted
  credentials or `chmod 600` files, never from `votebot.env`, never logged (redacted if they ever appear).
- **Game servers**: the RCON password can kick, ban and rewrite server config. The bot only changes one
  rotation line per write, validates first, uses revision checks, verifies afterwards and disables itself
  on anything unexpected.
- **Players**: a Discord account can only be linked to a SteamID whose player received the code in-game.

## Deployment checklist

- Allow-list the bot machine's IP on every RCON endpoint; prefer `https://`.
- Keep the systemd unit's sandboxing (`systemd-analyze security wardogs-votebot`).
- Keep code and the virtualenv root-owned; the bot user should not be able to modify them.
- Install libraries only with `--require-hashes` (both installer scripts do).
- Never commit real `votebot.env`, `servers.json`, `options.json`, tokens, IP addresses or Discord IDs.
  CI runs `tools/check_generic.py` to catch addresses and IDs.

## Supported versions

Only the latest release gets fixes.
