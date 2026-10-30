# Agentic SDLC Control Plane + URL Shortener — Design

**Date:** 2026-10-30
**Status:** Approved
**Author:** Engineering prototype for the Agentic Software Engineer assignment

## 1. Problem

Build a runnable URL-shortener service **and** an agentic orchestration layer that takes a
requirement through the full SDLC — requirements, design, implementation, testing,
documentation, release readiness — with governance, human oversight, resilience, and
audit-grade observability. The orchestration layer is the primary differentiator; the URL
shortener is the concrete engineering output it produces and validates.

## 2. Goals

- A working, reviewable prototype that runs end-to-end with one command and no external services.
- Explicit DAG orchestration: dependencies, parallel branches, join synchronization, gates.
- Human approval checkpoints for high-impact actions (scope normalization, release, re-plan).
- Bounded retries, fallback, compensation/rollback, safe-stop, budgets.
- Declarative policy guardrails (secrets, PII, SSRF design controls, production change control).
- Audit-grade traceability: append-only hash-chained event log, versioned artifacts, decision lineage.
- Reliability metrics: success rate, retry/rollback frequency, MTTR, end-to-end latency.
- Dynamic re-planning with impact analysis and governance.
- Three scenarios: greenfield, brownfield, ambiguous.
- Production-quality URL shortener: secure URL validation, collision-safe codes, TTL, analytics,
  idempotency, rate limiting, authentication, health/readiness, metrics.

## 3. Non-goals

- Horizontal scale, multi-region deployment, or distributed durable execution.
- A production identity provider; API-key auth is a prototype control.
- Real LLM calls by default. Reference agents are deterministic and do real work; an optional
  LLM adapter demonstrates how model-backed agents plug in.
- Full OpenAPI-generated clients or a React build pipeline.

## 4. Architecture

### 4.1 Components

```
┌─────────────────────────────────────────────────────────────────────┐
│  FastAPI application                                                │
│                                                                     │
│  ┌────────────────────┐        ┌──────────────────────────────────┐ │
│  │ URL Shortener      │        │ Agentic SDLC Control Plane       │ │
│  │ - create/resolve   │        │ - workflow registry              │ │
│  │ - analytics        │        │ - DAG scheduler                  │ │
│  │ - rate limiting    │        │ - gates + policy engine          │ │
│  │ - TTL / soft delete│        │ - approvals + safe-stop          │ │
│  └─────────┬──────────┘        │ - retries / rollback / re-plan   │ │
│            │                   │ - audit chain + metrics          │ │
│            │                   └──────────────┬───────────────────┘ │
│            └──────────────┬───────────────────┘                     │
│                           ▼                                         │
│                  SQLite (WAL) + artifact workspace                  │
└─────────────────────────────────────────────────────────────────────┘
                           │
              ┌────────────┴─────────────┐
              ▼                          ▼
        REST API + CLI            Single-page dashboard
```

### 4.2 Orchestration model

A **workflow** is a validated DAG of **stages**. Each stage declares an agent, dependencies,
entry/exit gates, approval requirement, retry budget, failure policy, compensation handler,
and policy tags.

- **Scheduler:** repeatedly selects stages whose dependencies have all succeeded and executes
  ready stages in parallel (bounded thread pool). Join stages synchronize naturally because
  they become ready only when every dependency succeeds. Stage start order is deterministic.
- **State machine:** `PENDING → READY → RUNNING → AWAITING_APPROVAL → SUCCEEDED | FAILED |
  ROLLED_BACK | SAFE_STOPPED | CANCELLED`.
- **Gates:** entry gates validate preconditions; exit gates validate artifacts/quality evidence.
  A failed exit gate consumes a retry; a failed entry gate is terminal.
- **Context:** every stage receives the scenario requirement, all upstream artifacts (latest
  versions), decisions, attempt number, and re-plan reason. Artifacts are content-addressed
  and versioned; decisions record rationale and actor. This preserves decision lineage.
- **Approvals:** approval-required stages create an approval record and pause the run in
  `AWAITING_APPROVAL`. Approve resumes; reject cancels the stage and fails the run (or triggers
  safe-stop). Every decision is audited with actor and rationale.
- **Resilience:** bounded exponential backoff with jitter, retry budget, non-retryable error
  classification, optional fallback agent, per-stage timeout, run wall-clock/attempt budgets,
  global safe-stop, reverse-order compensation on rollback. Compensation failure escalates to
  `SAFE_STOPPED`.
- **Re-planning:** a changed artifact/stage triggers a reverse-dependency impact closure.
  Impacted stages become `PENDING/STALE`; prior artifacts remain as lineage. Re-plan can require
  approval. Each re-execution produces a new artifact version.
- **Policy:** declarative rule evaluation before execution and after artifact generation.
  Denials are persisted as decisions and audited. Retryable violations consume retry budget.
- **Audit:** append-only event log where each record includes the previous record's hash and a
  SHA-256 digest over canonical content. A verification endpoint walks and re-computes the chain.
- **Metrics:** computed from run/stage/audit tables: success rate, retry frequency, rollback
  frequency, MTTR, end-to-end latency percentiles, stage latency percentiles.

### 4.3 URL shortener

- `POST /api/v1/links` — create; validates http/https, blocks private/loopback/link-local
  targets and dangerous schemes, enforces length and TTL bounds, supports idempotency keys and
  custom aliases, reserves codes transactionally with collision retry.
- `GET /{code}` — 302 redirect; records click metadata (hashed visitor, referrer, UA, country),
  increments counters transactionally.
- `GET /api/v1/links/{code}` — metadata; `DELETE` soft-deletes.
- `GET /api/v1/links/{code}/analytics` — totals, unique visitors, daily series, top referrers.
- `GET /healthz`, `/readyz`, `/metrics`.
- Reliability/security: API-key auth on management routes, token-bucket rate limiting,
  security headers, TTL expiry, idempotent creation, WAL + busy timeout, reserved aliases.

### 4.4 Data model

`links`, `click_events` for the shortener. `workflows`, `runs`, `stage_runs`, `artifacts`,
`decisions`, `approvals`, `audit_events` for orchestration. All timestamps UTC ISO-8601.
Artifacts store content plus SHA-256 hash, version, parent artifact, and supersession link.

### 4.5 API surface

- Shortener routes as above.
- `GET /api/v1/scenarios`, `POST /api/v1/runs`, `GET /api/v1/runs`, `GET /api/v1/runs/{id}`
- `POST /api/v1/runs/{id}/approve|reject|replan|safe-stop|resume`
- `GET /api/v1/runs/{id}/audit`, `GET /api/v1/runs/{id}/audit/verify`
- `GET /api/v1/approvals`, `GET /api/v1/artifacts/{id}`, `GET /api/v1/metrics/summary`
- `GET /metrics` (Prometheus text)

### 4.6 Interface

- **CLI:** `serve`, `scenarios`, `run`, `status`, `approve`, `reject`, `replan`, `safe-stop`,
  `audit-verify`, `metrics`, `demo`.
- **Dashboard:** run list, live DAG by topological level, stage detail drawer with artifacts,
  decisions, policy and audit trail, approvals inbox, metrics panel, scenario launcher.

## 5. Scenarios

1. **Greenfield:** build URL shortener v1. Requirement → architecture + threat model (parallel)
   → implementation + test plan (parallel) → validation join → documentation → release approval
   → release. Demonstrates parallel paths, join sync, gate enforcement, human release approval.
2. **Brownfield:** add aliases, analytics, TTL. Impact analysis → migration + API contract review
   (parallel) → implementation (first attempt violates the secrets policy, retries clean) →
   regression tests → docs → deploy approval. Demonstrates codebase reasoning, policy-enforced
   retry, change control.
3. **Ambiguous:** "make links safer and give us insights." Ambiguity intake produces questions
   and assumptions; a human approves the normalized interpretation; design proceeds; a scope
   change triggers a governed re-plan of downstream stages with artifact lineage; release approval
   completes the run.

## 6. Error handling and safety

- Validation errors return 4xx with explicit codes; domain conflicts return 409; auth 401;
  rate limit 429 with `Retry-After`.
- Orchestrator stage failures follow declared policy; unexpected exceptions are classified and
  audited; the run never silently hangs (deadlock detection → safe-stop).
- Safe-stop preserves state and permits an explicit resume.
- Rollback is best-effort by design; failed compensation escalates to safe-stop for human action.
- Secrets/PII are scanned in generated code artifacts; production deploy requires approval,
  green test evidence, and an open change window.

## 7. Testing strategy

- Unit: graph validation/topology/impact closure, URL validation, code generation, policy rules,
  audit chain, retry classification, token bucket.
- Integration: shortener API, analytics, auth, TTL; orchestrator engine with synthetic agents
  covering success, parallel join, retry, policy denial, approval pause/resume/reject, rollback,
  safe-stop, re-plan, budgets.
- End-to-end: all three scenarios with auto-approval; assert terminal success, artifacts,
  retry/re-plan evidence, valid audit chain, metrics.
- Static: ruff. CI workflow runs lint + tests.

## 8. Trade-offs

- **SQLite + in-process threads** instead of Postgres/queue workers: zero-infra, transparent,
  reviewable; not horizontally scalable and not crash-durable mid-stage. Production path:
  Postgres, transactional outbox, queue-backed workers, object-store artifacts, idempotent stage
  leases. The store/engine interfaces are the seams.
- **Deterministic reference agents** instead of live LLM calls: reproducible demos and tests;
  optional LLM adapter shows the integration point. Live LLM output would need evaluation harnesses.
- **API-key auth** instead of OAuth2: sufficient to demonstrate control boundaries; documented
  production replacement.
- **Best-effort rollback** instead of distributed sagas: appropriate for a single-node prototype;
  compensation failure escalates to human safe-stop.

## 9. Acceptance criteria

- `make demo` runs a complete scenario and prints evidence.
- All tests pass; `make test` is the single command.
- All three scenarios complete with auto-approval, and pause correctly without it.
- Audit verification passes for every run and fails on tampering.
- Metrics endpoint reports success rate, retry/rollback frequency, MTTR, latency.
- Documentation maps every assignment requirement to code and evidence.
