"""The orchestration engine.

The engine owns the run loop: it selects ready stages from the dependency graph,
executes independent stages in parallel, enforces gates and policy, pauses for
human approvals, retries with bounded backoff, compensates on rollback, stops
safely on budget/operator conditions, and re-plans downstream stages when an
upstream decision changes.
"""

from __future__ import annotations

import json
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings, utcnow_iso
from ..errors import Conflict, NotFound, StageExecutionError
from .agents.base import Agent, AgentRegistry
from .agents.reference import build_reference_registry
from .gates import check_entry_gate, check_exit_gate
from .graph import downstream_closure, reverse_topological, topological_levels
from .metrics import MetricsService
from .models import (
    AgentContext,
    AgentResult,
    DecisionDraft,
    RunStatus,
    StageSpec,
    StageStatus,
    WorkflowDefinition,
)
from .policy import PolicyDecision, PolicyEngine, PolicyInput
from .scenarios import ScenarioRegistry, workflow_from_dict
from .store import OrchestratorStore

REPLAN_STAGE = "__replan__"

TERMINAL_RUN_STATUSES = {
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.ROLLED_BACK.value,
    RunStatus.CANCELLED.value,
}
TERMINAL_STAGE_STATUSES = {
    StageStatus.SUCCEEDED.value,
    StageStatus.FAILED.value,
    StageStatus.ROLLED_BACK.value,
    StageStatus.CANCELLED.value,
    StageStatus.SKIPPED.value,
}


@dataclass
class StageOutcome:
    stage_id: str
    status: str
    error: str | None = None


class OrchestrationEngine:
    def __init__(
        self,
        store: OrchestratorStore,
        settings: Settings,
        *,
        registry: AgentRegistry | None = None,
        policy: PolicyEngine | None = None,
        scenarios: ScenarioRegistry | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        self.registry = registry or self._default_registry()
        self.policy = policy or PolicyEngine()
        self.scenarios = scenarios or ScenarioRegistry.load()
        self.metrics = MetricsService(store.db)
        self._run_locks: dict[str, Any] = {}
        self._lock_guard = __import__("threading").Lock()

    @staticmethod
    def _default_registry() -> AgentRegistry:
        registry = AgentRegistry()
        for name, agent in build_reference_registry().items():
            registry.register(name, agent)
        return registry

    # -- launch ------------------------------------------------------------

    def launch(
        self,
        scenario_key: str,
        *,
        actor: str = "operator",
        requirement_override: str | None = None,
        auto_approve: bool = False,
    ) -> dict[str, Any]:
        try:
            scenario = self.scenarios.get(scenario_key)
        except KeyError as exc:
            raise NotFound(f"unknown scenario {scenario_key!r}", code="scenario_not_found") from exc
        return self.create_run(
            scenario.workflow,
            requirement=requirement_override or scenario.requirement,
            scenario=scenario.key,
            name=scenario.name,
            actor=actor,
            environment=scenario.environment,
            context={"intent": scenario.intent, "tags": list(scenario.tags)},
            auto_approve=auto_approve,
        )

    def create_run(
        self,
        workflow: WorkflowDefinition,
        *,
        requirement: str,
        scenario: str = "custom",
        name: str | None = None,
        actor: str = "operator",
        environment: str = "development",
        context: dict[str, Any] | None = None,
        auto_approve: bool = False,
    ) -> dict[str, Any]:
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        mode = self.settings.agent_mode
        self.store.save_workflow(workflow)
        self.store.create_run(
            run_id=run_id,
            scenario=scenario,
            name=name or workflow.name,
            workflow=workflow,
            mode=mode,
            requirement=requirement,
            actor=actor,
            context={
                "environment": environment,
                **(context or {}),
            },
        )
        self.store.create_stage_runs(run_id, workflow)
        self.store.record_event(
            run_id=run_id,
            event_type="run.created",
            actor=actor,
            payload={
                "scenario": scenario,
                "workflow": workflow.id,
                "version": workflow.version,
                "mode": mode,
                "environment": environment,
            },
        )
        self.settings.runs_dir.mkdir(parents=True, exist_ok=True)
        (self.settings.runs_dir / run_id).mkdir(parents=True, exist_ok=True)
        self.execute(run_id, actor=actor)
        if auto_approve:
            self._auto_approve(run_id, actor="demo-reviewer")
        return self.snapshot(run_id)

    def _auto_approve(self, run_id: str, *, actor: str) -> None:
        for _ in range(60):
            run = self.store.get_run(run_id)
            if not run or run["status"] != RunStatus.AWAITING_APPROVAL.value:
                return
            pending = self.store.list_approvals(
                status="PENDING", run_id=run_id, limit=20
            )
            if not pending:
                return
            for approval in pending:
                self.store.decide_approval(
                    approval["id"],
                    status="APPROVED",
                    decided_by=actor,
                    rationale="Auto-approved for deterministic demo/test execution",
                )
                self.store.record_event(
                    run_id=run_id,
                    event_type="approval.approved",
                    actor=actor,
                    stage_id=approval["stage_id"],
                    payload={"approval_id": approval["id"], "automatic": True},
                )
            self.execute(run_id, actor=actor)

    # -- execution loop ----------------------------------------------------

    def execute(self, run_id: str, *, actor: str = "engine") -> dict[str, Any]:
        lock = self._lock_for(run_id)
        with lock:
            run = self._require_run(run_id)
            if run["status"] in TERMINAL_RUN_STATUSES:
                return self.snapshot(run_id)
            if run["status"] == RunStatus.SAFE_STOPPED.value:
                return self.snapshot(run_id)
            self.store.update_run(
                run_id,
                status=RunStatus.RUNNING.value,
                started_at=run["started_at"] or utcnow_iso(),
                ended_at=None,
            )
            workflow = self._workflow_for_run(run)
            started_monotonic = time.monotonic()
            while True:
                run = self._require_run(run_id)
                if run["status"] in TERMINAL_RUN_STATUSES:
                    return self.snapshot(run_id)
                if run["status"] == RunStatus.SAFE_STOPPED.value:
                    return self.snapshot(run_id)
                if time.monotonic() - started_monotonic > self.settings.max_wall_seconds:
                    self._safe_stop(run_id, "wall_clock_budget_exceeded", actor)
                    return self.snapshot(run_id)
                if self._total_attempts(run_id) > self.settings.max_stage_attempts:
                    self._safe_stop(run_id, "stage_attempt_budget_exceeded", actor)
                    return self.snapshot(run_id)

                # 1. Governed re-plan awaiting a decision.
                pending_replan = run.get("context", {}).get("pending_replan")
                if pending_replan:
                    approval = self.store.find_approval(run_id, REPLAN_STAGE, "replan")
                    if approval is None:
                        self.store.update_run(
                            run_id, context_json=self._context_without(run, "pending_replan")
                        )
                    elif approval["status"] == "PENDING":
                        self.store.update_run(run_id, status=RunStatus.AWAITING_APPROVAL.value)
                        return self.snapshot(run_id)
                    elif approval["status"] == "REJECTED":
                        self._safe_stop(run_id, "replan_rejected", actor)
                        return self.snapshot(run_id)
                    else:
                        self._apply_replan(
                            run_id,
                            workflow,
                            targets=set(pending_replan["targets"]),
                            reason=pending_replan["reason"],
                            changed_stage=pending_replan.get("changed_stage"),
                            actor=actor,
                            source=pending_replan.get("source", "manual"),
                        )
                        continue

                # 2. Stage approval requests.
                stages = self.store.list_stage_runs(run_id)
                awaiting = [
                    stage
                    for stage in stages
                    if stage["status"] == StageStatus.AWAITING_APPROVAL.value
                ]
                if awaiting:
                    made_progress = False
                    for stage in awaiting:
                        approval = self.store.find_approval(run_id, stage["stage_id"])
                        if approval and approval["status"] == "APPROVED":
                            self.store.update_stage_run(
                                stage["id"], status=StageStatus.READY.value
                            )
                            made_progress = True
                        elif approval and approval["status"] == "REJECTED":
                            self.store.update_stage_run(
                                stage["id"],
                                status=StageStatus.CANCELLED.value,
                                error="approval rejected",
                                ended_at=utcnow_iso(),
                            )
                            self.store.record_event(
                                run_id=run_id,
                                event_type="stage.cancelled",
                                actor=actor,
                                stage_id=stage["stage_id"],
                                payload={"reason": "approval rejected"},
                            )
                            self.store.cancel_stages(
                                run_id,
                                {
                                    other["stage_id"]
                                    for other in stages
                                    if other["status"]
                                    in {
                                        StageStatus.PENDING.value,
                                        StageStatus.READY.value,
                                    }
                                },
                                reason="run stopped after approval rejection",
                            )
                            self.store.update_run(
                                run_id,
                                status=RunStatus.FAILED.value,
                                error=f"approval rejected for stage {stage['stage_id']}",
                                ended_at=utcnow_iso(),
                            )
                            self.store.record_event(
                                run_id=run_id,
                                event_type="run.failed",
                                actor=actor,
                                payload={"reason": "approval_rejected", "stage": stage["stage_id"]},
                            )
                            return self.snapshot(run_id)
                    if made_progress:
                        continue
                    self.store.update_run(run_id, status=RunStatus.AWAITING_APPROVAL.value)
                    return self.snapshot(run_id)

                # 3. Auto re-plan trigger after its designated stage.
                if workflow.auto_replan and not run["auto_replan_done"]:
                    trigger = self.store.get_stage_run(run_id, workflow.auto_replan.after_stage)
                    if trigger and trigger["status"] == StageStatus.SUCCEEDED.value:
                        replan = workflow.auto_replan
                        if replan.require_approval:
                            approval = self.store.find_approval(run_id, REPLAN_STAGE, "replan")
                            if approval is None:
                                self._request_replan_approval(
                                    run, replan.targets, replan.reason, replan.after_stage, "auto"
                                )
                                return self.snapshot(run_id)
                            if approval["status"] == "PENDING":
                                self.store.update_run(
                                    run_id, status=RunStatus.AWAITING_APPROVAL.value
                                )
                                return self.snapshot(run_id)
                        self._apply_replan(
                            run_id,
                            workflow,
                            targets=set(replan.targets),
                            reason=replan.reason,
                            changed_stage=replan.after_stage,
                            actor=actor,
                            source="auto",
                        )
                        continue

                # 4. Select and run ready stages.
                stages = self.store.list_stage_runs(run_id)
                stage_map = {stage["stage_id"]: stage for stage in stages}
                succeeded = {
                    stage["stage_id"]
                    for stage in stages
                    if stage["status"] == StageStatus.SUCCEEDED.value
                }
                ready = [
                    stage
                    for stage in stages
                    if stage["status"]
                    in {StageStatus.PENDING.value, StageStatus.READY.value}
                    and all(
                        dependency in succeeded
                        for dependency in workflow.stage_map[stage["stage_id"]].depends_on
                    )
                ]
                ready.sort(key=lambda item: item["stage_id"])
                if not ready:
                    blocked = self._cancel_blocked(run_id, workflow, stages)
                    if blocked:
                        continue
                    terminal = all(
                        stage["status"] in TERMINAL_STAGE_STATUSES for stage in stages
                    )
                    if terminal:
                        self._finalize(run_id, actor)
                        return self.snapshot(run_id)
                    self._safe_stop(run_id, "scheduler_deadlock_detected", actor)
                    return self.snapshot(run_id)

                for stage in ready:
                    self.store.update_stage_run(
                        stage["id"], status=StageStatus.READY.value
                    )
                outcomes = self._run_batch(
                    run_id, workflow, ready, stage_map, actor
                )
                if any(outcome.status == StageStatus.AWAITING_APPROVAL.value for outcome in outcomes):
                    self.store.update_run(run_id, status=RunStatus.AWAITING_APPROVAL.value)
                    return self.snapshot(run_id)
                failures = [
                    outcome
                    for outcome in outcomes
                    if outcome.status == StageStatus.FAILED.value
                ]
                if failures:
                    rollback = any(
                        workflow.stage_map[outcome.stage_id].failure_policy == "rollback"
                        for outcome in failures
                    )
                    if rollback:
                        self._rollback_run(run_id, workflow, failures[0].error or "stage failed", actor)
                        return self.snapshot(run_id)
                    fatal = any(
                        workflow.stage_map[outcome.stage_id].failure_policy == "fail_run"
                        for outcome in failures
                    )
                    if fatal:
                        self._fail_run(
                            run_id,
                            failures[0].error or "stage failed",
                            [outcome.stage_id for outcome in failures],
                            actor,
                        )
                        return self.snapshot(run_id)

    def _run_batch(
        self,
        run_id: str,
        workflow: WorkflowDefinition,
        ready: list[dict[str, Any]],
        stage_map: dict[str, dict[str, Any]],
        actor: str,
    ) -> list[StageOutcome]:
        if len(ready) == 1:
            stage = ready[0]
            return [self._execute_stage(run_id, workflow.stage_map[stage["stage_id"]], stage, actor)]
        workers = min(self.settings.max_parallel, len(ready))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="asdlc-stage") as pool:
            futures = [
                pool.submit(
                    self._execute_stage,
                    run_id,
                    workflow.stage_map[stage["stage_id"]],
                    stage,
                    actor,
                )
                for stage in ready
            ]
            return [future.result() for future in futures]

    # -- single stage ------------------------------------------------------

    def _execute_stage(
        self,
        run_id: str,
        spec: StageSpec,
        stage_row: dict[str, Any],
        actor: str,
    ) -> StageOutcome:
        run = self._require_run(run_id)
        approval: dict[str, Any] | None = None
        if spec.requires_approval:
            approval = (
                self.store.get_approval(stage_row["approval_id"])
                if stage_row.get("approval_id")
                else None
            )
            if approval is None:
                approval = self.store.create_approval(
                    run_id=run_id,
                    stage_id=spec.id,
                    kind="stage",
                    context={
                        "stage": spec.name,
                        "description": spec.description,
                        "requirement": run["requirement"][:1500],
                        "upstream": sorted(
                            {
                                artifact["name"]
                                for artifact in self.store.list_artifacts(run_id)
                                if artifact["stage_id"] in spec.depends_on
                            }
                        ),
                    },
                    requested_by="orchestrator",
                )
                self.store.update_stage_run(
                    stage_row["id"],
                    status=StageStatus.AWAITING_APPROVAL.value,
                    approval_id=approval["id"],
                )
                self.store.record_event(
                    run_id=run_id,
                    event_type="approval.requested",
                    actor="orchestrator",
                    stage_id=spec.id,
                    payload={"approval_id": approval["id"], "kind": "stage"},
                )
                return StageOutcome(spec.id, StageStatus.AWAITING_APPROVAL.value)
            if approval["status"] == "PENDING":
                self.store.update_stage_run(
                    stage_row["id"],
                    status=StageStatus.AWAITING_APPROVAL.value,
                    approval_id=approval["id"],
                )
                return StageOutcome(spec.id, StageStatus.AWAITING_APPROVAL.value)
            if approval["status"] == "REJECTED":
                return StageOutcome(spec.id, StageStatus.CANCELLED.value, "approval rejected")

        entry = check_entry_gate(
            spec.entry_gate,
            spec,
            AgentResult(summary=""),
            AgentContext(
                run_id=run_id,
                stage_id=spec.id,
                scenario=run["scenario"],
                requirement=run["requirement"],
                attempt=0,
                mode=run["mode"],
            ),
            self.store,
        )
        if not entry.passed:
            self.store.update_stage_run(
                stage_row["id"],
                status=StageStatus.FAILED.value,
                error=f"entry gate failed: {entry.reason}",
                ended_at=utcnow_iso(),
            )
            self.store.record_event(
                run_id=run_id,
                event_type="stage.failed",
                actor=actor,
                stage_id=spec.id,
                payload={"reason": entry.reason, "gate": "entry"},
            )
            return StageOutcome(spec.id, StageStatus.FAILED.value, entry.reason)

        attempt = int(stage_row["attempt"] or 0)
        workspace = self.settings.runs_dir / run_id
        workspace.mkdir(parents=True, exist_ok=True)
        last_error = "stage failed"
        while attempt <= spec.max_retries:
            attempt += 1
            started_at = utcnow_iso()
            self.store.update_stage_run(
                stage_row["id"],
                status=StageStatus.RUNNING.value,
                attempt=attempt,
                total_attempts=int(stage_row.get("total_attempts") or 0) + 1,
                started_at=started_at,
                error=None,
            )
            self.store.record_event(
                run_id=run_id,
                event_type="stage.started",
                actor=actor,
                stage_id=spec.id,
                payload={"attempt": attempt, "agent": spec.agent},
            )
            context = self._build_context(
                run, spec, attempt, approval, workspace
            )
            try:
                agent = self._agent_for(spec, attempt)
                result = self._run_agent(agent, context, spec.timeout_seconds)
            except StageExecutionError as exc:
                result = AgentResult(
                    summary="stage exception",
                    success=False,
                    error=exc.message,
                    retryable=exc.retryable,
                )
            except Exception as exc:  # noqa: BLE001 - agent boundary
                result = AgentResult(
                    summary="unexpected stage exception",
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                    retryable=True,
                )

            policy_decisions: list[PolicyDecision] = []
            if result.success:
                policy_decisions = self.policy.evaluate(
                    PolicyInput(
                        stage=spec,
                        context=context,
                        artifacts=result.artifacts,
                        target_environment=run.get("context", {}).get(
                            "environment", "development"
                        ),
                        approval_status=approval["status"] if approval else "not_required",
                        test_evidence=self.store.has_test_evidence(run_id),
                        change_freeze=self.settings.change_freeze,
                    )
                )
                self.store.record_event(
                    run_id=run_id,
                    event_type="policy.evaluated",
                    actor="policy-engine",
                    stage_id=spec.id,
                    payload={
                        "attempt": attempt,
                        "decisions": [
                            {
                                "rule_id": decision.rule_id,
                                "allow": decision.allow,
                                "severity": decision.severity,
                                "reason": decision.reason,
                            }
                            for decision in policy_decisions
                        ],
                    },
                )
                denials = self.policy.denied(policy_decisions)
                if denials:
                    self._persist_policy_denials(run_id, spec, stage_row, denials)
                    last_error = "; ".join(
                        f"{decision.rule_id}: {decision.reason}" for decision in denials
                    )
                    retryable = any(decision.retryable for decision in denials)
                    result = AgentResult(
                        summary="policy denied", success=False, error=last_error,
                        retryable=retryable,
                    )

            if result.success:
                gate = check_exit_gate(
                    spec.exit_gate, spec, result, context, self.store
                )
                self.store.record_event(
                    run_id=run_id,
                    event_type="gate.evaluated",
                    actor="gate-registry",
                    stage_id=spec.id,
                    payload={"gate": spec.exit_gate, "passed": gate.passed, "reason": gate.reason},
                )
                if not gate.passed:
                    last_error = f"exit gate failed: {gate.reason}"
                    result = AgentResult(
                        summary="exit gate failed",
                        success=False,
                        error=last_error,
                        retryable=True,
                    )

            if result.success:
                self._persist_success(
                    run_id, spec, stage_row, context, result, policy_decisions, attempt, actor
                )
                return StageOutcome(spec.id, StageStatus.SUCCEEDED.value)

            last_error = result.error or last_error
            first_failure = stage_row.get("first_failure_at") or utcnow_iso()
            self.store.update_stage_run(
                stage_row["id"],
                first_failure_at=first_failure,
                error=last_error,
            )
            self.store.record_event(
                run_id=run_id,
                event_type="stage.attempt_failed",
                actor=actor,
                stage_id=spec.id,
                payload={
                    "attempt": attempt,
                    "error": last_error,
                    "retryable": result.retryable,
                },
            )
            retryable = result.retryable and attempt <= spec.max_retries
            if not retryable:
                break
            delay = min(
                self.settings.retry_max_delay,
                self.settings.retry_base_delay * (2 ** (attempt - 1)),
            )
            delay *= 1 + random.uniform(0, 0.2)
            self.store.record_event(
                run_id=run_id,
                event_type="stage.retry_scheduled",
                actor=actor,
                stage_id=spec.id,
                payload={"attempt": attempt + 1, "delay_seconds": round(delay, 4)},
            )
            self.store.update_stage_run(
                stage_row["id"],
                retry_count=int(stage_row.get("retry_count") or 0) + 1,
            )
            time.sleep(delay)
            stage_row = self.store.get_stage_run(run_id, spec.id) or stage_row

        self.store.update_stage_run(
            stage_row["id"],
            status=StageStatus.FAILED.value,
            error=last_error,
            ended_at=utcnow_iso(),
        )
        self.store.record_event(
            run_id=run_id,
            event_type="stage.failed",
            actor=actor,
            stage_id=spec.id,
            payload={"error": last_error, "attempts": attempt},
        )
        return StageOutcome(spec.id, StageStatus.FAILED.value, last_error)

    def _persist_success(
        self,
        run_id: str,
        spec: StageSpec,
        stage_row: dict[str, Any],
        context: AgentContext,
        result: AgentResult,
        policy_decisions: list[PolicyDecision],
        attempt: int,
        actor: str,
    ) -> None:
        artifact_ids: list[dict[str, Any]] = []
        for draft in result.artifacts:
            stored = self.store.add_artifact(
                run_id=run_id,
                stage_id=spec.id,
                stage_run_id=stage_row["id"],
                draft=draft,
            )
            self._write_artifact(run_id, spec.id, draft.name, draft.content)
            artifact_ids.append(stored)
        decision_ids: list[str] = []
        for draft in result.decisions:
            decision_ids.append(
                self.store.add_decision(
                    run_id=run_id,
                    stage_id=spec.id,
                    stage_run_id=stage_row["id"],
                    draft=draft,
                    actor=actor,
                )
            )
        for decision in policy_decisions:
            if not decision.allow:
                continue
            self.store.add_decision(
                run_id=run_id,
                stage_id=spec.id,
                stage_run_id=stage_row["id"],
                draft=DecisionDraft(
                    kind="policy",
                    topic=decision.rule_id,
                    decision="allow",
                    rationale=decision.reason,
                    payload={"severity": decision.severity},
                ),
                actor="policy-engine",
            )
        self.store.update_stage_run(
            stage_row["id"],
            status=StageStatus.SUCCEEDED.value,
            ended_at=utcnow_iso(),
            error=None,
            output_summary=result.summary,
            stale=0,
        )
        self.store.record_event(
            run_id=run_id,
            event_type="stage.succeeded",
            actor=actor,
            stage_id=spec.id,
            payload={
                "summary": result.summary,
                "attempts": attempt,
                "artifacts": artifact_ids,
                "decisions": decision_ids,
                "metadata": result.metadata,
            },
        )

    def _persist_policy_denials(
        self,
        run_id: str,
        spec: StageSpec,
        stage_row: dict[str, Any],
        denials: list[PolicyDecision],
    ) -> None:
        for decision in denials:
            self.store.add_decision(
                run_id=run_id,
                stage_id=spec.id,
                stage_run_id=stage_row["id"],
                draft=DecisionDraft(
                    kind="policy",
                    topic=decision.rule_id,
                    decision="deny",
                    rationale=decision.reason,
                    payload={
                        "severity": decision.severity,
                        "retryable": decision.retryable,
                        "obligation": decision.obligation,
                        "metadata": decision.metadata,
                    },
                ),
                actor="policy-engine",
            )
            self.store.record_event(
                run_id=run_id,
                event_type="policy.violation",
                actor="policy-engine",
                stage_id=spec.id,
                payload={
                    "rule_id": decision.rule_id,
                    "reason": decision.reason,
                    "retryable": decision.retryable,
                    "obligation": decision.obligation,
                },
            )

    # -- context and agent execution --------------------------------------

    def _build_context(
        self,
        run: dict[str, Any],
        spec: StageSpec,
        attempt: int,
        approval: dict[str, Any] | None,
        workspace: Path,
    ) -> AgentContext:
        replan_reason = None
        stage_row = self.store.get_stage_run(run["id"], spec.id)
        if stage_row and stage_row["stale"]:
            replan_reason = run.get("context", {}).get("last_replan_reason")
        return AgentContext(
            run_id=run["id"],
            stage_id=spec.id,
            scenario=run["scenario"],
            requirement=run["requirement"],
            attempt=attempt,
            mode=run["mode"],
            upstream=self.store.upstream_artifacts(run["id"], spec.depends_on),
            decisions=self.store.list_decisions(run["id"]),
            replan_reason=replan_reason,
            approval=approval,
            workspace=workspace,
        )

    def _agent_for(self, spec: StageSpec, attempt: int) -> Agent:
        name = spec.fallback_agent if attempt > 1 and spec.fallback_agent else spec.agent
        agent = self.registry.require(name)
        if self.settings.agent_mode == "llm" and self.settings.llm_api_key:
            from .agents.llm import LLMAgent

            if not isinstance(agent, LLMAgent):
                return LLMAgent(agent, self.settings)
        return agent

    def _run_agent(self, agent: Agent, context: AgentContext, timeout: float) -> AgentResult:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="asdlc-agent") as pool:
            future = pool.submit(agent.execute, context)
            try:
                return future.result(timeout=timeout)
            except FutureTimeout as exc:
                future.cancel()
                raise StageExecutionError(
                    f"agent exceeded {timeout}s timeout", retryable=True, code="agent_timeout"
                ) from exc

    def _write_artifact(self, run_id: str, stage_id: str, name: str, content: str) -> None:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise StageExecutionError(f"unsafe artifact name {name!r}", retryable=False)
        target = self.settings.runs_dir / run_id / "artifacts" / stage_id / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    # -- failure, rollback, safe stop -------------------------------------

    def _cancel_blocked(
        self,
        run_id: str,
        workflow: WorkflowDefinition,
        stages: list[dict[str, Any]],
    ) -> bool:
        by_id = {stage["stage_id"]: stage for stage in stages}
        blocked: set[str] = set()
        for stage in stages:
            if stage["status"] not in {
                StageStatus.PENDING.value,
                StageStatus.READY.value,
            }:
                continue
            spec = workflow.stage_map[stage["stage_id"]]
            if any(
                by_id[dependency]["status"]
                in {
                    StageStatus.FAILED.value,
                    StageStatus.CANCELLED.value,
                    StageStatus.ROLLED_BACK.value,
                }
                for dependency in spec.depends_on
            ):
                blocked.add(stage["stage_id"])
        if blocked:
            self.store.cancel_stages(run_id, blocked, reason="upstream dependency failed")
            self.store.record_event(
                run_id=run_id,
                event_type="stages.cancelled",
                actor="scheduler",
                payload={"stages": sorted(blocked), "reason": "upstream dependency failed"},
            )
        return bool(blocked)

    def _finalize(self, run_id: str, actor: str) -> None:
        run = self._require_run(run_id)
        if run["status"] in TERMINAL_RUN_STATUSES:
            return
        stages = self.store.list_stage_runs(run_id)
        statuses = {stage["status"] for stage in stages}
        if statuses <= {StageStatus.SUCCEEDED.value, StageStatus.SKIPPED.value}:
            status = RunStatus.SUCCEEDED.value
            event = "run.succeeded"
        elif statuses & {StageStatus.FAILED.value, StageStatus.ROLLED_BACK.value}:
            status = RunStatus.FAILED.value
            event = "run.failed"
        elif StageStatus.CANCELLED.value in statuses:
            status = RunStatus.CANCELLED.value
            event = "run.cancelled"
        else:
            status = RunStatus.FAILED.value
            event = "run.failed"
        self.store.update_run(
            run_id, status=status, ended_at=utcnow_iso(), error=None if status == "SUCCEEDED" else "stage failure"
        )
        self.store.record_event(
            run_id=run_id,
            event_type=event,
            actor=actor,
            payload={"stages": {stage["stage_id"]: stage["status"] for stage in stages}},
        )

    def _fail_run(self, run_id: str, error: str, stage_ids: list[str], actor: str) -> None:
        stages = self.store.list_stage_runs(run_id)
        pending = {
            stage["stage_id"]
            for stage in stages
            if stage["status"] in {StageStatus.PENDING.value, StageStatus.READY.value}
        }
        if pending:
            self.store.cancel_stages(run_id, pending, reason="run failed")
        self.store.update_run(
            run_id,
            status=RunStatus.FAILED.value,
            error=error,
            ended_at=utcnow_iso(),
        )
        self.store.record_event(
            run_id=run_id,
            event_type="run.failed",
            actor=actor,
            payload={"error": error, "failed_stages": stage_ids},
        )

    def _rollback_run(
        self, run_id: str, workflow: WorkflowDefinition, reason: str, actor: str
    ) -> None:
        stages = self.store.list_stage_runs(run_id)
        succeeded = {
            stage["stage_id"]
            for stage in stages
            if stage["status"] == StageStatus.SUCCEEDED.value
        }
        run = self._require_run(run_id)
        order = reverse_topological(workflow.stages, succeeded)
        self.store.record_event(
            run_id=run_id,
            event_type="rollback.started",
            actor=actor,
            payload={"reason": reason, "order": order},
        )
        for stage_id in order:
            spec = workflow.stage_map[stage_id]
            agent = self.registry.get(spec.agent)
            if spec.compensation and agent is not None:
                context = AgentContext(
                    run_id=run_id,
                    stage_id=stage_id,
                    scenario=run["scenario"],
                    requirement=run["requirement"],
                    attempt=0,
                    mode=run["mode"],
                    upstream=self.store.upstream_artifacts(run_id, spec.depends_on),
                    workspace=self.settings.runs_dir / run_id,
                )
                try:
                    agent.compensate(context)
                except Exception as exc:  # noqa: BLE001 - compensation boundary
                    self.store.record_event(
                        run_id=run_id,
                        event_type="compensation.failed",
                        actor=actor,
                        stage_id=stage_id,
                        payload={"error": f"{type(exc).__name__}: {exc}"},
                    )
                    self._safe_stop(run_id, "compensation_failed", actor)
                    return
                self.store.record_event(
                    run_id=run_id,
                    event_type="compensation.executed",
                    actor=actor,
                    stage_id=stage_id,
                    payload={"handler": spec.compensation},
                )
            stage_row = self.store.get_stage_run(run_id, stage_id)
            if stage_row:
                self.store.update_stage_run(
                    stage_row["id"],
                    status=StageStatus.ROLLED_BACK.value,
                    ended_at=utcnow_iso(),
                )
        self.store.update_run(
            run_id,
            status=RunStatus.ROLLED_BACK.value,
            error=reason,
            ended_at=utcnow_iso(),
        )
        self.store.record_event(
            run_id=run_id,
            event_type="rollback.completed",
            actor=actor,
            payload={"reason": reason, "compensated": order},
        )

    def _safe_stop(self, run_id: str, reason: str, actor: str) -> None:
        run = self.store.get_run(run_id)
        if not run or run["status"] in TERMINAL_RUN_STATUSES:
            return
        stages = self.store.list_stage_runs(run_id)
        pending = {
            stage["stage_id"]
            for stage in stages
            if stage["status"] not in TERMINAL_STAGE_STATUSES
        }
        if pending:
            self.store.cancel_stages(run_id, pending, reason=f"safe stop: {reason}")
        self.store.update_run(
            run_id,
            status=RunStatus.SAFE_STOPPED.value,
            safe_stop_reason=reason,
            error=None,
            ended_at=utcnow_iso(),
        )
        self.store.record_event(
            run_id=run_id,
            event_type="run.safe_stopped",
            actor=actor,
            payload={"reason": reason, "cancelled": sorted(pending)},
        )

    # -- re-plan -----------------------------------------------------------

    def request_replan(
        self,
        run_id: str,
        *,
        changed_stage: str,
        reason: str,
        actor: str = "operator",
        targets: list[str] | None = None,
        require_approval: bool = False,
    ) -> dict[str, Any]:
        run = self._require_run(run_id)
        if run["status"] in {RunStatus.RUNNING.value, RunStatus.AWAITING_APPROVAL.value}:
            raise Conflict("run is active; wait for it to settle before re-planning", code="run_active")
        workflow = self._workflow_for_run(run)
        if changed_stage not in workflow.stage_map:
            raise NotFound(f"unknown stage {changed_stage!r}", code="stage_not_found")
        if targets:
            selected = set(targets)
            unknown = selected - set(workflow.stage_map)
            if unknown:
                raise NotFound(f"unknown re-plan targets: {sorted(unknown)}", code="stage_not_found")
        else:
            selected = downstream_closure(workflow.stages, {changed_stage}) - {changed_stage}
        if not selected:
            raise Conflict("no downstream stages are impacted by this change", code="no_impact")
        if require_approval:
            self._request_replan_approval(
                run, tuple(sorted(selected)), reason, changed_stage, "manual"
            )
            return self.snapshot(run_id)
        self._apply_replan(
            run_id,
            workflow,
            targets=selected,
            reason=reason,
            changed_stage=changed_stage,
            actor=actor,
            source="manual",
        )
        self.execute(run_id, actor=actor)
        return self.snapshot(run_id)

    def _request_replan_approval(
        self,
        run: dict[str, Any],
        targets: tuple[str, ...],
        reason: str,
        changed_stage: str | None,
        source: str,
    ) -> dict[str, Any]:
        approval = self.store.create_approval(
            run_id=run["id"],
            stage_id=REPLAN_STAGE,
            kind="replan",
            context={
                "reason": reason,
                "targets": list(targets),
                "changed_stage": changed_stage,
                "source": source,
            },
            requested_by="orchestrator",
        )
        context = dict(run.get("context", {}))
        context["pending_replan"] = {
            "targets": list(targets),
            "reason": reason,
            "changed_stage": changed_stage,
            "source": source,
            "approval_id": approval["id"],
        }
        self.store.update_run(
            run["id"],
            context_json=json.dumps(context, sort_keys=True),
            status=RunStatus.AWAITING_APPROVAL.value,
        )
        self.store.record_event(
            run_id=run["id"],
            event_type="replan.requested",
            actor="orchestrator",
            stage_id=REPLAN_STAGE,
            payload={
                "reason": reason,
                "targets": list(targets),
                "changed_stage": changed_stage,
                "approval_id": approval["id"],
            },
        )
        return approval

    def _apply_replan(
        self,
        run_id: str,
        workflow: WorkflowDefinition,
        *,
        targets: set[str],
        reason: str,
        changed_stage: str | None,
        actor: str,
        source: str,
    ) -> None:
        unknown = targets - set(workflow.stage_map)
        if unknown:
            raise NotFound(f"unknown re-plan targets: {sorted(unknown)}", code="stage_not_found")
        self.store.reset_stage_for_replan(run_id, targets)
        self.store.increment_replan(run_id)
        run = self._require_run(run_id)
        context = dict(run.get("context", {}))
        context.pop("pending_replan", None)
        context["last_replan_reason"] = reason
        context["last_replan_targets"] = sorted(targets)
        self.store.update_run(
            run_id,
            context_json=json.dumps(context, sort_keys=True),
            status=RunStatus.RUNNING.value,
            ended_at=None,
            error=None,
            auto_replan_done=1 if source == "auto" else run["auto_replan_done"],
        )
        self.store.add_decision(
            run_id=run_id,
            stage_id=REPLAN_STAGE,
            stage_run_id=None,
            draft=DecisionDraft(
                kind="replan",
                topic="change.impact",
                decision=f"Re-plan {len(targets)} downstream stage(s)",
                rationale=reason,
                payload={
                    "targets": sorted(targets),
                    "changed_stage": changed_stage,
                    "source": source,
                },
            ),
            actor=actor,
        )
        self.store.record_event(
            run_id=run_id,
            event_type="replan.applied",
            actor=actor,
            stage_id=REPLAN_STAGE,
            payload={
                "targets": sorted(targets),
                "changed_stage": changed_stage,
                "reason": reason,
                "source": source,
            },
        )

    # -- approvals and operator controls ----------------------------------

    def approve(
        self,
        run_id: str,
        *,
        stage_id: str | None = None,
        actor: str = "reviewer",
        rationale: str = "Approved",
    ) -> dict[str, Any]:
        self._require_run(run_id)
        pending = self.store.list_approvals(status="PENDING", run_id=run_id, limit=20)
        if stage_id:
            pending = [item for item in pending if item["stage_id"] == stage_id]
        if not pending:
            raise Conflict("no pending approval for this request", code="no_pending_approval")
        approval = pending[0]
        self.store.decide_approval(
            approval["id"], status="APPROVED", decided_by=actor, rationale=rationale
        )
        self.store.record_event(
            run_id=run_id,
            event_type="approval.approved",
            actor=actor,
            stage_id=approval["stage_id"],
            payload={
                "approval_id": approval["id"],
                "rationale": rationale,
                "kind": approval["kind"],
            },
        )
        self.execute(run_id, actor=actor)
        return self.snapshot(run_id)

    def reject(
        self,
        run_id: str,
        *,
        stage_id: str | None = None,
        actor: str = "reviewer",
        rationale: str = "Rejected",
    ) -> dict[str, Any]:
        self._require_run(run_id)
        pending = self.store.list_approvals(status="PENDING", run_id=run_id, limit=20)
        if stage_id:
            pending = [item for item in pending if item["stage_id"] == stage_id]
        if not pending:
            raise Conflict("no pending approval for this request", code="no_pending_approval")
        approval = pending[0]
        self.store.decide_approval(
            approval["id"], status="REJECTED", decided_by=actor, rationale=rationale
        )
        self.store.record_event(
            run_id=run_id,
            event_type="approval.rejected",
            actor=actor,
            stage_id=approval["stage_id"],
            payload={
                "approval_id": approval["id"],
                "rationale": rationale,
                "kind": approval["kind"],
            },
        )
        if approval["kind"] == "replan":
            self._safe_stop(run_id, "replan_rejected", actor)
        else:
            stage = self.store.get_stage_run(run_id, approval["stage_id"])
            if stage:
                self.store.update_stage_run(
                    stage["id"],
                    status=StageStatus.CANCELLED.value,
                    error=f"approval rejected: {rationale}",
                    ended_at=utcnow_iso(),
                )
            self._fail_run(run_id, f"approval rejected: {rationale}", [approval["stage_id"]], actor)
        return self.snapshot(run_id)

    def safe_stop(self, run_id: str, *, reason: str, actor: str = "operator") -> dict[str, Any]:
        self._require_run(run_id)
        self._safe_stop(run_id, reason, actor)
        return self.snapshot(run_id)

    def resume(self, run_id: str, *, actor: str = "operator") -> dict[str, Any]:
        run = self._require_run(run_id)
        if run["status"] != RunStatus.SAFE_STOPPED.value:
            raise Conflict("only a safe-stopped run can be resumed", code="run_not_resumable")
        stages = self.store.list_stage_runs(run_id)
        with self.store.db.tx() as conn:
            conn.execute(
                "UPDATE stage_runs SET status = 'PENDING', attempt = 0, error = NULL, "
                "ended_at = NULL, started_at = NULL WHERE run_id = ? AND status = 'CANCELLED'",
                (run_id,),
            )
        self.store.update_run(
            run_id,
            status=RunStatus.RUNNING.value,
            safe_stop_reason=None,
            ended_at=None,
            error=None,
        )
        self.store.record_event(
            run_id=run_id,
            event_type="run.resumed",
            actor=actor,
            payload={"previous_reason": run["safe_stop_reason"], "stages": len(stages)},
        )
        self.execute(run_id, actor=actor)
        return self.snapshot(run_id)

    # -- read models -------------------------------------------------------

    def snapshot(self, run_id: str) -> dict[str, Any]:
        run = self._require_run(run_id)
        workflow = self._workflow_for_run(run)
        stages = self.store.list_stage_runs(run_id)
        artifacts = self.store.list_artifacts(run_id)
        decisions = self.store.list_decisions(run_id)
        approvals = self.store.list_approvals(run_id=run_id, limit=100)
        audit = self.store.list_audit(run_id=run_id, limit=400)
        spec_map = workflow.stage_map
        artifacts_by_stage: dict[str, list[dict[str, Any]]] = {}
        for artifact in artifacts:
            artifacts_by_stage.setdefault(artifact["stage_id"], []).append(artifact)
        enriched = []
        for stage in stages:
            spec = spec_map.get(stage["stage_id"])
            item = dict(stage)
            if spec:
                item.update(
                    {
                        "depends_on": list(spec.depends_on),
                        "requires_approval": spec.requires_approval,
                        "policy_tags": list(spec.policy_tags),
                        "exit_gate": spec.exit_gate,
                        "failure_policy": spec.failure_policy,
                        "description": spec.description,
                    }
                )
            item["artifacts"] = [
                {
                    "id": artifact["id"],
                    "name": artifact["name"],
                    "kind": artifact["kind"],
                    "version": artifact["version"],
                    "content_hash": artifact["content_hash"],
                }
                for artifact in artifacts_by_stage.get(stage["stage_id"], [])
            ]
            enriched.append(item)
        actions = []
        if run["status"] == RunStatus.AWAITING_APPROVAL.value:
            actions = ["approve", "reject", "safe_stop"]
        elif run["status"] == RunStatus.SAFE_STOPPED.value:
            actions = ["resume", "replan"]
        elif run["status"] in TERMINAL_RUN_STATUSES:
            actions = ["replan"]
        return {
            "run": run,
            "stages": enriched,
            "levels": topological_levels(workflow.stages),
            "workflow": {
                "id": workflow.id,
                "name": workflow.name,
                "version": workflow.version,
                "auto_replan": None
                if not workflow.auto_replan
                else {
                    "after_stage": workflow.auto_replan.after_stage,
                    "targets": list(workflow.auto_replan.targets),
                    "reason": workflow.auto_replan.reason,
                },
            },
            "artifacts": [
                {
                    "id": artifact["id"],
                    "stage_id": artifact["stage_id"],
                    "name": artifact["name"],
                    "kind": artifact["kind"],
                    "version": artifact["version"],
                    "content_hash": artifact["content_hash"],
                    "created_at": artifact["created_at"],
                }
                for artifact in artifacts
            ],
            "decisions": decisions,
            "approvals": approvals,
            "audit": audit,
            "audit_verification": self.store.verify_audit(run_id=run_id),
            "metrics": self.metrics.summary(),
            "actions": actions,
        }

    def list_runs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        runs = self.store.list_runs(limit=limit)
        for run in runs:
            stages = self.store.list_stage_runs(run["id"])
            run["stage_counts"] = {
                status: sum(1 for stage in stages if stage["status"] == status)
                for status in {stage["status"] for stage in stages}
            }
        return runs

    def metrics_summary(self) -> dict[str, Any]:
        return self.metrics.summary()

    # -- helpers -----------------------------------------------------------

    def _require_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if not run:
            raise NotFound(f"run {run_id!r} was not found", code="run_not_found")
        return run

    def _workflow_for_run(self, run: dict[str, Any]) -> WorkflowDefinition:
        try:
            return self.scenarios.get(run["scenario"]).workflow
        except KeyError:
            stored = self.store.get_workflow_json(run["workflow_id"])
            if not stored:
                raise NotFound("workflow definition not found", code="workflow_not_found") from None
            return workflow_from_dict(stored["definition"])

    def _total_attempts(self, run_id: str) -> int:
        return sum(int(stage["attempt"] or 0) for stage in self.store.list_stage_runs(run_id))

    def _context_without(self, run: dict[str, Any], key: str) -> str:
        context = dict(run.get("context", {}))
        context.pop(key, None)
        return json.dumps(context, sort_keys=True)

    def _lock_for(self, run_id: str) -> Any:
        import threading

        with self._lock_guard:
            if run_id not in self._run_locks:
                self._run_locks[run_id] = threading.RLock()
            return self._run_locks[run_id]
