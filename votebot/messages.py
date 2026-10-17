"""Footer messages read from a plain text file, one message per line.

- Blank lines and lines starting with '#' are ignored.
- The file is re-read automatically when it changes (no restart needed).
- Lines are capped in length and Discord mentions are neutralised by the bot's
  AllowedMentions.none() policy, so an edited file can never ping @everyone.
"""

from __future__ import annotations

import logging
import random
from pathlib import Path

log = logging.getLogger("votebot.messages")
MAX_LINE = 300
MAX_FILE_BYTES = 256_000


class MessageFile:
    def __init__(self, path: Path):
        self.path = path
        self._mtime: float | None = None
        self._lines: list[str] = []
        self._rng = random.SystemRandom()

    def _reload(self) -> None:
        try:
            st = self.path.stat()
        except FileNotFoundError:
            if self._lines:
                log.warning("messages file %s disappeared", self.path)
            self._lines, self._mtime = [], None
            return
        if self._mtime == st.st_mtime:
            return
        if st.st_size > MAX_FILE_BYTES:
            log.error("messages file %s is larger than %d bytes; ignoring", self.path, MAX_FILE_BYTES)
            self._lines, self._mtime = [], st.st_mtime
            return
        text = self.path.read_text(encoding="utf-8", errors="replace")
        self._lines = [ln.strip()[:MAX_LINE] for ln in text.splitlines()
                       if ln.strip() and not ln.strip().startswith("#")]
        self._mtime = st.st_mtime
        log.info("loaded %d footer messages from %s", len(self._lines), self.path)

    def pick(self) -> str | None:
        self._reload()
        return self._rng.choice(self._lines) if self._lines else None

    def count(self) -> int:
        self._reload()
        return len(self._lines)
