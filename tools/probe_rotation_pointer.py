"""Find out what a rotation write does to the server's "next map" pointer.

Run ONLY on an EMPTY server. It makes small, reversible rotation edits:

  1. reads the pointer (which entry is now / next)
  2. changes the weather of ONE entry that is neither now nor next, reads the pointer again
  3. puts that line back exactly, reads the pointer again
  4. with --advance: ends the current (empty) match so a rotation entry is playing,
     then repeats steps 1-3 (this tells us if the result differs when the
     current map is part of the rotation)

The config file ends up byte-for-byte as it started. Never uses force=true.
Never prints secrets.

    /opt/wardogs-votebot/venv/bin/python tools/probe_rotation_pointer.py http://203.0.113.10:7779 --advance
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from votebot.rcon import RconClient, RconError  # noqa: E402
from votebot.rotation import entry_fields, parse_doc  # noqa: E402


async def pointer(c: RconClient) -> tuple[int | None, int | None, str]:
    rot = await c.rotation()
    st = await c.status()
    now = nxt = None
    for i, e in enumerate(rot.get("entries") or []):
        s = str(e.get("status") or "").lower()
        if s == "now":
            now = int(e.get("index", i))
        elif s == "next":
            nxt = int(e.get("index", i))
    r = st.get("rotation") or {}
    live = f"{st.get('map')} {'+'.join(st.get('experiences') or [])} {st.get('lighting')} {st.get('alternator') or ''}"
    return now, nxt, f"now={now} next={nxt} (status says now={r.get('nowIndex')} next={r.get('nextIndex')}) | live: {live}"


async def write(c: RconClient, text: str, revision: str) -> None:
    v = await c.validate_config(text)
    if v.get("errors") or v.get("ok") is False:
        raise SystemExit(f"validate refused: {v.get('errors')}")
    await c.put_config(text, revision)


async def one_round(c: RconClient, label: str, lightings: list[str]) -> dict:
    print(f"\n=== {label} ===")
    now0, nxt0, d = await pointer(c)
    print("  before        :", d)
    cfg = await c.get_config()
    doc = parse_doc(cfg["text"])
    n = len(doc.entry_idx)
    target = next(i for i in range(n - 1, -1, -1) if i not in (now0, nxt0))
    line = doc.entry(target)
    cur = entry_fields(line).get("Lighting", "")
    alt = next(x for x in lightings if x != cur)
    print(f"  editing entry {target}: Lighting {cur} -> {alt}")
    result = {"label": label, "before": (now0, nxt0)}
    try:
        await write(c, doc.replaced(target, line.replace(f'Lighting="{cur}"', f'Lighting="{alt}"')), cfg["revision"])
        await asyncio.sleep(3)
        a = await pointer(c)
        print("  after edit    :", a[2])
        result["after_edit"] = a[:2]
    finally:
        cfg2 = await c.get_config()
        await write(c, parse_doc(cfg2["text"]).replaced(target, line), cfg2["revision"])
        await asyncio.sleep(3)
        b = await pointer(c)
        print("  after restore :", b[2])
        result["after_restore"] = b[:2]
        same = (await c.get_config())["text"] == cfg["text"]
        print("  config identical to start:", same)
    return result


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("--advance", action="store_true", help="end the empty match and repeat with a rotation map live")
    a = ap.parse_args()
    c = RconClient(a.base_url.rstrip("/"), getpass.getpass("RCON password (hidden): "), 10, 120)
    try:
        players = await c.players()
        if players:
            raise SystemExit(f"{len(players)} player(s) online - run this only on an empty server.")
        lightings = [x["id"] for x in await c.catalog_lightings()]
        results = [await one_round(c, "current map as it is now", lightings)]
        if a.advance:
            _n, _x, before = await pointer(c)
            print("\nending the empty match (POST /v1/match/end) ...")
            await c.request("POST", "/v1/match/end")
            for _ in range(60):
                await asyncio.sleep(5)
                _n2, _x2, d = await pointer(c)
                if d.split("| live:")[1] != before.split("| live:")[1]:
                    print("  new match:", d)
                    break
            else:
                print("  map did not change within 5 minutes; skipping second round")
            results.append(await one_round(c, "after advancing into the rotation", lightings))
        print("\nSUMMARY (copy this back):")
        for r in results:
            print(f"  {r['label']}: before={r['before']} after_edit={r.get('after_edit')} "
                  f"after_restore={r.get('after_restore')}")
    except RconError as e:
        print("RCON error:", e)
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
