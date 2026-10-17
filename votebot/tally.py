"""Vote tallying.

Counting modes
- additive: every vote counts (a player may spread or stack their 3 votes).
- latest:   only each player's most recent vote counts (the 3 votes act as
            up to 3 changes of mind).

Ranking: most votes first. Ties go to the option that reached that total
first (the "oldest" highest value), i.e. the option whose last counted vote is
oldest. Final fallback is the earliest vote id, so ordering is deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .db import Vote


@dataclass(frozen=True)
class Result:
    choice: str
    count: int
    reached_at: float   # time the option reached its current count
    first_vote_id: int


def tally(votes: Iterable[Vote], counting: str = "additive") -> list[Result]:
    votes = sorted(votes, key=lambda v: v.id)
    if counting == "latest":
        latest: dict[str, Vote] = {}
        for v in votes:
            if getattr(v, "kind", "vote") == "vote":
                latest[v.steam_id] = v
        ups = [v for v in votes if getattr(v, "kind", "vote") == "up"]
        counted = sorted(list(latest.values()) + ups, key=lambda v: v.id)
    else:
        counted = list(votes)

    agg: dict[str, list[Vote]] = {}
    for v in counted:
        agg.setdefault(v.choice, []).append(v)

    results = [
        Result(choice=c, count=len(vs), reached_at=max(x.ts for x in vs), first_vote_id=min(x.id for x in vs))
        for c, vs in agg.items()
    ]
    # Stable tie-break: reached_at then the id of the vote that completed the count.
    last_id = {c: max(x.id for x in vs) for c, vs in agg.items()}
    results.sort(key=lambda r: (-r.count, r.reached_at, last_id[r.choice]))
    return results


def leader(results: list[Result], min_votes: int = 1) -> Result | None:
    if results and results[0].count >= min_votes:
        return results[0]
    return None
