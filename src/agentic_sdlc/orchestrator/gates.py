"""Entry and exit gate registry.

Gates are the contract between a stage and the scheduler: they decide whether a
stage may start and whether its output is acceptable. Failed exit gates consume
retry budget; failed entry gates are terminal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import AgentContext, AgentResult, StageSpec
from .store import OrchestratorStore


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str


GateFn = Callable[[StageSpec, AgentResult, AgentContext, OrchestratorStore], GateResult]


def _always(stage: StageSpec, result: AgentResult, context: AgentContext, store: OrchestratorStore) -> GateResult:
    return GateResult(True, "no precondition")


def _artifact_present(
    stage: StageSpec, result: AgentResult, context: AgentContext, store: OrchestratorStore
) -> GateResult:
    usable = [a for a in result.artifacts if a.content.strip()]
    if not usable:
        return GateResult(False, "stage produced no non-empty artifacts")
    return GateResult(True, f"{len(usable)} artifact(s) present")


def _requirements_normalized(
    stage: StageSpec, result: AgentResult, context: AgentContext, store: OrchestratorStore
) -> GateResult:
    if not result.artifacts:
        return GateResult(False, "requirements artifact missing")
    blocking = int(result.metadata.get("blocking_ambiguities", 0) or 0)
    if blocking:
        return GateResult(False, f"{blocking} blocking ambiguities require human normalization")
    return GateResult(True, "requirements normalized with no blocking ambiguity")


def _tests_passed(
    stage: StageSpec, result: AgentResult, context: AgentContext, store: OrchestratorStore
) -> GateResult:
    if result.metadata.get("tests_passed") is True:
        return GateResult(True, "tests passed in isolated execution")
    return GateResult(False, "test evidence is missing or failing")


def _release_ready(
    stage: StageSpec, result: AgentResult, context: AgentContext, store: OrchestratorStore
) -> GateResult:
    if result.metadata.get("release_ready") is not True:
        return GateResult(False, "release metadata is not marked ready")
    if not store.has_test_evidence(context.run_id):
        return GateResult(False, "no green test evidence attached to the run")
    return GateResult(True, "release checklist and test evidence present")


EXIT_GATES: dict[str, GateFn] = {
    "artifact_present": _artifact_present,
    "requirements_normalized": _requirements_normalized,
    "tests_passed": _tests_passed,
    "release_ready": _release_ready,
}

ENTRY_GATES: dict[str, GateFn] = {
    "always": _always,
}


def check_exit_gate(
    name: str,
    stage: StageSpec,
    result: AgentResult,
    context: AgentContext,
    store: OrchestratorStore,
) -> GateResult:
    gate = EXIT_GATES.get(name)
    if gate is None:
        return GateResult(False, f"unknown exit gate {name!r}")
    return gate(stage, result, context, store)


def check_entry_gate(
    name: str,
    stage: StageSpec,
    result: AgentResult,
    context: AgentContext,
    store: OrchestratorStore,
) -> GateResult:
    gate = ENTRY_GATES.get(name)
    if gate is None:
        return GateResult(False, f"unknown entry gate {name!r}")
    return gate(stage, result, context, store)
