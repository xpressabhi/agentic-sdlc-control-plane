"""Append-only hash-chained audit log.

Each event commits to the previous event's digest, so altering or removing any
historical record invalidates every later digest. Verification recomputes the
chain from the persisted payloads.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Any

from ..config import utcnow_iso
from ..db import Database

GENESIS_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(prev_hash: str, body: dict[str, Any]) -> str:
    return hashlib.sha256(f"{prev_hash}|{canonical_json(body)}".encode()).hexdigest()


class AuditLog:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._lock = threading.Lock()

    def append(
        self,
        *,
        run_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
        stage_id: str | None = None,
    ) -> dict[str, Any]:
        created_at = utcnow_iso()
        body = {
            "run_id": run_id,
            "stage_id": stage_id,
            "event_type": event_type,
            "actor": actor,
            "payload": payload or {},
            "created_at": created_at,
        }
        with self._lock, self.db.tx() as conn:
            row = conn.execute(
                "SELECT hash FROM audit_events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = row["hash"] if row else GENESIS_HASH
            digest = _digest(prev_hash, body)
            cursor = conn.execute(
                """
                INSERT INTO audit_events
                    (run_id, stage_id, event_type, actor, payload_json,
                     created_at, prev_hash, hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    stage_id,
                    event_type,
                    actor,
                    canonical_json(payload or {}),
                    created_at,
                    prev_hash,
                    digest,
                ),
            )
            seq = int(cursor.lastrowid or 0)
        return {"seq": seq, "hash": digest, "prev_hash": prev_hash, **body}

    def list(
        self, *, run_id: str | None = None, limit: int = 500, after_seq: int = 0
    ) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            if run_id:
                rows = conn.execute(
                    """
                    SELECT * FROM audit_events
                    WHERE run_id = ? AND seq > ? ORDER BY seq LIMIT ?
                    """,
                    (run_id, after_seq, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_events WHERE seq > ? ORDER BY seq LIMIT ?",
                    (after_seq, limit),
                ).fetchall()
        events = []
        for row in rows:
            events.append(
                {
                    "seq": row["seq"],
                    "run_id": row["run_id"],
                    "stage_id": row["stage_id"],
                    "event_type": row["event_type"],
                    "actor": row["actor"],
                    "payload": json.loads(row["payload_json"]),
                    "created_at": row["created_at"],
                    "prev_hash": row["prev_hash"],
                    "hash": row["hash"],
                }
            )
        return events

    def verify(self, *, run_id: str | None = None) -> dict[str, Any]:
        """Recompute the chain. Returns a structured verification result."""
        with self.db.read() as conn:
            # The chain is global across runs; verifying a run must still walk the
            # whole chain because events from concurrent runs are interleaved.
            rows = conn.execute("SELECT * FROM audit_events ORDER BY seq").fetchall()
            if run_id and not any(row["run_id"] == run_id for row in rows):
                return {
                    "valid": False,
                    "checked": 0,
                    "first_bad_seq": None,
                    "message": f"no audit events recorded for run {run_id}",
                }
        expected_prev = GENESIS_HASH
        checked = 0
        for row in rows:
            body = {
                "run_id": row["run_id"],
                "stage_id": row["stage_id"],
                "event_type": row["event_type"],
                "actor": row["actor"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            if expected_prev is not None and row["prev_hash"] != expected_prev:
                return {
                    "valid": False,
                    "checked": checked,
                    "first_bad_seq": row["seq"],
                    "message": "previous-hash link mismatch",
                }
            if _digest(row["prev_hash"], body) != row["hash"]:
                return {
                    "valid": False,
                    "checked": checked,
                    "first_bad_seq": row["seq"],
                    "message": "digest mismatch (record was altered)",
                }
            expected_prev = row["hash"]
            checked += 1
        return {
            "valid": True,
            "checked": checked,
            "first_bad_seq": None,
            "message": "audit chain verified",
        }
