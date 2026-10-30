"""Declarative policy guardrails.

Rules are small, named predicates so every decision is explainable and testable.
A single engine evaluates them uniformly before execution and after artifacts are
generated; violations become persisted decisions and audit events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import AgentContext, ArtifactDraft, StageSpec

SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "generic_secret",
        re.compile(
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\b"
            r"\s*[:=]\s*['\"][^'\"]{8,}['\"]"
        ),
    ),
)
SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
GPL_PATTERN = re.compile(r"(?i)\bGPL(?:-|\s)?(?:v)?3(?:\.0)?\b")


@dataclass(frozen=True)
class PolicyDecision:
    rule_id: str
    allow: bool
    reason: str
    severity: str = "info"  # info | warn | deny
    retryable: bool = False
    obligation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PolicyInput:
    stage: StageSpec
    context: AgentContext
    artifacts: list[ArtifactDraft]
    target_environment: str = "development"
    approval_status: str = "not_required"
    test_evidence: bool = False
    change_freeze: bool = False


class PolicyEngine:
    """Evaluates security, change-control and design-control rules."""

    def evaluate(self, policy_input: PolicyInput) -> list[PolicyDecision]:
        decisions: list[PolicyDecision] = []
        decisions.extend(self._scan_secrets(policy_input))
        decisions.extend(self._scan_pii(policy_input))
        decisions.extend(self._scan_licenses(policy_input))
        decisions.extend(self._design_controls(policy_input))
        decisions.extend(self._release_controls(policy_input))
        return decisions

    @property
    def rules(self) -> list[dict[str, str]]:
        return [
            {"id": "SEC-001", "description": "No hardcoded secrets in code artifacts"},
            {"id": "SEC-002", "description": "No SSNs/PII in code artifacts"},
            {"id": "SEC-003", "description": "Design must document SSRF/private-address controls"},
            {"id": "LIC-001", "description": "No GPL-3 dependencies in generated code"},
            {
                "id": "CHG-001",
                "description": "Production deploy requires approval and green test evidence",
            },
            {"id": "CHG-002", "description": "Change freeze blocks production deployment"},
            {"id": "REL-001", "description": "Release checklist must include rollback"},
        ]

    # -- rules -------------------------------------------------------------

    def _code_artifacts(self, artifacts: list[ArtifactDraft]) -> list[ArtifactDraft]:
        return [a for a in artifacts if a.kind in {"code", "sql", "test"}]

    def _scan_secrets(self, pi: PolicyInput) -> list[PolicyDecision]:
        if "code" not in pi.stage.policy_tags and not self._code_artifacts(pi.artifacts):
            return []
        findings: list[str] = []
        for artifact in self._code_artifacts(pi.artifacts):
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(artifact.content):
                    findings.append(f"{artifact.name}:{name}")
        if not findings:
            return [
                PolicyDecision(
                    "SEC-001",
                    True,
                    "no hardcoded secrets detected",
                    metadata={"scanned": [a.name for a in self._code_artifacts(pi.artifacts)]},
                )
            ]
        return [
            PolicyDecision(
                "SEC-001",
                False,
                f"hardcoded secret material detected in {', '.join(findings)}",
                severity="deny",
                retryable=True,
                metadata={"findings": findings},
            )
        ]

    def _scan_pii(self, pi: PolicyInput) -> list[PolicyDecision]:
        findings = []
        for artifact in self._code_artifacts(pi.artifacts):
            if SSN_PATTERN.search(artifact.content):
                findings.append(artifact.name)
        if not findings:
            return []
        return [
            PolicyDecision(
                "SEC-002",
                False,
                f"possible SSN/PII detected in {', '.join(findings)}",
                severity="deny",
                retryable=True,
            )
        ]

    def _scan_licenses(self, pi: PolicyInput) -> list[PolicyDecision]:
        findings = [
            artifact.name
            for artifact in self._code_artifacts(pi.artifacts)
            if GPL_PATTERN.search(artifact.content)
        ]
        if not findings:
            return []
        return [
            PolicyDecision(
                "LIC-001",
                False,
                f"disallowed GPL-3 license reference in {', '.join(findings)}",
                severity="deny",
                retryable=False,
            )
        ]

    def _design_controls(self, pi: PolicyInput) -> list[PolicyDecision]:
        if pi.stage.agent != "architecture" and "design_controls" not in pi.stage.policy_tags:
            return []
        blob = "\n".join(a.content for a in pi.artifacts).lower()
        required = ["ssrf", "private", "redirect"]
        missing = [term for term in required if term not in blob]
        if missing:
            return [
                PolicyDecision(
                    "SEC-003",
                    False,
                    f"design artifact is missing required controls: {', '.join(missing)}",
                    severity="deny",
                    retryable=True,
                )
            ]
        return [
            PolicyDecision(
                "SEC-003", True, "SSRF and private-address controls documented", severity="info"
            )
        ]

    def _release_controls(self, pi: PolicyInput) -> list[PolicyDecision]:
        if "production_deploy" not in pi.stage.policy_tags:
            return []
        decisions: list[PolicyDecision] = []
        if pi.change_freeze:
            decisions.append(
                PolicyDecision(
                    "CHG-002",
                    False,
                    "production change freeze is active",
                    severity="deny",
                    retryable=False,
                )
            )
        if pi.approval_status != "APPROVED":
            decisions.append(
                PolicyDecision(
                    "CHG-001",
                    False,
                    "production deployment requires a recorded human approval",
                    severity="deny",
                    retryable=False,
                    obligation="obtain release approval",
                )
            )
        if not pi.test_evidence:
            decisions.append(
                PolicyDecision(
                    "CHG-001",
                    False,
                    "production deployment requires green test evidence",
                    severity="deny",
                    retryable=False,
                    obligation="run and attach validation evidence",
                )
            )
        if decisions:
            return decisions
        decisions.append(
            PolicyDecision(
                "CHG-001",
                True,
                "approval and test evidence present for production change",
                severity="info",
            )
        )
        rollback_documented = any(
            "rollback" in artifact.content.lower() for artifact in pi.artifacts
        )
        if not rollback_documented:
            decisions.append(
                PolicyDecision(
                    "REL-001",
                    False,
                    "release evidence must document rollback",
                    severity="deny",
                    retryable=True,
                )
            )
        return decisions

    def denied(self, decisions: list[PolicyDecision]) -> list[PolicyDecision]:
        return [decision for decision in decisions if decision.severity == "deny"]
