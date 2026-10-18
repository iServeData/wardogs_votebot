"""The guided server manager (deploy/manage_servers.sh), driven with scripted answers."""
import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from mock_rcon import make_app  # noqa: E402
from votebot.config import load_servers  # noqa: E402
from votebot.manage import Manager, Paths, Probe, System, probe  # noqa: E402

MAIN_URL = "http://203.0.113.10:7779"
EU_URL = "http://198.51.100.20:7779"


class FakeSystem(System):
    def __init__(self, tmp: Path, restart_log: str = "synced 9 slash commands\ncatalog loaded\ncatalog loaded\n"):
        super().__init__(Paths(etc=tmp / "etc", credstore=tmp / "enc", credstore_plain=tmp / "creds",
                               unit=tmp / "unit.service", dropins=tmp / "unit.service.d"))
        self.restart_log, self.restarts, self.reloads = restart_log, 0, 0

    def encrypted_creds(self):
        return False

    def daemon_reload(self):
        self.reloads += 1

    def restart(self):
        self.restarts += 1
        return time.time()

    def is_failed(self):
        return False

    def log_since(self, start):
        return self.restart_log

    def sleep(self, s):
        pass

    def bot_group(self):
        return None


class World:
    """Two game servers that answer the connection test, plus scripted keyboard input."""

    def __init__(self, tmp: Path, **kw):
        self.tmp = tmp
        (tmp / "etc").mkdir()
        (tmp / "etc" / "votebot.env").write_text(
            "# settings\nDISCORD_GUILD_ID=1\nRCON_BASE_URL=http://203.0.113.10:7779\nRCON_ALLOW_INSECURE_HTTP=true\n"
            f"MAX_VOTES_PER_MAP=1\nUPVOTES_PER_MAP=2\nVOTE_CLOSE_MODE=live\nDATA_DIR={tmp / 'data'}\n")
        (tmp / "unit.service").write_text(
            "[Service]\nLoadCredentialEncrypted=discord_token:/x\nLoadCredentialEncrypted=rcon_password:/y\n")
        (tmp / "creds").mkdir()
        (tmp / "creds" / "votebot_rcon_password").write_text("main-pw-123")
        self.sys = FakeSystem(tmp, **kw)
        self.servers = {MAIN_URL: ("main-pw-123", "Example | NA West | Map Voting", "join-main"),
                        EU_URL: ("eu-pw-123456", "Example | EU | Map Voting", "join-eu")}
        self.answers, self.secrets, self.output = [], [], []

    def prober(self, url, pw, ca=None):
        if url not in self.servers:
            return Probe(False, error="no answer")
        want, name, sid = self.servers[url]
        if pw != want:
            return Probe(False, error="the server rejected the password")
        return Probe(True, name, "3/100 players", sid)

    def run(self, answers, secrets=()):
        self.answers, self.secrets = list(answers), list(secrets)

        def ask(prompt):
            self.output.append(prompt)
            if not self.answers:
                raise AssertionError(f"ran out of answers at: {prompt}")
            return self.answers.pop(0)

        def secret(prompt):
            self.output.append(prompt)
            return self.secrets.pop(0)

        m = Manager(self.sys, ask=ask, secret=secret, out=lambda *a: self.output.append(" ".join(map(str, a))),
                    prober=self.prober)
        code = m.run()
        assert not self.answers, f"unused answers: {self.answers}"
        return code

    @property
    def text(self):
        return "\n".join(self.output)

    def doc(self):
        return json.loads((self.tmp / "etc" / "servers.json").read_text())

    def env(self):
        return (self.tmp / "etc" / "votebot.env").read_text()

    def configs(self):
        """What the bot itself would load (passwords via a credentials directory, like systemd)."""
        cd = self.tmp / "credentials"
        cd.mkdir(exist_ok=True)
        for f in (self.tmp / "creds").iterdir():
            (cd / f.name.removeprefix("votebot_")).write_text(f.read_text())
        (cd / "discord_token").write_text("t" * 30)
        from votebot.manage import read_env
        return load_servers({**read_env(self.tmp / "etc" / "votebot.env"), "CREDENTIALS_DIRECTORY": str(cd)})


ADD_EU = ["a", "http", "198.51.100.20", "", "", "y", "2", "", "", "", "admin", "", "yes", "", "", "", "", ""]
#         add proto   host            port name settings? votes up cap min  close  minv block apply ann adm save restart


def test_add_second_server_from_single_server_setup(tmp_path):
    w = World(tmp_path)
    assert w.run(ADD_EU + ["q"], secrets=["eu-pw-123456", "eu-pw-123456"]) == 0
    main, eu = w.doc()["servers"]
    assert (main["id"], main["name"], main["nameSource"], main["rconPasswordCredential"]) == \
        ("main", "Example | NA West | Map Voting", "server", "rcon_password")
    assert (eu["id"], eu["name"], eu["rconBaseUrl"], eu["serverId"]) == \
        ("server_2", "Example | EU | Map Voting", EU_URL, "join-eu")
    assert eu["settings"] == {"MAX_VOTES_PER_MAP": 2, "VOTE_CLOSE_MODE": "admin", "BLOCK_CURRENT_MAP": True}
    assert "SERVERS_FILE=" in w.env() and "RCON_BASE_URL=" in w.env()          # nothing else in the env changed
    assert (tmp_path / "creds" / "votebot_rcon_password_server_2").read_text() == "eu-pw-123456"
    drop = (tmp_path / "unit.service.d" / "rcon_server_2.conf").read_text()
    assert "LoadCredential=rcon_password_server_2:" in drop
    assert w.sys.restarts == 1 and w.sys.reloads == 1
    a, b = w.configs()
    assert (a.server_id, a.rcon_password, a.max_votes_per_map, a.name_from_server) == ("main", "main-pw-123", 1, True)
    assert (b.server_id, b.rcon_password, b.max_votes_per_map, b.vote_close_mode, b.block_current_map,
            b.upvotes_per_map) == ("server_2", "eu-pw-123456", 2, "admin", True, 2)
    assert "eu-pw-123456" not in w.text                                         # never echoed


def test_modify_keeps_password_and_sets_custom_name(tmp_path):
    w = World(tmp_path)
    w.run(ADD_EU + ["q"], secrets=["eu-pw-123456", "eu-pw-123456"])
    w.servers["http://198.51.100.20:7780"] = w.servers.pop(EU_URL)
    w.run(["m", "2", "", "", "7780", "EU Central", "n", "", "", "q"], secrets=[""])
    eu = w.doc()["servers"][1]
    assert (eu["rconBaseUrl"], eu["name"], eu["nameSource"]) == ("http://198.51.100.20:7780", "EU Central", "custom")
    assert "settings" in eu                                                     # untouched by "n"
    assert (tmp_path / "creds" / "votebot_rcon_password_server_2").read_text() == "eu-pw-123456"
    # back to following the server's name
    w.run(["m", "2", "", "", "", "server", "n", "", "", "q"], secrets=[""])
    assert w.doc()["servers"][1]["name"] == "Example | EU | Map Voting"


def test_new_password_replaces_the_stored_one(tmp_path):
    w = World(tmp_path)
    w.run(ADD_EU + ["q"], secrets=["eu-pw-123456", "eu-pw-123456"])
    w.servers[MAIN_URL] = ("main-pw-NEW-1", *w.servers[MAIN_URL][1:])
    w.run(["m", "1", "", "", "", "", "n", "", "", "q"], secrets=["main-pw-NEW-1", "main-pw-NEW-1"])
    assert (tmp_path / "creds" / "votebot_rcon_password").read_text() == "main-pw-NEW-1"


def test_remove_needs_confirmation_and_deletes_password(tmp_path):
    w = World(tmp_path)
    w.run(ADD_EU + ["q"], secrets=["eu-pw-123456", "eu-pw-123456"])
    w.run(["r", "2", "no", "q"])                                                # not confirmed
    assert len(w.doc()["servers"]) == 2 and "Nothing was changed" in w.text
    w.run(["r", "2", "yes", "", "q"])
    assert [s["id"] for s in w.doc()["servers"]] == ["main"]
    assert not (tmp_path / "unit.service.d" / "rcon_server_2.conf").exists()
    assert not (tmp_path / "creds" / "votebot_rcon_password_server_2").exists()
    assert (tmp_path / "creds" / "votebot_rcon_password").exists()              # the unit still needs this one
    w.run(["r", "q"])
    assert "last one can't be removed" in w.text


def test_failed_restart_puts_everything_back(tmp_path):
    w = World(tmp_path, restart_log="Traceback (most recent call last):\n  boom\n")
    env_before = w.env()
    w.run(ADD_EU + ["q"], secrets=["eu-pw-123456", "eu-pw-123456"])
    assert "previous files were put back" in w.text
    assert w.env() == env_before and not (tmp_path / "etc" / "servers.json").exists()
    assert not (tmp_path / "unit.service.d" / "rcon_server_2.conf").exists()
    assert not (tmp_path / "creds" / "votebot_rcon_password_server_2").exists()
    assert w.sys.restarts == 2                                                  # tried, then the old setup


def test_bad_input_is_asked_again_and_duplicates_refused(tmp_path):
    w = World(tmp_path)
    w.run(["a", "ftp", "http", "http://198.51.100.20", "198.51.100.20", "99999", "7779", "",
           "n", "", "", "q"], secrets=["short", "eu-pw-123456", "nope", "eu-pw-123456", "eu-pw-123456"])
    assert "Type https or http" in w.text and "valid IP address or hostname" in w.text
    assert "1 to 65535" in w.text and "too short" in w.text and "didn't match" in w.text
    assert len(w.doc()["servers"]) == 2
    w.run(["a", "http", "203.0.113.10", "", "q"])                               # same address as main
    assert "already set up" in w.text and len(w.doc()["servers"]) == 2


def test_unreachable_server_can_be_retried_or_saved(tmp_path):
    w = World(tmp_path)
    w.run(["a", "https", "eu.example.org", "", "r", "s", "My EU", "n", "", "", "q"],
          secrets=["eu-pw-123456", "eu-pw-123456"])
    assert w.text.count("Couldn't connect") == 2
    eu = w.doc()["servers"][1]
    assert (eu["name"], eu["nameSource"], eu["rconBaseUrl"]) == ("My EU", "custom", "https://eu.example.org:7779")


def test_server_rename_is_picked_up(tmp_path):
    w = World(tmp_path)
    w.run(ADD_EU + ["q"], secrets=["eu-pw-123456", "eu-pw-123456"])
    w.servers[EU_URL] = ("eu-pw-123456", "Example | EU | Seed tonight", "join-eu")
    w.run(["q"])
    assert "renamed itself" in w.text and w.doc()["servers"][1]["name"] == "Example | EU | Seed tonight"


def test_hand_edited_keys_are_kept(tmp_path):
    w = World(tmp_path)
    w.run(ADD_EU + ["q"], secrets=["eu-pw-123456", "eu-pw-123456"])
    doc = w.doc()
    doc["servers"][1]["optionsFile"] = "/etc/wardogs-votebot/options_eu.json"
    doc["servers"][1]["settings"]["RCON_POLL_SECONDS"] = 20
    (tmp_path / "etc" / "servers.json").write_text(json.dumps(doc))
    w.run(["m", "2", "", "", "", "", "n", "", "", "q"], secrets=[""])
    eu = w.doc()["servers"][1]
    assert eu["optionsFile"].endswith("options_eu.json") and eu["settings"]["RCON_POLL_SECONDS"] == 20


def test_real_connection_test_against_a_mock_server():
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    box = {}

    def serve():
        asyncio.set_event_loop(loop)
        srv = TestServer(make_app("pw-123456", server_name="Example | EU"), host="127.0.0.1")
        loop.run_until_complete(srv.start_server())
        box["srv"], box["url"] = srv, f"http://127.0.0.1:{srv.port}"
        ready.set()
        loop.run_forever()
        loop.run_until_complete(srv.close())

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    ready.wait(5)
    try:
        ok = probe(box["url"], "pw-123456")
        assert ok.ok and ok.name == "Example | EU" and ok.server_id
        bad = probe(box["url"], "wrong-pw-1")
        assert not bad.ok and "rejected the password" in bad.error
        none = probe("http://127.0.0.1:9", "pw-123456")
        assert not none.ok and "no answer" in none.error
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(5)


def test_live_server_name_is_used_by_the_bot(tmp_path):
    from test_multi_server import Two

    async def go():
        t = await Two().start(tmp_path)
        try:
            west = t.svcs[0]
            object.__setattr__(west.cfg, "name_from_server", True)
            assert west.name == "Example Community | Seed nights 7 PM"
            from mock_rcon import STATE
            t.apps[0][STATE]["server_name"] = "Example | Renamed"
            await west.tracker.poll()
            assert west.name == "Example | Renamed" and t.hub.by_ref("example | renamed") is west
        finally:
            await t.close()
    asyncio.run(go())


@pytest.mark.parametrize("v,ok", [("203.0.113.10", True), ("eu.example.org", True), ("::1", True),
                                  ("http://x", False), ("a b", False), ("x:7779", False)])
def test_host_validation(v, ok):
    from votebot.manage import valid_host
    assert valid_host(v) is ok


def test_example_servers_file_is_valid():
    cfgs = load_servers({"DISCORD_GUILD_ID": "1", "SERVERS_FILE": str(ROOT / "config" / "servers.json.example")},
                        check_only=True)
    assert [(c.server_id, c.name_from_server) for c in cfgs] == [("main", True), ("server_2", False)]
