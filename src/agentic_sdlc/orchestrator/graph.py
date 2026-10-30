"""DAG validation and traversal helpers."""

from __future__ import annotations

from collections import deque

from .models import StageSpec, WorkflowDefinition, WorkflowValidationError


def validate_stages(stages: tuple[StageSpec, ...]) -> None:
    if not stages:
        raise WorkflowValidationError("a workflow must contain at least one stage")
    seen: set[str] = set()
    for stage in stages:
        if stage.id in seen:
            raise WorkflowValidationError(f"duplicate stage id: {stage.id}")
        seen.add(stage.id)
        if stage.max_retries < 0:
            raise WorkflowValidationError(f"{stage.id}: max_retries must be >= 0")
        if stage.failure_policy not in {"fail_run", "rollback", "continue"}:
            raise WorkflowValidationError(f"{stage.id}: unknown failure policy")
        if stage.requires_approval and stage.failure_policy == "rollback":
            # Allowed, but compensation must exist for rollback to be meaningful.
            pass
    for stage in stages:
        for dependency in stage.depends_on:
            if dependency not in seen:
                raise WorkflowValidationError(
                    f"{stage.id}: dependency {dependency!r} does not exist"
                )
            if dependency == stage.id:
                raise WorkflowValidationError(f"{stage.id}: a stage cannot depend on itself")
    # Detect cycles with Kahn's algorithm.
    indegree = {stage.id: len(stage.depends_on) for stage in stages}
    adjacency: dict[str, list[str]] = {stage.id: [] for stage in stages}
    for stage in stages:
        for dependency in stage.depends_on:
            adjacency[dependency].append(stage.id)
    queue = deque(sorted(stage_id for stage_id, degree in indegree.items() if degree == 0))
    visited = 0
    while queue:
        node = queue.popleft()
        visited += 1
        for neighbor in sorted(adjacency[node]):
            indegree[neighbor] -= 1
            if indegree[neighbor] == 0:
                queue.append(neighbor)
    if visited != len(stages):
        cyclic = sorted(stage_id for stage_id, degree in indegree.items() if degree > 0)
        raise WorkflowValidationError(f"workflow contains a cycle among: {', '.join(cyclic)}")


def validate_workflow(workflow: WorkflowDefinition) -> None:
    validate_stages(workflow.stages)
    if workflow.auto_replan:
        stage_map = workflow.stage_map
        replan = workflow.auto_replan
        if replan.after_stage not in stage_map:
            raise WorkflowValidationError(
                f"auto_replan.after_stage references unknown stage {replan.after_stage!r}"
            )
        for target in replan.targets:
            if target not in stage_map:
                raise WorkflowValidationError(
                    f"auto_replan target references unknown stage {target!r}"
                )


def topological_levels(stages: tuple[StageSpec, ...]) -> list[list[str]]:
    """Return dependency levels; stages in one level may run in parallel."""
    validate_stages(stages)
    stage_map = {stage.id: stage for stage in stages}
    levels: list[list[str]] = []
    remaining = dict(stage_map)
    completed: set[str] = set()
    while remaining:
        ready = sorted(
            stage_id
            for stage_id, stage in remaining.items()
            if all(dependency in completed for dependency in stage.depends_on)
        )
        if not ready:
            raise WorkflowValidationError("workflow contains a cycle")
        levels.append(ready)
        completed.update(ready)
        for stage_id in ready:
            del remaining[stage_id]
    return levels


def downstream_closure(stages: tuple[StageSpec, ...], changed: set[str]) -> set[str]:
    """All stages transitively dependent on any changed stage (includes changed)."""
    adjacency: dict[str, set[str]] = {stage.id: set() for stage in stages}
    for stage in stages:
        for dependency in stage.depends_on:
            adjacency[dependency].add(stage.id)
    seen = set(changed)
    queue = deque(sorted(changed))
    while queue:
        node = queue.popleft()
        for dependent in sorted(adjacency.get(node, ())):
            if dependent not in seen:
                seen.add(dependent)
                queue.append(dependent)
    return seen


def reverse_topological(stages: tuple[StageSpec, ...], include: set[str]) -> list[str]:
    """Completed stages in safe compensation order (dependents first)."""
    ordered: list[str] = []
    for level in reversed(topological_levels(stages)):
        ordered.extend(stage_id for stage_id in reversed(level) if stage_id in include)
    return ordered
