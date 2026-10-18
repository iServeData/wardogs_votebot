"""Configuration loading.

All settings come from environment variables (normally an EnvironmentFile
loaded by systemd, or docker-compose `env_file`). Secrets can be supplied
three ways, in order of preference:

1. systemd credentials ($CREDENTIALS_DIRECTORY/<name>) - never touch disk
   in the clear outside root-owned /etc/credstore, never visible in `ps`.
2. <NAME>_FILE pointing to a file readable only by the bot user.
3. <NAME> directly in the environment (least preferred).
"""

from __future__ import annotations

import ipaddress
import os
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(RuntimeError):
    pass


DEFAULT_SERVER_ID = "main"
_SERVER_ID = re.compile(r"^[a-z0-9_]{1,32}$")


def _secret(name: str, cred_name: str, required: bool = True, env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    cred_dir = env.get("CREDENTIALS_DIRECTORY")
    if cred_dir:
        p = Path(cred_dir) / cred_name
        if p.is_file():
            return p.read_text(encoding="utf-8").strip()
    file_var = env.get(f"{name}_FILE")
    if file_var:
        p = Path(file_var)
        if not p.is_file():
            raise ConfigError(f"{name}_FILE points to a missing file: {p}")
        _check_private(p, name)
        return p.read_text(encoding="utf-8").strip()
    val = env.get(name, "").strip()
    if required and not val:
        raise ConfigError(f"Missing secret {name} (set {name}_FILE, a systemd credential '{cred_name}', or {name})")
    return val


def _check_private(p: Path, name: str) -> None:
    """Refuse secret files that other local users can read (POSIX only)."""
    if os.name != "posix":
        return
    mode = p.stat().st_mode & 0o777
    if mode & 0o077:
        raise ConfigError(f"{name}_FILE {p} is readable by group/other (mode {oct(mode)}); run: chmod 600 {p}")


_ENV: Mapping[str, str] = os.environ  # the mapping the helpers below read (swapped per server)


def _env(name: str) -> str | None:
    """Read a non-secret setting, tolerating trailing '# comments'."""
    raw = _ENV.get(name)
    return None if raw is None else raw.split(" #")[0].strip()


def _int(name: str, default: int, lo: int, hi: int) -> int:
    raw = _env(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer") from e
    if not lo <= v <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}")
    return v


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _choice(name: str, default: str, allowed: set[str]) -> str:
    v = (_env(name) or default).lower()
    if v not in allowed:
        raise ConfigError(f"{name} must be one of: {', '.join(sorted(allowed))}")
    return v


def _opt_id(name: str) -> int | None:
    v = _env(name) or ""
    if not v:
        return None
    if not v.isdigit():
        raise ConfigError(f"{name} must be a numeric Discord channel ID")
    return int(v)


def _ids(name: str) -> frozenset[int]:
    raw = _ENV.get(name, "")
    out = set()
    for part in raw.replace(" ", "").split(","):
        if part:
            if not part.isdigit():
                raise ConfigError(f"{name} must be a comma-separated list of numeric Discord IDs")
            out.add(int(part))
    return frozenset(out)


@dataclass(frozen=True)
class Config:
    discord_token: str = field(repr=False)
    guild_id: int
    vote_channel_ids: frozenset[int]
    admin_role_ids: frozenset[int]

    rcon_base_url: str
    rcon_password: str = field(repr=False)
    rcon_ca_file: str | None
    rcon_timeout_s: int
    rcon_max_requests_per_min: int
    poll_seconds: int

    min_presence_minutes: int
    presence_grace_seconds: int
    max_votes_per_map: int
    vote_counting: str  # "additive" | "latest"
    min_votes_to_finalize: int
    map_application_mode: str  # "rotation_slot" | "off"
    vote_close_mode: str       # "admin" | "timer" | "live"
    vote_close_after_minutes: int
    live_settle_seconds: int
    live_min_interval_seconds: int
    live_max_writes: int
    restore_delay_seconds: int
    announce_channel_id: int | None
    admin_alert_channel_id: int | None
    broadcast_result: bool
    dry_run: bool
    detect_score_reset: bool

    data_dir: Path
    messages_file: Path
    options_file: Path | None
    ephemeral_replies: bool
    results_max_lines: int
    link_code_ttl_s: int
    server_display_name: str
    log_level: str
    broadcast_mode: str = "off"   # off | match_end | changes
    match_score_cap: int = 100    # live builds don't report scoreCap; KOTH plays to 100
    upvotes_per_map: int = 0         # /voteup budget per player per match (0 = /voteup off)
    upvote_max_per_option: int = 0   # max upvotes one player may give the same option (0 = no extra cap)
    block_current_map: bool = False  # refuse votes for the map that is playing right now
    server_id: str = DEFAULT_SERVER_ID   # internal id (lowercase letters, digits, _); keys stored data
    server_name: str = ""                # shown to players when the bot serves more than one server
    name_from_server: bool = False       # True: use the name the game server reports (kept up to date)

    @property
    def label(self) -> str:
        return self.server_name or self.server_display_name or self.server_id

    @staticmethod
    def load(env: Mapping[str, str] | None = None, rcon_password_credential: str = "rcon_password",
             require_rcon: bool = True) -> "Config":
        """Build a Config from environment-style settings (defaults to os.environ)."""
        global _ENV
        env = os.environ if env is None else env
        prev, _ENV = _ENV, env
        try:
            return Config._load(env, rcon_password_credential, require_rcon)
        finally:
            _ENV = prev

    @staticmethod
    def _load(env: Mapping[str, str], cred: str, require_rcon: bool) -> "Config":
        guild = env.get("DISCORD_GUILD_ID", "").strip()
        if not guild.isdigit():
            raise ConfigError("DISCORD_GUILD_ID must be your Discord server ID (numeric)")

        base = env.get("RCON_BASE_URL", "").strip().rstrip("/")
        if require_rcon or base:
            _validate_rcon_url(base, _bool("RCON_ALLOW_INSECURE_HTTP", False))

        counting = env.get("VOTE_COUNTING", "additive").strip().lower()
        if counting not in {"additive", "latest"}:
            raise ConfigError("VOTE_COUNTING must be 'additive' or 'latest'")

        data_dir = Path(env.get("DATA_DIR", "./data")).resolve()
        opts = env.get("OPTIONS_FILE", "").strip()
        ca = env.get("RCON_CA_FILE", "").strip() or None
        sid = (env.get("SERVER_ID", "") or DEFAULT_SERVER_ID).strip()
        if not _SERVER_ID.match(sid):
            raise ConfigError("SERVER_ID may only use lowercase letters, digits and _ (max 32)")

        return Config(
            discord_token=_secret("DISCORD_TOKEN", "discord_token", env=env),
            guild_id=int(guild),
            vote_channel_ids=_ids("VOTE_CHANNEL_IDS"),
            admin_role_ids=_ids("ADMIN_ROLE_IDS"),
            rcon_base_url=base,
            rcon_password=_secret("RCON_PASSWORD", cred, required=require_rcon, env=env),
            rcon_ca_file=ca,
            rcon_timeout_s=_int("RCON_TIMEOUT_SECONDS", 8, 2, 30),
            rcon_max_requests_per_min=_int("RCON_MAX_REQUESTS_PER_MIN", 120, 10, 500),
            poll_seconds=_int("RCON_POLL_SECONDS", 15, 5, 120),
            min_presence_minutes=_int("MIN_PRESENCE_MINUTES", 10, 0, 240),
            presence_grace_seconds=_int("PRESENCE_GRACE_SECONDS", 180, 30, 1800),
            max_votes_per_map=_int("MAX_VOTES_PER_MAP", 3, 1, 50),
            upvotes_per_map=_int("UPVOTES_PER_MAP", 0, 0, 50),
            upvote_max_per_option=_int("UPVOTE_MAX_PER_OPTION", 0, 0, 50),
            vote_counting=counting,
            min_votes_to_finalize=_int("MIN_VOTES_TO_FINALIZE", 3, 1, 100),
            map_application_mode=_choice("MAP_APPLICATION_MODE", "rotation_slot", {"rotation_slot", "off"}),
            vote_close_mode=_choice("VOTE_CLOSE_MODE", "admin", {"admin", "timer", "live"}),
            vote_close_after_minutes=_int("VOTE_CLOSE_AFTER_MINUTES", 30, 5, 240),
            live_settle_seconds=_int("LIVE_APPLY_SETTLE_SECONDS", 20, 0, 600),
            live_min_interval_seconds=_int("LIVE_APPLY_MIN_INTERVAL_SECONDS", 60, 15, 1800),
            live_max_writes=_int("LIVE_APPLY_MAX_WRITES", 8, 1, 30),
            restore_delay_seconds=_int("RESTORE_DELAY_SECONDS", 45, 10, 600),
            announce_channel_id=_opt_id("ANNOUNCE_CHANNEL_ID"),
            admin_alert_channel_id=_opt_id("ADMIN_ALERT_CHANNEL_ID"),
            broadcast_result=_bool("BROADCAST_RESULT", False),
            dry_run=_bool("DRY_RUN", False),
            detect_score_reset=_bool("DETECT_SCORE_RESET", True),
            data_dir=data_dir,
            messages_file=Path(env.get("MESSAGES_FILE", str(data_dir / "messages.txt"))).resolve(),
            options_file=Path(opts).resolve() if opts else None,
            ephemeral_replies=_bool("EPHEMERAL_REPLIES", True),
            results_max_lines=_int("RESULTS_MAX_LINES", 10, 1, 25),
            link_code_ttl_s=_int("LINK_CODE_TTL_SECONDS", 300, 60, 1800),
            server_display_name=env.get("SERVER_DISPLAY_NAME", "Map vote bot").strip()[:40] or "Map vote bot",
            log_level=env.get("LOG_LEVEL", "INFO").strip().upper(),
            broadcast_mode=_choice("BROADCAST_MODE",
                                   "changes" if _bool("BROADCAST_RESULT", False) else "off",
                                   {"off", "match_end", "changes"}),
            match_score_cap=_int("MATCH_SCORE_CAP", 100, 1, 100000),
            block_current_map=_bool("BLOCK_CURRENT_MAP", False),
            server_id=sid,
            server_name=env.get("SERVER_NAME", "").strip()[:100],
            name_from_server=env.get("SERVER_NAME_SOURCE", "").strip() == "server",
        )


# --------------------------------------------------------------------------- several game servers
# Settings that belong to the whole bot (one Discord bot, one database) and so can't differ per server.
GLOBAL_ONLY = frozenset({"DISCORD_GUILD_ID", "DISCORD_TOKEN", "DISCORD_TOKEN_FILE", "VOTE_CHANNEL_IDS",
                         "ADMIN_ROLE_IDS", "DATA_DIR", "LOG_LEVEL", "EPHEMERAL_REPLIES", "SERVERS_FILE",
                         "CREDENTIALS_DIRECTORY", "LINK_CODE_TTL_SECONDS"})
MAX_SERVERS = 10
_SERVER_KEYS = {"id", "name", "rconBaseUrl", "allowInsecureHttp", "rconCaFile", "rconPasswordCredential",
                "rconPasswordFile", "optionsFile", "messagesFile", "inGameName", "announceChannelId",
                "adminAlertChannelId", "settings", "nameSource", "serverName", "serverId"}


def load_servers(env: Mapping[str, str] | None = None, check_only: bool = False) -> list[Config]:
    """One Config per game server.

    Without SERVERS_FILE the bot serves exactly one server from the plain settings (the original
    single-server setup keeps working unchanged). With SERVERS_FILE, each entry adds a server; its
    values override the shared settings for that server only.
    """
    env = dict(os.environ if env is None else env)
    path = (env.get("SERVERS_FILE") or "").strip()
    if check_only:  # validate settings without reading any secret (used by the server manager)
        for k in ("CREDENTIALS_DIRECTORY", "DISCORD_TOKEN_FILE", "RCON_PASSWORD_FILE"):
            env.pop(k, None)
        env["DISCORD_TOKEN"] = "check-only-placeholder"
    if not path:
        if check_only:
            env.setdefault("RCON_PASSWORD", "check-only-placeholder")
        return [Config.load(env)]
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"SERVERS_FILE {path} not found") from None
    except (OSError, ValueError) as e:
        raise ConfigError(f"SERVERS_FILE {path} is not valid JSON ({e})") from None
    servers = doc.get("servers") if isinstance(doc, dict) else None
    if not isinstance(servers, list) or not servers:
        raise ConfigError('SERVERS_FILE needs a "servers" list with at least one server')
    if len(servers) > MAX_SERVERS:
        raise ConfigError(f"SERVERS_FILE lists {len(servers)} servers; the limit is {MAX_SERVERS}")
    out: list[Config] = []
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for n, s in enumerate(servers, start=1):
        where = f"SERVERS_FILE server #{n}"
        if not isinstance(s, dict):
            raise ConfigError(f"{where} must be an object")
        unknown = set(s) - _SERVER_KEYS - {"_comment"}
        if unknown:
            raise ConfigError(f"{where}: unknown key(s) {', '.join(sorted(unknown))}")
        sid = str(s.get("id") or "").strip()
        if not _SERVER_ID.match(sid):
            raise ConfigError(f"{where}: id must be lowercase letters, digits or _ (max 32), e.g. \"na_west\"")
        name = str(s.get("name") or "").strip()
        if not name or len(name) > 100:
            raise ConfigError(f"{where} ({sid}): name is required (max 100 characters)")
        if s.get("nameSource", "server") not in ("server", "custom"):
            raise ConfigError(f"{where} ({sid}): nameSource must be \"server\" or \"custom\"")
        if sid in seen_ids or name.lower() in seen_names:
            raise ConfigError(f"{where}: id and name must be unique ({sid} / {name})")
        seen_ids.add(sid)
        seen_names.add(name.lower())
        e = dict(env)
        settings = s.get("settings") or {}
        if not isinstance(settings, dict):
            raise ConfigError(f"{where} ({sid}): settings must be an object of NAME: value")
        bad = {k for k in settings if k in GLOBAL_ONLY or k.startswith("RCON_PASSWORD")}
        if bad:
            raise ConfigError(f"{where} ({sid}): {', '.join(sorted(bad))} can't be set per server")
        for k, v in settings.items():
            e[str(k)] = "true" if v is True else "false" if v is False else str(v)
        for key, var in (("rconBaseUrl", "RCON_BASE_URL"), ("rconCaFile", "RCON_CA_FILE"),
                         ("optionsFile", "OPTIONS_FILE"), ("messagesFile", "MESSAGES_FILE"),
                         ("inGameName", "SERVER_DISPLAY_NAME"), ("announceChannelId", "ANNOUNCE_CHANNEL_ID"),
                         ("adminAlertChannelId", "ADMIN_ALERT_CHANNEL_ID")):
            if s.get(key) not in (None, ""):
                e[var] = str(s[key])
        if "allowInsecureHttp" in s:
            e["RCON_ALLOW_INSECURE_HTTP"] = "true" if s["allowInsecureHttp"] is True else "false"
        for k in [k for k in e if k.startswith("RCON_PASSWORD")]:
            del e[k]  # never inherit another server's password
        if check_only:
            e["RCON_PASSWORD"] = "check-only-placeholder"
        elif s.get("rconPasswordFile"):
            e["RCON_PASSWORD_FILE"] = str(s["rconPasswordFile"])
        cred = str(s.get("rconPasswordCredential") or f"rcon_password_{sid}")
        if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", cred):
            raise ConfigError(f"{where} ({sid}): rconPasswordCredential has unsupported characters")
        e["SERVER_ID"], e["SERVER_NAME"] = sid, name
        e["SERVER_NAME_SOURCE"] = str(s.get("nameSource", "server"))
        try:
            out.append(Config.load(e, rcon_password_credential=cred))
        except ConfigError as err:
            raise ConfigError(f"{where} ({sid}): {err}") from None
    urls = [c.rcon_base_url for c in out]
    if len(set(urls)) != len(urls):
        raise ConfigError("SERVERS_FILE: two servers use the same rconBaseUrl")
    return out


def _validate_rcon_url(base: str, allow_insecure: bool) -> None:
    if not base:
        raise ConfigError("RCON_BASE_URL is required, e.g. https://203.0.113.10:27016")
    u = urlparse(base)
    if u.scheme not in {"http", "https"} or not u.hostname:
        raise ConfigError("RCON_BASE_URL must look like https://host:port")
    if u.username or u.password or u.query or u.fragment:
        raise ConfigError("RCON_BASE_URL must not contain credentials, a query or a fragment")
    if u.scheme == "http" and not allow_insecure and not _is_loopback(u.hostname):
        raise ConfigError(
            "RCON_BASE_URL uses plain http:// to a non-local host. The RCON password would cross the "
            "internet unencrypted. Use https://, an SSH/WireGuard tunnel to 127.0.0.1, or set "
            "RCON_ALLOW_INSECURE_HTTP=true if you accept the risk (and IP-allow-list the bot)."
        )


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
