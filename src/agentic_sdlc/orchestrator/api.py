"""HTTP surface for the agentic SDLC control plane."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from ..errors import PolicyDenied
from ..models import (
    ApprovalRequest,
    RejectRequest,
    ReplanRequest,
    RunLaunchRequest,
    SafeStopRequest,
)
from ..shortener.api import management_rate_limit, require_api_key

router = APIRouter(
    prefix="/api/v1",
    tags=["orchestrator"],
    dependencies=[Depends(require_api_key), Depends(management_rate_limit)],
)


def _engine(request: Request):
    return request.app.state.engine


def _settings(request: Request):
    return request.app.state.settings


@router.get("/scenarios")
def list_scenarios(request: Request) -> dict:
    return {
        "items": [scenario.summary() for scenario in request.app.state.scenarios.list()]
    }


@router.get("/policies")
def list_policies(request: Request) -> dict:
    return {"rules": _engine(request).policy.rules}


@router.get("/agents")
def list_agents(request: Request) -> dict:
    return {
        "items": [
            {"name": name}
            for name in _engine(request).registry.names()
        ]
    }


@router.post("/runs", status_code=201)
def launch_run(payload: RunLaunchRequest, request: Request) -> dict:
    if payload.auto_approve and _settings(request).environment == "production":
        raise PolicyDenied(
            "auto-approve is disabled in production; a human must approve each gate",
            code="auto_approve_forbidden",
        )
    snapshot = _engine(request).launch(
        payload.scenario,
        actor=request.headers.get("x-api-key", "operator"),
        requirement_override=payload.requirement_override,
        auto_approve=payload.auto_approve,
    )
    return snapshot


@router.get("/runs")
def list_runs(request: Request, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict:
    return {"items": _engine(request).list_runs(limit=limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: str, request: Request) -> dict:
    return _engine(request).snapshot(run_id)


@router.get("/runs/{run_id}/audit")
def get_audit(
    run_id: str, request: Request, limit: Annotated[int, Query(ge=1, le=2000)] = 500
) -> dict:
    _engine(request).snapshot(run_id)  # existence check
    events = request.app.state.store.list_audit(run_id=run_id, limit=limit)
    return {"items": events, "verification": request.app.state.store.verify_audit(run_id=run_id)}


@router.get("/runs/{run_id}/audit/verify")
def verify_audit(run_id: str, request: Request) -> dict:
    _engine(request).snapshot(run_id)
    return request.app.state.store.verify_audit(run_id=run_id)


@router.post("/runs/{run_id}/approve")
def approve(run_id: str, payload: ApprovalRequest, request: Request) -> dict:
    return _engine(request).approve(
        run_id,
        stage_id=payload.stage_id,
        actor=request.headers.get("x-api-key", "reviewer"),
        rationale=payload.rationale,
    )


@router.post("/runs/{run_id}/reject")
def reject(run_id: str, payload: RejectRequest, request: Request) -> dict:
    return _engine(request).reject(
        run_id,
        stage_id=payload.stage_id,
        actor=request.headers.get("x-api-key", "reviewer"),
        rationale=payload.rationale,
    )


@router.post("/runs/{run_id}/replan")
def replan(run_id: str, payload: ReplanRequest, request: Request) -> dict:
    return _engine(request).request_replan(
        run_id,
        changed_stage=payload.changed_stage,
        reason=payload.reason,
        targets=payload.targets,
        require_approval=bool(payload.require_approval),
        actor=request.headers.get("x-api-key", "operator"),
    )


@router.post("/runs/{run_id}/safe-stop")
def safe_stop(run_id: str, payload: SafeStopRequest, request: Request) -> dict:
    return _engine(request).safe_stop(
        run_id, reason=payload.reason, actor=request.headers.get("x-api-key", "operator")
    )


@router.post("/runs/{run_id}/resume")
def resume(run_id: str, request: Request) -> dict:
    return _engine(request).resume(
        run_id, actor=request.headers.get("x-api-key", "operator")
    )


@router.get("/approvals")
def list_approvals(
    request: Request,
    status: Annotated[str | None, Query(pattern="^(PENDING|APPROVED|REJECTED)$")] = None,
) -> dict:
    approvals = request.app.state.store.list_approvals(status=status)
    runs = {run["id"]: run for run in request.app.state.store.list_runs(limit=200)}
    for approval in approvals:
        run = runs.get(approval["run_id"], {})
        approval["run_name"] = run.get("name")
        approval["scenario"] = run.get("scenario")
    return {"items": approvals, "count": len(approvals)}


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, request: Request) -> dict:
    artifact = request.app.state.store.get_artifact(artifact_id)
    if not artifact:
        from ..errors import NotFound

        raise NotFound("artifact not found", code="artifact_not_found")
    return artifact


@router.get("/metrics/summary")
def metrics_summary(request: Request) -> dict:
    return _engine(request).metrics_summary()


@router.get("/metrics/prometheus", response_class=PlainTextResponse)
def metrics_prometheus(request: Request) -> str:
    return _engine(request).metrics.prometheus()


__all__ = ["router"]
