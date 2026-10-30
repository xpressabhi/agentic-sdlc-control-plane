"""Developer CLI for the control plane."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import Settings
from .db import Database
from .orchestrator.engine import OrchestrationEngine
from .orchestrator.scenarios import ScenarioRegistry
from .orchestrator.store import OrchestratorStore


def _services() -> tuple[Settings, OrchestratorStore, OrchestrationEngine]:
    settings = Settings.load()
    settings.ensure_dirs()
    db = Database(settings.db_path)
    store = OrchestratorStore(db)
    scenarios = ScenarioRegistry.load()
    engine = OrchestrationEngine(store, settings, scenarios=scenarios)
    return settings, store, engine


def _status_icon(status: str) -> str:
    return {
        "SUCCEEDED": "PASS",
        "FAILED": "FAIL",
        "ROLLED_BACK": "ROLL",
        "SAFE_STOPPED": "STOP",
        "AWAITING_APPROVAL": "WAIT",
        "RUNNING": "RUN ",
        "CANCELLED": "CANC",
        "SKIPPED": "SKIP",
        "PENDING": "PEND",
        "READY": "READY",
    }.get(status, status[:4])


def print_snapshot(snapshot: dict[str, Any]) -> None:
    run = snapshot["run"]
    print(f"\nRun     : {run['id']}")
    print(f"Scenario: {run['scenario']} — {run['name']}")
    print(f"Status  : {run['status']}   mode={run['mode']}   replans={run['replan_count']}")
    if run.get("error"):
        print(f"Error   : {run['error']}")
    if run.get("safe_stop_reason"):
        print(f"Stopped : {run['safe_stop_reason']}")
    print("\nStages")
    print(f"  {'STAGE':22} {'STATUS':10} ATT  ARTIFACTS  APPROVAL")
    for stage in snapshot["stages"]:
        approval = next(
            (item for item in snapshot["approvals"] if item["stage_id"] == stage["stage_id"]),
            None,
        )
        print(
            f"  {stage['stage_id']:22} {_status_icon(stage['status']):10} "
            f"{stage['attempt']:<4} {len(stage['artifacts']):<10} "
            f"{approval['status'] if approval else '-'}"
        )
    print("\nArtifacts")
    for artifact in snapshot["artifacts"]:
        print(
            f"  v{artifact['version']}  {artifact['name']:44} "
            f"{artifact['kind']:10} {artifact['content_hash'][:12]}"
        )
    verifier = snapshot["audit_verification"]
    print(
        f"\nAudit   : {'valid' if verifier['valid'] else 'INVALID'} "
        f"({verifier['checked']} events) — {verifier['message']}"
    )
    print(f"Metrics : {json.dumps(snapshot['metrics']['reliability'], sort_keys=True)}")
    pending = [a for a in snapshot["approvals"] if a["status"] == "PENDING"]
    if pending:
        print("\nPending approvals")
        for approval in pending:
            print(
                f"  {approval['id'][:8]}  {approval['stage_id']:22} "
                f"kind={approval['kind']}  requested={approval['requested_at']}"
            )
        print(f"  Approve: asdlc approve {run['id']} --rationale '...'")
    print()


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    if args.reload:
        uvicorn.run(
            "agentic_sdlc.api:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
        )
    else:
        uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


def cmd_scenarios(args: argparse.Namespace) -> int:
    settings, _, engine = _services()
    print(f"Database: {settings.db_path}")
    for scenario in engine.scenarios.list():
        print(f"\n{scenario.key:12} {scenario.name}")
        print(f"  {scenario.intent}")
        print("  stages: " + " -> ".join(stage.id for stage in scenario.workflow.stages))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    _, _, engine = _services()
    snapshot = engine.launch(
        args.scenario,
        actor="cli",
        requirement_override=args.requirement,
        auto_approve=args.auto_approve,
    )
    print_snapshot(snapshot)
    if snapshot["run"]["status"] == "AWAITING_APPROVAL":
        print("Run is paused at a human approval gate. Use the dashboard or:")
        print(f"  asdlc approve {snapshot['run']['id']} --rationale 'approved'")
    return 0 if snapshot["run"]["status"] in {"SUCCEEDED", "AWAITING_APPROVAL"} else 1


def cmd_status(args: argparse.Namespace) -> int:
    _, _, engine = _services()
    print_snapshot(engine.snapshot(args.run_id))
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    _, _, engine = _services()
    snapshot = engine.approve(
        args.run_id,
        stage_id=args.stage,
        actor="reviewer",
        rationale=args.rationale,
    )
    print_snapshot(snapshot)
    return 0 if snapshot["run"]["status"] == "SUCCEEDED" else 1


def cmd_reject(args: argparse.Namespace) -> int:
    _, _, engine = _services()
    snapshot = engine.reject(
        args.run_id,
        stage_id=args.stage,
        actor="reviewer",
        rationale=args.rationale,
    )
    print_snapshot(snapshot)
    return 0


def cmd_replan(args: argparse.Namespace) -> int:
    _, _, engine = _services()
    targets = args.targets.split(",") if args.targets else None
    snapshot = engine.request_replan(
        args.run_id,
        changed_stage=args.stage,
        reason=args.reason,
        targets=targets,
        require_approval=args.require_approval,
        actor="cli",
    )
    print_snapshot(snapshot)
    return 0


def cmd_safe_stop(args: argparse.Namespace) -> int:
    _, _, engine = _services()
    print_snapshot(engine.safe_stop(args.run_id, reason=args.reason, actor="cli"))
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    _, _, engine = _services()
    print_snapshot(engine.resume(args.run_id, actor="cli"))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    _, store, _ = _services()
    result = store.verify_audit(run_id=args.run_id)
    print(json.dumps(result, indent=2))
    if args.run_id:
        for event in store.list_audit(run_id=args.run_id, limit=args.limit):
            payload = json.dumps(event["payload"], separators=(",", ":"))
            print(f"{event['seq']:>4} {event['created_at']} {event['event_type']:24} {payload[:120]}")
    return 0 if result["valid"] else 1


def cmd_metrics(args: argparse.Namespace) -> int:
    _, _, engine = _services()
    print(json.dumps(engine.metrics_summary(), indent=2))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    _, _, engine = _services()
    keys = ["greenfield", "brownfield", "ambiguous"] if args.all else ["greenfield"]
    failed = False
    for key in keys:
        print(f"\n{'=' * 78}\nLaunching scenario: {key}\n{'=' * 78}")
        snapshot = engine.launch(key, actor="demo", auto_approve=True)
        print_snapshot(snapshot)
        failed = failed or snapshot["run"]["status"] != "SUCCEEDED"
    print("=" * 78)
    print("Demo complete." if not failed else "One or more scenarios did not succeed.")
    print("Dashboard: uv run asdlc serve  ->  http://localhost:8000")
    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asdlc",
        description="Agentic SDLC control plane and URL shortener",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the API and dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=cmd_serve)

    scenarios = sub.add_parser("scenarios", help="list scenarios and stages")
    scenarios.set_defaults(func=cmd_scenarios)

    run = sub.add_parser("run", help="launch a scenario")
    run.add_argument("scenario", choices=["greenfield", "brownfield", "ambiguous"])
    run.add_argument("--auto-approve", action="store_true")
    run.add_argument("--requirement", default=None)
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="show a run snapshot")
    status.add_argument("run_id")
    status.set_defaults(func=cmd_status)

    approve = sub.add_parser("approve", help="approve a pending gate")
    approve.add_argument("run_id")
    approve.add_argument("--stage", default=None)
    approve.add_argument("--rationale", default="Approved after review")
    approve.set_defaults(func=cmd_approve)

    reject = sub.add_parser("reject", help="reject a pending gate")
    reject.add_argument("run_id")
    reject.add_argument("--stage", default=None)
    reject.add_argument("--rationale", default="Rejected after review")
    reject.set_defaults(func=cmd_reject)

    replan = sub.add_parser("replan", help="re-plan downstream stages")
    replan.add_argument("run_id")
    replan.add_argument("--stage", required=True, help="changed upstream stage")
    replan.add_argument("--reason", required=True)
    replan.add_argument("--targets", default=None, help="comma-separated stage ids")
    replan.add_argument("--require-approval", action="store_true")
    replan.set_defaults(func=cmd_replan)

    safe_stop = sub.add_parser("safe-stop", help="stop a run safely")
    safe_stop.add_argument("run_id")
    safe_stop.add_argument("--reason", default="operator requested")
    safe_stop.set_defaults(func=cmd_safe_stop)

    resume = sub.add_parser("resume", help="resume a safe-stopped run")
    resume.add_argument("run_id")
    resume.set_defaults(func=cmd_resume)

    audit = sub.add_parser("audit-verify", help="verify the audit hash chain")
    audit.add_argument("run_id", nargs="?")
    audit.add_argument("--limit", type=int, default=100)
    audit.set_defaults(func=cmd_audit)

    metrics = sub.add_parser("metrics", help="print reliability metrics")
    metrics.set_defaults(func=cmd_metrics)

    demo = sub.add_parser("demo", help="run the demo scenarios end-to-end")
    demo.add_argument("--all", action="store_true", help="run all three scenarios")
    demo.set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
