"""Domain models for workflow orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    SAFE_STOPPED = "SAFE_STOPPED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.ROLLED_BACK,
            RunStatus.CANCELLED,
        }


class StageStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"
    CANCELLED = "CANCELLED"
    SKIPPED = "SKIPPED"

    @property
    def terminal(self) -> bool:
        return self in {
            StageStatus.SUCCEEDED,
            StageStatus.FAILED,
            StageStatus.ROLLED_BACK,
            StageStatus.CANCELLED,
            StageStatus.SKIPPED,
        }


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class FailurePolicy(StrEnum):
    FAIL_RUN = "fail_run"
    ROLLBACK = "rollback"
    CONTINUE = "continue"


@dataclass(frozen=True)
class AutoReplan:
    after_stage: str
    targets: tuple[str, ...]
    reason: str
    require_approval: bool = True


@dataclass(frozen=True)
class StageSpec:
    id: str
    name: str
    agent: str
    depends_on: tuple[str, ...] = ()
    description: str = ""
    requires_approval: bool = False
    max_retries: int = 1
    timeout_seconds: float = 30.0
    failure_policy: str = FailurePolicy.FAIL_RUN.value
    compensation: str | None = None
    policy_tags: tuple[str, ...] = ()
    entry_gate: str = "always"
    exit_gate: str = "artifact_present"
    fallback_agent: str | None = None


@dataclass(frozen=True)
class WorkflowDefinition:
    id: str
    name: str
    stages: tuple[StageSpec, ...]
    version: int = 1
    auto_replan: AutoReplan | None = None

    @property
    def stage_map(self) -> dict[str, StageSpec]:
        return {stage.id: stage for stage in self.stages}


@dataclass(frozen=True)
class ArtifactDraft:
    name: str
    kind: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DecisionDraft:
    topic: str
    decision: str
    rationale: str
    kind: str = "agent"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    summary: str
    artifacts: list[ArtifactDraft] = field(default_factory=list)
    decisions: list[DecisionDraft] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    success: bool = True
    error: str | None = None
    retryable: bool = True


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    run_id: str
    stage_id: str
    name: str
    kind: str
    version: int
    content: str
    content_hash: str
    created_at: str
    metadata: dict[str, Any]


@dataclass
class AgentContext:
    run_id: str
    stage_id: str
    scenario: str
    requirement: str
    attempt: int
    mode: str
    upstream: dict[str, list[ArtifactRecord]] = field(default_factory=dict)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    replan_reason: str | None = None
    approval: dict[str, Any] | None = None
    workspace: Any = None


class WorkflowValidationError(ValueError):
    pass
