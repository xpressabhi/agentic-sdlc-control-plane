"""Reliability and delivery metrics computed from immutable run history."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..db import Database

TERMINAL = {"SUCCEEDED", "FAILED", "ROLLED_BACK", "CANCELLED"}
FAILED = {"FAILED", "ROLLED_BACK"}


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds(start: str | None, end: str | None) -> float | None:
    left, right = _parse(start), _parse(end)
    if left is None or right is None:
        return None
    return max(0.0, (right - left).total_seconds())


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


class MetricsService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def summary(self) -> dict[str, Any]:
        with self.db.read() as conn:
            runs = [dict(row) for row in conn.execute("SELECT * FROM runs").fetchall()]
            stages = [
                dict(row) for row in conn.execute("SELECT * FROM stage_runs").fetchall()
            ]
            pending_approvals = conn.execute(
                "SELECT COUNT(*) AS n FROM approvals WHERE status = 'PENDING'"
            ).fetchone()["n"]
            policy_violations = conn.execute(
                "SELECT COUNT(*) AS n FROM decisions WHERE kind = 'policy' AND decision = 'deny'"
            ).fetchone()["n"]
            audit_events = conn.execute("SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"]
            links = conn.execute("SELECT COUNT(*) AS n FROM links").fetchone()["n"]
            clicks = conn.execute("SELECT COUNT(*) AS n FROM click_events").fetchone()["n"]

        status_counts: dict[str, int] = {}
        for run in runs:
            status_counts[run["status"]] = status_counts.get(run["status"], 0) + 1
        terminal_runs = [run for run in runs if run["status"] in TERMINAL]
        succeeded = status_counts.get("SUCCEEDED", 0)
        success_rate = round(succeeded / len(terminal_runs), 4) if terminal_runs else None

        total_attempts = sum(int(stage["total_attempts"] or 0) for stage in stages)
        retries = sum(int(stage["retry_count"] or 0) for stage in stages)
        retry_frequency = round(retries / total_attempts, 4) if total_attempts else 0.0

        rollback_runs = {
            run["id"]
            for run in runs
            if run["status"] == "ROLLED_BACK"
            or any(
                stage["run_id"] == run["id"] and stage["status"] == "ROLLED_BACK"
                for stage in stages
            )
        }
        rollback_frequency = (
            round(len(rollback_runs) / len(runs), 4) if runs else 0.0
        )

        recovery_times: list[float] = []
        for stage in stages:
            if stage["status"] == "SUCCEEDED" and stage["first_failure_at"]:
                seconds = _seconds(stage["first_failure_at"], stage["ended_at"])
                if seconds is not None:
                    recovery_times.append(seconds)
        for run in runs:
            if run["status"] in FAILED and run["ended_at"]:
                failures = [
                    stage["first_failure_at"]
                    for stage in stages
                    if stage["run_id"] == run["id"] and stage["first_failure_at"]
                ]
                if failures:
                    seconds = _seconds(min(failures), run["ended_at"])
                    if seconds is not None:
                        recovery_times.append(seconds)

        run_latencies = [
            seconds
            for run in runs
            if (seconds := _seconds(run["started_at"], run["ended_at"])) is not None
        ]
        stage_latencies = [
            seconds
            for stage in stages
            if (seconds := _seconds(stage["started_at"], stage["ended_at"])) is not None
        ]

        return {
            "runs": {
                "total": len(runs),
                "terminal": len(terminal_runs),
                "succeeded": succeeded,
                "failed": status_counts.get("FAILED", 0),
                "rolled_back": status_counts.get("ROLLED_BACK", 0),
                "awaiting_approval": status_counts.get("AWAITING_APPROVAL", 0),
                "running": status_counts.get("RUNNING", 0),
                "by_status": status_counts,
                "success_rate": success_rate,
            },
            "stages": {
                "total": len(stages),
                "total_attempts": total_attempts,
                "retries": retries,
                "retry_frequency": retry_frequency,
                "latency_p50_seconds": _percentile(stage_latencies, 0.50),
                "latency_p95_seconds": _percentile(stage_latencies, 0.95),
            },
            "reliability": {
                "rollback_runs": len(rollback_runs),
                "rollback_frequency": rollback_frequency,
                "mttr_seconds": _mean(recovery_times),
                "e2e_latency_p50_seconds": _percentile(run_latencies, 0.50),
                "e2e_latency_p95_seconds": _percentile(run_latencies, 0.95),
                "replans": sum(int(run["replan_count"] or 0) for run in runs),
                "policy_violations": int(policy_violations),
                "audit_events": int(audit_events),
                "pending_approvals": int(pending_approvals),
            },
            "shortener": {"links": int(links), "clicks": int(clicks)},
        }

    def prometheus(self) -> str:
        summary = self.summary()
        runs = summary["runs"]
        stages = summary["stages"]
        reliability = summary["reliability"]

        def value(number: float | int | None) -> str:
            return "NaN" if number is None else str(number)

        lines = [
            "# HELP asdlc_runs_total Total orchestration runs.",
            "# TYPE asdlc_runs_total gauge",
            f"asdlc_runs_total {runs['total']}",
            "# HELP asdlc_runs_succeeded_total Successful runs.",
            "# TYPE asdlc_runs_succeeded_total gauge",
            f"asdlc_runs_succeeded_total {runs['succeeded']}",
            "# HELP asdlc_run_success_rate Successful runs divided by terminal runs.",
            "# TYPE asdlc_run_success_rate gauge",
            f"asdlc_run_success_rate {value(runs['success_rate'])}",
            "# HELP asdlc_stage_retry_frequency Retried attempts divided by all attempts.",
            "# TYPE asdlc_stage_retry_frequency gauge",
            f"asdlc_stage_retry_frequency {stages['retry_frequency']}",
            "# HELP asdlc_run_rollback_frequency Runs with rollback divided by all runs.",
            "# TYPE asdlc_run_rollback_frequency gauge",
            f"asdlc_run_rollback_frequency {reliability['rollback_frequency']}",
            "# HELP asdlc_mttr_seconds Mean time to recovery in seconds.",
            "# TYPE asdlc_mttr_seconds gauge",
            f"asdlc_mttr_seconds {value(reliability['mttr_seconds'])}",
            "# HELP asdlc_run_latency_p95_seconds End-to-end run p95 latency.",
            "# TYPE asdlc_run_latency_p95_seconds gauge",
            f"asdlc_run_latency_p95_seconds {value(reliability['e2e_latency_p95_seconds'])}",
            "# HELP asdlc_pending_approvals Approval requests awaiting a human.",
            "# TYPE asdlc_pending_approvals gauge",
            f"asdlc_pending_approvals {reliability['pending_approvals']}",
            "# HELP asdlc_policy_violations_total Persisted policy denials.",
            "# TYPE asdlc_policy_violations_total counter",
            f"asdlc_policy_violations_total {reliability['policy_violations']}",
            "# HELP asdlc_short_links_total Short links created.",
            "# TYPE asdlc_short_links_total counter",
            f"asdlc_short_links_total {summary['shortener']['links']}",
            "# HELP asdlc_clicks_total Redirect clicks recorded.",
            "# TYPE asdlc_clicks_total counter",
            f"asdlc_clicks_total {summary['shortener']['clicks']}",
        ]
        return "\n".join(lines) + "\n"
