from __future__ import annotations

from agentic_sdlc.orchestrator.models import AgentContext, ArtifactDraft, StageSpec
from agentic_sdlc.orchestrator.policy import PolicyEngine, PolicyInput


def context() -> AgentContext:
    return AgentContext(
        run_id="run_1",
        stage_id="implementation",
        scenario="greenfield",
        requirement="build a shortener",
        attempt=1,
        mode="reference",
    )


def stage(**overrides) -> StageSpec:
    defaults = dict(
        id="implementation",
        name="Implementation",
        agent="implementation",
        policy_tags=("code",),
    )
    defaults.update(overrides)
    return StageSpec(**defaults)


def evaluate(artifacts, spec=None, **overrides) -> list:
    engine = PolicyEngine()
    return engine.evaluate(
        PolicyInput(
            stage=spec or stage(),
            context=context(),
            artifacts=artifacts,
            **overrides,
        )
    )


def test_secret_detection_blocks_code() -> None:
    artifacts = [
        ArtifactDraft(
            "module.py",
            "code",
            'AWS_ACCESS_KEY = "AKIA' + 'IOSFODNN7EXAMPLE"\n',
        )
    ]
    decisions = evaluate(artifacts)
    denied = [decision for decision in decisions if decision.severity == "deny"]
    assert denied and denied[0].rule_id == "SEC-001"
    assert denied[0].retryable is True


def test_private_key_and_generic_secret() -> None:
    for content in (
        "-----BEGIN RSA PRIVATE KEY-----\nabc",
        'password = "super-' + 'secret-value"',
    ):
        decisions = evaluate([ArtifactDraft("module.py", "code", content)])
        assert any(decision.rule_id == "SEC-001" and not decision.allow for decision in decisions)


def test_clean_code_passes() -> None:
    decisions = evaluate(
        [ArtifactDraft("module.py", "code", "def add(a, b):\n    return a + b\n")]
    )
    assert not [decision for decision in decisions if decision.severity == "deny"]


def test_pii_detection() -> None:
    decisions = evaluate(
        [ArtifactDraft("module.py", "code", 'SSN = "123-' + '45-6789"')]
    )
    assert any(decision.rule_id == "SEC-002" for decision in decisions)


def test_gpl_license_denied() -> None:
    decisions = evaluate([ArtifactDraft("module.py", "code", "# license: GPL-3.0")])
    assert any(decision.rule_id == "LIC-001" and not decision.allow for decision in decisions)


def test_design_requires_ssrf_controls() -> None:
    design_stage = stage(id="architecture", name="Architecture", agent="architecture")
    missing = evaluate(
        [ArtifactDraft("architecture.md", "markdown", "A short design")], spec=design_stage
    )
    assert any(decision.rule_id == "SEC-003" and not decision.allow for decision in missing)
    present = evaluate(
        [
            ArtifactDraft(
                "architecture.md",
                "markdown",
                "SSRF controls reject private destinations before redirect.",
            )
        ],
        spec=design_stage,
    )
    assert not [decision for decision in present if decision.severity == "deny"]


def test_production_deploy_requires_approval_and_tests() -> None:
    deploy = stage(
        id="publish",
        name="Publish",
        agent="release",
        policy_tags=("production_deploy",),
    )
    artifacts = [ArtifactDraft("release-checklist.md", "report", "Rollback documented")]
    denied = evaluate(
        artifacts,
        spec=deploy,
        target_environment="production",
        approval_status="PENDING",
        test_evidence=False,
    )
    assert [decision.rule_id for decision in denied if not decision.allow].count("CHG-001") == 2
    allowed = evaluate(
        artifacts,
        spec=deploy,
        target_environment="production",
        approval_status="APPROVED",
        test_evidence=True,
    )
    assert not [decision for decision in allowed if decision.severity == "deny"]


def test_change_freeze_blocks_deploy() -> None:
    deploy = stage(
        id="publish",
        name="Publish",
        agent="release",
        policy_tags=("production_deploy",),
    )
    decisions = evaluate(
        [ArtifactDraft("release-checklist.md", "report", "rollback")],
        spec=deploy,
        approval_status="APPROVED",
        test_evidence=True,
        change_freeze=True,
    )
    assert any(decision.rule_id == "CHG-002" and not decision.allow for decision in decisions)


def test_release_requires_rollback_documentation() -> None:
    deploy = stage(
        id="deploy",
        name="Deploy",
        agent="release",
        policy_tags=("production_deploy",),
    )
    decisions = evaluate(
        [ArtifactDraft("release-checklist.md", "report", "ship it")],
        spec=deploy,
        approval_status="APPROVED",
        test_evidence=True,
    )
    assert any(decision.rule_id == "REL-001" and not decision.allow for decision in decisions)
