"""SQLite event store: scrubbed events, per-source cursors, render bookkeeping
and the annotations read back from edited drafts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS commands (
    key      TEXT PRIMARY KEY,           -- salted hash of the raw entry
    source   TEXT NOT NULL,              -- history file path
    shell    TEXT NOT NULL,
    ts       INTEGER NOT NULL,           -- epoch seconds
    ts_exact INTEGER NOT NULL,           -- 0 when the shell stored no timestamp (ts = ingest time)
    text     TEXT NOT NULL,              -- scrubbed
    heads    TEXT NOT NULL               -- json [[argv0, arg, …], …]: one entry per simple command
);
CREATE INDEX IF NOT EXISTS commands_ts ON commands (ts);

CREATE TABLE IF NOT EXISTS repos (
    path TEXT PRIMARY KEY, slug TEXT NOT NULL, name TEXT NOT NULL,
    remote TEXT, branch TEXT, seen INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS commits (
    repo TEXT NOT NULL, sha TEXT NOT NULL, ts INTEGER NOT NULL, author TEXT, email TEXT,
    subject TEXT, files TEXT NOT NULL, ins INTEGER NOT NULL, del INTEGER NOT NULL,
    PRIMARY KEY (repo, sha)
);
CREATE INDEX IF NOT EXISTS commits_ts ON commits (ts);

CREATE TABLE IF NOT EXISTS log_cursor (path TEXT PRIMARY KEY, inode INTEGER, offset INTEGER, last_ts INTEGER);
CREATE TABLE IF NOT EXISTS log_agg (
    source TEXT NOT NULL, day TEXT NOT NULL, level TEXT NOT NULL, sig TEXT NOT NULL,
    count INTEGER NOT NULL, sample TEXT, first_ts INTEGER, last_ts INTEGER,
    PRIMARY KEY (source, day, level, sig)
);

CREATE TABLE IF NOT EXISTS scripts (
    path TEXT PRIMARY KEY, root TEXT NOT NULL, rel TEXT NOT NULL, sha TEXT NOT NULL,
    mtime REAL NOT NULL, facts TEXT NOT NULL, seen INTEGER NOT NULL
);

-- one row per node ever rendered
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, hash TEXT NOT NULL, rev INTEGER NOT NULL,
    created TEXT NOT NULL, updated TEXT NOT NULL,
    machine TEXT NOT NULL,               -- json: tags/summary/status last written by the machine
    stale INTEGER NOT NULL DEFAULT 0,
    file_sig TEXT                        -- "mtime_ns:size" of the draft as last written or read back
);

-- human edits read back from drafts (the closing half of the loop)
CREATE TABLE IF NOT EXISTS annotations (id TEXT PRIMARY KEY, data TEXT NOT NULL, seen TEXT NOT NULL);
"""


class Store:
    def __init__(self, state_dir: Path) -> None:
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "cll.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(_DDL)

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- meta ---------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value)
        )

    # -- annotations --------------------------------------------------------
    def annotations(self) -> dict[str, dict[str, Any]]:
        return {r["id"]: json.loads(r["data"]) for r in self.db.execute("SELECT id, data FROM annotations")}

    def put_annotation(self, node_id: str, data: dict[str, Any], seen: str) -> None:
        self.db.execute(
            "INSERT INTO annotations VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data, seen=excluded.seen",
            (node_id, json.dumps(data, sort_keys=True), seen),
        )

    def drop_annotation(self, node_id: str) -> None:
        self.db.execute("DELETE FROM annotations WHERE id=?", (node_id,))

    # -- nodes --------------------------------------------------------------
    def node_rows(self) -> dict[str, sqlite3.Row]:
        return {r["id"]: r for r in self.db.execute("SELECT * FROM nodes")}

    def put_node(
        self,
        node_id: str,
        digest: str,
        rev: int,
        created: str,
        updated: str,
        machine: dict[str, Any],
        stale: int = 0,
        file_sig: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "hash=excluded.hash, rev=excluded.rev, updated=excluded.updated, "
            "machine=excluded.machine, stale=excluded.stale, file_sig=excluded.file_sig",
            (node_id, digest, rev, created, updated, json.dumps(machine, sort_keys=True), stale, file_sig),
        )

    def set_file_sig(self, node_id: str, file_sig: str) -> None:
        self.db.execute("UPDATE nodes SET file_sig=? WHERE id=?", (file_sig, node_id))
