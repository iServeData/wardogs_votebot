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

from mock_rcon import STATE, make_app  # noqa: E402
from votebot.catalog import Catalog, Selection  # noqa: E402
from votebot.config import Config, ConfigError, _validate_rcon_url  # noqa: E402
from votebot.db import DB, Vote  # noqa: E402
from votebot.messages import MessageFile  # noqa: E402
from votebot.ratelimit import RateLimiter  # noqa: E402
from votebot.rcon import RconClient  # noqa: E402
from votebot.service import VoteService, VoteStatus  # noqa: E402
from votebot.tally import tally  # noqa: E402

PW = "test-password-123"


def cfg(tmp: Path, url: str, **kw) -> Config:
    base = dict(discord_token="x" * 30, guild_id=1, vote_channel_ids=frozenset(), admin_role_ids=frozenset(),
                rcon_base_url=url, rcon_password=PW, rcon_ca_file=None, rcon_timeout_s=5,
                rcon_max_requests_per_min=500, poll_seconds=15, min_presence_minutes=10,
                presence_grace_seconds=180, max_votes_per_map=3, vote_counting="additive",
                min_votes_to_finalize=1, map_application_mode="rotation_slot", vote_close_mode="admin",
                vote_close_after_minutes=30, live_settle_seconds=0, live_min_interval_seconds=15,
                live_max_writes=8, restore_delay_seconds=10, announce_channel_id=None,
                admin_alert_channel_id=None, broadcast_result=False, dry_run=False, detect_score_reset=True, data_dir=tmp,
                messages_file=tmp / "messages.txt", options_file=None, ephemeral_replies=True,
                results_max_lines=10, link_code_ttl_s=300, server_display_name="Example", log_level="INFO")
    base.update(kw)
    return Config(**base)


# ------------------------------------------------------------------ tally
def V(i, steam, choice, ts):
    return Vote(i, int(steam), str(steam), choice, ts)


def test_tie_goes_to_option_that_reached_count_first():
    votes = [V(1, 1, "A", 1), V(2, 2, "B", 2), V(3, 3, "B", 3), V(4, 4, "A", 4)]
    r = tally(votes)
    assert [x.choice for x in r] == ["B", "A"]  # B reached 2 at t=3, A at t=4


def test_strict_majority_overtakes():
    votes = [V(1, 1, "A", 1), V(2, 2, "B", 2), V(3, 3, "B", 3)]
    assert tally(votes)[0].choice == "B"


def test_latest_mode_counts_one_per_player():
    votes = [V(1, 1, "A", 1), V(2, 1, "A", 2), V(3, 1, "B", 3), V(4, 2, "A", 4)]
    add = {r.choice: r.count for r in tally(votes, "additive")}
    lat = {r.choice: r.count for r in tally(votes, "latest")}
    assert add == {"A": 3, "B": 1}
    assert lat == {"A": 1, "B": 1}


# ------------------------------------------------------------------ misc
def test_rcon_url_rules():
    _validate_rcon_url("https://203.0.113.4:7777", False)
    _validate_rcon_url("http://127.0.0.1:7776", False)
    with pytest.raises(ConfigError):
        _validate_rcon_url("http://203.0.113.4:7777", False)
    _validate_rcon_url("http://203.0.113.4:7777", True)
    with pytest.raises(ConfigError):
        _validate_rcon_url("https://user:pw@203.0.113.4", False)


def test_path_segments_are_validated():
    assert RconClient.seg("76561198000000001") == "76561198000000001"
    for bad in ["../config", "a/b", "", "x" * 65, "id?x=1"]:
        with pytest.raises(ValueError):
            RconClient.seg(bad)


def test_ratelimiter_blocks_spammers():
    rl = RateLimiter()
    allowed = sum(1 for _ in range(20) if rl.take("vote", 42) == 0)
    assert allowed == 3
    assert rl.blocked_for(42) > 0
    assert rl.take("query", 7) == 0  # other users unaffected


def test_messages_file(tmp_path):
    f = tmp_path / "m.txt"
    f.write_text("# comment\n\nJoin us at 7 PM Sunday - Thursday.\n   \n")
    mf = MessageFile(f)
    assert mf.pick() == "Join us at 7 PM Sunday - Thursday."
    assert MessageFile(tmp_path / "missing.txt").pick() is None


