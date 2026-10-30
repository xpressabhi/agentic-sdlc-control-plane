"""Load and validate scenario definitions from YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .graph import validate_workflow
from .models import AutoReplan, StageSpec, WorkflowDefinition, WorkflowValidationError


@dataclass(frozen=True)
class ScenarioDefinition:
    key: str
    name: str
    intent: str
    requirement: str
    environment: str
    tags: tuple[str, ...]
    workflow: WorkflowDefinition

    def summary(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "intent": self.intent,
            "environment": self.environment,
            "tags": list(self.tags),
            "stages": [
                {
                    "id": stage.id,
                    "name": stage.name,
                    "agent": stage.agent,
                    "depends_on": list(stage.depends_on),
                    "requires_approval": stage.requires_approval,
                    "description": stage.description,
                }
                for stage in self.workflow.stages
            ],
            "auto_replan": None
            if not self.workflow.auto_replan
            else {
                "after_stage": self.workflow.auto_replan.after_stage,
                "targets": list(self.workflow.auto_replan.targets),
                "reason": self.workflow.auto_replan.reason,
                "require_approval": self.workflow.auto_replan.require_approval,
            },
        }


def _stage(raw: dict[str, Any]) -> StageSpec:
    return StageSpec(
        id=str(raw["id"]),
        name=str(raw.get("name", raw["id"])),
        agent=str(raw["agent"]),
        depends_on=tuple(raw.get("depends_on") or ()),
        description=str(raw.get("description", "")),
        requires_approval=bool(raw.get("requires_approval", False)),
        max_retries=int(raw.get("max_retries", 1)),
        timeout_seconds=float(raw.get("timeout_seconds", 30.0)),
        failure_policy=str(raw.get("failure_policy", "fail_run")),
        compensation=raw.get("compensation"),
        policy_tags=tuple(raw.get("policy_tags") or ()),
        entry_gate=str(raw.get("entry_gate", "always")),
        exit_gate=str(raw.get("exit_gate", "artifact_present")),
        fallback_agent=raw.get("fallback_agent"),
    )


def load_scenario(path: str | Path) -> ScenarioDefinition:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise WorkflowValidationError(f"{path}: scenario must be a mapping")
    workflow_raw = raw.get("workflow") or {}
    stages = tuple(_stage(stage) for stage in workflow_raw.get("stages") or ())
    auto_raw = workflow_raw.get("auto_replan")
    auto_replan = None
    if auto_raw:
        auto_replan = AutoReplan(
            after_stage=str(auto_raw["after_stage"]),
            targets=tuple(auto_raw.get("targets") or ()),
            reason=str(auto_raw.get("reason", "")),
            require_approval=bool(auto_raw.get("require_approval", True)),
        )
    workflow = WorkflowDefinition(
        id=str(workflow_raw.get("id", f"wf-{raw['key']}")),
        name=str(raw["name"]),
        version=int(workflow_raw.get("version", 1)),
        stages=stages,
        auto_replan=auto_replan,
    )
    validate_workflow(workflow)
    return ScenarioDefinition(
        key=str(raw["key"]),
        name=str(raw["name"]),
        intent=str(raw.get("intent", "")).strip(),
        requirement=str(raw.get("requirement", "")).strip(),
        environment=str(raw.get("environment", "development")),
        tags=tuple(raw.get("tags") or ()),
        workflow=workflow,
    )


@dataclass
class ScenarioRegistry:
    scenarios: dict[str, ScenarioDefinition] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: str | Path | None = None) -> ScenarioRegistry:
        base = Path(directory) if directory else default_scenarios_dir()
        scenarios: dict[str, ScenarioDefinition] = {}
        for path in sorted(base.glob("*.yaml")):
            scenario = load_scenario(path)
            if scenario.key in scenarios:
                raise WorkflowValidationError(f"duplicate scenario key: {scenario.key}")
            scenarios[scenario.key] = scenario
        if not scenarios:
            raise WorkflowValidationError(f"no scenarios found in {base}")
        return cls(scenarios=scenarios)

    def get(self, key: str) -> ScenarioDefinition:
        if key not in self.scenarios:
            raise KeyError(key)
        return self.scenarios[key]

    def list(self) -> list[ScenarioDefinition]:
        return [self.scenarios[key] for key in sorted(self.scenarios)]

    def keys(self) -> list[str]:
        return sorted(self.scenarios)


def default_scenarios_dir() -> Path:
    override = os.getenv("ASDLC_SCENARIOS_DIR")
    if override:
        return Path(override)
    # src/agentic_sdlc/orchestrator/scenarios.py -> repository root
    repository = Path(__file__).resolve().parents[3] / "scenarios"
    if repository.exists():
        return repository
    # Installed package: fall back to the working directory used by the container.
    return Path.cwd() / "scenarios"


def workflow_from_dict(data: dict[str, Any]) -> WorkflowDefinition:
    """Rehydrate a workflow persisted as JSON (used after a restart)."""
    stages = tuple(_stage(stage) for stage in data.get("stages") or ())
    auto_raw = data.get("auto_replan")
    auto_replan = None
    if auto_raw:
        auto_replan = AutoReplan(
            after_stage=str(auto_raw["after_stage"]),
            targets=tuple(auto_raw.get("targets") or ()),
            reason=str(auto_raw.get("reason", "")),
            require_approval=bool(auto_raw.get("require_approval", True)),
        )
    workflow = WorkflowDefinition(
        id=str(data["id"]),
        name=str(data.get("name", data["id"])),
        version=int(data.get("version", 1)),
        stages=stages,
        auto_replan=auto_replan,
    )
    validate_workflow(workflow)
    return workflow

