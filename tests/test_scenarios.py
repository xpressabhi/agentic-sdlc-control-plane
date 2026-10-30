from __future__ import annotations

from fastapi.testclient import TestClient


def launch(client: TestClient, auth: dict[str, str], scenario: str) -> dict:
    response = client.post(
        "/api/v1/runs",
        json={"scenario": scenario, "auto_approve": True},
        headers=auth,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_greenfield_scenario_end_to_end(client: TestClient, auth: dict[str, str]) -> None:
    snapshot = launch(client, auth, "greenfield")
    assert snapshot["run"]["status"] == "SUCCEEDED"
    stages = {stage["stage_id"]: stage for stage in snapshot["stages"]}
    assert all(stage["status"] == "SUCCEEDED" for stage in stages.values())
    assert stages["implementation"]["attempt"] == 1
    names = {artifact["name"] for artifact in snapshot["artifacts"]}
    assert {
        "requirements.md",
        "architecture.md",
        "openapi.yaml",
        "threat-model.md",
        "generated/shortener_core.py",
        "generated/test_shortener_core.py",
        "test-report.md",
        "runbook.md",
        "release-checklist.md",
        "deployment-record.md",
    } <= names
    publish_approval = [
        approval for approval in snapshot["approvals"] if approval["stage_id"] == "publish"
    ][0]
    assert publish_approval["status"] == "APPROVED"
    assert snapshot["audit_verification"]["valid"] is True
    assert snapshot["metrics"]["runs"]["succeeded"] >= 1


def test_brownfield_scenario_retries_policy_violation(
    client: TestClient, auth: dict[str, str]
) -> None:
    snapshot = launch(client, auth, "brownfield")
    assert snapshot["run"]["status"] == "SUCCEEDED"
    stages = {stage["stage_id"]: stage for stage in snapshot["stages"]}
    assert stages["implementation"]["attempt"] == 2
    assert stages["deploy"]["status"] == "SUCCEEDED"
    denials = [decision for decision in snapshot["decisions"] if decision["decision"] == "deny"]
    assert any(decision["topic"] == "SEC-001" for decision in denials)
    events = [event["event_type"] for event in snapshot["audit"]]
    assert "policy.violation" in events
    assert "rollback.completed" not in events
    assert snapshot["audit_verification"]["valid"] is True


def test_ambiguous_scenario_replans_with_approval(
    client: TestClient, auth: dict[str, str]
) -> None:
    snapshot = launch(client, auth, "ambiguous")
    assert snapshot["run"]["status"] == "SUCCEEDED"
    assert snapshot["run"]["replan_count"] == 1
    kinds = {(approval["kind"], approval["stage_id"]) for approval in snapshot["approvals"]}
    assert ("replan", "__replan__") in kinds
    assert ("stage", "clarification") in kinds
    assert ("stage", "release_readiness") in kinds
    assert all(approval["status"] == "APPROVED" for approval in snapshot["approvals"])
    versions = {artifact["name"]: artifact["version"] for artifact in snapshot["artifacts"]}
    assert versions["runbook.md"] == 2
    assert versions["test-report.md"] == 2
    events = [event["event_type"] for event in snapshot["audit"]]
    assert "replan.applied" in events
    assert snapshot["audit_verification"]["valid"] is True


def test_human_approval_flow_without_auto_approve(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = client.post(
        "/api/v1/runs", json={"scenario": "greenfield"}, headers=auth
    )
    snapshot = response.json()
    assert snapshot["run"]["status"] == "AWAITING_APPROVAL"
    run_id = snapshot["run"]["id"]
    pending = [approval for approval in snapshot["approvals"] if approval["status"] == "PENDING"]
    assert pending and pending[0]["stage_id"] == "publish"

    dashboard = client.get(f"/api/v1/runs/{run_id}", headers=auth)
    assert dashboard.status_code == 200

    approved = client.post(
        f"/api/v1/runs/{run_id}/approve",
        json={"stage_id": "publish", "rationale": "evidence reviewed"},
        headers=auth,
    )
    assert approved.status_code == 200
    final = approved.json()
    assert final["run"]["status"] == "SUCCEEDED"
    publish = [a for a in final["approvals"] if a["stage_id"] == "publish"][0]
    assert publish["rationale"] == "evidence reviewed"


def test_audit_and_artifact_endpoints(client: TestClient, auth: dict[str, str]) -> None:
    snapshot = launch(client, auth, "greenfield")
    run_id = snapshot["run"]["id"]
    audit = client.get(f"/api/v1/runs/{run_id}/audit", headers=auth)
    assert audit.status_code == 200
    assert audit.json()["verification"]["valid"] is True
    artifact_id = snapshot["artifacts"][0]["id"]
    artifact = client.get(f"/api/v1/artifacts/{artifact_id}", headers=auth)
    assert artifact.status_code == 200
    assert artifact.json()["content"]


def test_safe_stop_and_resume_endpoint(client: TestClient, auth: dict[str, str]) -> None:
    snapshot = client.post(
        "/api/v1/runs", json={"scenario": "greenfield"}, headers=auth
    ).json()
    run_id = snapshot["run"]["id"]
    stopped = client.post(
        f"/api/v1/runs/{run_id}/safe-stop",
        json={"reason": "operator pause"},
        headers=auth,
    )
    assert stopped.status_code == 200
    assert stopped.json()["run"]["status"] == "SAFE_STOPPED"
    resumed = client.post(f"/api/v1/runs/{run_id}/resume", headers=auth)
    assert resumed.status_code == 200
    assert resumed.json()["run"]["status"] == "AWAITING_APPROVAL"


def test_replan_endpoint(client: TestClient, auth: dict[str, str]) -> None:
    snapshot = launch(client, auth, "greenfield")
    run_id = snapshot["run"]["id"]
    response = client.post(
        f"/api/v1/runs/{run_id}/replan",
        json={"changed_stage": "requirements", "reason": "scope clarification"},
        headers=auth,
    )
    assert response.status_code == 200, response.text
    replanned = response.json()
    assert replanned["run"]["status"] == "AWAITING_APPROVAL"
    assert replanned["run"]["replan_count"] == 1
    approved = client.post(
        f"/api/v1/runs/{run_id}/approve",
        json={"rationale": "re-plan accepted"},
        headers=auth,
    )
    final = approved.json()
    assert final["run"]["status"] == "SUCCEEDED"
    versions = {artifact["name"]: artifact["version"] for artifact in final["artifacts"]}
    assert versions["architecture.md"] == 2


def test_metrics_and_policies_endpoints(client: TestClient, auth: dict[str, str]) -> None:
    launch(client, auth, "greenfield")
    summary = client.get("/api/v1/metrics/summary", headers=auth)
    assert summary.status_code == 200
    assert summary.json()["runs"]["success_rate"] == 1.0
    policies = client.get("/api/v1/policies", headers=auth)
    assert policies.status_code == 200
    assert any(rule["id"] == "SEC-001" for rule in policies.json()["rules"])
    prometheus = client.get("/metrics")
    assert "asdlc_run_success_rate 1.0" in prometheus.text


def test_auto_approve_is_forbidden_in_production(settings, auth: dict[str, str]) -> None:
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from agentic_sdlc.api import create_app

    production = create_app(replace(settings, environment="production"))
    client = TestClient(production)
    response = client.post(
        "/api/v1/runs",
        json={"scenario": "greenfield", "auto_approve": True},
        headers=auth,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "auto_approve_forbidden"
