"""SQLite persistence (stdlib only). All queries are parameterised."""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS links (
    discord_id INTEGER PRIMARY KEY,
    steam_id   TEXT NOT NULL UNIQUE,
    player_name TEXT,
    linked_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS link_codes (
    discord_id INTEGER PRIMARY KEY,
    steam_id   TEXT NOT NULL,
    code_hash  TEXT NOT NULL,
    expires_at REAL NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS presence (
    server     TEXT NOT NULL,
    steam_id   TEXT NOT NULL,
    name       TEXT,
    first_seen REAL NOT NULL,
    last_seen  REAL NOT NULL,
    PRIMARY KEY (server, steam_id)
);
CREATE TABLE IF NOT EXISTS rounds (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    server     TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at   REAL,
    signature  TEXT NOT NULL,
    end_reason TEXT
);
CREATE INDEX IF NOT EXISTS rounds_server ON rounds(server, ended_at);
CREATE TABLE IF NOT EXISTS votes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id   INTEGER NOT NULL REFERENCES rounds(id),
    discord_id INTEGER NOT NULL,
    steam_id   TEXT NOT NULL,
    choice     TEXT NOT NULL,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'vote'
);
CREATE INDEX IF NOT EXISTS votes_round ON votes(round_id);
CREATE TABLE IF NOT EXISTS submissions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id   INTEGER NOT NULL REFERENCES rounds(id),
    choice     TEXT NOT NULL,
    ts         REAL NOT NULL,
    ok         INTEGER NOT NULL,
    detail     TEXT,
    superseded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (server TEXT NOT NULL, k TEXT NOT NULL, v TEXT, PRIMARY KEY (server, k));
"""

SCHEMA_VERSION = 2
log = logging.getLogger("votebot.db")


@dataclass
class Vote:
    id: int
    discord_id: int
    steam_id: str
    choice: str
    ts: float
    kind: str = "vote"   # "vote" (/vote) or "up" (/voteup)


class Store:
    """The bot's SQLite file, shared by every game server it serves.

    Player links (Discord <-> Steam) are shared: link once, vote on any of the servers.
    Everything else (presence, rounds, votes, rotation state) is kept per server.
    """

    def __init__(self, path: Path, first_server: str = "main"):
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        if not new:
            _backup_before_migration(path)
        self.conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit; explicit txns where needed
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate(first_server)
        if new and os.name == "posix":
            os.chmod(path, 0o600)

    def _migrate(self, first_server: str) -> None:
        c = self.conn
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        version = int(c.execute("PRAGMA user_version").fetchone()[0])
        if "rounds" in tables and version < 2:
            # Version 1 (one server): tag every existing row with the first configured server.
            log.info("upgrading the database for several servers; existing data belongs to '%s'", first_server)
            if "kind" not in {r["name"] for r in c.execute("PRAGMA table_info(votes)")}:
                c.execute("ALTER TABLE votes ADD COLUMN kind TEXT NOT NULL DEFAULT 'vote'")
            c.execute("BEGIN")
            try:
                c.execute("ALTER TABLE rounds ADD COLUMN server TEXT NOT NULL DEFAULT ''")
                c.execute("UPDATE rounds SET server=?", (first_server,))
                c.execute("ALTER TABLE presence RENAME TO presence_v1")
                c.execute("ALTER TABLE kv RENAME TO kv_v1")
                for stmt in SCHEMA.split(";"):
                    if "CREATE TABLE IF NOT EXISTS presence" in stmt or "CREATE TABLE IF NOT EXISTS kv" in stmt:
                        c.execute(stmt)
                c.execute("INSERT INTO presence(server, steam_id, name, first_seen, last_seen) "
                          "SELECT ?, steam_id, name, first_seen, last_seen FROM presence_v1", (first_server,))
                c.execute("INSERT INTO kv(server, k, v) SELECT ?, k, v FROM kv_v1", (first_server,))
                c.execute("DROP TABLE presence_v1")
                c.execute("DROP TABLE kv_v1")
                c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
        c.executescript(SCHEMA)
        if version < SCHEMA_VERSION:
            c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def server(self, server_id: str) -> "DB":
        return DB(self, server_id)

    def close(self) -> None:
        self.conn.close()


def _backup_before_migration(path: Path) -> None:
    """Copy a version-1 database aside once before it is upgraded (votebot.db.v1_backup)."""
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as ro:
            version = int(ro.execute("PRAGMA user_version").fetchone()[0])
            has_rounds = ro.execute("SELECT 1 FROM sqlite_master WHERE name='rounds'").fetchone()
        if has_rounds and version < SCHEMA_VERSION:
            dest = path.with_name(path.name + ".v1_backup")
            if not dest.exists():
                with sqlite3.connect(str(path)) as src, sqlite3.connect(str(dest)) as dst:
                    src.backup(dst)
                if os.name == "posix":
                    os.chmod(dest, 0o600)
                log.info("database backed up to %s before the upgrade", dest)
    except sqlite3.Error as e:
        log.warning("could not back up the database before upgrading (%s)", e)


class DB:
    """One server's view of the Store. Pass a Path to open a private single-server store."""

    def __init__(self, store: "Store | Path", server_id: str = "main"):
        self._owns = not isinstance(store, Store)
        self.store = Store(store, server_id) if self._owns else store
        self.conn = self.store.conn
        self.server_id = server_id

    def close(self) -> None:
        if self._owns:
            self.store.close()

    # ---- kv (per server) ----
    def get(self, k: str) -> str | None:
        r = self.conn.execute("SELECT v FROM kv WHERE server=? AND k=?", (self.server_id, k)).fetchone()
        return r["v"] if r else None

    def set(self, k: str, v: str | None) -> None:
        if v is None:
            self.conn.execute("DELETE FROM kv WHERE server=? AND k=?", (self.server_id, k))
        else:
            self.conn.execute("INSERT INTO kv(server,k,v) VALUES(?,?,?) "
                              "ON CONFLICT(server,k) DO UPDATE SET v=excluded.v", (self.server_id, k, v))

    # ---- links ----
    def link_for_discord(self, discord_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM links WHERE discord_id=?", (discord_id,)).fetchone()

    def link_for_steam(self, steam_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM links WHERE steam_id=?", (steam_id,)).fetchone()

    def add_link(self, discord_id: int, steam_id: str, name: str | None) -> None:
        self.conn.execute("INSERT INTO links(discord_id, steam_id, player_name, linked_at) VALUES(?,?,?,?)",
                          (discord_id, steam_id, name, time.time()))

    def remove_link(self, discord_id: int) -> bool:
        return self.conn.execute("DELETE FROM links WHERE discord_id=?", (discord_id,)).rowcount > 0

    def put_code(self, discord_id: int, steam_id: str, code_hash: str, expires_at: float) -> None:
        self.conn.execute(
            "INSERT INTO link_codes(discord_id, steam_id, code_hash, expires_at, attempts) VALUES(?,?,?,?,0) "
            "ON CONFLICT(discord_id) DO UPDATE SET steam_id=excluded.steam_id, code_hash=excluded.code_hash, "
            "expires_at=excluded.expires_at, attempts=0",
            (discord_id, steam_id, code_hash, expires_at))

    def get_code(self, discord_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM link_codes WHERE discord_id=?", (discord_id,)).fetchone()

    def bump_code_attempt(self, discord_id: int) -> None:
        self.conn.execute("UPDATE link_codes SET attempts=attempts+1 WHERE discord_id=?", (discord_id,))

    def delete_code(self, discord_id: int) -> None:
        self.conn.execute("DELETE FROM link_codes WHERE discord_id=?", (discord_id,))

    def purge_codes(self) -> None:
        self.conn.execute("DELETE FROM link_codes WHERE expires_at < ?", (time.time(),))

    # ---- presence ----
    def load_presence(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM presence WHERE server=?", (self.server_id,)).fetchall()

    def save_presence(self, rows: list[tuple[str, str, float, float]]) -> None:
        with self.conn:
            self.conn.execute("BEGIN")
            self.conn.execute("DELETE FROM presence WHERE server=?", (self.server_id,))
            self.conn.executemany("INSERT INTO presence(server, steam_id, name, first_seen, last_seen) "
                                  "VALUES(?,?,?,?,?)", [(self.server_id, *r) for r in rows])

    # ---- rounds ----
    def current_round(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM rounds WHERE server=? AND ended_at IS NULL ORDER BY id DESC LIMIT 1",
                                 (self.server_id,)).fetchone()

    def start_round(self, signature: str, reason: str | None = None) -> int:
        now = time.time()
        self.conn.execute("UPDATE rounds SET ended_at=?, end_reason=? WHERE server=? AND ended_at IS NULL",
                          (now, reason, self.server_id))
        cur = self.conn.execute("INSERT INTO rounds(server, started_at, signature) VALUES(?,?,?)",
                                (self.server_id, now, signature))
        return int(cur.lastrowid)

    def set_round_signature(self, round_id: int, signature: str) -> None:
        self.conn.execute("UPDATE rounds SET signature=? WHERE id=? AND server=?", (signature, round_id, self.server_id))

    # ---- votes ----
    def votes_used(self, round_id: int, discord_id: int, steam_id: str, kind: str = "vote",
                   choice: str | None = None) -> int:
        q = "SELECT COUNT(*) AS n FROM votes WHERE round_id=? AND (discord_id=? OR steam_id=?) AND kind=?"
        args: tuple = (round_id, discord_id, steam_id, kind)
        if choice is not None:
            q += " AND choice=?"
            args += (choice,)
        return int(self.conn.execute(q, args).fetchone()["n"])

    def add_vote(self, round_id: int, discord_id: int, steam_id: str, choice: str, kind: str = "vote") -> None:
        self.conn.execute("INSERT INTO votes(round_id, discord_id, steam_id, choice, ts, kind) VALUES(?,?,?,?,?,?)",
                          (round_id, discord_id, steam_id, choice, time.time(), kind))

    def round_votes(self, round_id: int) -> list[Vote]:
        rows = self.conn.execute("SELECT id, discord_id, steam_id, choice, ts, kind FROM votes WHERE round_id=? "
                                 "ORDER BY id", (round_id,)).fetchall()
        return [Vote(r["id"], r["discord_id"], r["steam_id"], r["choice"], r["ts"], r["kind"]) for r in rows]

    # ---- submissions ----
    def last_ok_submission(self, round_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM submissions WHERE round_id=? AND ok=1 AND superseded=0 "
                                 "ORDER BY id DESC LIMIT 1", (round_id,)).fetchone()

    def record_submission(self, round_id: int, choice: str, ok: bool, detail: str) -> None:
        with self.conn:
            self.conn.execute("BEGIN")
            if ok:
                self.conn.execute("UPDATE submissions SET superseded=1 WHERE round_id=? AND ok=1", (round_id,))
            self.conn.execute("INSERT INTO submissions(round_id, choice, ts, ok, detail) VALUES(?,?,?,?,?)",
                              (round_id, choice, time.time(), 1 if ok else 0, detail[:500]))
