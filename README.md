# WARDOGS Map Vote Bot (Discord)

Lets players who are **on your WARDOGS server right now** vote in Discord for the next map, layout, game mode
(Standard / Infantry / Hardcore), weather and play-area circle. The winner is written into the **next slot of
your normal ordered rotation**. The current match is never interrupted, and the default entry is put back once
the voted round has started.

One bot can serve **several game servers**. Players link their Discord account once; their votes always go to
the server they are playing on.

```
Thank you for your vote for Bakurani (New spawns), Hardcore, Rain, Farmland Circle. Next map results:
* Zestafona, Infantry, Day Clear, Houses Circle - 5 | Current next map
* Ozeti, Standard, Day Early Fog, Church Circle - 5
* Bakurani (New spawns), Hardcore, Rain, Farmland Circle - 2

Seed nights are posted in Discord. Bring a friend and help fill the server.
Voting limit: 2 of 3 votes used for the current map.
```

Requirements: Python 3.11+ (3.12 recommended), a Linux host with systemd 250+ (or Docker), a Discord
application, and RCON access to each WARDOGS server.

---

## How it works

| Rule | How the bot enforces it |
|---|---|
| Voter must be on the server | Polls `GET /v1/players` every 15 s. The voter's linked SteamID must be in the latest list, and the list must be fresh (if RCON is unreachable, votes are refused rather than guessed). |
| On the server at least 10 minutes | Tracks a continuous session per SteamID. Drop-outs shorter than `PRESENCE_GRACE_SECONDS` (default 3 min, e.g. map loads) don't reset the timer. |
| 3 votes per map | Counted per round **by Discord ID and by SteamID**, so unlinking/relinking or a second Discord account gains nothing. |
| Optional upvotes | With `UPVOTES_PER_MAP` above 0, `/voteup` lets players back a pick that is already listed, separate from their own votes. |
| Votes never change the map | Casting a vote only writes to the bot's database. |
| Winner becomes the next map | `VOTE_CLOSE_MODE=live` queues the leader automatically (and swaps it if another pick takes the lead). `admin` waits for `/voteadmin finalize`; `timer` closes after a set time. Either way the bot replaces the single rotation entry that is next, then the match ends naturally and the server advances into it. |
| Ties | The option that **reached** the tied total first keeps the slot. A newer option has to strictly pass it. |
| New map = votes reset | A new round starts when `/v1/status` shows a different map/mode/circle, the faction scores reset (same map replayed), or the server restarts. |
| Who is who | `/link` sends a one-time 6-character code to the player **in-game** (RCON whisper); `/verify` completes it. Nobody can claim another player's SteamID. |

## Commands

| Command | Who | What |
|---|---|---|
| `/vote map mode weather [circle]` | linked players on a server | Cast a vote. Every option autocompletes; `circle` follows the chosen map. |
| `/voteup choice` | linked players on a server | Upvote a pick that already has votes (only when `UPVOTES_PER_MAP` is above 0). |
| `/votestatus [server]` | everyone | Current map, next-map results and your votes used. With several servers and no choice, shows every server unless you are playing on one. |
| `/voteoptions [map] [server]` | everyone | Lists maps, layouts, game modes, weather and each map's circles. |
| `/link player` | everyone | Pick your in-game name (autocompletes from who's online on any server). The code arrives in in-game chat. |
| `/verify code` | everyone | Finish linking. 5 wrong tries cancels the code; codes expire after 5 min. |
| `/mystatus` | linked players | Online? Minutes on the server? Eligible? Votes used. |
| `/unlink` | linked players | Removes your link (doesn't refund votes). |
| `/voteadmin status [server]` | admins | Bot version, RCON health, voting state, override state, the server's now/next rotation entries. |
| `/voteadmin finalize [server]` | admins | Close voting and put the winner in the next rotation slot. Nothing changes in the current match. |
| `/voteadmin canceloverride [server]` | admins | Put the default rotation entry back now and reopen voting. |
| `/voteadmin enableapply [server]` | admins | Re-enable rotation writes after the bot stopped itself for safety. |
| `/voteadmin newround [server]` / `reload [server]` / `unlink member` | admins | Force a new round, reload vote options, remove a member's link. |

The `server` option only matters when the bot serves more than one server. For actions that change something
(`finalize`, `canceloverride`, `enableapply`, `newround`) it is then required, so an admin can't act on the wrong
server by accident.

Rate limits (per user): queries burst 8 then 1 every 3 s; votes burst 3 then 1 every 10 s; `/link` 1 per 2 min
(plus 2 min per target player, so nobody can spam a player's chat); autocomplete 4/s. Six limit hits within
a minute = 5-minute silent block. A global cap protects the bot and the RCON budget.

---

## Several game servers

Without a servers file the bot serves one server, configured entirely in `votebot.env`. To add more, create
`/etc/wardogs-votebot/servers.json` (see `config/servers.json.example`) and set
`SERVERS_FILE=/etc/wardogs-votebot/servers.json` in `votebot.env`.

```json
{
  "servers": [
    { "id": "main", "name": "NA West", "rconBaseUrl": "https://203.0.113.10:7779",
      "rconPasswordCredential": "rcon_password" },
    { "id": "eu", "name": "EU Central", "rconBaseUrl": "https://198.51.100.20:7779",
      "optionsFile": "/etc/wardogs-votebot/options_eu.json",
      "settings": { "MAX_VOTES_PER_MAP": 2, "BLOCK_CURRENT_MAP": true } }
  ]
}
```

| Key | Meaning |
|---|---|
| `id` | Internal id: lowercase letters, digits, `_`. Stored data is keyed by it, so don't rename it later. |
| `name` | Shown to players (`[EU Central] …` on channel posts, the `server` choice in commands). |
| `rconBaseUrl`, `allowInsecureHttp`, `rconCaFile` | That server's RCON endpoint (same rules as the single-server settings). |
| `rconPasswordCredential` | systemd credential holding its RCON password. Default `rcon_password_<id>`. |
| `rconPasswordFile` | Alternative to a credential: a `chmod 600` file (Docker, testing). |
| `optionsFile`, `messagesFile`, `inGameName` | Per-server vote options, footer lines, and the name used in in-game whispers. |
| `announceChannelId`, `adminAlertChannelId` | Per-server channels (otherwise the shared ones from `votebot.env`). |
| `settings` | Any other `votebot.env` setting for this server only, e.g. `VOTE_CLOSE_MODE`, `MAX_VOTES_PER_MAP`. |

Rules the bot checks at startup: at most 10 servers; ids, names and RCON addresses must be unique; RCON
passwords are never shared between servers; settings that belong to the whole bot (`DISCORD_GUILD_ID`,
`VOTE_CHANNEL_IDS`, `ADMIN_ROLE_IDS`, `DATA_DIR`, `LOG_LEVEL`, `EPHEMERAL_REPLIES`, `LINK_CODE_TTL_SECONDS`)
can't be set per server.

Storing a server's RCON password (encrypted for this machine; the main unit file is not edited):

```
sudo bash deploy/add_server.sh eu
sudo systemctl restart wardogs-votebot
```

How players experience it:

- `/link` once, on any server. The link works on every server.
- `/vote` and `/voteup` go to the server they are playing on. If they aren't on any, the bot lists the servers.
- Each server has its own rounds, votes, limits, rotation handling and safety stops.

Moving from one server to several: the existing data (rounds, votes, rotation state) belongs to the server
id the bot used before, `main`. Give your existing server `"id": "main"` and
`"rconPasswordCredential": "rcon_password"` in the servers file and nothing else changes for it.

---

## Rotation-slot override

```
default:          [0][1][2 = now][3 = next][4][5][6][7][8]
finalize:         [0][1][2 = now][3 = VOTE ][4][5][6][7][8]   one line changed, current match untouched
match ends:       server advances normally into slot 3 = the vote (kept in place while it plays)
voted round ends: [0][1][2][3 = VOTE ][4 = now][5 = next]...
then:             [0][1][2][3 = default][4 = now][5 = next]  default line restored (RESTORE_DELAY_SECONDS after slot 4 loads)
```

Live mode adds no extra polling. It runs on the normal status check every `RCON_POLL_SECONDS` (15 s, 2 requests)
and writes only when the leader changes. One write costs about 15 RCON requests, including the check afterwards.

Back-to-back votes: when people vote while the previous winner is still playing, the new winner goes into the
slot after it. The previous winner's line stays put until the server has moved past it, then it is restored.

Why the restore waits one extra round: after any rotation write the server looks for the running map in the new
list. If it can't find it, it loses its place and starts again at entry 0. So the bot never edits the entry
that is playing. For the same reason, if the current map isn't in the rotation at all (for example after an
admin jumped to a map), the vote goes into **slot 0**, because that is where the server restarts. If a write
still moves the pointer, the bot moves its entry there once and checks again. If it moves again, it stops safely.

Every write follows the same steps:

1. Fresh `GET /v1/config` (+ revision), `GET /v1/rotation`, `GET /v1/status`. Refuse unless rotation is enabled,
   **Ordered**, and the live rotation matches the document entry for entry.
2. Change **exactly one line**: the entry the server marks `next`. Comments, every other section, the
   `!RotationEntries=ClearArray` directive and line endings stay byte-for-byte identical.
3. `POST /v1/config/validate` first. Refuse if the server reports errors or changes outside the rotation.
4. `PUT /v1/config` with `If-Match: "<revision>"`, never `force=true`. If an admin panel, the host's web panel
   or a console changed the config in the meantime, the server answers 412 and nothing is written.
5. Verify for about 12 s: the vote slot shows as `next`, and the current map/mode/circle did not change. If the
   map changes or the next pointer moves, the bot puts the default line back, **disables itself**, and alerts
   staff (`/voteadmin enableapply` turns it back on).

Restoring works the same way. The bot puts the default line back **only if** the slot still holds its own line
and every other entry is exactly as it left it. Otherwise staff changes win: the bot writes nothing, drops its
override and posts an alert containing the default line. If a different map loads than the winner (an admin
changed or skipped the map), the bot alerts and removes its entry so it doesn't play a round late. State is
saved in the database, so a restart in the middle of this still restores correctly.

### Admin panel rules while an override is active

- Avoid "next map", "jump to map" or rotation edits in your admin tools between finalize and the voted map
  loading. If you must override, just do it: the bot detects it, keeps your change and alerts the staff channel.
- Finalize **before** the match-end screen. Once the server has picked its next entry, a later edit can't change it.

---

## Setup

### 1. Create the Discord bot

1. <https://discord.com/developers/applications>, **New Application**, name it (e.g. *Map Vote*).
2. **Bot** tab:
   - **Reset Token**, copy it. Treat it like a password and never paste it in Discord.
   - Turn **Public Bot OFF**. Leave **all three Privileged Gateway Intents OFF**. **Requires OAuth2 Code Grant** OFF.
3. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands`; permissions **View Channels, Send Messages,
   Embed Links** only. Open the URL and add the bot to your Discord server.
4. Discord **User Settings → Advanced → Developer Mode ON**. Right-click your server, **Copy Server ID**: that's
   `DISCORD_GUILD_ID`. Optionally copy the vote channel ID (`VOTE_CHANNEL_IDS`) and admin role IDs.

The bot only works in `DISCORD_GUILD_ID`. If someone adds it anywhere else, it leaves immediately.

### 2. Prepare WARDOGS RCON (for each game server)

1. In the host panel, enable RCON (or `[/Script/WDRCON.WDRCONSettings] bEnabled=true` in `ServerSettings.ini`)
   and note the **address, port and password**.
2. **Use a long random password** (32+ characters). The same token can kick, ban and rewrite config.
3. **Allow-list the bot machine's public IP** so nobody else can reach RCON (`AllowedHosts` in the RCON block,
   or `Configs/APIWhitelist.txt` on hosts that use it; ask your host if neither is exposed).
4. Prefer `https://`. If the host only offers `http://`, the password crosses the internet in clear text: set
   `RCON_ALLOW_INSECURE_HTTP=true` only together with the IP allow-list, or tunnel it. For a self-signed
   certificate, set `RCON_CA_FILE` to pin it instead of disabling verification.

### 3. Install (Linux with systemd)

```
git clone <this repository> wardogs_votebot
cd wardogs_votebot
sudo bash deploy/install.sh
sudo nano /etc/wardogs-votebot/votebot.env
sudo nano /etc/wardogs-votebot/messages.txt
sudo systemctl enable --now wardogs-votebot
journalctl -u wardogs-votebot -f
```

`install.sh` creates the `votebot` user, a virtualenv with hash-verified libraries, root-owned code, the hardened
unit, and stores the Discord token and RCON password as encrypted systemd credentials (you're prompted; input is
hidden). It never overwrites an existing `votebot.env`, `messages.txt` or `options.json`.

Optional health snapshot for a status page: copy `deploy/health.conf` to
`/etc/systemd/system/wardogs-votebot.service.d/health.conf` and run `sudo systemctl daemon-reload`.

The bot needs **no inbound ports**. It only connects out to Discord and to RCON.

### Updating

```
cd wardogs_votebot
git pull
sudo bash deploy/update.sh --check
sudo bash deploy/update.sh
```

`--check` shows the installed and new version and which files differ, without changing anything. The update
checks the new code compiles, stops the bot, backs up the code and database to
`/var/backups/wardogs-votebot/<time>`, installs the new code (root-owned), installs libraries only if
`requirements.lock` changed, starts the bot and watches the log. If the bot doesn't come up cleanly, it puts the
backup back and restarts the old version. It never edits the unit file, `votebot.env`, options or credentials.

### Docker

```
mkdir -p secrets && chmod 700 secrets
printf '%s' 'PASTE_DISCORD_TOKEN' > secrets/discord_token
printf '%s' 'PASTE_RCON_PASSWORD' > secrets/rcon_password
sudo chown 10001:10001 secrets/* && sudo chmod 600 secrets/*
cp config/votebot.env.example config/votebot.env && nano config/votebot.env
cp config/options.json.example config/options.json
docker compose up -d --build && docker compose logs -f
```

For several servers in Docker, use `rconPasswordFile` per server with files under `/run/secrets/`.

### 4. Test before going live

Pick an empty-server window and set `ADMIN_ALERT_CHANNEL_ID` to a staff channel first.

1. `DRY_RUN=true`, restart, run `/voteadmin status`: RCON OK, the player count, vote slot override none.
2. `/link` + `/verify`, wait 10 min, vote (temporarily `MIN_VOTES_TO_FINALIZE=1` for a solo test). Confirm the
   server's rotation is unchanged.
3. `/voteadmin finalize`. In dry run the server only validates: "Dry run OK … would replace rotation slot N".
4. `DRY_RUN=false`, restart, vote again, `/voteadmin finalize`. Check `/v1/rotation` or your admin tool: **only**
   the next entry changed, and the current match keeps running.
5. Let the match end naturally. The voted map, mode, weather and circle should load, and about 45 s after the
   *following* match starts the default entry is back (`audit.log`: `VOTE_SLOT_APPLIED`, `VOTE_SLOT_CONSUMED`,
   `VOTE_SLOT_RESTORED`). `tools/check_rcon_config.py` shows what the server will play next.
6. If step 4 changes or restarts the current match, your build applies rotation edits immediately. The bot will
   have disabled itself. Set `MAP_APPLICATION_MODE=off` and apply winners by hand.

---

## Configuration reference (`votebot.env`)

| Setting | Default | Meaning |
|---|---|---|
| `DISCORD_GUILD_ID` | required | Your Discord server ID |
| `VOTE_CHANNEL_IDS` | empty (any) | Only answer in these channels |
| `ADMIN_ROLE_IDS` | empty | Roles for `/voteadmin` (otherwise Manage Server) |
| `RCON_BASE_URL` | required (single server) | e.g. `https://203.0.113.10:7779` |
| `RCON_ALLOW_INSECURE_HTTP` | `false` | Allow `http://` to a remote host |
| `RCON_CA_FILE` | empty | Pin a self-signed RCON certificate |
| `MIN_PRESENCE_MINUTES` | `10` | Required continuous time on the server |
| `PRESENCE_GRACE_SECONDS` | `180` | Disconnect tolerance before the timer resets |
| `MAX_VOTES_PER_MAP` | `3` | Votes per player per map |
| `UPVOTES_PER_MAP` | `0` | `/voteup` budget per player per map (0 = off) |
| `UPVOTE_MAX_PER_OPTION` | `0` | Most upvotes one player may give the same pick (0 = no extra cap) |
| `BLOCK_CURRENT_MAP` | `false` | Refuse votes for the map that is playing now |
| `VOTE_COUNTING` | `additive` | `additive`: each vote counts. `latest`: only each player's newest vote counts |
| `MAP_APPLICATION_MODE` | `rotation_slot` | `rotation_slot`: write the winner into the next slot. `off`: votes only |
| `VOTE_CLOSE_MODE` | `admin` | `live`, `admin` (`/voteadmin finalize`) or `timer` |
| `VOTE_CLOSE_AFTER_MINUTES` | `30` | Timer mode: minutes after the match starts |
| `LIVE_APPLY_SETTLE_SECONDS` | `20` | Live mode: a new leader must hold the lead this long before it is written |
| `LIVE_APPLY_MIN_INTERVAL_SECONDS` | `60` | Live mode: minimum time between rotation writes |
| `LIVE_APPLY_MAX_WRITES` | `8` | Live mode: automatic writes per match, then staff are alerted |
| `MIN_VOTES_TO_FINALIZE` | `3` | The leader needs this many votes, otherwise the rotation is not touched |
| `RESTORE_DELAY_SECONDS` | `45` | Wait before restoring the default entry |
| `ANNOUNCE_CHANNEL_ID` | empty | Public "next round" posts |
| `ADMIN_ALERT_CHANNEL_ID` | empty | Staff channel for conflicts, safety stops, failed restores (recommended) |
| `BROADCAST_MODE` | `off` | In-game chat: `off`, `match_end` or `changes` |
| `DETECT_SCORE_RESET` | `true` | Detect a new match when the same map replays |
| `EPHEMERAL_REPLIES` | `true` | `false` makes successful vote/status replies public (errors and link codes stay private) |
| `RCON_POLL_SECONDS` | `15` | Player/status poll interval |
| `SERVER_DISPLAY_NAME` | `Map vote bot` | Name at the start of in-game whispers |
| `DRY_RUN` | `false` | Finalize runs the server's validate only; nothing is written |
| `MESSAGES_FILE` | `DATA_DIR/messages.txt` | Footer lines, re-read automatically on change |
| `OPTIONS_FILE` | empty | Rename, hide or restrict vote options (see below) |
| `SERVERS_FILE` | empty | Serve several game servers (see above) |
| `HEALTH_FILE`, `HEALTH_INTERVAL_SECONDS` | empty, `20` | Optional private health snapshot (see `deploy/health.conf`) |

Secrets never go in `votebot.env`. They come from systemd encrypted credentials (installer default), or
`DISCORD_TOKEN_FILE` / `RCON_PASSWORD_FILE` (must be `chmod 600`).

### Vote options (`options.json`)

Options come from each server's live catalog (`/v1/catalog/maps`, `/lightings`, `/maps/{map}/experiences`,
`/maps/{map}/alternators`), refreshed hourly. Without an options file, everything the server offers is listed.

```json
{
  "maps":   { "Kavkazi": { "name": "Bakurani" }, "Europe": { "name": "Ozeti", "enabled": true } },
  "layouts": { "Bakurani_KOTH_02": "New spawns" },
  "modes":  { "Standard": [], "Infantry": ["KOTH_InfantryOnly"], "Hardcore": ["KOTH_Hardcore"] },
  "lightingNames": { "DayLateRain": "Rain" }
}
```

| Key | Effect |
|---|---|
| `maps` | Rename a map, or hide it with `"enabled": false`. |
| `onlyMaps` | List of map ids: offer only these. |
| `layouts` | A map with several base layouts (e.g. `Bakurani_KOTH_01` and `_02`) gets one extra choice per layout, listed as "Bakurani (Layout 2)". Name it here, or hide it with `false`. |
| `modes` | Mode name → extra experiences added to the layout. |
| `lightingNames` | Rename weather without limiting which ones are offered. |
| `lightings` | List of ids to offer only those, or an object of id → name to offer exactly those. |

Weather and circle names are made readable automatically ("DayLateRain" → "Day Late Rain",
"Zestafona SmallFactory Circle" → "Small Factory Circle"). Every submitted value is checked against this list on
the bot side; autocomplete is never trusted.

---

## Security summary

- **Secrets**: systemd `LoadCredentialEncrypted` (encrypted at rest, never in env or `ps`), redacted from logs,
  file-permission checks, refuses to run as root. Each server's RCON password is separate.
- **Sandbox** (systemd): dedicated no-login user, read-only OS and code (root-owned code and venv), private /tmp
  and devices, no capabilities, syscall allow-list, `MemoryDenyWriteExecute`, IP/Unix sockets only, resource caps.
- **Supply chain**: dependencies pinned **with SHA-256 hashes** (`requirements.lock`, `--require-hashes`). One
  direct dependency (discord.py). Dependabot proposes updates; CI runs the tests on every change.
- **Discord**: no privileged intents, minimal permissions, single-guild lock, `AllowedMentions.none()`, markdown
  from players and servers escaped.
- **Abuse**: per-user token buckets plus temporary blocks, a global cap, a per-target whisper cooldown, link codes
  hashed in the database, 5 attempts max, constant-time comparison.
- **RCON**: TLS verification on by default, plain HTTP to a remote host refused unless allowed, strict timeouts,
  1 MB response cap, honours `429 Retry-After`, a local request budget, validated path parameters, no redirects,
  proxy variables ignored.
- **Data**: SQLite, parameterised queries only, files `0600`, audit log of every vote, link and rotation change.

See [SECURITY.md](SECURITY.md) to report a problem.

## Things to verify on your server

The WARDOGS RCON API is documented by the community, not the developer, and it changes between builds:

- Rotation edits should apply "from the next map change". Step 4 of the test plan proves that on your build.
- Live builds don't report a match clock, so `VOTE_CLOSE_MODE=timer` counts from when the bot saw the match start.
- New-match detection relies on map changes and score resets. If a round isn't detected, `/voteadmin newround`.

## Development

```
python3 -m venv .venv
.venv/bin/pip install --require-hashes --no-deps -r requirements.lock
.venv/bin/pip install --require-hashes --no-deps -r requirements-dev.lock
.venv/bin/python -m pytest -q
```

`tools/mock_rcon.py` is a fake WARDOGS server (config document, ordered rotation, revisions, two layouts of one
map, and a `/v1/match/map` that travels immediately so tests prove the bot never calls it). Never expose it.

```
votebot/   bot source: config, rcon, catalog, presence, tally, rotation, service, hub (several servers), bot (Discord)
config/    votebot.env.example, servers.json.example, options.json.example, messages.txt
deploy/    hardened systemd unit, health drop-in, install.sh, update.sh, add_server.sh
tools/     mock_rcon.py, check_rcon_config.py, probe_rotation_pointer.py, update_lock.sh, check_lock.py, check_generic.py
tests/     unit and end-to-end tests
```

## License

MIT, see [LICENSE](LICENSE).
