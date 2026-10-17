"""Fails if the repository contains values that belong in a private config, not in shared code.

    python tools/check_generic.py

Flags: IPv4 addresses outside the documentation/loopback ranges, Discord-style IDs, real-looking
SteamIDs, and any term listed in the PRIVATE_TERMS environment variable (comma separated; set it
as a GitHub Actions variable so the terms themselves never appear in the repository).
"""
from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_SUFFIX = {".lock", ".png", ".jpg", ".ico", ".db"}
OK_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
                                              "127.0.0.0/8", "0.0.0.0/32", "10.0.0.0/8")]
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
STEAM = re.compile(r"\b7656119\d{10}\b")
SNOWFLAKE = re.compile(r"\b\d{17,20}\b")


def files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [ROOT / f for f in out.splitlines()]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts]


def main() -> int:
    terms = [t.strip().lower() for t in os.environ.get("PRIVATE_TERMS", "").split(",") if t.strip()]
    problems = []
    for f in files():
        if f.suffix in SKIP_SUFFIX or not f.is_file():
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = f.relative_to(ROOT)
        for n, line in enumerate(text.splitlines(), start=1):
            for m in IPV4.finditer(line):
                try:
                    ip = ipaddress.ip_address(m.group(1))
                except ValueError:
                    continue
                if not any(ip in net for net in OK_NETS):
                    problems.append(f"{rel}:{n}: IP address {ip} (use 203.0.113.x or 198.51.100.x in examples)")
            for m in STEAM.finditer(line):
                if "000000" not in m.group(0):
                    problems.append(f"{rel}:{n}: real-looking SteamID {m.group(0)}")
            for m in SNOWFLAKE.finditer(line):
                v = m.group(0)
                if not STEAM.fullmatch(v) and set(v) != {"0"}:
                    problems.append(f"{rel}:{n}: Discord-style ID {v} (use 000000000000000000 in examples)")
            low = line.lower()
            for t in terms:
                if t in low:
                    problems.append(f"{rel}:{n}: private term (#{terms.index(t) + 1} in PRIVATE_TERMS)")
    for p in problems:
        print(p)
    print(f"check_generic: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
