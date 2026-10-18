"""Discord front-end: slash commands, autocomplete, permission + rate-limit checks."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from . import __version__
from . import health as _health
from .catalog import MODE_NAMES, Selection
from .config import Config
from .hub import Hub
from .ratelimit import RateLimiter
from .service import VoteService, VoteStatus

log = logging.getLogger("votebot.discord")

BUCKET_FOR = {
    "vote": "vote", "voteup": "vote", "link": "link", "verify": "verify",
    "votestatus": "query", "voteoptions": "query", "mystatus": "query", "unlink": "query",
}


def _clean(text: str) -> str:
    """Escape markdown in text that originates from players / the game server."""
    return discord.utils.escape_markdown(discord.utils.escape_mentions(text))


class GuardedTree(app_commands.CommandTree):
    """Global checks for every interaction (commands and autocomplete)."""

    def __init__(self, client: "VoteBot"):
        super().__init__(client)
        self.vb = client

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        cfg = self.vb.cfg
        uid = interaction.user.id
        is_auto = interaction.type is discord.InteractionType.autocomplete

        if interaction.guild_id != cfg.guild_id or interaction.user.bot:
            if not is_auto and not interaction.response.is_done():
                await interaction.response.send_message("This bot only works in its home Discord server.", ephemeral=True)
            return False
        if self.vb.limiter.blocked_for(uid) > 0:
            return False  # silently ignore abusive users while blocked
        if self.vb.limiter.take("global", 0) > 0:
            if not is_auto:
                await interaction.response.send_message("The bot is busy. Try again in a few seconds.", ephemeral=True)
            return False
        if is_auto:
            if self.vb.limiter.take("autocomplete", uid) > 0:
                await interaction.response.autocomplete([])
                return False
            return True

        name = interaction.command.root_parent.name if interaction.command and interaction.command.root_parent \
            else (interaction.command.name if interaction.command else "")
        if cfg.vote_channel_ids and name != "voteadmin" and interaction.channel_id not in cfg.vote_channel_ids:
            chans = " ".join(f"<#{c}>" for c in cfg.vote_channel_ids)
            await interaction.response.send_message(f"Please use the bot in {chans}.", ephemeral=True)
            return False
        bucket = "admin" if name == "voteadmin" else BUCKET_FOR.get(name, "query")
        wait = self.vb.limiter.take(bucket, uid)
        if wait > 0:
            await interaction.response.send_message(
                f"Slow down a little - try again in {max(1, round(wait))}s.", ephemeral=True)
            return False
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            msg = "You don't have permission to use that."
        else:
            log.exception("command error", exc_info=error)
            msg = "Something went wrong. The admins have been notified in the bot log."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class VoteBot(discord.Client):
    def __init__(self, cfg: Config, hub: Hub):
        intents = discord.Intents.none()
        intents.guilds = True  # no privileged intents, no message content
        super().__init__(
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
            max_messages=None,
            member_cache_flags=discord.MemberCacheFlags.none(),
        )
        self.cfg = cfg          # bot-wide settings (Discord server, channels, roles)
        self.hub = hub
        self.limiter = RateLimiter()
        self.tree = GuardedTree(self)
        self._loop_task = None
        register_commands(self)

    @property
    def svc(self) -> VoteService:
        """The first (or only) game server."""
        return self.hub.first

    def tag(self, svc: VoteService) -> str:
        """'[Server name] ' in front of channel posts when the bot serves several servers."""
        return f"[{svc.name}] " if self.hub.multi else ""

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.cfg.guild_id)
        self.tree.copy_global_to(guild=guild)
        self.tree.clear_commands(guild=None)
        await self.tree.sync(guild=None)       # remove any global commands
        synced = await self.tree.sync(guild=guild)
        log.info("synced %d slash commands to guild %s", len(synced), self.cfg.guild_id)
        rec = _health.recorder()
        for svc in self.hub.all():
            svc.on_admin_alert = self._alert_poster(svc)
            svc.on_announce = self._announce_poster(svc)
            try:
                rec.wrap_tracker(svc.tracker, svc.server_id, lambda s=svc: s.name)
            except Exception as e:  # noqa: BLE001  never block the bot
                log.warning("health snapshot for %s disabled (%s)", svc.server_id, type(e).__name__)
        try:
            rec.start(self)
        except Exception as e:  # noqa: BLE001
            log.warning("health snapshot disabled (%s)", type(e).__name__)
        self._loop_task = self.loop.create_task(self.hub.run(), name="rcon-poll")

    def _alert_poster(self, svc: VoteService):
        async def post(text: str) -> None:
            await self._post(svc.cfg.admin_alert_channel_id, f"Vote bot alert: {self.tag(svc)}{text}")
        return post

    def _announce_poster(self, svc: VoteService):
        async def post(text: str) -> None:
            await self._post(svc.cfg.announce_channel_id, self.tag(svc) + text)
        return post

    async def _post(self, channel_id: int | None, text: str) -> None:
        if not channel_id:
            return
        ch = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        if getattr(ch, "guild", None) is None or ch.guild.id != self.cfg.guild_id:
            log.error("channel %s is not in the home guild; not posting", channel_id)
            return
        await ch.send(_clean(text)[:2000])

    async def on_connect(self) -> None:
        _health.recorder().on_connect()

    async def on_disconnect(self) -> None:
        _health.recorder().on_disconnect()

    async def on_resumed(self) -> None:
        _health.recorder().on_resumed()

    async def on_ready(self) -> None:
        _health.recorder().on_ready()
        log.info("logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        for g in list(self.guilds):
            if g.id != self.cfg.guild_id:
                log.warning("leaving unexpected guild %s (%s)", g.name, g.id)
                await g.leave()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        if guild.id != self.cfg.guild_id:
            log.warning("added to unexpected guild %s (%s); leaving", guild.name, guild.id)
            await guild.leave()

    async def close(self) -> None:
        self.hub.stop()
        await super().close()

    def ephemeral(self) -> bool:
        return self.cfg.ephemeral_replies


# --------------------------------------------------------------------------- commands
SERVER_HELP = "Game server (only needed if this community runs more than one)"


def register_commands(bot: VoteBot) -> None:
    hub = bot.hub
    cfg = bot.cfg

    def _filter(items: list[tuple[str, str]], current: str) -> list[app_commands.Choice[str]]:
        cur = (current or "").lower()
        out = [app_commands.Choice(name=n[:100], value=v[:100]) for n, v in items if cur in n.lower() or cur in v.lower()]
        return out[:25]

    def ns_server(interaction: discord.Interaction) -> str | None:
        return str(getattr(interaction.namespace, "server", "") or "") or None

    def svc_for(interaction: discord.Interaction) -> VoteService:
        return hub.for_member(interaction.user.id, ns_server(interaction))

    def header(svc: VoteService) -> list[str]:
        return [f"Server: **{_clean(svc.name)}**"] if hub.multi else []

    async def ac_server(interaction: discord.Interaction, current: str):
        return _filter([(s.name, s.server_id) for s in hub.all()], current)

    async def ac_map(interaction: discord.Interaction, current: str):
        cat = svc_for(interaction).catalog
        return _filter([(m.name, m.id) for m in cat.maps.values()], current)

    async def ac_weather(interaction: discord.Interaction, current: str):
        cat = svc_for(interaction).catalog
        return _filter([(name, lid) for lid, name in cat.lightings.items()], current)

    async def ac_circle(interaction: discord.Interaction, current: str):
        cat = svc_for(interaction).catalog
        mo = cat.resolve_map(str(getattr(interaction.namespace, "map", "") or ""))
        items = [("Default Circle", "default")]
        if mo:
            items += [(name, tag) for tag, name in mo.circles.items() if name.lower() != "default circle"]
        return _filter(items, current)

    async def ac_mode(interaction: discord.Interaction, current: str):
        cat = svc_for(interaction).catalog
        return _filter([(m, m) for m in cat.modes], current)

    async def ac_player(interaction: discord.Interaction, current: str):
        items = []
        for svc, sid, name in hub.online_players():
            where = f" - {svc.name}" if hub.multi else ""
            items.append((f"{name} (…{sid[-4:]}){where}", sid))
        items.sort(key=lambda x: x[0].lower())
        return _filter(items, current)

    def limit_line(svc: VoteService, used: int) -> str:
        return f"Voting limit: {used} of {svc.cfg.max_votes_per_map} votes used for the current map."

    def up_line(svc: VoteService, used: int) -> str:
        return f"Upvotes: {used} of {svc.cfg.upvotes_per_map} used for the current map."

    def limits_for(svc: VoteService, discord_id: int) -> str:
        line = limit_line(svc, svc.votes_used(discord_id))
        if svc.cfg.upvotes_per_map > 0:
            line += "\n" + up_line(svc, svc.upvotes_used(discord_id))
        return line

    def up_hint(svc: VoteService, discord_id: int) -> str | None:
        """Point players at /voteup when they still have upvotes and there is something to back."""
        if svc.cfg.upvotes_per_map > 0 and svc.upvotes_used(discord_id) < svc.cfg.upvotes_per_map and svc.results():
            return "Like a pick that's already listed? Back it with /voteup."
        return None

    async def ac_listed(interaction: discord.Interaction, current: str):
        svc = svc_for(interaction)
        items = []
        for r in svc.listed_choices():
            n = f"{svc.catalog.label(Selection.from_key(r.choice))} ({r.count} vote{'s' if r.count != 1 else ''})"
            items.append((n, r.choice))
        return _filter(items, current)

    async def not_on_a_server(interaction: discord.Interaction, verb: str = "vote") -> None:
        """Several servers, and the player isn't on any of them."""
        if not hub.db.link_for_discord(interaction.user.id):
            text = "Link your in-game player first: join one of our servers and run /link, then /verify."
        elif hub.any_unreachable():
            text = "I can't reach one of the game servers to confirm where you're playing. Try again shortly."
        else:
            text = f"You must be on one of our game servers to {verb}: {_clean(hub.names())}. Join one and try again."
        await interaction.response.send_message(text, ephemeral=True)

    # ---------------- /vote
    @bot.tree.command(name="vote", description="Vote for the next map, game mode, weather and circle")
    @app_commands.describe(map="Map to play next", mode="Game mode", weather="Weather / time of day",
                           circle="Play-area circle (optional, map default if empty)")
    @app_commands.autocomplete(map=ac_map, mode=ac_mode, weather=ac_weather, circle=ac_circle)
    @app_commands.guild_only()
    async def vote(interaction: discord.Interaction, map: app_commands.Range[str, 1, 100],
                   mode: app_commands.Range[str, 1, 20], weather: app_commands.Range[str, 1, 100],
                   circle: app_commands.Range[str, 0, 100] | None = None):
        S = VoteStatus
        svc = hub.vote_target(interaction.user.id)
        if svc is None:
            await not_on_a_server(interaction)
            return
        cat, scfg = svc.catalog, svc.cfg
        pre = svc.precheck(interaction.user.id, map, mode, weather, circle)
        if pre.status is S.OK:
            await interaction.response.defer(ephemeral=bot.ephemeral(), thinking=True)
            out = await svc.cast_vote(interaction.user.id, map, mode, weather, circle)
        else:
            out = pre
        if out.status is S.OK:
            assert out.selection
            parts = header(svc) + [f"Thank you for your vote for {_clean(cat.label(out.selection))}. Next map results:",
                                   svc.format_results(_clean)]
            hint = up_hint(svc, interaction.user.id)
            if hint:
                parts.append(hint)
            footer = svc.messages.pick()
            if footer:
                parts.append("")
                parts.append(footer)
            parts.append(limit_line(svc, out.used))
            if scfg.upvotes_per_map > 0:
                parts.append(up_line(svc, svc.upvotes_used(interaction.user.id)))
            await interaction.followup.send("\n".join(parts)[:2000], ephemeral=bot.ephemeral())
            return
        hint = up_hint(svc, interaction.user.id) if out.status is S.LIMIT else None
        msgs = {
            S.CLOSED: "Voting for the next map is closed for this round. Use /votestatus to see the result.",
            S.NOT_READY: "The vote options are still loading from the game server. Try again in a minute.",
            S.BAD_MAP: "That map isn't one of the options. Pick one from the list (see /voteoptions).",
            S.BAD_MODE: f"Game mode must be one of: {', '.join(cat.modes) or ', '.join(MODE_NAMES)}.",
            S.BAD_WEATHER: "That weather isn't one of the options. Pick one from the list (see /voteoptions).",
            S.BAD_CIRCLE: "That circle isn't available on this map. Pick one from the list or leave it empty.",
            S.SAME_MAP: "That map is playing right now. Pick a different map for the next round.",
            S.NOT_LINKED: "Link your in-game player first: join the server and run /link, then /verify.",
            S.SERVER_UNKNOWN: "I can't reach the game server to confirm you're online. Try again shortly.",
            S.OFFLINE: "You must be on the game server to vote. Join the server and try again.",
            S.TOO_NEW: (f"You need to be on the server for at least {scfg.min_presence_minutes} minutes before voting. "
                        f"You've been on for {out.minutes_on} min - try again in about {out.wait_minutes} min."),
            S.LIMIT: (f"You have reached the voting limit ({out.used} of {scfg.max_votes_per_map}) for the current map. "
                      "Your votes reset when the next map starts." + (f" {hint}" if hint else "") +
                      "\n\nNext map results:\n" + svc.format_results(_clean)),
        }
        text = msgs.get(out.status, "That didn't work. Try again shortly.")[:2000]
        if interaction.response.is_done():  # failed after deferring (e.g. race with another vote)
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    # ---------------- /votestatus
    def status_block(svc: VoteService, discord_id: int) -> list[str]:
        parts = header(svc) + [f"Current map: {_clean(svc.current_map_label())}", "Next map results:",
                               svc.format_results(_clean)]
        if hub.db.link_for_discord(discord_id):
            parts.append(limits_for(svc, discord_id))
        if not svc.tracker.fresh():
            parts.append("(Game server currently unreachable - results may be out of date.)")
        return parts

    @bot.tree.command(name="votestatus", description="Show the current next-map vote results (does not vote)")
    @app_commands.describe(server=SERVER_HELP)
    @app_commands.autocomplete(server=ac_server)
    @app_commands.guild_only()
    async def votestatus(interaction: discord.Interaction, server: app_commands.Range[str, 1, 60] | None = None):
        uid = interaction.user.id
        if server and not hub.by_ref(server):
            await interaction.response.send_message(f"Unknown server. Choose one of: {_clean(hub.names())}.",
                                                    ephemeral=True)
            return
        if hub.multi and not server and not hub.playing_on(uid):
            parts: list[str] = []
            for svc in hub.all():
                parts += status_block(svc, uid) + [""]
            text = "\n".join(parts).strip()
        else:
            text = "\n".join(status_block(hub.for_member(uid, server), uid))
        await interaction.response.send_message(text[:2000], ephemeral=bot.ephemeral())

    # ---------------- /voteoptions
    @bot.tree.command(name="voteoptions", description="List the maps, game modes, weather and circles you can vote for")
    @app_commands.describe(map="Show the circles for one map (optional)", server=SERVER_HELP)
    @app_commands.autocomplete(map=ac_map, server=ac_server)
    @app_commands.guild_only()
    async def voteoptions(interaction: discord.Interaction, map: app_commands.Range[str, 1, 100] | None = None,
                          server: app_commands.Range[str, 1, 60] | None = None):
        if server and not hub.by_ref(server):
            await interaction.response.send_message(f"Unknown server. Choose one of: {_clean(hub.names())}.",
                                                    ephemeral=True)
            return
        svc = hub.for_member(interaction.user.id, server)
        cat, scfg = svc.catalog, svc.cfg
        if not cat.loaded:
            await interaction.response.send_message("Options are still loading from the game server.", ephemeral=True)
            return
        e = discord.utils.escape_markdown
        lines = header(svc) + [
            "**Maps:** " + ", ".join(e(m.name) for m in cat.maps.values()),
            "**Game modes:** " + ", ".join(e(m) for m in cat.modes),
            "**Weather:** " + ", ".join(e(v) for v in cat.lightings.values())]
        # Layout variants share their map's circles, so list circles once per map.
        maps = [cat.resolve_map(map)] if map else [m for m in cat.maps.values() if not m.is_layout]
        for mo in maps:
            if mo is None:
                lines.append("Unknown map.")
                continue
            names = ["Default Circle"] + [c for c in mo.circles.values() if c.lower() != "default circle"]
            lines.append(f"**{e(mo.name)} circles:** " + ", ".join(e(c) for c in names))
        lines.append(f"You can vote {scfg.max_votes_per_map} time{'s' if scfg.max_votes_per_map != 1 else ''} per map "
                     f"while on the server (after {scfg.min_presence_minutes} min online).")
        if scfg.upvotes_per_map > 0:
            lines.append(f"You also get {scfg.upvotes_per_map} upvote{'s' if scfg.upvotes_per_map != 1 else ''} "
                         "per map: use /voteup to back a pick that's already listed.")
        if scfg.block_current_map:
            lines.append("The map that is playing now can't be voted for the next round.")
        await interaction.response.send_message(_clean_keep_bold("\n".join(lines))[:2000], ephemeral=bot.ephemeral())

    # ---------------- /link /verify /unlink /mystatus
    @bot.tree.command(name="link", description="Link your Discord to your in-game player (you must be on the server)")
    @app_commands.describe(player="Your in-game name (start typing) or SteamID64")
    @app_commands.autocomplete(player=ac_player)
    @app_commands.guild_only()
    async def link(interaction: discord.Interaction, player: app_commands.Range[str, 1, 64]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await hub.start_link(interaction.user.id, player)
        await interaction.followup.send(_clean_keep_bold(msg), ephemeral=True)

    @bot.tree.command(name="verify", description="Finish linking with the code sent to you in the in-game chat")
    @app_commands.describe(code="The 6-character code from the in-game chat")
    @app_commands.guild_only()
    async def verify(interaction: discord.Interaction, code: app_commands.Range[str, 1, 12]):
        ok, msg = await hub.verify_link(interaction.user.id, code)
        await interaction.response.send_message(msg, ephemeral=True)

    @bot.tree.command(name="unlink", description="Unlink your Discord from your in-game player")
    @app_commands.guild_only()
    async def unlink(interaction: discord.Interaction):
        ok = hub.unlink(interaction.user.id)
        await interaction.response.send_message("Unlinked." if ok else "You weren't linked.", ephemeral=True)

    def presence_line(svc: VoteService, sid: str) -> str:
        mins = int(svc.tracker.seconds_on(sid) // 60)
        need = svc.cfg.min_presence_minutes
        if not svc.tracker.fresh():
            return "Game server unreachable right now."
        if not svc.tracker.is_online(sid):
            return "You're not on the game server."
        if mins < need:
            return f"On the server for {mins} min - you can vote after {need} min."
        return f"On the server for {mins} min - eligible to vote."

    @bot.tree.command(name="mystatus", description="Check whether you can vote right now")
    @app_commands.guild_only()
    async def mystatus(interaction: discord.Interaction):
        uid = interaction.user.id
        link_row = hub.db.link_for_discord(uid)
        if not link_row:
            await interaction.response.send_message("Not linked yet. Join the server and run /link.", ephemeral=True)
            return
        sid = link_row["steam_id"]
        name = link_row["player_name"] or "player"
        if not hub.multi:
            svc = hub.first
            await interaction.response.send_message(
                f"Linked to {_clean(name)} (…{sid[-4:]}). {presence_line(svc, sid)}\n{limits_for(svc, uid)}",
                ephemeral=True)
            return
        lines = [f"Linked to {_clean(name)} (…{sid[-4:]})."]
        for svc in hub.all():
            lines.append(f"**{_clean(svc.name)}:** {presence_line(svc, sid)}")
        here = hub.playing_on(uid)
        if here:
            lines.append(f"Your votes go to **{_clean(here.name)}**. {limits_for(here, uid)}")
        else:
            lines.append("Join one of the servers to vote.")
        await interaction.response.send_message(_clean_keep_bold("\n".join(lines))[:2000], ephemeral=True)

    # ---------------- /voteup
    @bot.tree.command(name="voteup", description="Upvote a map pick that's already in the vote (yours or someone else's)")
    @app_commands.describe(choice="Pick from the options that already have votes")
    @app_commands.autocomplete(choice=ac_listed)
    @app_commands.guild_only()
    async def voteup(interaction: discord.Interaction, choice: app_commands.Range[str, 1, 100]):
        S = VoteStatus
        svc = hub.vote_target(interaction.user.id)
        if svc is None:
            await not_on_a_server(interaction, "upvote")
            return
        cat, scfg = svc.catalog, svc.cfg
        pre = svc.precheck_up(interaction.user.id, choice)
        if pre.status is S.OK:
            await interaction.response.defer(ephemeral=bot.ephemeral(), thinking=True)
            out = await svc.cast_upvote(interaction.user.id, choice)
        else:
            out = pre
        if out.status is S.OK:
            assert out.selection
            parts = header(svc) + [f"Upvote counted for {_clean(cat.label(out.selection))}. Next map results:",
                                   svc.format_results(_clean)]
            footer = svc.messages.pick()
            if footer:
                parts.append("")
                parts.append(footer)
            parts.append(limit_line(svc, svc.votes_used(interaction.user.id)))
            parts.append(up_line(svc, out.used))
            await interaction.followup.send("\n".join(parts)[:2000], ephemeral=bot.ephemeral())
            return
        cap = scfg.upvote_max_per_option
        msgs = {
            S.UP_OFF: "Upvotes are turned off on this server. Use /vote instead.",
            S.CLOSED: "Voting for the next map is closed for this round. Use /votestatus to see the result.",
            S.NOT_READY: "The vote options are still loading from the game server. Try again in a minute.",
            S.NOT_LISTED: ("That pick doesn't have any votes yet. Choose one from the list, "
                           "or start it with /vote.\n\nNext map results:\n" + svc.format_results(_clean)),
            S.NOT_LINKED: "Link your in-game player first: join the server and run /link, then /verify.",
            S.SERVER_UNKNOWN: "I can't reach the game server to confirm you're online. Try again shortly.",
            S.OFFLINE: "You must be on the game server to upvote. Join the server and try again.",
            S.TOO_NEW: (f"You need to be on the server for at least {scfg.min_presence_minutes} minutes before voting. "
                        f"You've been on for {out.minutes_on} min - try again in about {out.wait_minutes} min."),
            S.LIMIT: (f"You have used all your upvotes ({out.used} of {scfg.upvotes_per_map}) for the current map. "
                      "They reset when the next map starts.\n\nNext map results:\n" + svc.format_results(_clean)),
            S.UP_OPTION_CAP: (f"You've already upvoted that pick {cap} time{'s' if cap != 1 else ''}, the most allowed "
                              "for one pick. Back a different one, or wait for the next map."),
        }
        text = msgs.get(out.status, "That didn't work. Try again shortly.")[:2000]
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)

    # ---------------- /voteadmin
    admin = app_commands.Group(name="voteadmin", description="Map vote admin tools",
                               default_permissions=None, guild_only=True)  # visibility; access is checked by is_admin()

    def is_admin(interaction: discord.Interaction) -> bool:
        m = interaction.user
        if not isinstance(m, discord.Member):
            return False
        if m.guild_permissions.administrator:
            return True
        if cfg.admin_role_ids:
            return any(r.id in cfg.admin_role_ids for r in m.roles)
        return m.guild_permissions.manage_guild

    async def admin_target(interaction: discord.Interaction, server: str | None) -> VoteService | None:
        """The server an admin action applies to. With several servers it must be named."""
        if server:
            svc = hub.by_ref(server)
            if svc is None:
                await interaction.response.send_message(f"Unknown server. Choose one of: {_clean(hub.names())}.",
                                                        ephemeral=True)
            return svc
        if hub.multi:
            await interaction.response.send_message(
                f"This bot runs several servers. Choose one with the server option: {_clean(hub.names())}.",
                ephemeral=True)
            return None
        return hub.first

    async def status_text(svc: VoteService) -> str:
        st = svc.tracker.state
        scfg, cat = svc.cfg, svc.catalog
        ov = svc.slots.override
        if ov:
            slot_txt = f"{ov.status} - slot {ov.slot} holds {cat.label(Selection.from_key(ov.choice))}"
            if ov.prev:
                slot_txt += (f"; previous vote in slot {ov.prev['slot']} "
                             f"({ov.prev.get('status')}, restored after its round)")
        else:
            slot_txt = "none (default rotation untouched)"
        rot_txt = "unknown"
        try:
            rot = await svc.rcon.rotation()
            ents = rot.get("entries") or []

            def fmt(e):
                alt = (e.get("zoneAlternator") or "").split(".")
                circle = f" {alt[2]}" if len(alt) > 2 else ""
                return (f"[{e.get('index')}] {e.get('map')} {'+'.join(e.get('experiences') or [])} "
                        f"{e.get('lighting')}{circle}")
            now_e = next((e for e in ents if str(e.get('status')).lower() == "now"), None)
            next_e = next((e for e in ents if str(e.get('status')).lower() == "next"), None)
            rot_txt = (f"now {fmt(now_e) if now_e else 'not a rotation entry'} | "
                       f"next {fmt(next_e) if next_e else 'none'}")
        except Exception as e:  # status must always answer
            rot_txt = f"unavailable ({type(e).__name__})"
        return ((f"Server: {svc.name} ({svc.server_id})\n" if hub.multi else "") +
                f"RCON: {'OK' if svc.tracker.fresh() else 'UNREACHABLE'}"
                f"{' - ' + st.last_error if st.last_error else ''}\n"
                f"Players online: {len(st.online)} | Round id: {svc.round_id()}\n"
                f"Current map: {svc.current_map_label()}\n"
                f"Vote options: {len(cat.maps)} maps, {len(cat.lightings)} weather\n"
                f"Voting: {'closed' if svc.voting_closed() else 'open'} | close mode: {scfg.vote_close_mode}"
                f"{f' ({scfg.vote_close_after_minutes} min)' if scfg.vote_close_mode == 'timer' else ''}"
                f" | min votes: {scfg.min_votes_to_finalize}\n"
                f"Map application: {scfg.map_application_mode}{' (DRY RUN)' if scfg.dry_run else ''}"
                f"{' - DISABLED: ' + svc.slots.disabled_reason if svc.slots.disabled_reason else ''}\n"
                f"Vote slot override: {slot_txt}\n"
                f"Server rotation: {rot_txt}\n"
                f"Footer messages loaded: {svc.messages.count()}")

    @admin.command(name="status", description="Bot health: version, RCON, round, next map submitted")
    @app_commands.describe(server=SERVER_HELP)
    @app_commands.autocomplete(server=ac_server)
    @app_commands.check(is_admin)
    async def a_status(interaction: discord.Interaction, server: app_commands.Range[str, 1, 60] | None = None):
        if server and not hub.by_ref(server):
            await interaction.response.send_message(f"Unknown server. Choose one of: {_clean(hub.names())}.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        targets = [hub.by_ref(server)] if server else hub.all()
        blocks = [f"Vote bot {__version__} | servers: {len(hub.services)}"]
        for svc in targets:
            blocks.append(await status_text(svc))
        await interaction.followup.send(_clean("\n\n".join(blocks))[:2000], ephemeral=True)

    @admin.command(name="newround", description="Force-start a new voting round (resets everyone's votes)")
    @app_commands.describe(server=SERVER_HELP)
    @app_commands.autocomplete(server=ac_server)
    @app_commands.check(is_admin)
    async def a_newround(interaction: discord.Interaction, server: app_commands.Range[str, 1, 60] | None = None):
        svc = await admin_target(interaction, server)
        if not svc:
            return
        await svc.new_match(f"manual by {interaction.user.id}")
        await interaction.response.send_message(f"{bot.tag(svc)}New voting round started.", ephemeral=True)

    @admin.command(name="finalize", description="Close voting and put the winner in the NEXT rotation slot (no map change now)")
    @app_commands.describe(server=SERVER_HELP)
    @app_commands.autocomplete(server=ac_server)
    @app_commands.check(is_admin)
    async def a_finalize(interaction: discord.Interaction, server: app_commands.Range[str, 1, 60] | None = None):
        svc = await admin_target(interaction, server)
        if not svc:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, msg = await svc.finalize()
        log.info("finalize on %s by %s: %s", svc.server_id, interaction.user.id, msg)
        await interaction.followup.send(_clean(bot.tag(svc) + msg)[:2000], ephemeral=True)

    @admin.command(name="canceloverride", description="Put the default rotation entry back and reopen voting")
    @app_commands.describe(server=SERVER_HELP)
    @app_commands.autocomplete(server=ac_server)
    @app_commands.check(is_admin)
    async def a_cancel(interaction: discord.Interaction, server: app_commands.Range[str, 1, 60] | None = None):
        svc = await admin_target(interaction, server)
        if not svc:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        msg = await svc.cancel_override()
        log.info("canceloverride on %s by %s: %s", svc.server_id, interaction.user.id, msg)
        await interaction.followup.send(_clean(bot.tag(svc) + msg)[:2000], ephemeral=True)

    @admin.command(name="enableapply", description="Re-enable rotation writes after the bot stopped itself for safety")
    @app_commands.describe(server=SERVER_HELP)
    @app_commands.autocomplete(server=ac_server)
    @app_commands.check(is_admin)
    async def a_enable(interaction: discord.Interaction, server: app_commands.Range[str, 1, 60] | None = None):
        svc = await admin_target(interaction, server)
        if not svc:
            return
        reason = svc.slots.disabled_reason
        svc.slots.enable()
        log.info("enableapply on %s by %s (was: %s)", svc.server_id, interaction.user.id, reason)
        await interaction.response.send_message(
            f"{bot.tag(svc)}Rotation writes re-enabled{f' (was disabled: {reason})' if reason else ''}.", ephemeral=True)

    @admin.command(name="unlink", description="Remove a member's player link")
    @app_commands.check(is_admin)
    async def a_unlink(interaction: discord.Interaction, member: discord.User):
        ok = hub.unlink(member.id)
        await interaction.response.send_message("Unlinked." if ok else "That member wasn't linked.", ephemeral=True)

    @admin.command(name="reload", description="Reload vote options from the game server and options file")
    @app_commands.describe(server=SERVER_HELP)
    @app_commands.autocomplete(server=ac_server)
    @app_commands.check(is_admin)
    async def a_reload(interaction: discord.Interaction, server: app_commands.Range[str, 1, 60] | None = None):
        if server and not hub.by_ref(server):
            await interaction.response.send_message(f"Unknown server. Choose one of: {_clean(hub.names())}.",
                                                    ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        targets = [hub.by_ref(server)] if server else hub.all()  # reload is harmless: do all by default
        lines = []
        for svc in targets:
            ok = await svc.refresh_catalog()
            lines.append(f"{bot.tag(svc)}{'Options reloaded' if ok else 'Reload failed - see the bot log'}"
                         f" ({len(svc.catalog.maps)} maps, {len(svc.catalog.lightings)} weather).")
        await interaction.followup.send(_clean("\n".join(lines))[:2000], ephemeral=True)

    bot.tree.add_command(admin)


def _clean_keep_bold(text: str) -> str:
    """Neutralise mentions but keep our own **bold** formatting."""
    return discord.utils.escape_mentions(text)
