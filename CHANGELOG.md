# Changelog

## 0.2.2 (2026-10-17)

- Server tool: "Test connections" no longer prints the server list twice; it prints how many are online.

## 0.2.1 (2026-10-17)

- Guided server setup: `sudo bash deploy/manage_servers.sh` adds, modifies, removes and tests game
  servers. It asks for protocol, address, port and password (hidden), tests the connection, and can set
  per-server voting settings and channels. Passwords go into encrypted systemd credentials.
- Server names come from the game server (`serverName` in `/v1/status`) and follow renames
  (`"nameSource": "server"`); a custom name is still possible.
- The join code (`/v1/server-id`) is recorded so the same server can't be added twice.
- Every change is checked with the bot's own rules, backed up, and undone if the bot doesn't restart cleanly.
- `deploy/add_server.sh` was replaced by the guided tool. README section on several servers rewritten.

## 0.2.0 (2026-10-17)

Several servers
- One bot can serve up to 10 WARDOGS servers (`SERVERS_FILE`, see `config/servers.json.example`).
  Players link once; `/vote` and `/voteup` go to the server the player is on.
- `/votestatus`, `/voteoptions` and `/voteadmin` take an optional `server` choice. Admin actions that change
  something require it when there are several servers. Channel posts start with `[Server name]`.
- Each server has its own rounds, votes, limits, rotation handling, safety stops and health entry.
- The database is upgraded automatically (backup: `votebot.db.v1_backup`). Existing data belongs to the
  server id `main`, so a single-server install keeps working with no config change.
- `deploy/add_server.sh` stores another server's RCON password as an encrypted credential.

Vote options
- Map layouts: a map with several base layouts (e.g. `Bakurani_KOTH_02`) gets its own choice.
  Name it with `"layouts"` in options.json, or hide it with `false`.
- Readable weather names ("Day Late Rain"), renamable with `"lightingNames"` without limiting the list.
- Circle names drop the map name ("Small Factory Circle").
- Optional `BLOCK_CURRENT_MAP=true`: the map that is playing can't be voted for the next round.

Operations
- `/voteadmin status` shows the bot version; the version is also logged at startup.
- Health snapshot: a `servers` list with each server's RCON and player count when there are several
  (the single-server format is unchanged).
- `deploy/update.sh`: backup, install, start, watch the log, automatic rollback.
- CI tests on Python 3.12 and 3.13, a check for private values, Dependabot for libraries and Actions.
- `requirements.lock` now also covers Python 3.12 (`typing-extensions`); `tools/update_lock.sh` refreshes it.
- Generic wording and examples throughout; the Windows installer was removed.

## 0.1.10

- Bakurani KOTH_02 offered as its own map choice; readable weather names.

## 0.1.9

- `/voteup` upvotes; health snapshot for the status page.
