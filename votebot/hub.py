"""Several game servers behind one Discord bot.

Each game server gets its own VoteService (catalog, presence tracker, rounds, votes and
rotation handling). Player links are shared, so a player links once and can vote on any
of the servers.

Which server a command is about:
- /vote and /voteup: the server the player is on right now (the bot already polls every
  server's player list). Nobody can vote for a server they aren't playing on.
- /votestatus, /voteoptions, /voteadmin: an optional server choice; otherwise the server
  the player is on, otherwise the first server.
With a single server none of this is visible: everything behaves as before.
"""

from __future__ import annotations

import asyncio
import logging

from .db import DB
from .service import VoteService

log = logging.getLogger("votebot.hub")


class Hub:
    def __init__(self, services: list[VoteService]):
        if not services:
            raise ValueError("at least one server is required")
        self.services: dict[str, VoteService] = {s.server_id: s for s in services}
        if len(self.services) != len(services):
            raise ValueError("server ids must be unique")
        self._stop = asyncio.Event()

    # ---------------------------------------------------------------- lookup
    @property
    def multi(self) -> bool:
        return len(self.services) > 1

    @property
    def first(self) -> VoteService:
        return next(iter(self.services.values()))

    @property
    def db(self) -> DB:
        """Any server's view works for the shared tables (links, link codes)."""
        return self.first.db

    def all(self) -> list[VoteService]:
        return list(self.services.values())

    def names(self) -> str:
        return ", ".join(s.name for s in self.services.values())

    def by_ref(self, ref: str | None) -> VoteService | None:
        """A server by id or display name (case-insensitive)."""
        r = (ref or "").strip().lower()
        if not r:
            return None
        for s in self.services.values():
            if r in (s.server_id.lower(), s.name.lower()):
                return s
        return None

    def steam_for(self, discord_id: int) -> str | None:
        row = self.db.link_for_discord(discord_id)
        return row["steam_id"] if row else None

    def playing_on(self, discord_id: int) -> VoteService | None:
        """The server this member's linked player is on now. If (briefly) on two, the longest session."""
        steam = self.steam_for(discord_id)
        if not steam:
            return None
        hits = [s for s in self.services.values() if s.tracker.is_online(steam)]
        if not hits:
            return None
        return max(hits, key=lambda s: s.tracker.seconds_on(steam))

    def for_member(self, discord_id: int, ref: str | None = None) -> VoteService:
        """Explicit choice, else where they play, else the first server."""
        return self.by_ref(ref) or self.playing_on(discord_id) or self.first

    def vote_target(self, discord_id: int) -> VoteService | None:
        """Where /vote and /voteup go: with one server always that server (its own checks explain
        any problem); with several, only the server the player is on."""
        if not self.multi:
            return self.first
        return self.playing_on(discord_id)

    def any_unreachable(self) -> bool:
        return any(not s.tracker.fresh() for s in self.services.values())

    def online_players(self) -> list[tuple[VoteService, str, str]]:
        """(server, steamId, name) for everyone on any server right now."""
        out = []
        for s in self.services.values():
            if s.tracker.fresh():
                out += [(s, sid, name) for sid, name in s.tracker.state.online.items()]
        return out

    # ---------------------------------------------------------------- linking (shared)
    async def start_link(self, discord_id: int, steam_id: str) -> tuple[bool, str]:
        steam_id = (steam_id or "").strip()
        if not self.multi:
            return await self.first.start_link(discord_id, steam_id)
        if not steam_id.isdigit() or not 5 <= len(steam_id) <= 20:
            return False, "Pick your in-game name from the list (or paste your 17-digit SteamID64)."
        hits = [s for s in self.services.values() if s.tracker.is_online(steam_id)]
        if hits:
            return await max(hits, key=lambda s: s.tracker.seconds_on(steam_id)).start_link(discord_id, steam_id)
        if self.any_unreachable():
            return False, "I can't reach one of the game servers right now. Try again in a minute."
        return False, f"That player isn't on any of our servers right now ({self.names()}). Join one, then run /link."

    async def verify_link(self, discord_id: int, code: str) -> tuple[bool, str]:
        row = self.db.get_code(discord_id)
        svc = self.first
        if row:
            svc = next((s for s in self.services.values() if row["steam_id"] in s.tracker.state.online), svc)
        ok, msg = await svc.verify_link(discord_id, code)
        if ok and self.multi:
            msg = "Linked. You can now use /vote while you're on any of our servers."
        return ok, msg

    def unlink(self, discord_id: int) -> bool:
        return self.first.unlink(discord_id)

    # ---------------------------------------------------------------- loops
    async def run(self) -> None:
        """Poll every server. Start times are spread out so the servers aren't polled in lockstep."""
        async def one(i: int, s: VoteService) -> None:
            if i:
                await asyncio.sleep(min(5.0, s.cfg.poll_seconds / max(2, len(self.services))) * i)
            await s.run()

        tasks = [asyncio.create_task(one(i, s), name=f"poll-{s.server_id}") for i, s in enumerate(self.all())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()

    def stop(self) -> None:
        for s in self.services.values():
            s.stop()


def session_minutes(svc: VoteService, steam: str) -> int:
    return int(svc.tracker.seconds_on(steam) // 60) if svc.tracker.is_online(steam) else 0

