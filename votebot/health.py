"""Private health snapshot for the WARDOGS vote bot (schema 1).

Written atomically every HEALTH_INTERVAL_SECONDS by a task on the bot's own asyncio
event loop, so it stops advancing if the loop is blocked. It also records when the
RCON poll loop last *started an iteration* (loop_progress_at), so a coroutine that is
stuck inside the poll loop is detected even while the event loop itself is fine.

Contents are deliberately minimal: timestamps, states, sanitised categories and the
server's player count / slot limit (two numbers, v2 addition).
With more than one game server, a "servers" list repeats the rcon/players blocks per
server (id + display name); the top-level blocks always describe the first server, so
readers written for one server keep working.
No tokens, passwords, host addresses, SteamIDs, player names, Discord IDs,
raw responses or exception text are ever written.

Any failure here is logged (class name only) and swallowed; it never affects voting.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import secrets
import tempfile
import time

log = logging.getLogger("votebot.health")

SCHEMA_VERSION = 1

# Sanitised RCON result categories (public-safe).
OK = "ok"
UNREACHABLE = "unreachable"      # connection refused / DNS / socket errors
TIMEOUT = "timeout"              # request exceeded RCON_TIMEOUT_SECONDS
AUTH = "auth"                    # 401 / 403
RATE_LIMITED = "rate_limited"    # 429 or local request budget
API_ERROR = "api_error"          # other 4xx / 5xx from the game API
INVALID = "invalid_response"     # 2xx but not a usable status document
INTERNAL = "internal_error"      # unexpected exception inside the bot


def classify_rcon_error(status: int | None, code: str | None, message: str | None) -> str:
    """Map an RconError's (status, code, message) to a sanitised category."""
    code = (code or "").lower()
    msg = (message or "")
    if status in (401, 403):
        return AUTH
    if status == 429 or code in ("rate_limited", "local_budget"):
        return RATE_LIMITED
    if (status or 0) == 0 and code == "unreachable":
        return TIMEOUT if "Timeout" in msg else UNREACHABLE
    if status and status >= 400:
        return API_ERROR
    if code in ("bad_config", "too_large"):
        return INVALID
    return API_ERROR


def _players(srv: "_ServerHealth", status: dict, online) -> None:
    """Record only two numbers. Prefer the server's own count (what the server browser
    shows), else the size of the /v1/players list. Anything implausible -> None."""
    def ok_int(v, hi):
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= hi else None
    try:
        p = status.get("players") if isinstance(status.get("players"), dict) else {}
        mx = ok_int(p.get("max"), 1000)
        if mx == 0:
            mx = None
        cur = ok_int(p.get("current"), 1000)
        if cur is None:
            cur = ok_int(len(online), 1000) if online is not None else None
        if cur is not None and mx is not None and cur > mx:
            mx = None
        srv.players_current, srv.players_max = cur, mx
        srv.players_observed_at = time.time() if cur is not None else None
    except Exception:  # noqa: BLE001 - never affect the bot
        srv.players_current = srv.players_max = srv.players_observed_at = None


def _rcon_result(srv: "_ServerHealth", started: float, category: str) -> None:
    srv.rcon_last_attempt_at = started
    srv.rcon_last_result = category
    if category == OK:
        srv.rcon_last_success_at = time.time()
        srv.rcon_consecutive_failures = 0
    else:
        srv.rcon_consecutive_failures += 1


class _ServerHealth:
    """RCON result + population for one game server (status poll = GET /v1/players + /v1/status)."""

    def __init__(self, server_id: str, name: str):
        self.server_id, self.name = server_id, name
        self.rcon_last_attempt_at: float | None = None
        self.rcon_last_result: str | None = None
        self.rcon_last_success_at: float | None = None
        self.rcon_consecutive_failures = 0
        self.players_current: int | None = None
        self.players_max: int | None = None
        self.players_observed_at: float | None = None
        self.loop_progress_at: float | None = None

    def rcon(self) -> dict:
        return {"last_attempt_at": self.rcon_last_attempt_at, "last_result": self.rcon_last_result,
                "last_success_at": self.rcon_last_success_at,
                "consecutive_failures": self.rcon_consecutive_failures}

    def players(self) -> dict:
        return {"current": self.players_current, "max": self.players_max, "observed_at": self.players_observed_at}


class HealthRecorder:
    def __init__(self, path: str | None, interval_s: float = 20.0):
        self.path = path or None
        self.interval_s = max(5.0, min(60.0, float(interval_s)))
        self.instance_id = secrets.token_hex(8)
        self.pid = os.getpid()
        self.process_started_at = time.time()
        # Discord
        self.discord_state = "starting"       # starting|connecting|connected|reconnecting|closed
        self.discord_state_since = self.process_started_at
        self.discord_last_ready_at: float | None = None
        self.servers: list[_ServerHealth] = []
        self._client = None
        self._task: asyncio.Task | None = None
        self._fail_logged_at = 0.0

    # ---------------------------------------------------------------- discord
    def _set_discord(self, state: str) -> None:
        if state != self.discord_state:
            self.discord_state = state
            self.discord_state_since = time.time()

    def on_connect(self) -> None:
        self._set_discord("connecting")      # gateway session up, READY not yet processed

    def on_ready(self) -> None:
        self.discord_last_ready_at = time.time()
        self._set_discord("connected")

    def on_resumed(self) -> None:
        self.discord_last_ready_at = time.time()
        self._set_discord("connected")

    def on_disconnect(self) -> None:
        # discord.py reconnects automatically unless the client is closing.
        closed = bool(self._client is not None and self._client.is_closed())
        self._set_discord("closed" if closed else "reconnecting")

    def _discord_now(self) -> str:
        """Cross-check the event-derived state against the live websocket."""
        st = self.discord_state
        c = self._client
        if c is None:
            return st
        try:
            if c.is_closed():
                return "closed"
            if st == "connected":
                ws = getattr(c, "ws", None)
                if ws is None or not getattr(ws, "open", False):
                    return "reconnecting"
                lat = c.latency
                if isinstance(lat, float) and math.isnan(lat):
                    return "reconnecting"
        except Exception:  # noqa: BLE001
            return st
        return st

    # ---------------------------------------------------------------- rcon
    def wrap_tracker(self, tracker, server_id: str = "main", name: str = "") -> None:
        """Instrument PresenceTracker.poll (the bot's existing authenticated status poll)
        without changing what it does or returns. Call once per game server."""
        orig = tracker.poll
        srv = _ServerHealth(server_id, name or server_id)
        self.servers.append(srv)

        async def poll():
            started = time.time()
            srv.loop_progress_at = started
            before_ok = tracker.state.last_ok
            try:
                result = await orig()
            except Exception:
                _rcon_result(srv, started, INTERNAL)
                raise
            try:
                if tracker.state.last_ok > before_ok and tracker.state.last_ok >= started:
                    st = tracker.state.status
                    if isinstance(st, dict) and st.get("map"):
                        _rcon_result(srv, started, OK)
                        _players(srv, st, tracker.state.online)
                    else:
                        _rcon_result(srv, started, INVALID)
                else:
                    cat = getattr(tracker.state, "last_error_category", None) or API_ERROR
                    _rcon_result(srv, started, cat)
            except Exception:  # noqa: BLE001
                pass
            return result

        tracker.poll = poll

    @property
    def loop_progress_at(self) -> float | None:
        """The oldest poll-loop heartbeat: if any server's loop is stuck, the snapshot shows it."""
        vals = [s.loop_progress_at for s in self.servers]
        if not vals or any(v is None for v in vals):
            return None
        return min(vals)  # type: ignore[type-var]

    # ---------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        now = time.time()
        first = self.servers[0] if self.servers else _ServerHealth("main", "main")
        snap = {
            "schema_version": SCHEMA_VERSION,
            "instance_id": self.instance_id,
            "pid": self.pid,
            "process_started_at": round(self.process_started_at, 3),
            "written_at": round(now, 3),
            "loop_progress_at": self.loop_progress_at,
            "health_interval_s": self.interval_s,
            "discord": {
                "state": self._discord_now(),
                "since": round(self.discord_state_since, 3),
                "last_ready_at": self.discord_last_ready_at,
                "observed_at": round(now, 3),
            },
            "rcon": first.rcon(),
            "players": first.players(),
        }
        if len(self.servers) > 1:
            snap["servers"] = [{"id": x.server_id, "name": x.name, "rcon": x.rcon(), "players": x.players()}
                               for x in self.servers]
        return snap

    def write(self) -> None:
        if not self.path:
            return
        d = os.path.dirname(self.path)
        data = json.dumps(self.snapshot(), separators=(",", ":")).encode()
        fd, tmp = tempfile.mkstemp(prefix=".health.", suffix=".tmp", dir=d)
        try:
            os.write(fd, data)
            os.fchmod(fd, 0o640)   # owner votebot rw, group (wdhealth via setgid dir) read
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async def _run(self) -> None:
        while True:
            try:
                self.write()
            except Exception as e:  # noqa: BLE001 - never affect the bot
                now = time.time()
                if now - self._fail_logged_at > 600:
                    self._fail_logged_at = now
                    log.warning("health snapshot not written (%s)", type(e).__name__)
            await asyncio.sleep(self.interval_s)

    def start(self, client) -> None:
        self._client = client
        if self.path and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="health-snapshot")
            log.info("health snapshot enabled every %.0fs", self.interval_s)

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None


_recorder: HealthRecorder | None = None


def recorder() -> HealthRecorder:
    """Process-wide recorder, configured from HEALTH_FILE / HEALTH_INTERVAL_SECONDS.
    HEALTH_FILE unset -> recorder is inert (no file is ever written)."""
    global _recorder
    if _recorder is None:
        try:
            iv = float(os.environ.get("HEALTH_INTERVAL_SECONDS", "20"))
        except ValueError:
            iv = 20.0
        _recorder = HealthRecorder(os.environ.get("HEALTH_FILE") or None, iv)
    return _recorder
