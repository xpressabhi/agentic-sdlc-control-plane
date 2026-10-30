# Orchestration Model

This document specifies how Conductor executes work: states, scheduling, gates, approvals,
retries, rollback, safe-stop, re-planning, policy and audit. It is the reference for reviewing
the governance behavior.

## 1. Run and stage lifecycle

### Run states

| State | Meaning | Terminal? |
|---|---|---|
| `PENDING` | Created, not started | no |
| `RUNNING` | Scheduler is executing | no |
| `AWAITING_APPROVAL` | Paused at a human gate or re-plan decision | no |
| `SUCCEEDED` | Every stage succeeded | yes |
| `FAILED` | A fatal stage failed, approval was rejected, or dependents were cancelled | yes |
| `ROLLED_BACK` | A failure triggered compensation and the run was reverted | yes |
| `SAFE_STOPPED` | Operator/budget stop; state preserved; resumable | no |
| `CANCELLED` | Cancelled outside the normal flow | yes |

### Stage states

```
PENDING ──▶ READY ──▶ RUNNING ──▶ SUCCEEDED
   ▲          │          │
   │          │          ├──▶ AWAITING_APPROVAL ──▶ READY (approved)
   │          │          │                        └──▶ CANCELLED (rejected)
   │          │          ├──▶ FAILED ──▶ ROLLED_BACK
   │          │          └──▶ CANCELLED
   └──────────┴── (re-plan resets to PENDING/STALE)
```

Every transition is written to `stage_runs` and emitted as an audit event. Approval state is
tracked separately so the same approval cannot silently authorize a re-executed stage.

## 2. Scheduling algorithm

At each iteration:

1. Check terminal/safe-stop state and budgets (wall clock, total attempts).
2. Handle a pending re-plan: awaiting approval → pause; approved → apply; rejected → safe-stop.
3. Handle stage approvals: approved → mark `READY`; rejected → cancel and fail; pending → pause.
4. Trigger an auto re-plan if its `after_stage` has succeeded and it has not run yet.
5. Select stages whose status is `PENDING` or `READY` and whose dependencies are all `SUCCEEDED`.
6. Sort ready stage ids deterministically, mark them `READY`, and execute the batch in a bounded
   `ThreadPoolExecutor`.
7. Inspect outcomes: any `AWAITING_APPROVAL` pauses the run; failures are handled by policy.
8. If no stage is ready: cancel stages blocked by failed dependencies; if everything is
   terminal, finalize; otherwise safe-stop with `scheduler_deadlock_detected`.

The join semantics are structural: a stage with multiple dependencies cannot become ready until
all of them succeed. No barrier primitive is needed.

## 3. Gates

| Gate | Kind | Pass condition |
|---|---|---|
| `always` | entry | unconditional |
| `artifact_present` | exit | at least one non-empty artifact |
| `requirements_normalized` | exit | requirements artifact exists and reports zero blocking ambiguities |
| `tests_passed` | exit | result metadata `tests_passed: true` |
| `release_ready` | exit | release metadata ready + green test evidence in the run |

Failed entry gates are terminal (a precondition cannot be repaired by retrying the same stage).
Failed exit gates consume retry budget because the agent may produce better output.

## 4. Approvals and human oversight

- Approval-required stages create a PENDING approval with context: stage, description,
  requirement excerpt and upstream artifact names.
- The run pauses in `AWAITING_APPROVAL`; no downstream stage can start.
- `approve` records actor, rationale and timestamp; `reject` cancels the stage and fails the run.
- Re-plan approvals carry `kind=replan`, targets, changed stage and reason.
- Auto-approval is a demo/test convenience and is blocked when `ASDLC_ENV=production`.
- Approval reuse is prevented across re-plans by clearing `stage_runs.approval_id` when a stage
  is reset.

## 5. Retry and fallback

```
attempt = 0
while attempt <= max_retries:
    attempt += 1
    result = agent.execute(context with attempt)
    if success and policy clean and exit gate passes:
        persist; SUCCEEDED
    if not result.retryable or attempt > max_retries:
        break
    sleep(min(max_delay, base * 2^(attempt-1)) * jitter)
FAILED
```

- Attempt failures are audited with error and retryability.
- Retry scheduling is audited with the chosen delay.
- `fallback_agent` is used from attempt 2 onward when configured.
- Policy denials can be retryable (secret/PII/design gap) or non-retryable (license, change freeze).
- `first_failure_at` is preserved for MTTR even after a later success.

## 6. Rollback and compensation

When a stage fails with `failure_policy: rollback`:

1. Compute succeeded stages in reverse topological order (dependents before dependencies).
2. Call the agent's `compensate(context)` for stages that declare a compensation handler.
3. Mark each compensated stage `ROLLED_BACK`; audit `compensation.executed`.
4. Mark the run `ROLLED_BACK` and audit `rollback.completed`.
5. If any compensation raises: audit `compensation.failed` and `SAFE_STOPPED`; a human decides.

Artifacts are preserved after rollback for forensics. Rollback is best-effort by design; the
safe-stop escalation prevents pretending a failed compensation succeeded.

## 7. Safe-stop and resume

Triggers:

- operator `safe-stop` with a reason
- wall-clock budget exceeded (`max_wall_seconds`)
- stage attempt budget exceeded (`max_stage_attempts`)
- compensation failure
- scheduler deadlock detection
- re-plan rejection

Effects: non-terminal stages are `CANCELLED`, the run is `SAFE_STOPPED`, the reason is recorded,
and pending approvals remain. `resume` resets cancelled stages to `PENDING`, clears the stop
reason, audits `run.resumed`, and re-enters the scheduler. Previously completed stages are not
re-run.

## 8. Re-planning

### Impact closure

```
targets = reverse_dependency_closure(changed_stage) - {changed_stage}   # default
```

Callers may pass explicit targets. Targets are validated against the workflow.

### Governed re-plan

- `require_approval=true` creates a `replan` approval with `stage_id=__replan__` and pauses.
- On approval, targets are reset to `PENDING/STALE`, `replan_count` increments, context stores
  `last_replan_reason`, and a `replan` decision + `replan.applied` audit event are recorded.
- Re-execution produces new artifact versions. Old versions remain queryable by ID; the latest
  artifact list excludes superseded versions.
- Re-planning a completed run reopens it (`RUNNING`) and reuses valid upstream artifacts.
- Auto re-plans are declared in scenario YAML (`auto_replan`), including whether approval is
  required.

## 9. Policy guardrails

Policy is evaluated before execution (stage tags/approval state) and after artifact generation.
Each rule returns an allow/deny decision with a severity, retryability and optional obligation.
All evaluated rules are audited; denials are persisted as `kind=policy` decisions. See the rule
table in [ARCHITECTURE.md](ARCHITECTURE.md#47-policy-guardrails).

Policy is intentionally not a general-purpose DSL: named, testable rules keep decisions
defensible and avoid a false sense of completeness.

## 10. Audit chain

Every event body is canonical JSON and appended under a lock:

```
prev = last event hash (or 64 zeros)
hash = SHA256(prev + "|" + canonical({run_id, stage_id, event_type, actor, payload, created_at}))
```

Verification walks the **global** chain in sequence order (run-scoped verification still walks
the global chain because concurrent runs interleave). It reports the first bad sequence number
and whether the failure is a link mismatch or a digest mismatch. Tests cover payload tampering
and record deletion.

## 11. Metrics definitions

| Metric | Definition |
|---|---|
| Success rate | succeeded terminal runs / all terminal runs |
| Retry frequency | retry schedules / total stage attempts (attempts include re-plan executions) |
| Rollback frequency | runs with any ROLLED_BACK stage / all runs |
| MTTR | mean(first failure → recovery or terminal end) across recovered and failed stages/runs |
| E2E p50/p95 | run start → end latency percentiles |
| Stage p50/p95 | stage start → end latency percentiles |
| Policy denials | persisted policy decisions with `decision=deny` |
| Replans | sum of `runs.replan_count` |
| Pending approvals | PENDING approval count |
| Audit events | total append-only events |

MTTR is an approximation in the prototype: it is computed from timestamps, not from a wall-clock
incident system. The definition is stable and testable.

## 12. Error and edge behavior

| Situation | Behavior |
|---|---|
| Unknown agent | stage fails with a recorded KeyError-derived error; run fails, no hang |
| Agent timeout | bounded future timeout, retryable error, attempt consumed |
| Agent returns no artifacts | exit gate fails, retries then fails |
| Approval rejected | stage cancelled, pending stages cancelled, run FAILED |
| Approval request lost | next scheduler iteration recreates it |
| Compensation fails | safe-stop; artifacts preserved; human required |
| Re-plan with no downstream impact | 409 conflict, no state change |
| Re-plan on an active run | 409 conflict; wait for settle |
| Two operators approve concurrently | conditional update `WHERE status='PENDING'`; second is a no-op |
| Scheduler deadlock | safe-stop, never an infinite loop |
| Audit tampering | verification fails at the altered sequence |
