"""Tracks who is on the game server, for how long, and when a new match starts.

Presence rule (configurable): a player may vote only if they are on the server
right now AND have been continuously on it for at least MIN_PRESENCE_MINUTES.
Short drop-outs (map travel, reconnects) of up to PRESENCE_GRACE_SECONDS do not
reset their timer.

New-match detection (live builds don't report a match clock):
- map / mode / circle changed in /v1/status
- match clock went backwards (if the build reports matchSeconds)
- faction scores reset to ~0 (catches the same map being replayed)
- server uptime went backwards (server restarted)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .db import DB
from .rcon import RconClient, RconError

log = logging.getLogger("votebot.presence")


@dataclass
class Session:
    name: str
    first_seen: float
    last_seen: float


@dataclass
class ServerState:
    sessions: dict[str, Session] = field(default_factory=dict)
    online: dict[str, str] = field(default_factory=dict)  # steamId -> name (latest snapshot)
    status: dict = field(default_factory=dict)
    last_ok: float = 0.0
    last_error: str | None = None
    consecutive_errors: int = 0


def signature(status: dict) -> str:
    exps = sorted(str(x) for x in (status.get("experiences") or []))
    return "|".join([str(status.get("map", "")), "+".join(exps), str(status.get("alternator") or "")])


def _max_score(status: dict) -> float:
    vals = []
    for f in status.get("factionScores") or []:
        try:
            vals.append(float(f.get("score", 0)))
        except (TypeError, ValueError):
            pass
    return max(vals) if vals else 0.0


class PresenceTracker:
    def __init__(self, rcon: RconClient, db: DB, poll_seconds: int, grace_s: int,
                 min_presence_s: int, detect_score_reset: bool = True):
        self.rcon = rcon
        self.db = db
        self.poll_seconds = poll_seconds
        self.grace_s = grace_s
        self.min_presence_s = min_presence_s
        self.detect_score_reset = detect_score_reset
        self.state = ServerState()
        self._prev_status: dict | None = None
        self._prev_uptime: float | None = None
        self._poll_n = 0
        now = time.time()
        for r in db.load_presence():
            if now - r["last_seen"] <= grace_s:
                self.state.sessions[r["steam_id"]] = Session(r["name"] or "", r["first_seen"], r["last_seen"])

    # ---- queries -----------------------------------------------------------
    def fresh(self) -> bool:
        return time.time() - self.state.last_ok <= max(3 * self.poll_seconds, 60)

    def is_online(self, steam_id: str) -> bool:
        return self.fresh() and steam_id in self.state.online

    def seconds_on(self, steam_id: str) -> float:
        s = self.state.sessions.get(steam_id)
        if not s or steam_id not in self.state.online:
            return 0.0
        return max(0.0, time.time() - s.first_seen)

    # ---- polling ------------------------------------------------------------
    async def poll(self) -> str | None:
        """Poll the server. Returns a reason string if a new match was detected."""
        try:
            players = await self.rcon.players()
            status = await self.rcon.status()
            uptime = None
            if self._poll_n % 4 == 0:
                try:
                    uptime = float((await self.rcon.health()).get("uptimeSeconds"))
                except (RconError, TypeError, ValueError):
                    uptime = None
            self._poll_n += 1
        except RconError as e:
            self.state.consecutive_errors += 1
            self.state.last_error = str(e)
            try:  # health snapshot
                from .health import classify_rcon_error
                self.state.last_error_category = classify_rcon_error(
                    getattr(e, "status", None), getattr(e, "code", None), getattr(e, "message", None))
            except Exception:  # noqa: BLE001
                self.state.last_error_category = None
            if self.state.consecutive_errors in (1, 5) or self.state.consecutive_errors % 40 == 0:
                log.warning("RCON poll failed (%d in a row): %s", self.state.consecutive_errors, e)
            return None

        now = time.time()
        prev_online = set(self.state.online)
        online: dict[str, str] = {}
        for p in players:
            sid = str(p.get("steamId"))
            name = str(p.get("name") or "")[:64]
            online[sid] = name
            s = self.state.sessions.get(sid)
            continuous = s is not None and (sid in prev_online or now - s.last_seen <= self.grace_s)
            if continuous:
                s.last_seen, s.name = now, name
            else:
                self.state.sessions[sid] = Session(name, now, now)
        for sid in list(self.state.sessions):
            if sid not in online and now - self.state.sessions[sid].last_seen > self.grace_s:
                del self.state.sessions[sid]

        self.state.online = online
        self.state.status = status
        self.state.last_ok = now
        if self.state.consecutive_errors:
            log.info("RCON reachable again after %d failed polls", self.state.consecutive_errors)
        self.state.consecutive_errors = 0
        self.state.last_error = None
        self.db.save_presence([(sid, s.name, s.first_seen, s.last_seen) for sid, s in self.state.sessions.items()])

        reason = self._detect_new_match(status, uptime)
        self._prev_status = status
        if uptime is not None:
            self._prev_uptime = uptime
        return reason

    def _detect_new_match(self, status: dict, uptime: float | None) -> str | None:
        rnd = self.db.current_round()
        sig = signature(status)
        if rnd is None:
            self.db.start_round(sig, None)
            log.info("first round recorded: %s", sig)
            return None
        if rnd["signature"] != sig:
            a, b = str(rnd["signature"]).split("|"), sig.split("|")
            if len(a) == 3 and len(b) == 3 and a[:2] == b[:2] and (a[2] == "" or b[2] == ""):
                # Same map and mode; the server just filled in (or briefly dropped) the circle
                # while the map was loading. Not a new match.
                self.db.set_round_signature(rnd["id"], sig)
                log.info("circle reported late (%s -> %s); same match", rnd["signature"], sig)
                return None
            return f"map changed ({rnd['signature']} -> {sig})"
        prev = self._prev_status
        if prev is None:
            return None
        try:
            ps, cs = prev.get("matchSeconds"), status.get("matchSeconds")
            if ps is not None and cs is not None and float(cs) + 30 < float(ps):
                return "match clock reset"
        except (TypeError, ValueError):
            pass
        if uptime is not None and self._prev_uptime is not None and uptime + 60 < self._prev_uptime:
            return "server restarted"
        if self.detect_score_reset:
            pm, cm = _max_score(prev), _max_score(status)
            if pm >= 20 and cm <= max(2.0, pm * 0.1):
                return f"scores reset ({pm:g} -> {cm:g})"
        return None
