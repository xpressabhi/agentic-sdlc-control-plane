"""SQLite persistence.

One schema, one connection factory. Connections are short-lived and every write
uses ``BEGIN IMMEDIATE`` so concurrent stage workers serialize cleanly under WAL.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE COLLATE NOCASE,
    target_url      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    expires_at      TEXT,
    deleted_at      TEXT,
    created_by      TEXT,
    idempotency_key TEXT UNIQUE,
    click_count     INTEGER NOT NULL DEFAULT 0,
    metadata_json   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS click_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    link_id      INTEGER NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    code         TEXT NOT NULL,
    clicked_at   TEXT NOT NULL,
    referrer     TEXT,
    user_agent   TEXT,
    visitor_hash TEXT,
    country      TEXT
);
CREATE INDEX IF NOT EXISTS idx_click_link_time ON click_events(link_id, clicked_at);
CREATE INDEX IF NOT EXISTS idx_click_code_time ON click_events(code, clicked_at);

CREATE TABLE IF NOT EXISTS workflows (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    version         INTEGER NOT NULL,
    definition_json TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id                  TEXT PRIMARY KEY,
    scenario            TEXT NOT NULL,
    name                TEXT NOT NULL,
    workflow_id         TEXT NOT NULL,
    workflow_version    INTEGER NOT NULL,
    status              TEXT NOT NULL,
    mode                TEXT NOT NULL,
    requirement         TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    ended_at            TEXT,
    actor               TEXT,
    error               TEXT,
    safe_stop_reason    TEXT,
    replan_count        INTEGER NOT NULL DEFAULT 0,
    auto_replan_done    INTEGER NOT NULL DEFAULT 0,
    context_json        TEXT NOT NULL DEFAULT '{}',
    parent_run_id       TEXT
);

CREATE TABLE IF NOT EXISTS stage_runs (
    id               TEXT PRIMARY KEY,
    run_id           TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stage_id         TEXT NOT NULL,
    name             TEXT NOT NULL,
    agent            TEXT NOT NULL,
    status           TEXT NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 0,
    total_attempts   INTEGER NOT NULL DEFAULT 0,
    retry_count      INTEGER NOT NULL DEFAULT 0,
    max_retries      INTEGER NOT NULL DEFAULT 1,
    started_at       TEXT,
    ended_at         TEXT,
    first_failure_at TEXT,
    error            TEXT,
    output_summary   TEXT,
    approval_id      TEXT,
    stale            INTEGER NOT NULL DEFAULT 0,
    UNIQUE(run_id, stage_id)
);
CREATE INDEX IF NOT EXISTS idx_stage_run ON stage_runs(run_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id                 TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stage_run_id       TEXT,
    stage_id           TEXT NOT NULL,
    name               TEXT NOT NULL,
    kind               TEXT NOT NULL,
    version            INTEGER NOT NULL DEFAULT 1,
    content            TEXT NOT NULL,
    content_hash       TEXT NOT NULL,
    parent_artifact_id TEXT,
    created_at         TEXT NOT NULL,
    superseded_by      TEXT,
    metadata_json      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_artifact_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_artifact_stage ON artifacts(run_id, stage_id, name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_version
    ON artifacts(run_id, stage_id, name, version);

CREATE TABLE IF NOT EXISTS decisions (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stage_run_id TEXT,
    stage_id     TEXT,
    kind         TEXT NOT NULL,
    topic        TEXT NOT NULL,
    decision     TEXT NOT NULL,
    rationale    TEXT NOT NULL,
    actor        TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    supersedes   TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision_run ON decisions(run_id);

CREATE TABLE IF NOT EXISTS approvals (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stage_id     TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'stage',
    status       TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    decided_at   TEXT,
    requested_by TEXT,
    decided_by   TEXT,
    rationale    TEXT,
    context_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_approval_run ON approvals(run_id);
CREATE INDEX IF NOT EXISTS idx_approval_status ON approvals(status);

CREATE TABLE IF NOT EXISTS audit_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    stage_id    TEXT,
    event_type  TEXT NOT NULL,
    actor       TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_run ON audit_events(run_id, seq);

CREATE TABLE IF NOT EXISTS limiter_state (
    bucket TEXT PRIMARY KEY,
    tokens REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._schema_lock:
            conn = self.connect()
            try:
                conn.executescript(SCHEMA)
                self._migrate(conn)
            finally:
                conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Small forward-only migrations for prototype databases."""
        columns = {row[1] for row in conn.execute("PRAGMA table_info(stage_runs)")}
        if "total_attempts" not in columns:
            conn.execute(
                "ALTER TABLE stage_runs ADD COLUMN total_attempts INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute("UPDATE stage_runs SET total_attempts = attempt")
        if "retry_count" not in columns:
            conn.execute(
                "ALTER TABLE stage_runs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute("UPDATE stage_runs SET retry_count = MAX(attempt - 1, 0)")

    def reset(self) -> None:
        """Drop all data. Used only by tests and ``asdlc init --reset``."""
        with self._schema_lock:
            conn = self.connect()
            try:
                tables = [
                    "click_events",
                    "links",
                    "artifacts",
                    "decisions",
                    "approvals",
                    "audit_events",
                    "stage_runs",
                    "runs",
                    "workflows",
                    "limiter_state",
                ]
                for table in tables:
                    conn.execute(f"DELETE FROM {table}")
                conn.execute("DELETE FROM sqlite_sequence")
            finally:
                conn.close()
