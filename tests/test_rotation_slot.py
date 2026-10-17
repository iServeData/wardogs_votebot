"""End-to-end tests of voting + rotation-slot override against the mock WARDOGS server."""
import asyncio
import re
import sys
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))

from mock_rcon import DEFAULT_DOC, STATE, make_app  # noqa: E402
from test_votebot import PW, cfg  # noqa: E402
from votebot.catalog import Catalog  # noqa: E402
from votebot.db import DB  # noqa: E402
from votebot.messages import MessageFile  # noqa: E402
from votebot.rcon import RconClient  # noqa: E402
from votebot.rotation import RotationError, build_entry, parse_doc  # noqa: E402
from votebot.service import VoteService, VoteStatus  # noqa: E402

VOTED = ('.RotationEntries=(Map="Kavkazi", Experiences="Bakurani_KOTH_01+KOTH_Hardcore", '
         'Lighting="DayEndClear", ZoneAlternator="ZoneAlternator.Bakurani.Farmland.Circle")')


# ------------------------------------------------------------------ pure document handling
def test_parse_and_replace_keeps_everything_else():
    doc = parse_doc(DEFAULT_DOC)
    assert len(doc.entry_idx) == 9                      # ClearArray is not counted
    assert doc.entry(0).startswith('.RotationEntries=(Map="Kavkazi"')
    new = doc.replaced(3, VOTED)
    a, b = DEFAULT_DOC.split("\r\n"), new.split("\r\n")
    assert len(a) == len(b)
    assert [i for i in range(len(a)) if a[i] != b[i]] == [doc.entry_idx[3]]  # exactly one line changed
    assert "\r\n" in new and "\n\n" not in new.replace("\r\n", "")         # CRLF preserved
    assert parse_doc(new).replaced(3, doc.entry(3)) == DEFAULT_DOC           # perfect round trip


def test_build_entry_formats_and_rejects_injection():
    assert build_entry(".", "Europe", ["Madrid_KOTH_01"], "DayClear", "") == \
        '.RotationEntries=(Map="Europe", Experience="Madrid_KOTH_01", Lighting="DayClear")'
    for bad in ['Europe")\n[/Script/WDRCON.WDRCONSettings]', 'a"b', "x y"]:
        with pytest.raises(RotationError):
            build_entry(".", bad, ["Madrid_KOTH_01"], "DayClear", "")


def test_unsupported_rotation_ops_refused():
    with pytest.raises(RotationError):
        parse_doc(DEFAULT_DOC.replace("!RotationEntries=ClearArray", "-RotationEntries=(Map=\"Europe\")"))
    with pytest.raises(RotationError):
        parse_doc("[/Script/Engine.GameSession]\r\nMaxPlayers=10\r\n")


# ------------------------------------------------------------------ helpers
class Env:
    async def start(self, tmp: Path, **cfgkw):
        self.app = make_app(PW)
        self.server = TestServer(self.app, host="127.0.0.1")
        await self.server.start_server()
        self.st = self.app[STATE]
        self.url = f"http://127.0.0.1:{self.server.port}"
        self.tmp = tmp
        self.cfg = cfg(tmp, self.url, min_votes_to_finalize=3, **cfgkw)
        (tmp / "messages.txt").write_text("Join us at 7 PM Sunday - Thursday.\n")
        self.db = DB(tmp / "t.db")
        self.svc = self.new_service()
        self.alerts, self.announcements = [], []
        self.wire(self.svc)
        assert await self.svc.refresh_catalog()
        self.st["players"] = [{"name": f"P{i}", "steamId": f"7656119800000000{i}"} for i in range(1, 7)]
        await self.svc.tracker.poll()
        for i, p in enumerate(self.st["players"], start=1):
            self.svc._last_whisper.clear()
            ok, msg = await self.svc.start_link(i, p["steamId"])
            assert ok, msg
            code = re.search(r"code: ([A-Z0-9]{6})", self.st["whispers"][-1][1]).group(1)
            assert (await self.svc.verify_link(i, code))[0]
        for s in self.svc.tracker.state.sessions.values():
            s.first_seen -= 11 * 60
        return self

    def new_service(self):
        svc = VoteService(self.cfg, self.db, RconClient(self.url, PW, 5, 500), Catalog(None),
                          MessageFile(self.cfg.messages_file))
        svc.slots.verify_seconds = 0  # one post-write check is enough against the mock
        return svc

    def wire(self, svc):
        async def alert(t):
            self.alerts.append(t)

        async def ann(t):
            self.announcements.append(t)
        svc.on_admin_alert, svc.on_announce = alert, ann

    async def vote(self, uid, *a):
        return await self.svc.cast_vote(uid, *a)

    async def end_match(self):
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.post(self.server.make_url("/_mock/endmatch")) as r:
                await r.json()
        reason = await self.svc.tracker.poll()
        assert reason, "new match not detected"
        await self.svc.new_match(reason)

    async def run_restore(self):
        ov = self.svc.slots.override
        assert ov and ov.status == "restore_pending", ov
        ov.restore_after = 0
        self.svc.slots._save(ov)
        await self.svc.slots.tick()

    async def close(self):
        await self.svc.rcon.close()
        await self.server.close()
        self.db.close()


HC = ("Bakurani", "Hardcore", "Day End Clear", "Farmland Circle")


def run(coro):
    asyncio.run(coro)


# ------------------------------------------------------------------ the bug: votes must never change the map
def test_votes_never_touch_the_server(tmp_path):
    async def go():
        e = await Env().start(tmp_path)
        try:
            live_before = dict(e.st["live"])
            for uid in (1, 2, 3, 4):
                assert (await e.vote(uid, *HC)).status is VoteStatus.OK
            assert e.st["match_map_calls"] == []          # never the immediate-travel route
            assert e.st["puts"] == 0                      # no config write either
            assert e.st["live"] == live_before            # current match untouched
            assert e.st["config"] == DEFAULT_DOC
            assert "| Leading" in e.svc.format_results()
        finally:
            await e.close()
    run(go())


def test_full_cycle_finalize_play_restore(tmp_path):
    async def go():
        e = await Env().start(tmp_path)
        try:
            # now = slot 2 (Zestafona Houses), next = slot 3 (Bakurani Farmland DayEarlyFog Infantry)
            for uid in (1, 2):
                await e.vote(uid, *HC)
            ok, msg = await e.svc.finalize()
            assert not ok and "at least 3" in msg and e.st["puts"] == 0       # quorum not met
            await e.vote(3, *HC)
            await e.vote(4, "Ozeti", "Standard", "Day Clear", "Church Circle")
            live_before = dict(e.st["live"])

            ok, msg = await e.svc.finalize()
            assert ok, msg
            assert e.st["puts"] == 1 and e.st["match_map_calls"] == []
            assert e.st["live"] == live_before                                 # no map change now
            doc = parse_doc(e.st["config"])
            assert doc.entry(3) == VOTED                                       # only the next slot replaced
            base = parse_doc(DEFAULT_DOC)
            assert [doc.entry(i) for i in range(9) if i != 3] == [base.entry(i) for i in range(9) if i != 3]
            assert "Next round: Bakurani, Hardcore, Day End Clear, Farmland Circle" in e.announcements[-1]
            assert "| Current next map" in e.svc.format_results().splitlines()[0]
            assert (await e.vote(5, *HC)).status is VoteStatus.CLOSED          # voting closed
            ok, msg = await e.svc.finalize()
            assert not ok and "already closed" in msg

            # natural match end -> server advances into the voted slot
            await e.end_match()
            assert e.st["live"]["map"] == "Kavkazi"
            assert e.st["live"]["experiences"] == ["Bakurani_KOTH_01", "KOTH_Hardcore"]
            assert e.svc.results() == [] and not e.svc.voting_closed()       # new round, fresh votes
            assert e.svc.slots.override.status == "consumed"                  # kept while it is playing
            assert e.st["config"] != DEFAULT_DOC
            # voted round ends -> rotation continues with the default entry after the vote slot
            await e.end_match()
            assert e.st["live"]["lighting"] == "DayEndClear" and e.st["live"]["map"] == "Europe"  # slot 4
            await e.run_restore()
            assert e.st["config"] == DEFAULT_DOC                               # byte-for-byte default again
            assert e.svc.slots.override is None
            assert e.st["now"] == 4                                            # pointer not disturbed
            await e.end_match()
            assert e.st["now"] == 5
            assert e.alerts == []
        finally:
            await e.close()
    run(go())


def test_no_votes_means_rotation_untouched(tmp_path):
    async def go():
        e = await Env().start(tmp_path, vote_close_mode="timer", vote_close_after_minutes=5)
        try:
            e.db.conn.execute("UPDATE rounds SET started_at = started_at - 600")
            await e.svc._timer_close()
            assert e.svc.voting_closed() and e.st["puts"] == 0
            assert "not enough votes" in e.announcements[-1]
            await e.end_match()
            assert e.st["config"] == DEFAULT_DOC
        finally:
            await e.close()
    run(go())


def test_timer_mode_finalizes_automatically(tmp_path):
    async def go():
        e = await Env().start(tmp_path, vote_close_mode="timer", vote_close_after_minutes=5)
        try:
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            await e.svc._timer_close()
            assert not e.svc.voting_closed()       # not yet 5 minutes
            e.db.conn.execute("UPDATE rounds SET started_at = started_at - 600")
            await e.svc._timer_close()
            assert e.svc.voting_closed() and parse_doc(e.st["config"]).entry(3) == VOTED
        finally:
            await e.close()
    run(go())


def test_staff_edit_wins_no_overwrite(tmp_path):
    async def go():
        e = await Env().start(tmp_path)
        try:
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            assert (await e.svc.finalize())[0]
            # an admin panel edits another rotation entry while the override is active
            base = parse_doc(DEFAULT_DOC)
            staff_line = base.entry(7).replace("DayStartClear", "DayClear")
            e.st["config"] = e.st["config"].replace(base.entry(7), staff_line)
            e.st["rev"] += 1
            await e.end_match()
            await e.end_match()
            await e.run_restore()
            assert staff_line in e.st["config"]                 # staff change kept
            assert VOTED in e.st["config"]                      # bot did not blindly restore
            assert any("did NOT restore" in a for a in e.alerts)
            assert e.svc.slots.override is None
        finally:
            await e.close()
    run(go())


def test_staff_jumps_to_other_map(tmp_path):
    async def go():
        e = await Env().start(tmp_path)
        try:
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            assert (await e.svc.finalize())[0]
            # staff jump: current match changes to slot 6 instead of advancing into the vote slot
            e.st["now"] = 6
            e.st["live"] = {"map": "Kavkazi", "experiences": ["Bakurani_KOTH_01", "KOTH_InfantryOnly"],
                            "lighting": "DayLateGray", "zoneAlternator": "ZoneAlternator.Bakurani.Lumberyard.Circle"}
            e.st["scores"] = [0, 0]
            reason = await e.svc.tracker.poll()
            await e.svc.new_match(reason)
            assert any("different map loaded" in a for a in e.alerts)
            await e.run_restore()
            assert e.st["config"] == DEFAULT_DOC                 # vote entry removed, default back
        finally:
            await e.close()
    run(go())


def test_stale_revision_is_refused(tmp_path):
    async def go():
        e = await Env().start(tmp_path)
        try:
            cfg_doc = await e.svc.rcon.get_config()
            e.st["rev"] += 1                                    # someone else wrote in between
            new = parse_doc(cfg_doc["text"]).replaced(3, VOTED)
            with pytest.raises(RotationError, match="revision conflict"):
                await e.svc.slots._write(cfg_doc, new, "test")
            assert e.st["config"] == DEFAULT_DOC
        finally:
            await e.close()
    run(go())


@pytest.mark.parametrize("flag", ["travel_on_put", "chaos_now"])
def test_misbehaving_server_triggers_safe_stop(tmp_path, flag):
    async def go():
        e = await Env().start(tmp_path)
        try:
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            e.st[flag] = 1 if flag == "chaos_now" else True
            ok, msg = await e.svc.finalize()
            assert not ok and "Not applied" in msg
            assert e.svc.slots.disabled_reason                   # kill switch engaged
            assert any("verification failed" in a for a in e.alerts)
            e.st[flag] = 0 if flag == "chaos_now" else False
            assert e.st["config"] == DEFAULT_DOC                 # default slot put back
            ok, msg = await e.svc.finalize()
            assert not ok and "disabled" in msg                 # stays off until an admin re-enables
            e.svc.slots.enable()
        finally:
            await e.close()
    run(go())


def test_restart_recovery_restores_default(tmp_path):
    async def go():
        e = await Env().start(tmp_path)
        try:
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            assert (await e.svc.finalize())[0]
            await e.svc.rcon.close()
            # bot goes down; the voted round starts while it is offline
            import aiohttp
            async with aiohttp.ClientSession() as s:
                await (await s.post(e.server.make_url("/_mock/endmatch"))).json()
            e.svc = e.new_service()
            e.wire(e.svc)
            assert await e.svc.refresh_catalog()
            reason = await e.svc.tracker.poll()
            assert reason
            await e.svc.new_match(reason)
            assert e.svc.slots.override.status == "consumed"
            await e.end_match()
            await e.run_restore()
            assert e.st["config"] == DEFAULT_DOC
        finally:
            await e.close()
    run(go())


def test_cancel_override(tmp_path):
    async def go():
        e = await Env().start(tmp_path)
        try:
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            assert (await e.svc.finalize())[0]
            msg = await e.svc.cancel_override()
            assert "restored" in msg and e.st["config"] == DEFAULT_DOC
            assert not e.svc.voting_closed()
        finally:
            await e.close()
    run(go())


def test_dry_run_validates_only(tmp_path):
    async def go():
        e = await Env().start(tmp_path, dry_run=True)
        try:
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            ok, msg = await e.svc.finalize()
            assert ok and "Dry run OK" in msg
            assert e.st["puts"] == 0 and e.st["config"] == DEFAULT_DOC
            assert not e.svc.voting_closed()
        finally:
            await e.close()
    run(go())


def test_vote_eligibility_rules_still_hold(tmp_path):
    async def go():
        e = await Env().start(tmp_path)
        try:
            for _ in range(3):
                assert (await e.vote(1, *HC)).status is VoteStatus.OK
            out = await e.vote(1, *HC)
            assert out.status is VoteStatus.LIMIT and out.used == 3
            assert (await e.vote(99, *HC)).status is VoteStatus.NOT_LINKED
            s = e.svc.tracker.state.sessions[e.st["players"][1]["steamId"]]
            s.first_seen = time.time() - 120
            out = await e.vote(2, *HC)
            assert out.status is VoteStatus.TOO_NEW and out.wait_minutes == 8
            e.st["players"] = e.st["players"][3:]
            await e.svc.tracker.poll()
            assert (await e.vote(3, *HC)).status is VoteStatus.OFFLINE
        finally:
            await e.close()
    run(go())


# ------------------------------------------------------------------ config response shapes (update 0.1.2)
@pytest.mark.parametrize("shape", ["text", "standard"])
def test_config_shapes_are_understood(tmp_path, shape):
    async def go():
        e = await Env().start(tmp_path)
        try:
            e.st["config_shape"] = shape
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            ok, msg = await e.svc.finalize()
            assert ok, msg
            assert parse_doc(e.st["config"]).entry(3) == VOTED
            assert "Password=SuperSecretRconPw1" in e.st["config"]
        finally:
            await e.close()
    run(go())


def test_config_without_text_explains_shape(tmp_path):
    async def go():
        e = await Env().start(tmp_path)
        try:
            e.st["config_shape"] = "never"
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            ok, msg = await e.svc.finalize()
            assert not ok and "JSON keys=" in msg and e.st["puts"] == 0
        finally:
            await e.close()
    run(go())


def test_masked_password_is_never_written_back(tmp_path):
    async def go():
        e = await Env().start(tmp_path)
        try:
            e.st["config_shape"] = "masked"
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            ok, msg = await e.svc.finalize()
            assert not ok and "masks 'Password'" in msg
            assert e.st["puts"] == 0 and "Password=SuperSecretRconPw1" in e.st["config"]
        finally:
            await e.close()
    run(go())


def test_current_map_not_in_rotation_uses_slot_0(tmp_path):
    """The live case from 2026-10-05: an off-rotation map is running, the server says next=1,
    but any rotation write makes it start again at 0."""
    async def go():
        e = await Env().start(tmp_path)
        try:
            e.st["live"] = {"map": "Europe", "experiences": ["Madrid_KOTH_01", "KOTH_Hardcore"],
                            "lighting": "DayEarlyFog", "zoneAlternator": None}
            e.st["now"] = 0                 # server still remembers its old place (next = 1)
            reason = await e.svc.tracker.poll()
            await e.svc.new_match(reason)
            for s in e.svc.tracker.state.sessions.values():
                s.first_seen -= 11 * 60
            # the mock reports now=0 although the live map is not entry 0; emulate the live server,
            # which marks no entry as now in this situation
            e.st["now"] = -1
            e.st["force_now_after_put"] = None
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            ok, msg = await e.svc.finalize()
            assert ok, msg
            assert parse_doc(e.st["config"]).entry(0) == VOTED.replace(".RotationEntries", ".RotationEntries")
            await e.end_match()
            assert e.st["live"]["experiences"] == ["Bakurani_KOTH_01", "KOTH_Hardcore"]
        finally:
            await e.close()
    run(go())


def test_bot_follows_a_moved_pointer_once(tmp_path):
    async def go():
        e = await Env().start(tmp_path)
        try:
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            e.st["force_now_after_put"] = 5           # every write makes the server point at 6
            ok, msg = await e.svc.finalize()
            assert ok, msg
            doc = parse_doc(e.st["config"])
            assert doc.entry(6) == VOTED and doc.entry(3) == parse_doc(DEFAULT_DOC).entry(3)
            assert e.svc.slots.override.slot == 6
        finally:
            await e.close()
    run(go())


def test_large_config_reply_is_read_completely(tmp_path):
    """Regression: content.read(n) returned only the first chunk of a 4 KB+ reply."""
    async def go():
        e = await Env().start(tmp_path)
        try:
            pad = "".join(f"; padding line {i} " + "x" * 80 + "\r\n" for i in range(400))  # ~40 KB
            e.st["config"] = pad + e.st["config"]
            cfg = await e.svc.rcon.get_config()
            assert cfg["text"] == e.st["config"] and cfg["revision"]
        finally:
            await e.close()
    run(go())


OZ = ("Ozeti", "Standard", "Day Clear", "River Circle")


async def live_tick(e):
    e.svc._live_last = 0.0          # skip the min-interval wait in tests
    await e.svc._live_apply()


def test_live_mode_applies_without_finalize_and_follows_the_leader(tmp_path):
    async def go():
        e = await Env().start(tmp_path, vote_close_mode="live")
        try:
            for uid in (1, 2):
                await e.vote(uid, *HC)
            await live_tick(e)
            assert e.st["config"] == DEFAULT_DOC                    # 2 votes < 3: nothing written
            await e.vote(3, *HC)
            await live_tick(e)
            assert parse_doc(e.st["config"]).entry(3) == VOTED      # queued automatically
            assert not e.svc.voting_closed()                        # voting stays open
            await live_tick(e)                                      # same leader: no extra write
            writes_before = e.db.get("live_writes")
            # another map takes the lead -> same slot is swapped, nothing else changes
            for uid in (4, 5, 6):
                await e.vote(uid, *OZ)
                await e.vote(uid, *OZ)
            await live_tick(e)
            line = parse_doc(e.st["config"]).entry(3)
            assert 'Map="Europe"' in line and "Ozeti.River" in line
            assert e.db.get("live_writes") != writes_before
            assert sum(a != b for a, b in zip(parse_doc(e.st["config"]).entries(),
                                              parse_doc(DEFAULT_DOC).entries())) == 1
            await e.end_match()
            assert e.st["live"]["map"] == "Europe"
            assert e.svc.slots.override.status == "consumed"
            assert not any("different map" in a for a in e.alerts)
        finally:
            await e.close()
    run(go())


def test_live_mode_back_to_back_votes(tmp_path):
    """Vote while the previous winner is still playing: the new vote goes in the slot after it,
    the previous slot is restored once the server has moved past it."""
    async def go():
        e = await Env().start(tmp_path, vote_close_mode="live")
        try:
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            await live_tick(e)
            await e.end_match()                                     # vote 1 playing (slot 3)
            assert e.st["now"] == 3
            for uid in (1, 2, 3):
                await e.vote(uid, *OZ)
            await live_tick(e)
            doc = parse_doc(e.st["config"])
            assert doc.entry(3) == VOTED                            # still in place while playing
            assert "Ozeti.River" in doc.entry(4)                    # vote 2 in the next slot
            assert e.st["now"] == 3                                 # server did not lose its place
            await e.end_match()                                     # vote 2 playing (slot 4)
            assert e.st["now"] == 4 and e.st["live"]["map"] == "Europe"
            ov = e.svc.slots.override
            assert ov.prev and ov.prev["status"] == "restore_pending"
            ov.prev["restore_after"] = 0
            e.svc.slots._save(ov)
            await e.svc.slots.tick()                                # slot 3 back to default
            doc = parse_doc(e.st["config"])
            assert doc.entry(3) == parse_doc(DEFAULT_DOC).entry(3)
            assert "Ozeti.River" in doc.entry(4)
            assert e.st["now"] == 4
            await e.end_match()                                     # default slot 5 plays
            assert e.st["now"] == 5
            await e.run_restore()
            assert e.st["config"] == DEFAULT_DOC
            assert e.alerts == []
        finally:
            await e.close()
    run(go())


def test_live_mode_write_cap(tmp_path):
    async def go():
        e = await Env().start(tmp_path, vote_close_mode="live", live_max_writes=1)
        try:
            for uid in (1, 2, 3):
                await e.vote(uid, *HC)
            await live_tick(e)
            for uid in (4, 5, 6):
                await e.vote(uid, *OZ)
                await e.vote(uid, *OZ)
            await live_tick(e)
            assert parse_doc(e.st["config"]).entry(3) == VOTED      # capped: no second write
            assert any("reached 1 rotation writes" in a for a in e.alerts)
        finally:
            await e.close()
    run(go())


def test_results_labels_leader_and_current_next(tmp_path):
    async def go():
        e = await Env().start(tmp_path, vote_close_mode="live")
        try:
            e.svc.cfg = e.svc.cfg.__class__(**{**e.svc.cfg.__dict__, "min_votes_to_finalize": 1})
            await e.vote(1, *HC)
            await live_tick(e)
            await e.vote(2, *OZ)
            await e.vote(3, *OZ)
            txt = e.svc.format_results()
            lines = txt.splitlines()
            assert lines[0].startswith("* Ozeti") and "Leading - becomes the next map within a minute" in lines[0]
            assert lines[1].startswith("* Bakurani") and lines[1].endswith("| Current next map")
            await live_tick(e)
            assert e.svc.format_results().splitlines()[0].endswith("| Current next map")
        finally:
            await e.close()
    run(go())
