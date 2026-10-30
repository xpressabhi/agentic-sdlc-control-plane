from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from agentic_sdlc.config import Settings
from agentic_sdlc.db import Database
from agentic_sdlc.errors import StageExecutionError
from agentic_sdlc.orchestrator.agents.base import AgentRegistry
from agentic_sdlc.orchestrator.engine import OrchestrationEngine
from agentic_sdlc.orchestrator.models import (
    AgentContext,
    AgentResult,
    ArtifactDraft,
    StageSpec,
    WorkflowDefinition,
)
from agentic_sdlc.orchestrator.scenarios import ScenarioRegistry
from agentic_sdlc.orchestrator.store import OrchestratorStore


class FunctionAgent:
    def __init__(self, name: str, fn: Callable[[AgentContext], AgentResult]) -> None:
        self.name = name
        self.fn = fn
        self.calls: list[int] = []
        self.compensated: list[str] = []

    def execute(self, context: AgentContext) -> AgentResult:
        self.calls.append(context.attempt)
        return self.fn(context)

    def compensate(self, context: AgentContext) -> None:
        self.compensated.append(context.stage_id)


def artifact_result(name: str = "a.md", **metadata) -> AgentResult:
    return AgentResult(
        summary=f"{name} produced",
        artifacts=[ArtifactDraft(name, "markdown", f"# {name}\n")],
        metadata=metadata,
    )


def engine_for(tmp_path: Path, **settings_overrides) -> tuple[OrchestrationEngine, OrchestratorStore, AgentRegistry]:
    defaults = dict(
        db_path=tmp_path / "engine.db",
        data_dir=tmp_path / "data",
        api_key="test",
        max_parallel=4,
        max_wall_seconds=60,
        retry_base_delay=0.001,
        retry_max_delay=0.005,
        environment="test",
    )
    defaults.update(settings_overrides)
    settings = Settings(**defaults)
    db = Database(settings.db_path)
    store = OrchestratorStore(db)
    registry = AgentRegistry()
    engine = OrchestrationEngine(
        store, settings, registry=registry, scenarios=ScenarioRegistry.load()
    )
    return engine, store, registry


def static(name: str, filename: str | None = None, **metadata) -> FunctionAgent:
    return FunctionAgent(
        name,
        lambda context: artifact_result(filename or f"{name}.md", **metadata),
    )


def workflow(*stages: StageSpec, auto_replan=None) -> WorkflowDefinition:
    return WorkflowDefinition(
        id="wf-test",
        name="Test Workflow",
        stages=tuple(stages),
        auto_replan=auto_replan,
    )


def test_linear_run_succeeds_and_is_auditable(tmp_path: Path) -> None:
    engine, store, registry = engine_for(tmp_path)
    registry.register("one", static("one"))
    registry.register("two", static("two"))
    snapshot = engine.create_run(
        workflow(
            StageSpec(id="one", name="One", agent="one"),
            StageSpec(id="two", name="Two", agent="two", depends_on=("one",)),
        ),
        requirement="test",
    )
    assert snapshot["run"]["status"] == "SUCCEEDED"
    assert [stage["status"] for stage in snapshot["stages"]] == ["SUCCEEDED", "SUCCEEDED"]
    assert snapshot["audit_verification"]["valid"] is True
    assert len(snapshot["artifacts"]) == 2


def test_parallel_branches_and_join(tmp_path: Path) -> None:
    engine, store, registry = engine_for(tmp_path)
    lock = threading.Lock()
    active = {"count": 0, "peak": 0}

    def slow(context: AgentContext) -> AgentResult:
        with lock:
            active["count"] += 1
            active["peak"] = max(active["peak"], active["count"])
        time.sleep(0.06)
        with lock:
            active["count"] -= 1
        return artifact_result(f"{context.stage_id}.md")

    for name in ("a", "b"):
        registry.register(name, FunctionAgent(name, slow))
    registry.register("join", static("join"))
    snapshot = engine.create_run(
        workflow(
            StageSpec(id="a", name="A", agent="a"),
            StageSpec(id="b", name="B", agent="b"),
            StageSpec(id="join", name="Join", agent="join", depends_on=("a", "b")),
        ),
        requirement="test",
    )
    assert snapshot["run"]["status"] == "SUCCEEDED"
    assert active["peak"] == 2
    assert snapshot["levels"] == [["a", "b"], ["join"]]


def test_bounded_retry_then_success(tmp_path: Path) -> None:
    engine, store, registry = engine_for(tmp_path)

    def flaky(context: AgentContext) -> AgentResult:
        if context.attempt == 1:
            raise StageExecutionError("transient failure", retryable=True)
        return artifact_result("ok.md")

    registry.register("flaky", FunctionAgent("flaky", flaky))
    snapshot = engine.create_run(
        workflow(StageSpec(id="flaky", name="Flaky", agent="flaky", max_retries=2)),
        requirement="test",
    )
    assert snapshot["run"]["status"] == "SUCCEEDED"
    stage = snapshot["stages"][0]
    assert stage["attempt"] == 2
    events = [event["event_type"] for event in snapshot["audit"]]
    assert "stage.attempt_failed" in events
    assert "stage.retry_scheduled" in events
    assert snapshot["metrics"]["stages"]["retries"] == 1


def test_policy_denial_consumes_retry_and_remediates(tmp_path: Path) -> None:
    engine, store, registry = engine_for(tmp_path)

    def leaky(context: AgentContext) -> AgentResult:
        content = (
            'AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"\n'
            if context.attempt == 1
            else "def healthy():\n    return True\n"
        )
        return AgentResult(
            summary="code",
            artifacts=[ArtifactDraft("module.py", "code", content)],
        )

    registry.register("leaky", FunctionAgent("leaky", leaky))
    snapshot = engine.create_run(
        workflow(
            StageSpec(
                id="code", name="Code", agent="leaky", max_retries=1, policy_tags=("code",)
            )
        ),
        requirement="test",
    )
    assert snapshot["run"]["status"] == "SUCCEEDED"
    assert snapshot["stages"][0]["attempt"] == 2
    events = [event["event_type"] for event in snapshot["audit"]]
    assert "policy.violation" in events
    denied = [d for d in snapshot["decisions"] if d["decision"] == "deny"]
    assert denied and denied[0]["topic"] == "SEC-001"


def test_approval_pause_and_resume(tmp_path: Path) -> None:
    engine, store, registry = engine_for(tmp_path)
    registry.register("a", static("a"))
    snapshot = engine.create_run(
        workflow(
            StageSpec(id="a", name="A", agent="a"),
            StageSpec(
                id="gate", name="Gate", agent="a", depends_on=("a",), requires_approval=True
            ),
        ),
        requirement="test",
    )
    assert snapshot["run"]["status"] == "AWAITING_APPROVAL"
    assert snapshot["stages"][1]["status"] == "AWAITING_APPROVAL"
    approval = snapshot["approvals"][0]
    assert approval["status"] == "PENDING"
    resumed = engine.approve(snapshot["run"]["id"], rationale="looks good")
    assert resumed["run"]["status"] == "SUCCEEDED"
    approved = [a for a in resumed["approvals"] if a["id"] == approval["id"]][0]
    assert approved["status"] == "APPROVED"
    assert approved["decided_by"] == "reviewer"


def test_approval_rejection_fails_run(tmp_path: Path) -> None:
    engine, store, registry = engine_for(tmp_path)
    registry.register("a", static("a"))
    snapshot = engine.create_run(
        workflow(
            StageSpec(id="a", name="A", agent="a"),
            StageSpec(
                id="gate", name="Gate", agent="a", depends_on=("a",), requires_approval=True
            ),
        ),
        requirement="test",
    )
    rejected = engine.reject(snapshot["run"]["id"], rationale="missing evidence")
    assert rejected["run"]["status"] == "FAILED"
    assert rejected["stages"][1]["status"] == "CANCELLED"
    events = [event["event_type"] for event in rejected["audit"]]
    assert "approval.rejected" in events and "run.failed" in events


def test_rollback_compensates_succeeded_stages(tmp_path: Path) -> None:
    engine, store, registry = engine_for(tmp_path)
    first = static("first")
    registry.register("first", first)

    def explode(context: AgentContext) -> AgentResult:
        raise StageExecutionError("cannot deploy", retryable=False)

    registry.register("explode", FunctionAgent("explode", explode))
    snapshot = engine.create_run(
        workflow(
            StageSpec(
                id="first",
                name="First",
                agent="first",
                compensation="undo",
            ),
            StageSpec(
                id="second",
                name="Second",
                agent="explode",
                depends_on=("first",),
                failure_policy="rollback",
                max_retries=0,
            ),
        ),
        requirement="test",
    )
    assert snapshot["run"]["status"] == "ROLLED_BACK"
    statuses = {stage["stage_id"]: stage["status"] for stage in snapshot["stages"]}
    assert statuses["first"] == "ROLLED_BACK"
    assert first.compensated == ["first"]
    events = [event["event_type"] for event in snapshot["audit"]]
    assert "rollback.started" in events and "rollback.completed" in events


def test_safe_stop_and_resume(tmp_path: Path) -> None:
    engine, store, registry = engine_for(tmp_path)
    registry.register("a", static("a"))
    snapshot = engine.create_run(
        workflow(
            StageSpec(id="a", name="A", agent="a"),
            StageSpec(
                id="gate", name="Gate", agent="a", depends_on=("a",), requires_approval=True
            ),
        ),
        requirement="test",
    )
    run_id = snapshot["run"]["id"]
    stopped = engine.safe_stop(run_id, reason="operator pause")
    assert stopped["run"]["status"] == "SAFE_STOPPED"
    assert stopped["run"]["safe_stop_reason"] == "operator pause"
    resumed = engine.resume(run_id)
    assert resumed["run"]["status"] == "AWAITING_APPROVAL"
    approved = engine.approve(run_id)
    assert approved["run"]["status"] == "SUCCEEDED"
    events = [event["event_type"] for event in approved["audit"]]
    assert "run.safe_stopped" in events and "run.resumed" in events


def test_replan_reruns_downstream_with_lineage(tmp_path: Path) -> None:
    engine, store, registry = engine_for(tmp_path)
    for name in ("a", "b", "c"):
        registry.register(name, static(name))
    snapshot = engine.create_run(
        workflow(
            StageSpec(id="a", name="A", agent="a"),
            StageSpec(id="b", name="B", agent="b", depends_on=("a",)),
            StageSpec(id="c", name="C", agent="c", depends_on=("b",)),
        ),
        requirement="test",
    )
    run_id = snapshot["run"]["id"]
    assert snapshot["run"]["status"] == "SUCCEEDED"
    replanned = engine.request_replan(
        run_id,
        changed_stage="a",
        reason="requirement changed",
        require_approval=False,
    )
    assert replanned["run"]["status"] == "SUCCEEDED"
    assert replanned["run"]["replan_count"] == 1
    versions = {artifact["name"]: artifact["version"] for artifact in replanned["artifacts"]}
    assert versions["b.md"] == 2
    assert versions["c.md"] == 2
    events = [event["event_type"] for event in replanned["audit"]]
    assert "replan.applied" in events
    assert any(decision["kind"] == "replan" for decision in replanned["decisions"])


def test_replan_with_approval(tmp_path: Path) -> None:
    engine, store, registry = engine_for(tmp_path)
    for name in ("a", "b"):
        registry.register(name, static(name))
    snapshot = engine.create_run(
        workflow(
            StageSpec(id="a", name="A", agent="a"),
            StageSpec(id="b", name="B", agent="b", depends_on=("a",)),
        ),
        requirement="test",
    )
    run_id = snapshot["run"]["id"]
    requested = engine.request_replan(
        run_id,
        changed_stage="a",
        reason="scope change",
        require_approval=True,
    )
    assert requested["run"]["status"] == "AWAITING_APPROVAL"
    assert requested["approvals"][0]["kind"] == "replan"
    approved = engine.approve(run_id, rationale="accept scope")
    assert approved["run"]["status"] == "SUCCEEDED"
    assert approved["run"]["replan_count"] == 1


def test_wall_clock_budget_safe_stops(tmp_path: Path) -> None:
    engine, store, registry = engine_for(tmp_path, max_wall_seconds=0.0)
    registry.register("a", static("a"))
    snapshot = engine.create_run(
        workflow(StageSpec(id="a", name="A", agent="a")),
        requirement="test",
    )
    assert snapshot["run"]["status"] == "SAFE_STOPPED"
    assert snapshot["run"]["safe_stop_reason"] == "wall_clock_budget_exceeded"


def test_unknown_agent_fails_without_hanging(tmp_path: Path) -> None:
    engine, store, registry = engine_for(tmp_path)
    snapshot = engine.create_run(
        workflow(StageSpec(id="a", name="A", agent="missing-agent", max_retries=0)),
        requirement="test",
    )
    assert snapshot["run"]["status"] == "FAILED"
    assert "no agent registered" in (snapshot["stages"][0]["error"] or "")
