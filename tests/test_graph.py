from __future__ import annotations

import pytest

from agentic_sdlc.orchestrator.graph import (
    downstream_closure,
    reverse_topological,
    topological_levels,
    validate_stages,
)
from agentic_sdlc.orchestrator.models import StageSpec, WorkflowValidationError


def stage(stage_id: str, *deps: str) -> StageSpec:
    return StageSpec(id=stage_id, name=stage_id, agent="noop", depends_on=deps)


def test_topological_levels() -> None:
    stages = (stage("a"), stage("b"), stage("c", "a", "b"), stage("d", "c"))
    assert topological_levels(stages) == [["a", "b"], ["c"], ["d"]]


def test_cycle_detection() -> None:
    with pytest.raises(WorkflowValidationError):
        topological_levels((stage("a", "b"), stage("b", "a")))


def test_unknown_dependency() -> None:
    with pytest.raises(WorkflowValidationError):
        validate_stages((stage("a", "missing"),))


def test_duplicate_stage() -> None:
    with pytest.raises(WorkflowValidationError):
        validate_stages((stage("a"), stage("a")))


def test_downstream_closure() -> None:
    stages = (stage("a"), stage("b", "a"), stage("c", "b"), stage("d"))
    assert downstream_closure(stages, {"a"}) == {"a", "b", "c"}
    assert downstream_closure(stages, {"b"}) == {"b", "c"}


def test_reverse_topological() -> None:
    stages = (stage("a"), stage("b", "a"), stage("c", "b"))
    assert reverse_topological(stages, {"a", "b", "c"}) == ["c", "b", "a"]
