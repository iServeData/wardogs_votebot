"""Read-only check of what your WARDOGS server returns for the config document.

Never writes anything and never prints secret values.

    cd ~/wardogs-votebot
    /opt/wardogs-votebot/venv/bin/python tools/check_rcon_config.py http://203.0.113.10:7779

It asks for the RCON password (input hidden).
"""

from __future__ import annotations

import asyncio
import getpass
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from votebot.rcon import RconClient, RconError  # noqa: E402

SECRET = re.compile(r"^\s*(Password|PasswordHash|ServerPassword|Token)\s*=\s*(.*)$", re.IGNORECASE)


def describe(v):
    if isinstance(v, str):
        return f"string, {len(v)} chars"
    if isinstance(v, list):
        return f"list, {len(v)} items"
    if isinstance(v, dict):
        return f"object, keys={sorted(v)[:10]}"
    return repr(v)


def scan_text(text: str) -> None:
    lines = text.splitlines()
    print(f"    document: {len(lines)} lines")
    in_rot = False
    for ln in lines:
        if re.match(r"^\s*\[", ln):
            in_rot = "WDServerMapRotationSettings" in ln
            print(f"    section {ln.strip()}")
            continue
        m = SECRET.match(ln)
        if m:
            val = m.group(2).strip().strip('"')
            state = "EMPTY" if not val else ("MASKED -> unsafe to write back" if re.fullmatch(
                r"\(hidden\)|<hidden>|hidden|\*{3,}|•+|<redacted>|redacted|\[redacted\]", val, re.I) else "present (value not shown)")
            print(f"      {m.group(1)}: {state}")
        elif in_rot and ln.strip():
            print(f"      {ln.strip()[:150]}")


async def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    base = sys.argv[1].rstrip("/")
    pw = getpass.getpass("RCON password (hidden): ")
    c = RconClient(base, pw, 10, 60)
    try:
        caps = await c.capabilities()
        print("build:", caps.get("build"), "| apiVersion:", caps.get("apiVersion"))
        print("config capability:", json.dumps(caps.get("config")))
        print("config/rotation routes:", sorted(r for r in c.routes if "config" in r or "rotation" in r))
        for path in ("/v1/config", "/v1/config?detail=full"):
            print(f"\nGET {path}")
            try:
                payload, raw, headers = await c.request("GET", path, want_raw=True)
            except RconError as e:
                print("   error:", e)
                continue
            print("   Content-Type:", headers.get("Content-Type"), "| ETag present:", bool(headers.get("ETag")))
            if isinstance(payload, dict):
                for k, v in payload.items():
                    print(f"   {k}: {describe(v)}" + (f" = {v}" if k in ('revision', 'writable') else ""))
                for k in ("text", "document", "ini", "content"):
                    if isinstance(payload.get(k), str):
                        scan_text(payload[k])
            else:
                print(f"   non-JSON body, {len(raw)} chars")
                if raw.strip():
                    scan_text(raw)
        rot = await c.rotation()
        print("\nGET /v1/rotation: enabled=", rot.get("enabled"), "mode=", rot.get("mode"),
              "entries=", len(rot.get("entries") or []))
        for e in rot.get("entries") or []:
            print(f"   [{e.get('index')}] {e.get('status') or '':4} {e.get('map')} {'+'.join(e.get('experiences') or [])} "
                  f"{e.get('lighting')} {e.get('zoneAlternator') or ''}")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
