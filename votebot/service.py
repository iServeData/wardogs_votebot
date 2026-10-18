"""Voting logic, independent of Discord (so it can be unit-tested)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum

from .catalog import Catalog, Selection, same_map
from .config import Config
from .db import DB
from .messages import MessageFile
from .presence import PresenceTracker, signature
from .rcon import RconClient, RconError
from .rotation import RotationError, RotationSlotManager
from .tally import Result, leader, tally

log = logging.getLogger("votebot.service")
audit = logging.getLogger("votebot.audit")

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L
CODE_LEN = 6
MAX_CODE_ATTEMPTS = 5
TARGET_WHISPER_COOLDOWN_S = 120


class VoteStatus(Enum):
    OK = "ok"
    NOT_LINKED = "not_linked"
    SERVER_UNKNOWN = "server_unknown"
    OFFLINE = "offline"
    TOO_NEW = "too_new"
    LIMIT = "limit"
    BAD_MAP = "bad_map"
    BAD_MODE = "bad_mode"
    BAD_WEATHER = "bad_weather"
    BAD_CIRCLE = "bad_circle"
    NOT_READY = "not_ready"
    CLOSED = "closed"
    UP_OFF = "up_off"              # /voteup disabled (UPVOTES_PER_MAP=0)
    NOT_LISTED = "not_listed"      # /voteup target has no votes this match
    UP_OPTION_CAP = "up_option_cap"  # UPVOTE_MAX_PER_OPTION reached for that option
    SAME_MAP = "same_map"          # BLOCK_CURRENT_MAP: that map is playing right now


@dataclass
class VoteOutcome:
    status: VoteStatus
    selection: Selection | None = None
    used: int = 0
    wait_minutes: int = 0
    minutes_on: int = 0


def _votes(n: int) -> str:
    return f"{n} vote" if n == 1 else f"{n} votes"


class VoteService:
    def __init__(self, cfg: Config, db: DB, rcon: RconClient, catalog: Catalog, messages: MessageFile):
        self.cfg = cfg
        self.db = db
        self.rcon = rcon
        self.catalog = catalog
        self.messages = messages
        self.tracker = PresenceTracker(rcon, db, cfg.poll_seconds, cfg.presence_grace_seconds,
                                       cfg.min_presence_minutes * 60, cfg.detect_score_reset)
        self.lock = asyncio.Lock()
        self.slots = RotationSlotManager(rcon, db, catalog, cfg.dry_run, cfg.restore_delay_seconds)
        self.slots.alert = self._alert_admins
        # Discord callbacks, set by the bot: async (text) -> None
        self.on_admin_alert = None
        self.on_announce = None
        self._last_whisper: dict[str, float] = {}
        self._stop = asyncio.Event()
        self._live_cand: tuple[str, float] | None = None  # (leader choice, first seen leading)
        self._live_last = 0.0

    # ---------------------------------------------------------------- rounds
    def round_id(self) -> int | None:
        r = self.db.current_round()
        return int(r["id"]) if r else None

    async def new_match(self, reason: str) -> None:
        async with self.lock:
            sig = signature(self.tracker.state.status)
            rid = self.db.start_round(sig, reason)
            audit.info("NEW_ROUND id=%s reason=%s signature=%s", rid, reason, sig)
        if reason.startswith("manual"):
            # /voteadmin newround only resets the votes. The map did not change, so the
            # rotation override state must not move (no consume, no restore). A queued vote is
            # carried into the new round so new votes can replace it in the same slot.
            ov = self.slots.override
            if ov and ov.status == "applied":
                ov.round_id = rid
                self.slots._save(ov)
            self._live_cand = None
            return
        ov = self.slots.override
        if ov and ov.status == "applied":
            if self._choice_is_live(ov.choice):
                audit.info("VOTE_SLOT_CONSUMED slot=%s choice=%s", ov.slot, ov.choice)
                await self._announce(f"Now playing the vote winner: {self.catalog.label(Selection.from_key(ov.choice))}.")
                self.slots.mark_consumed()
            else:
                await self._alert_admins(
                    "A different map loaded than the vote winner (an admin changed or skipped the map). "
                    f"The bot will put the default entry back into rotation slot {ov.slot} if nobody else edited it.")
                self.slots.schedule_restore()
        elif ov and ov.status == "consumed":
            if self._choice_is_live(ov.choice):
                # Same voted map still running (a second "new match" signal for the same match):
                # keep the entry until the server has really moved on.
                log.info("vote winner still live after %s; keeping slot %s", reason, ov.slot)
            else:
                self.slots.schedule_restore()  # the voted round is over; safe to put the default back
        ov = self.slots.override
        if ov and ov.prev and ov.prev.get("status") == "consumed":
            self.slots.schedule_prev_restore()  # the previous winner's round is over
        self._live_cand = None

    def _choice_is_live(self, choice: str) -> bool:
        st = self.tracker.state.status
        sel = Selection.from_key(choice)
        names = self.catalog.map_names()
        if not same_map(st.get("map"), self.catalog.server_map(sel.map_id), names):
            return False
        try:
            want = sorted(self.catalog.experiences_for(sel))
        except KeyError:
            return True
        return sorted(str(x) for x in st.get("experiences") or []) == want

    # ---------------------------------------------------------------- notifications
    async def _alert_admins(self, text: str) -> None:
        log.warning("ADMIN ALERT: %s", text)
        if self.on_admin_alert:
            try:
                await self.on_admin_alert(text)
            except Exception:  # noqa: BLE001
                log.exception("admin alert failed")

    async def _announce(self, text: str) -> None:
        if self.on_announce:
            try:
                await self.on_announce(text)
            except Exception:  # noqa: BLE001
                log.exception("announcement failed")

    # ---------------------------------------------------------------- vote close / apply
    def voting_closed(self) -> bool:
        rid = self.round_id()
        return rid is not None and self.db.get("closed_round") == str(rid)

    def applied_choice(self) -> str | None:
        ov = self.slots.override
        rid = self.round_id()
        return ov.choice if ov and ov.status == "applied" and ov.round_id == rid else None

    async def finalize(self, auto: bool = False) -> tuple[bool, str]:
        """Close voting for this round and (if enabled) put the winner in the next rotation slot.
        Nothing on the game server changes until the current match ends naturally."""
        rid = self.round_id()
        if rid is None or not self.tracker.fresh():
            return False, "The game server is unreachable right now; nothing was changed."
        if self.voting_closed():
            return False, "Voting is already closed for this round."
        lead = leader(self.results(), self.cfg.min_votes_to_finalize)
        if lead is None:
            if auto:
                self.db.set("closed_round", str(rid))
                await self._announce("Map vote closed: not enough votes "
                                     f"(needs {self.cfg.min_votes_to_finalize}). The normal rotation continues.")
            return False, (f"Not enough votes yet (the leader needs at least {self.cfg.min_votes_to_finalize}). "
                           "The rotation was not touched.")
        label = self.catalog.label(Selection.from_key(lead.choice))
        self.db.set("closed_round", str(rid))  # stop new votes while we apply
        if self.cfg.map_application_mode == "off":
            audit.info("VOTE_CLOSED round=%s winner=%s (application off)", rid, lead.choice)
            await self._announce(f"Map vote closed. Winner: {label} ({_votes(lead.count)}). Staff will queue it.")
            return True, f"Voting closed. Winner: {label}. MAP_APPLICATION_MODE=off, so apply it by hand."
        rnd = self.db.current_round()
        try:
            summary = await self.slots.apply(rid, lead.choice, rnd["signature"], signature(self.tracker.state.status))
        except (RotationError, RconError) as e:
            self.db.set("closed_round", None)  # reopen voting; nothing (or nothing lasting) was written
            log.warning("finalize failed: %s", e)
            await self._alert_admins(f"Could not apply the vote winner ({label}): {e}")
            return False, f"Not applied: {e}. Voting stays open; the rotation is unchanged."
        audit.info("VOTE_CLOSED round=%s winner=%s result=%s", rid, lead.choice, summary)
        if summary.startswith("[dry-run]"):
            self.db.set("closed_round", None)
            return True, f"Dry run OK - the server validated the change but nothing was written. {summary}"
        await self._announce(f"Map vote closed. Next round: {label} ({_votes(lead.count)}). "
                             "It loads when the current match ends.")
        if self.cfg.broadcast_mode == "changes":
            try:
                await self.rcon.broadcast(f"Map vote: next round is {label}")
            except RconError as e:
                log.warning("broadcast failed: %s", e)
        return True, f"Voting closed. Next round: {label}. The current match is not affected."

    async def _live_apply(self) -> None:
        """VOTE_CLOSE_MODE=live: keep the rotation slot in step with the leader while the match runs,
        so the winner is already queued when the match ends. No extra polling: runs on the normal
        status poll and writes only when the leader changes (settle time, min interval, per-round cap)."""
        cfg = self.cfg
        if (cfg.vote_close_mode != "live" or cfg.map_application_mode != "rotation_slot"
                or self.slots.disabled_reason or self.voting_closed() or not self.tracker.fresh()):
            return
        rid = self.round_id()
        if rid is None or self.db.get("ended_round") == str(rid):
            return  # the match is over and the next map is loading: never write now
        lead = leader(self.results(), cfg.min_votes_to_finalize)
        if lead is None or lead.choice == self.applied_choice():
            self._live_cand = None
            return
        now = time.time()
        if not self._live_cand or self._live_cand[0] != lead.choice:
            self._live_cand = (lead.choice, now)
        if now - self._live_cand[1] < cfg.live_settle_seconds or now - self._live_last < cfg.live_min_interval_seconds:
            return
        raw = (self.db.get("live_writes") or "").split(":")
        n = int(raw[1]) if len(raw) == 2 and raw[0] == str(rid) and raw[1].isdigit() else 0
        if n >= cfg.live_max_writes:
            if n == cfg.live_max_writes:
                self.db.set("live_writes", f"{rid}:{n + 1}")
                await self._alert_admins(f"Live map vote: reached {cfg.live_max_writes} rotation writes this "
                                         "round; no more automatic changes until the next match. "
                                         "/voteadmin finalize still works.")
            return
        self._live_last = now
        self.db.set("live_writes", f"{rid}:{n + 1}")
        label = self.catalog.label(Selection.from_key(lead.choice))
        rnd = self.db.current_round()
        try:
            summary = await self.slots.apply(rid, lead.choice, rnd["signature"], signature(self.tracker.state.status))
        except (RotationError, RconError) as e:
            log.warning("live apply failed: %s", e)
            await self._alert_admins(f"Live map vote could not queue {label}: {e}")
            return
        audit.info("VOTE_LIVE_APPLIED round=%s leader=%s votes=%s result=%s", rid, lead.choice, lead.count, summary)
        if summary.startswith("[dry-run]"):
            return
        await self._announce(f"Map vote: {label} is now the next map ({_votes(lead.count)}). "
                             "Voting stays open until the match ends.")
        if cfg.broadcast_mode == "changes":
            try:
                await self.rcon.broadcast(f"Map vote: next map is now {label}")
            except RconError as e:
                log.warning("broadcast failed: %s", e)

    async def _match_end_check(self) -> None:
        """A faction reached the score cap: the match is over and the next map loads in ~40 s.
        Freeze live rotation writes for this round and (BROADCAST_MODE=match_end) send ONE
        in-game message with the vote winner. No extra RCON reads: uses the normal status poll."""
        st = self.tracker.state.status or {}
        rid = self.round_id()
        if rid is None or not self.tracker.fresh() or self.db.get("ended_round") == str(rid):
            return
        try:
            cap = float(st.get("scoreCap") or self.cfg.match_score_cap)
        except (TypeError, ValueError):
            cap = float(self.cfg.match_score_cap)
        top = 0.0
        for f in st.get("factionScores") or []:
            try:
                top = max(top, float(f.get("score", 0)))
            except (TypeError, ValueError):
                pass
        if top < cap:
            return
        self.db.set("ended_round", str(rid))
        choice = self.applied_choice()
        audit.info("MATCH_END_DETECTED round=%s top_score=%g cap=%g next_vote=%s", rid, top, cap, choice)
        if self.cfg.broadcast_mode == "match_end" and choice:
            label = self.catalog.label(Selection.from_key(choice))
            try:
                await self.rcon.broadcast(f"Next map (player vote): {label}")
            except RconError as e:
                log.warning("match-end broadcast failed: %s", e)

    async def cancel_override(self) -> str:
        try:
            msg = await self.slots.restore("cancelled by admin", force_now=True)
        except (RotationError, RconError) as e:
            return f"Restore failed: {e}"
        self.db.set("closed_round", None)
        return f"{msg[0].upper() + msg[1:]}. Voting is open again for this round."

    # ---------------------------------------------------------------- results
    def results(self) -> list[Result]:
        rid = self.round_id()
        if rid is None:
            return []
        return tally(self.db.round_votes(rid), self.cfg.vote_counting)

    def submitted_choice(self) -> str | None:
        return self.applied_choice()

    def format_results(self, esc=lambda t: t) -> str:
        res = self.results()
        if not res:
            return "No votes yet for the next map. The normal rotation will be used unless players vote."
        applied = self.applied_choice()
        need = self.cfg.min_votes_to_finalize
        lines = []
        for i, r in enumerate(res[: self.cfg.results_max_lines]):
            label = esc(self.catalog.label(Selection.from_key(r.choice)))
            if r.choice == applied:
                tag = " | Current next map"
            elif i == 0:
                # The leader is written to the rotation on the next status check (live mode),
                # so right after a vote it can lead while another entry is still the next map.
                soon = (self.cfg.vote_close_mode == "live" and self.cfg.map_application_mode == "rotation_slot"
                        and not self.voting_closed() and r.count >= need and not self.slots.disabled_reason)
                tag = " | Leading - becomes the next map within a minute" if soon else " | Leading"
            else:
                tag = ""
            lines.append(f"* {label} - {r.count}{tag}")
        if len(res) > self.cfg.results_max_lines:
            lines.append(f"* ...and {len(res) - self.cfg.results_max_lines} more")
        if not applied and not self.voting_closed() and res[0].count < need:
            lines.append(f"(The leader needs at least {need} votes to replace the next map in rotation.)")
        if self.voting_closed() and not applied:
            lines.append("(Voting is closed for this round.)")
        return "\n".join(lines)

    def votes_used(self, discord_id: int, kind: str = "vote", choice: str | None = None) -> int:
        link = self.db.link_for_discord(discord_id)
        rid = self.round_id()
        if not link or rid is None:
            return 0
        return self.db.votes_used(rid, discord_id, link["steam_id"], kind, choice)

    def upvotes_used(self, discord_id: int, choice: str | None = None) -> int:
        return self.votes_used(discord_id, "up", choice)

    def listed_choices(self) -> list:
        """Options that have at least one vote this match (what /voteup can back)."""
        return self.results()

    def _check_up(self, discord_id: int, choice: str) -> VoteOutcome:
        if self.cfg.upvotes_per_map <= 0:
            return VoteOutcome(VoteStatus.UP_OFF)
        if not self.catalog.loaded:
            return VoteOutcome(VoteStatus.NOT_READY)
        if self.voting_closed():
            return VoteOutcome(VoteStatus.CLOSED)
        choice = (choice or "").strip()
        if choice not in {r.choice for r in self.results()}:
            return VoteOutcome(VoteStatus.NOT_LISTED)
        try:
            sel = Selection.from_key(choice)
        except (ValueError, TypeError):
            return VoteOutcome(VoteStatus.NOT_LISTED)
        out = self._eligible(discord_id, sel)
        if out.status is not VoteStatus.OK:
            return out
        used = self.upvotes_used(discord_id)
        if used >= self.cfg.upvotes_per_map:
            return VoteOutcome(VoteStatus.LIMIT, sel, used=used)
        cap = self.cfg.upvote_max_per_option
        if cap and self.upvotes_used(discord_id, choice) >= cap:
            return VoteOutcome(VoteStatus.UP_OPTION_CAP, sel, used=used)
        return VoteOutcome(VoteStatus.OK, sel, used=used)

    def precheck_up(self, discord_id: int, choice: str) -> VoteOutcome:
        return self._check_up(discord_id, choice)

    async def cast_upvote(self, discord_id: int, choice: str) -> VoteOutcome:
        """+1 for an option somebody already voted for (counts like a vote; same tie rules)."""
        async with self.lock:
            out = self._check_up(discord_id, choice)
            if out.status is not VoteStatus.OK:
                return out
            sel = out.selection
            assert sel is not None
            rid = self.round_id()
            steam = self.db.link_for_discord(discord_id)["steam_id"]
            self.db.add_vote(rid, discord_id, steam, sel.key, kind="up")
            audit.info("UPVOTE round=%s discord=%s steam=%s choice=%s", rid, discord_id, steam, sel.key)
            return VoteOutcome(VoteStatus.OK, sel, used=out.used + 1)

    @property
    def server_id(self) -> str:
        return self.cfg.server_id

    @property
    def name(self) -> str:
        """Display name. With nameSource "server", the name the game server reports right now."""
        if self.cfg.name_from_server:
            live = str((self.tracker.state.status or {}).get("serverName") or "").strip()
            if live:
                return " ".join(live.split())[:100]
        return self.cfg.label

    def is_current_map(self, server_map: str) -> bool:
        st = self.tracker.state.status or {}
        return bool(st.get("map")) and same_map(st.get("map"), server_map, self.catalog.map_names())

    def current_map_label(self) -> str:
        st = self.tracker.state.status
        if not st:
            return "unknown"
        mid = str(st.get("map", ""))
        mo = self.catalog.option_for_status(st)
        name = mo.name if mo else mid
        mode = self.catalog.mode_of([str(x) for x in st.get("experiences") or []])
        light = self.catalog.lightings.get(str(st.get("lighting", "")), str(st.get("lighting", "")))
        circle = ""
        alt = st.get("alternator")
        if alt and mo:
            circle = ", " + mo.circles.get(alt, str(alt))
        p = st.get("players") or {}
        return f"{name}, {mode}, {light}{circle} ({p.get('current', '?')}/{p.get('max', '?')} players)"

    # ---------------------------------------------------------------- voting
    def precheck(self, discord_id: int, map_v: str, mode_v: str, weather_v: str,
                 circle_v: str | None) -> VoteOutcome:
        """Fast local checks (no network). Returns OK or the first failure."""
        out = self._check(discord_id, map_v, mode_v, weather_v, circle_v)
        if out.status is VoteStatus.OK:
            link = self.db.link_for_discord(discord_id)
            rid = self.round_id()
            used = self.db.votes_used(rid, discord_id, link["steam_id"]) if rid else 0
            if used >= self.cfg.max_votes_per_map:
                return VoteOutcome(VoteStatus.LIMIT, out.selection, used=used)
        return out

    def _check(self, discord_id: int, map_v: str, mode_v: str, weather_v: str,
               circle_v: str | None) -> VoteOutcome:
        if not self.catalog.loaded:
            return VoteOutcome(VoteStatus.NOT_READY)
        if self.voting_closed():
            return VoteOutcome(VoteStatus.CLOSED)
        mo = self.catalog.resolve_map(map_v)
        if not mo:
            return VoteOutcome(VoteStatus.BAD_MAP)
        mode = self.catalog.resolve_mode(mode_v)
        if not mode:
            return VoteOutcome(VoteStatus.BAD_MODE)
        light = self.catalog.resolve_lighting(weather_v)
        if not light:
            return VoteOutcome(VoteStatus.BAD_WEATHER)
        circle = self.catalog.resolve_circle(mo, circle_v)
        if circle is None:
            return VoteOutcome(VoteStatus.BAD_CIRCLE)
        sel = Selection(mo.id, mode, light, circle)
        if self.cfg.block_current_map and self.is_current_map(mo.map_id):
            return VoteOutcome(VoteStatus.SAME_MAP, sel)
        return self._eligible(discord_id, sel)

    def _eligible(self, discord_id: int, sel: Selection | None) -> VoteOutcome:
        """Player rules shared by /vote and /voteup: linked, on the server, on long enough."""
        link = self.db.link_for_discord(discord_id)
        if not link:
            return VoteOutcome(VoteStatus.NOT_LINKED, sel)
        steam = link["steam_id"]
        if not self.tracker.fresh():
            return VoteOutcome(VoteStatus.SERVER_UNKNOWN, sel)
        if not self.tracker.is_online(steam):
            return VoteOutcome(VoteStatus.OFFLINE, sel)
        on_s = self.tracker.seconds_on(steam)
        need = self.cfg.min_presence_minutes * 60
        if on_s < need:
            return VoteOutcome(VoteStatus.TOO_NEW, sel, minutes_on=int(on_s // 60),
                               wait_minutes=max(1, int((need - on_s + 59) // 60)))
        return VoteOutcome(VoteStatus.OK, sel)

    async def cast_vote(self, discord_id: int, map_v: str, mode_v: str, weather_v: str,
                        circle_v: str | None) -> VoteOutcome:
        out = self._check(discord_id, map_v, mode_v, weather_v, circle_v)
        if out.status is not VoteStatus.OK:
            return out
        sel = out.selection
        assert sel is not None
        steam = self.db.link_for_discord(discord_id)["steam_id"]
        async with self.lock:
            rid = self.round_id()
            if rid is None:
                return VoteOutcome(VoteStatus.SERVER_UNKNOWN, sel)
            used = self.db.votes_used(rid, discord_id, steam)
            if used >= self.cfg.max_votes_per_map:
                return VoteOutcome(VoteStatus.LIMIT, sel, used=used)
            self.db.add_vote(rid, discord_id, steam, sel.key)
            audit.info("VOTE round=%s discord=%s steam=%s choice=%s", rid, discord_id, steam, sel.key)
            # Votes are only recorded. The game server is NOT touched here: the winner is written
            # into the next rotation slot once, when voting closes (/voteadmin finalize or timer).
            return VoteOutcome(VoteStatus.OK, sel, used=used + 1)

    # ---------------------------------------------------------------- linking
    @staticmethod
    def _hash(code: str) -> str:
        return hashlib.sha256(code.upper().encode()).hexdigest()

    async def start_link(self, discord_id: int, steam_id: str) -> tuple[bool, str]:
        steam_id = (steam_id or "").strip()
        if not steam_id.isdigit() or not 5 <= len(steam_id) <= 20:
            return False, "Pick your in-game name from the list (or paste your 17-digit SteamID64)."
        if not self.tracker.fresh():
            return False, "I can't reach the game server right now. Try again in a minute."
        if steam_id not in self.tracker.state.online:
            return False, "That player isn't on the server right now. Join the server first, then run /link."
        existing = self.db.link_for_discord(discord_id)
        if existing and existing["steam_id"] == steam_id:
            return False, "You're already linked to that player."
        if existing:
            return False, "Your Discord account is already linked to another player. Use /unlink first."
        other = self.db.link_for_steam(steam_id)
        if other:
            return False, "That player is already linked to a different Discord account. Ask an admin if this is wrong."
        now = time.time()
        if now - self._last_whisper.get(steam_id, 0) < TARGET_WHISPER_COOLDOWN_S:
            return False, "A code was sent to that player very recently. Please wait two minutes."
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
        mins = max(1, self.cfg.link_code_ttl_s // 60)
        msg = (f"[{self.cfg.server_display_name}] Discord link code: {code} - type /verify {code} in Discord "
               f"within {mins} min. Didn't request this? Ignore it.")
        try:
            await self.rcon.whisper(steam_id, msg)
        except (RconError, ValueError) as e:
            log.warning("whisper to %s failed: %s", steam_id, e)
            return False, "I couldn't message that player in-game. Try again shortly."
        self._last_whisper[steam_id] = now
        self.db.put_code(discord_id, steam_id, self._hash(code), now + self.cfg.link_code_ttl_s)
        audit.info("LINK_CODE_SENT discord=%s steam=%s", discord_id, steam_id)
        name = self.tracker.state.online.get(steam_id, "your player")
        return True, (f"I sent a {CODE_LEN}-character code to **{name}** in the in-game chat. "
                      f"Run `/verify` with that code within {mins} minutes.")

    async def verify_link(self, discord_id: int, code: str) -> tuple[bool, str]:
        code = (code or "").strip().upper()
        row = self.db.get_code(discord_id)
        if not row or row["expires_at"] < time.time():
            self.db.delete_code(discord_id)
            return False, "No active link code. Run /link while you're on the server."
        if row["attempts"] >= MAX_CODE_ATTEMPTS:
            self.db.delete_code(discord_id)
            return False, "Too many wrong codes. Run /link again to get a new one."
        if len(code) != CODE_LEN or not hmac.compare_digest(self._hash(code), row["code_hash"]):
            self.db.bump_code_attempt(discord_id)
            return False, "That code doesn't match. Check the in-game chat and try again."
        steam = row["steam_id"]
        async with self.lock:
            if self.db.link_for_steam(steam) or self.db.link_for_discord(discord_id):
                self.db.delete_code(discord_id)
                return False, "That account was linked by someone else in the meantime. Ask an admin."
            try:
                self.db.add_link(discord_id, steam, self.tracker.state.online.get(steam))
            except sqlite3.IntegrityError:  # linked via another server at the same moment
                self.db.delete_code(discord_id)
                return False, "That account was linked by someone else in the meantime. Ask an admin."
            self.db.delete_code(discord_id)
        audit.info("LINKED discord=%s steam=%s", discord_id, steam)
        return True, "Linked. You can now use /vote while you're on the server."

    def unlink(self, discord_id: int) -> bool:
        ok = self.db.remove_link(discord_id)
        if ok:
            audit.info("UNLINKED discord=%s", discord_id)
        return ok

    # ---------------------------------------------------------------- loops
    async def refresh_catalog(self) -> bool:
        try:
            await self.catalog.refresh(self.rcon)
            return True
        except RconError as e:
            log.warning("catalog refresh failed: %s", e)
            return False

    async def run(self) -> None:
        last_catalog = 0.0
        while not self._stop.is_set():
            started = time.time()
            try:
                if not self.catalog.loaded and started - last_catalog > 60 or started - last_catalog > 3600:
                    last_catalog = started
                    try:
                        await self.rcon.capabilities()
                        need = {"GET /v1/config", "PUT /v1/config", "POST /v1/config/validate", "GET /v1/rotation"}
                        missing = need - self.rcon.routes if self.rcon.routes else set()
                        if missing and self.cfg.map_application_mode == "rotation_slot":
                            log.error("this server build lacks %s; the vote slot cannot be applied", sorted(missing))
                    except RconError as e:
                        log.warning("capabilities unavailable: %s", e)
                    await self.refresh_catalog()
                reason = await self.tracker.poll()
                if reason:
                    log.info("new match detected: %s", reason)
                    await self.new_match(reason)
                ov = self.slots.override
                if ov and ov.status == "applied" and ov.round_id != self.round_id():
                    # e.g. the bot was offline when the voted round started
                    if self._choice_is_live(ov.choice):
                        self.slots.mark_consumed()
                    else:
                        self.slots.schedule_restore()
                await self.slots.tick()
                await self._timer_close()
                await self._match_end_check()
                await self._live_apply()
                self.db.purge_codes()
            except Exception:  # never let the loop die
                log.exception("poll loop error")
            delay = max(1.0, self.cfg.poll_seconds - (time.time() - started))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _timer_close(self) -> None:
        if self.cfg.vote_close_mode != "timer" or self.voting_closed() or not self.tracker.fresh():
            return
        rnd = self.db.current_round()
        if rnd and time.time() - rnd["started_at"] >= self.cfg.vote_close_after_minutes * 60:
            ok, msg = await self.finalize(auto=True)
            log.info("timer vote close: %s", msg)

    def stop(self) -> None:
        self._stop.set()
