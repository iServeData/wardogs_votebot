"""CI check: every pin in requirements.txt must be in requirements.lock at the same version.

A Dependabot pull request changes requirements.txt only; this check then fails with the fix:
run tools/update_lock.sh and commit the new lock.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pins(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*==\s*([^\s;\\]+)", line)
        if m:
            out[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return out


def main() -> int:
    want, have = pins(ROOT / "requirements.txt"), pins(ROOT / "requirements.lock")
    bad = [f"{n}=={v} (lock has {have.get(n, 'nothing')})" for n, v in want.items() if have.get(n) != v]
    if bad:
        print("requirements.lock is out of date: " + ", ".join(bad))
        print("Fix: bash tools/update_lock.sh, then commit requirements.lock")
        return 1
    print(f"check_lock: ok ({len(want)} pin(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
