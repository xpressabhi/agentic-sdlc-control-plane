"""Persistence and read models for the orchestration engine."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from typing import Any

from ..config import utcnow_iso
from ..db import Database
from .audit import AuditLog
from .models import (
    ArtifactDraft,
    ArtifactRecord,
    DecisionDraft,
    WorkflowDefinition,
)


def _workflow_to_json(workflow: WorkflowDefinition) -> str:
    payload = asdict(workflow)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class OrchestratorStore:
    def __init__(self, db: Database, audit: AuditLog | None = None) -> None:
        self.db = db
        self.audit = audit or AuditLog(db)

    # -- workflows ---------------------------------------------------------

    def save_workflow(self, workflow: WorkflowDefinition) -> None:
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO workflows (id, name, version, definition_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    version = excluded.version,
                    definition_json = excluded.definition_json
                """,
                (
                    workflow.id,
                    workflow.name,
                    workflow.version,
                    _workflow_to_json(workflow),
                    utcnow_iso(),
                ),
            )

    def get_workflow_json(self, workflow_id: str) -> dict[str, Any] | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "version": row["version"],
            "definition": json.loads(row["definition_json"]),
        }

    # -- runs --------------------------------------------------------------

    def create_run(
        self,
        *,
        run_id: str,
        scenario: str,
        name: str,
        workflow: WorkflowDefinition,
        mode: str,
        requirement: str,
        actor: str,
        context: dict[str, Any],
        parent_run_id: str | None = None,
    ) -> None:
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO runs
                    (id, scenario, name, workflow_id, workflow_version, status, mode,
                     requirement, created_at, actor, context_json, parent_run_id)
                VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    scenario,
                    name,
                    workflow.id,
                    workflow.version,
                    mode,
                    requirement,
                    utcnow_iso(),
                    actor,
                    json.dumps(context, sort_keys=True),
                    parent_run_id,
                ),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.db.read() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._run_row(row) for row in rows]

    @staticmethod
    def _run_row(row: Any) -> dict[str, Any]:
        data = dict(row)
        try:
            data["context"] = json.loads(data.pop("context_json") or "{}")
        except json.JSONDecodeError:
            data["context"] = {}
        data["auto_replan_done"] = bool(data["auto_replan_done"])
        return data

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "started_at",
            "ended_at",
            "error",
            "safe_stop_reason",
            "replan_count",
            "auto_replan_done",
            "context_json",
            "workflow_version",
        }
        for key in fields:
            if key not in allowed:
                raise ValueError(f"cannot update run field {key!r}")
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                (*fields.values(), run_id),
            )

    def increment_replan(self, run_id: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE runs SET replan_count = replan_count + 1 WHERE id = ?", (run_id,)
            )

    # -- stage runs --------------------------------------------------------

    def create_stage_runs(self, run_id: str, workflow: WorkflowDefinition) -> None:
        with self.db.tx() as conn:
            for stage in workflow.stages:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO stage_runs
                        (id, run_id, stage_id, name, agent, status, attempt,
                         max_retries, stale)
                    VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?, 0)
                    """,
                    (
                        str(uuid.uuid4()),
                        run_id,
                        stage.id,
                        stage.name,
                        stage.agent,
                        stage.max_retries,
                    ),
                )

    def get_stage_run(self, run_id: str, stage_id: str) -> dict[str, Any] | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM stage_runs WHERE run_id = ? AND stage_id = ?",
                (run_id, stage_id),
            ).fetchone()
        return dict(row) if row else None

    def list_stage_runs(self, run_id: str) -> list[dict[str, Any]]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT * FROM stage_runs WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def update_stage_run(self, stage_run_id: str, **fields: Any) -> None:
        allowed = {
            "status",
            "attempt",
            "total_attempts",
            "retry_count",
            "started_at",
            "ended_at",
            "first_failure_at",
            "error",
            "output_summary",
            "approval_id",
            "stale",
        }
        for key in fields:
            if key not in allowed:
                raise ValueError(f"cannot update stage field {key!r}")
        if not fields:
            return
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self.db.tx() as conn:
            conn.execute(
                f"UPDATE stage_runs SET {assignments} WHERE id = ?",
                (*fields.values(), stage_run_id),
            )

    def reset_stage_for_replan(self, run_id: str, stage_ids: set[str]) -> None:
        with self.db.tx() as conn:
            for stage_id in sorted(stage_ids):
                conn.execute(
                    """
                    UPDATE stage_runs SET
                        status = 'PENDING', attempt = 0, started_at = NULL,
                        ended_at = NULL, first_failure_at = NULL, error = NULL,
                        output_summary = NULL, approval_id = NULL, stale = 1
                    WHERE run_id = ? AND stage_id = ?
                    """,
                    (run_id, stage_id),
                )

    def cancel_stages(self, run_id: str, stage_ids: set[str], *, reason: str) -> None:
        with self.db.tx() as conn:
            for stage_id in sorted(stage_ids):
                conn.execute(
                    """
                    UPDATE stage_runs SET status = 'CANCELLED', error = ?, ended_at = ?
                    WHERE run_id = ? AND stage_id = ?
                      AND status IN ('PENDING', 'READY', 'RUNNING', 'AWAITING_APPROVAL')
                    """,
                    (reason, utcnow_iso(), run_id, stage_id),
                )

    # -- artifacts ---------------------------------------------------------

    def add_artifact(
        self,
        *,
        run_id: str,
        stage_id: str,
        stage_run_id: str | None,
        draft: ArtifactDraft,
    ) -> dict[str, Any]:
        content_hash = hashlib.sha256(draft.content.encode()).hexdigest()
        artifact_id = str(uuid.uuid4())
        with self.db.tx() as conn:
            previous = conn.execute(
                """
                SELECT id, version FROM artifacts
                WHERE run_id = ? AND stage_id = ? AND name = ?
                ORDER BY version DESC LIMIT 1
                """,
                (run_id, stage_id, draft.name),
            ).fetchone()
            version = int(previous["version"]) + 1 if previous else 1
            if previous:
                conn.execute(
                    "UPDATE artifacts SET superseded_by = ? WHERE id = ?",
                    (artifact_id, previous["id"]),
                )
            conn.execute(
                """
                INSERT INTO artifacts
                    (id, run_id, stage_run_id, stage_id, name, kind, version, content,
                     content_hash, parent_artifact_id, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    run_id,
                    stage_run_id,
                    stage_id,
                    draft.name,
                    draft.kind,
                    version,
                    draft.content,
                    content_hash,
                    previous["id"] if previous else None,
                    utcnow_iso(),
                    json.dumps(draft.metadata, sort_keys=True),
                ),
            )
        return {
            "id": artifact_id,
            "run_id": run_id,
            "stage_id": stage_id,
            "name": draft.name,
            "kind": draft.kind,
            "version": version,
            "content_hash": content_hash,
            "parent_artifact_id": previous["id"] if previous else None,
        }

    def list_artifacts(
        self, run_id: str, *, stage_id: str | None = None, latest_only: bool = True
    ) -> list[dict[str, Any]]:
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if stage_id:
            clauses.append("stage_id = ?")
            params.append(stage_id)
        if latest_only:
            clauses.append("superseded_by IS NULL")
        where = " AND ".join(clauses)
        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM artifacts WHERE {where} ORDER BY created_at, name", params
            ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            try:
                data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
            except json.JSONDecodeError:
                data["metadata"] = {}
            result.append(data)
        return result

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            data["metadata"] = {}
        return data

    def upstream_artifacts(
        self, run_id: str, dependencies: tuple[str, ...]
    ) -> dict[str, list[ArtifactRecord]]:
        result: dict[str, list[ArtifactRecord]] = {}
        for dependency in dependencies:
            records = []
            for row in self.list_artifacts(run_id, stage_id=dependency):
                records.append(
                    ArtifactRecord(
                        id=row["id"],
                        run_id=row["run_id"],
                        stage_id=row["stage_id"],
                        name=row["name"],
                        kind=row["kind"],
                        version=row["version"],
                        content=row["content"],
                        content_hash=row["content_hash"],
                        created_at=row["created_at"],
                        metadata=row["metadata"],
                    )
                )
            result[dependency] = records
        return result

    def has_test_evidence(self, run_id: str) -> bool:
        for artifact in self.list_artifacts(run_id):
            metadata = artifact.get("metadata") or {}
            if metadata.get("tests_passed") is True:
                return True
            if artifact["kind"] == "report" and "tests_passed: true" in artifact["content"]:
                return True
        return False

    # -- decisions ---------------------------------------------------------

    def add_decision(
        self,
        *,
        run_id: str,
        stage_id: str | None,
        stage_run_id: str | None,
        draft: DecisionDraft,
        actor: str,
    ) -> str:
        decision_id = str(uuid.uuid4())
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO decisions
                    (id, run_id, stage_run_id, stage_id, kind, topic, decision,
                     rationale, actor, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision_id,
                    run_id,
                    stage_run_id,
                    stage_id,
                    draft.kind,
                    draft.topic,
                    draft.decision,
                    draft.rationale,
                    actor,
                    utcnow_iso(),
                    json.dumps(draft.payload, sort_keys=True),
                ),
            )
        return decision_id

    def list_decisions(
        self, run_id: str, *, stage_id: str | None = None, kind: str | None = None
    ) -> list[dict[str, Any]]:
        clauses = ["run_id = ?"]
        params: list[Any] = [run_id]
        if stage_id:
            clauses.append("stage_id = ?")
            params.append(stage_id)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM decisions WHERE {' AND '.join(clauses)} ORDER BY created_at",
                params,
            ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            try:
                data["payload"] = json.loads(data.pop("payload_json") or "{}")
            except json.JSONDecodeError:
                data["payload"] = {}
            result.append(data)
        return result

    # -- approvals ---------------------------------------------------------

    def create_approval(
        self,
        *,
        run_id: str,
        stage_id: str,
        kind: str,
        context: dict[str, Any],
        requested_by: str,
    ) -> dict[str, Any]:
        approval_id = str(uuid.uuid4())
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO approvals
                    (id, run_id, stage_id, kind, status, requested_at,
                     requested_by, context_json)
                VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    approval_id,
                    run_id,
                    stage_id,
                    kind,
                    utcnow_iso(),
                    requested_by,
                    json.dumps(context, sort_keys=True),
                ),
            )
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return self._approval_row(row) if row else None

    @staticmethod
    def _approval_row(row: Any) -> dict[str, Any]:
        data = dict(row)
        try:
            data["context"] = json.loads(data.pop("context_json") or "{}")
        except json.JSONDecodeError:
            data["context"] = {}
        return data

    def find_approval(self, run_id: str, stage_id: str, kind: str = "stage") -> dict[str, Any] | None:
        with self.db.read() as conn:
            row = conn.execute(
                """
                SELECT * FROM approvals
                WHERE run_id = ? AND stage_id = ? AND kind = ?
                ORDER BY requested_at DESC LIMIT 1
                """,
                (run_id, stage_id, kind),
            ).fetchone()
        return self._approval_row(row) if row else None

    def list_approvals(
        self, *, status: str | None = None, run_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.read() as conn:
            rows = conn.execute(
                f"SELECT * FROM approvals {where} ORDER BY requested_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [self._approval_row(row) for row in rows]

    def decide_approval(
        self, approval_id: str, *, status: str, decided_by: str, rationale: str
    ) -> dict[str, Any]:
        with self.db.tx() as conn:
            conn.execute(
                """
                UPDATE approvals
                SET status = ?, decided_by = ?, rationale = ?, decided_at = ?
                WHERE id = ? AND status = 'PENDING'
                """,
                (status, decided_by, rationale, utcnow_iso(), approval_id),
            )
        approval = self.get_approval(approval_id)
        if not approval:
            raise ValueError("approval not found")
        return approval

    # -- audit -------------------------------------------------------------

    def record_event(
        self,
        *,
        run_id: str,
        event_type: str,
        actor: str,
        payload: dict[str, Any] | None = None,
        stage_id: str | None = None,
    ) -> dict[str, Any]:
        return self.audit.append(
            run_id=run_id,
            event_type=event_type,
            actor=actor,
            payload=payload,
            stage_id=stage_id,
        )

    def list_audit(
        self, *, run_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        return self.audit.list(run_id=run_id, limit=limit)

    def verify_audit(self, *, run_id: str | None = None) -> dict[str, Any]:
        return self.audit.verify(run_id=run_id)
