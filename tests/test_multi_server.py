"""0.2.0 features: several game servers, layouts, readable names, BLOCK_CURRENT_MAP, migration, health."""
import asyncio
import importlib.util
import json
import re
import sqlite3
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from mock_rcon import STATE, make_app  # noqa: E402
from test_votebot import PW, cfg  # noqa: E402
from votebot import __version__  # noqa: E402
from votebot.catalog import Catalog, Selection, circle_name  # noqa: E402
from votebot.config import ConfigError, load_servers  # noqa: E402
from votebot.db import DB, Store  # noqa: E402
from votebot.health import HealthRecorder  # noqa: E402
from votebot.hub import Hub  # noqa: E402
from votebot.messages import MessageFile  # noqa: E402
from votebot.rcon import RconClient  # noqa: E402
from votebot.service import VoteService, VoteStatus  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ names
def test_version_is_set():
    assert re.match(r"^\d+\.\d+\.\d+$", __version__)


def test_circle_names_drop_the_map_name():
    assert circle_name("ZoneAlternator.Zestafona.SmallFactory.Circle", "Zestafona SmallFactory Circle") == \
        "Small Factory Circle"
    assert circle_name("ZoneAlternator.Bakurani.Farmland.Circle", "Farmland Circle") == "Farmland Circle"
    assert circle_name("ZoneAlternator.Ozeti.River.Circle", "") == "River Circle"


class _Srv:
    """Minimal stand-in for the catalog endpoints of a live server."""
    async def catalog_maps(self):
        return [{"id": "Kavkazi", "displayName": "Kavkazi"}, {"id": "Europe", "displayName": "Europe"}]

    async def map_experiences(self, m):
        return {"Kavkazi": ["Bakurani_KOTH_01", "Bakurani_KOTH_02", "KOTH_InfantryOnly", "KOTH_Hardcore"],
                "Europe": ["Madrid_KOTH_01", "KOTH_InfantryOnly", "KOTH_Hardcore"]}[m]

    async def map_alternators(self, m):
        city = {"Kavkazi": "Bakurani", "Europe": "Ozeti"}[m]
        return [{"tag": f"ZoneAlternator.{city}.River.Circle", "displayName": f"{city} River Circle"}]

    async def catalog_lightings(self):
        return [{"id": i, "displayName": i} for i in ("DayClear", "DayLateRain", "DayLateGrayFog")]


def _catalog(tmp: Path, opts: dict) -> Catalog:
    f = tmp / "options.json"
    f.write_text(json.dumps(opts))
    c = Catalog(f)
    run(c.refresh(_Srv()))
    assert c.loaded
    return c


OPTS = {"maps": {"Kavkazi": {"name": "Bakurani"}, "Europe": {"name": "Ozeti"}},
        "modes": {"Standard": [], "Infantry": ["KOTH_InfantryOnly"], "Hardcore": ["KOTH_Hardcore"]}}


def test_layouts_named_hidden_and_mapped(tmp_path):
    c = _catalog(tmp_path, dict(OPTS, layouts={"Bakurani_KOTH_02": "New spawns"}))
    assert [m.name for m in c.maps.values()] == ["Bakurani", "Bakurani (New spawns)", "Ozeti"]
    lay = c.resolve_map("Bakurani (New spawns)")
    assert lay.id == "Kavkazi@Bakurani_KOTH_02" and c.server_map(lay.id) == "Kavkazi"
    assert c.experiences_for(Selection(lay.id, "Infantry", "DayClear", "")) == ["Bakurani_KOTH_02", "KOTH_InfantryOnly"]
    assert c.option_for_status({"map": "Kavkazi", "experiences": ["Bakurani_KOTH_02"]}).id == lay.id
    hidden = _catalog(tmp_path, dict(OPTS, layouts={"Bakurani_KOTH_02": False}))
    assert [m.name for m in hidden.maps.values()] == ["Bakurani", "Ozeti"]


def test_weather_names_readable_and_renamable(tmp_path):
    c = _catalog(tmp_path, OPTS)
    assert c.lightings["DayLateRain"] == "Day Late Rain"
    c2 = _catalog(tmp_path, dict(OPTS, lightingNames={"DayLateRain": "Rain", "NotOnServer": "x"}))
    assert c2.lightings["DayLateRain"] == "Rain" and "NotOnServer" not in c2.lightings
    assert c2.resolve_map("Bakurani").circles["ZoneAlternator.Bakurani.River.Circle"] == "River Circle"


# ------------------------------------------------------------------ config: servers file
BASE_ENV = {"DISCORD_GUILD_ID": "1", "DISCORD_TOKEN": "t" * 30, "RCON_BASE_URL": "https://a.example:7779",
            "RCON_PASSWORD": "shared-should-not-leak"}


def _pw(path: Path, text: str = "pw-123456") -> Path:
    path.write_text(text)
    path.chmod(0o600)
    return path


def _servers(tmp: Path, servers, **env):
    f = tmp / "servers.json"
    f.write_text(json.dumps({"servers": servers}))
    return load_servers({**BASE_ENV, "SERVERS_FILE": str(f), **env})


def test_single_server_without_servers_file():
    (c,) = load_servers(BASE_ENV)
    assert c.server_id == "main" and c.rcon_password == "shared-should-not-leak"


def test_servers_file_builds_one_config_per_server(tmp_path):
    _pw(tmp_path / "pa", "pw-a-123456\n")
    _pw(tmp_path / "pb", "pw-b-123456\n")
    a, b = _servers(tmp_path, [
        {"id": "west", "name": "West", "rconBaseUrl": "https://w.example:7779", "rconPasswordFile": str(tmp_path / "pa"),
         "settings": {"MAX_VOTES_PER_MAP": 2, "BLOCK_CURRENT_MAP": True}},
        {"id": "east", "name": "East", "rconBaseUrl": "https://e.example:7779", "rconPasswordFile": str(tmp_path / "pb")},
    ])
    assert (a.server_id, a.label, a.rcon_password, a.max_votes_per_map, a.block_current_map) == \
        ("west", "West", "pw-a-123456", 2, True)
    assert (b.server_id, b.rcon_password, b.max_votes_per_map, b.block_current_map) == ("east", "pw-b-123456", 3, False)


@pytest.mark.parametrize("servers,msg", [
    ([{"id": "Bad Id", "name": "x", "rconBaseUrl": "https://w.example"}], "id must be"),
    ([{"id": "a", "name": "A", "rconBaseUrl": "https://w.example", "rconPasswordFile": "PA"},
      {"id": "a", "name": "B", "rconBaseUrl": "https://e.example", "rconPasswordFile": "PB"}], "unique"),
    ([{"id": "a", "name": "A", "rconBaseUrl": "https://w.example", "settings": {"DISCORD_GUILD_ID": "2"}}],
     "can't be set per server"),
    ([{"id": "a", "name": "A", "rconBaseUrl": "https://w.example", "rconPassword": "x"}], "unknown key"),
    ([{"id": "a", "name": "A", "rconBaseUrl": "https://w.example"}], "RCON_PASSWORD"),  # never inherited
])
def test_servers_file_rejects_mistakes(tmp_path, servers, msg):
    for s in servers:
        if s.get("rconPasswordFile"):
            s["rconPasswordFile"] = str(_pw(tmp_path / s["rconPasswordFile"]))
    with pytest.raises(ConfigError, match=msg):
        _servers(tmp_path, servers)


def test_same_rcon_url_twice_rejected(tmp_path):
    _pw(tmp_path / "p")
    s = {"rconBaseUrl": "https://w.example:7779", "rconPasswordFile": str(tmp_path / "p")}
    with pytest.raises(ConfigError, match="same rconBaseUrl"):
        _servers(tmp_path, [dict(s, id="a", name="A"), dict(s, id="b", name="B")])


# ------------------------------------------------------------------ database upgrade
def test_v1_database_upgrades_and_keeps_data(tmp_path):
    p = tmp_path / "votebot.db"
    c = sqlite3.connect(p)  # the 0.1.x layout
    c.executescript("""
      CREATE TABLE links (discord_id INTEGER PRIMARY KEY, steam_id TEXT NOT NULL UNIQUE, player_name TEXT,
                          linked_at REAL NOT NULL);
      CREATE TABLE link_codes (discord_id INTEGER PRIMARY KEY, steam_id TEXT NOT NULL, code_hash TEXT NOT NULL,
                               expires_at REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
      CREATE TABLE presence (steam_id TEXT PRIMARY KEY, name TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL);
      CREATE TABLE rounds (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at REAL NOT NULL, ended_at REAL,
                           signature TEXT NOT NULL, end_reason TEXT);
      CREATE TABLE votes (id INTEGER PRIMARY KEY AUTOINCREMENT, round_id INTEGER NOT NULL REFERENCES rounds(id),
                          discord_id INTEGER NOT NULL, steam_id TEXT NOT NULL, choice TEXT NOT NULL, ts REAL NOT NULL);
      CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT);
      INSERT INTO links VALUES (1, '76561198000000001', 'P1', 1);
      INSERT INTO presence VALUES ('76561198000000001', 'P1', 1, 2);
      INSERT INTO rounds(started_at, signature) VALUES (1, 'sig');
      INSERT INTO votes(round_id, discord_id, steam_id, choice, ts) VALUES (1, 1, '76561198000000001', 'Kavkazi|Standard|DayClear|', 1);
      INSERT INTO kv VALUES ('rotation_override', '{"x": 1}');
    """)
    c.commit()
    c.close()
    store = Store(p, "main")
    main, other = store.server("main"), store.server("eu")
    assert main.link_for_discord(1) and other.link_for_discord(1)          # links are shared
    assert main.current_round()["id"] == 1 and other.current_round() is None
    assert main.get("rotation_override") == '{"x": 1}' and other.get("rotation_override") is None
    assert len(main.load_presence()) == 1 and not other.load_presence()
    assert len(main.round_votes(1)) == 1
    assert other.start_round("sig2", None) != 1 and main.current_round()["id"] == 1
    assert (tmp_path / "votebot.db.v1_backup").exists()
    store.close()
    again = Store(p, "main")                                               # reopening is a no-op
    assert again.server("main").get("rotation_override") == '{"x": 1}'
    again.close()


# ------------------------------------------------------------------ two servers end to end
class Two:
    async def start(self, tmp: Path, **kw):
        self.store = Store(tmp / "votebot.db", "west")
        self.apps, self.servers, self.svcs = [], [], []
        (tmp / "messages.txt").write_text("Thanks for voting.\n")
        for sid, name in (("west", "West"), ("east", "East")):
            app = make_app(PW)
            srv = TestServer(app, host="127.0.0.1")
            await srv.start_server()
            url = f"http://127.0.0.1:{srv.port}"
            c = cfg(tmp, url, server_id=sid, server_name=name, min_votes_to_finalize=1, **kw)
            svc = VoteService(c, self.store.server(sid), RconClient(url, PW, 5, 500), Catalog(None),
                              MessageFile(c.messages_file))
            svc.slots.verify_seconds = 0
            assert await svc.refresh_catalog()
            self.apps.append(app)
            self.servers.append(srv)
            self.svcs.append(svc)
        self.hub = Hub(self.svcs)
        # P1 plays on West, P2 on East
        self.apps[0][STATE]["players"] = [{"name": "P1", "steamId": "76561198000000001"}]
        self.apps[1][STATE]["players"] = [{"name": "P2", "steamId": "76561198000000002"}]
        for s in self.svcs:
            await s.tracker.poll()
        for uid, app in ((1, self.apps[0]), (2, self.apps[1])):
            steam = app[STATE]["players"][0]["steamId"]
            ok, msg = await self.hub.start_link(uid, steam)
            assert ok, msg
            code = re.search(r"code: ([A-Z0-9]{6})", app[STATE]["whispers"][-1][1]).group(1)
            ok, msg = await self.hub.verify_link(uid, code)
            assert ok and "any of our servers" in msg
        for s in self.svcs:
            for sess in s.tracker.state.sessions.values():
                sess.first_seen -= 11 * 60
        return self

    async def close(self):
        for s in self.svcs:
            await s.rcon.close()
        for srv in self.servers:
            await srv.close()
        self.store.close()


def test_votes_go_to_the_server_the_player_is_on(tmp_path):
    async def go():
        t = await Two().start(tmp_path)
        west, east = t.svcs
        try:
            assert t.hub.multi and t.hub.vote_target(1) is west and t.hub.vote_target(2) is east
            assert t.hub.vote_target(99) is None                                   # not linked anywhere
            assert (await t.hub.vote_target(1).cast_vote(1, "Bakurani", "Hardcore", "Day Clear", None)).status \
                is VoteStatus.OK
            assert (await t.hub.vote_target(2).cast_vote(2, "Ozeti", "Standard", "Day Clear", None)).status \
                is VoteStatus.OK
            assert [r.count for r in west.results()] == [1] and "Bakurani" in west.format_results(str)
            assert [r.count for r in east.results()] == [1] and "Ozeti" in east.format_results(str)
            # The other server's own checks still refuse a player who isn't on it
            assert (await east.cast_vote(1, "Ozeti", "Standard", "Day Clear", None)).status is VoteStatus.OFFLINE
            assert t.hub.by_ref("EAST") is east and t.hub.by_ref("west") is west and t.hub.by_ref("nope") is None
            assert {(s.server_id, n) for s, _, n in t.hub.online_players()} == {("west", "P1"), ("east", "P2")}
            # a link started for a player who is on no server explains which servers exist
            ok, msg = await t.hub.start_link(5, "76561198000000005")
            assert not ok and "West, East" in msg
            # finalize on one server writes only that server's rotation
            ok, _ = await west.finalize()
            assert ok
            assert t.apps[0][STATE]["config"] != t.apps[1][STATE]["config"]
        finally:
            await t.close()
    run(go())


def test_block_current_map(tmp_path):
    async def go():
        t = await Two().start(tmp_path, block_current_map=True)
        west = t.svcs[0]
        try:
            playing = west.catalog.option_for_status(west.tracker.state.status)
            assert playing is not None
            out = await west.cast_vote(1, playing.name, "Standard", "Day Clear", None)
            assert out.status is VoteStatus.SAME_MAP
            other = next(m for m in west.catalog.maps.values() if m.map_id != playing.map_id)
            assert (await west.cast_vote(1, other.name, "Standard", "Day Clear", None)).status is VoteStatus.OK
        finally:
            await t.close()
    run(go())


def test_health_lists_each_server_and_keeps_the_old_shape(tmp_path):
    async def go():
        t = await Two().start(tmp_path)
        try:
            single = HealthRecorder(None)
            single.wrap_tracker(t.svcs[0].tracker, "west", "West")
            await t.svcs[0].tracker.poll()
            snap = single.snapshot()
            assert "servers" not in snap and snap["rcon"]["last_result"] == "ok" and snap["players"]["current"] == 1

            rec = HealthRecorder(None)
            for s in t.svcs:
                rec.wrap_tracker(s.tracker, s.server_id, s.name)
            await t.svcs[0].tracker.poll()
            assert rec.snapshot()["loop_progress_at"] is None                 # East hasn't polled yet
            await t.svcs[1].tracker.poll()
            snap = rec.snapshot()
            assert [(x["id"], x["name"], x["rcon"]["last_result"]) for x in snap["servers"]] == \
                [("west", "West", "ok"), ("east", "East", "ok")]
            assert snap["rcon"] == snap["servers"][0]["rcon"] and snap["loop_progress_at"]
        finally:
            await t.close()
    run(go())


def test_health_records_the_name_each_server_reports(tmp_path):
    from votebot.health import clean_server_name

    async def go():
        t = await Two().start(tmp_path)
        try:
            t.apps[0][STATE]["server_name"] = "Example | West | Map Voting | example.org/join"
            t.apps[1][STATE]["server_name"] = "  Example\u200b |\tEast\n| \x07Map Voting  "
            rec = HealthRecorder(None)
            for s in t.svcs:
                rec.wrap_tracker(s.tracker, s.server_id, s.name)
            snap = rec.snapshot()
            assert snap["server_name"] is None and [x["server_name"] for x in snap["servers"]] == [None, None]
            for s in t.svcs:
                await s.tracker.poll()
            snap = rec.snapshot()
            assert snap["server_name"] == "Example | West | Map Voting | example.org/join"
            assert [x["server_name"] for x in snap["servers"]] == \
                ["Example | West | Map Voting | example.org/join", "Example | East | Map Voting"]
            # A failed poll keeps the last good name; a rename shows on the next good poll
            t.apps[0][STATE]["server_name"] = "Example | West | renamed"
            await t.svcs[0].tracker.poll()
            assert rec.snapshot()["server_name"] == "Example | West | renamed"
        finally:
            await t.close()
    run(go())
    assert clean_server_name("x" * 150) == "x" * 100
    assert clean_server_name(None) is None and clean_server_name(12) is None and clean_server_name(" \n ") is None


def test_discord_commands_register_for_one_and_many_servers(tmp_path):
    if importlib.util.find_spec("discord") is None:
        pytest.skip("discord.py not installed")
    from votebot.bot import VoteBot

    async def go():
        t = await Two().start(tmp_path)
        try:
            for hub in (Hub(t.svcs[:1]), t.hub):
                bot = VoteBot(t.svcs[0].cfg, hub)
                names = {c.name for c in bot.tree.get_commands()}
                assert {"vote", "voteup", "votestatus", "voteoptions", "link", "verify", "unlink", "mystatus",
                        "voteadmin"} <= names
                status = next(c for c in bot.tree.get_commands() if c.name == "votestatus")
                assert "server" in {p.name for p in status.parameters}
                await bot.close()
        finally:
            await t.close()
    run(go())


def test_single_db_path_still_works(tmp_path):
    db = DB(tmp_path / "x.db")
    db.set("a", "1")
    assert db.get("a") == "1" and db.server_id == "main"
    db.close()
