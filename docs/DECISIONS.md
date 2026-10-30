# Architecture Decision Records

Short, defensible decisions with context, consequences and the alternative that was rejected.
These are the "why" behind the code.

---

## ADR-001 — Build a custom orchestration engine instead of adopting a workflow framework

**Status:** accepted

**Context.** The assignment's differentiator is governed, non-linear SDLC orchestration:
explicit DAG, gates, approvals, retries, rollback, safe-stop, policy, audit, metrics and
re-planning. Temporal, Prefect, Airflow and LangGraph provide pieces of this, but the governance
would live in framework configuration and adapters. Reviewers would have to infer behavior.

**Decision.** Implement a small, typed engine in `orchestrator/engine.py` with a durable SQLite
state store. The DAG, state machine, gate registry, policy engine, audit chain and metrics are
first-class code.

**Consequences.**
- Every control is readable, testable and explainable in the interview.
- We own the failure semantics (and their bugs).
- No crash recovery mid-stage or distributed execution.
- The engine's interfaces (`OrchestratorStore`, `Agent`, `PolicyEngine`, `AuditLog`) are the
  seams for durable execution later.

**Rejected.** Temporal (operationally heavy, hides orchestration), Airflow (batch-oriented,
awkward for interactive approvals), LangGraph (agent-loop oriented, weak SDLC governance).

---

## ADR-002 — Deterministic reference agents by default, optional LLM adapter

**Status:** accepted

**Context.** A live LLM demo is non-deterministic, needs credentials and network, costs money,
and cannot be reproduced by a reviewer. The assignment evaluates orchestration and engineering
judgment, not prompt quality.

**Decision.** Reference agents perform real work deterministically: generate a working module,
write tests, execute them in a subprocess, scan the actual codebase, and apply a migration to a
sandbox database. `ASDLC_AGENT_MODE=llm` wraps any agent with an OpenAI-compatible adapter;
transport or schema failures become retryable stage errors.

**Consequences.**
- `make demo` is reproducible offline and fast.
- Generated artifacts are real and executable, not prose.
- LLM mode is an integration point, not an evaluated model pipeline.
- Reviewer can inspect exactly what each agent does.

**Rejected.** Live LLM-only agents (non-reproducible, key-dependent), pure template mock agents
(no real validation evidence).

---

## ADR-003 — SQLite + in-process threads for the prototype

**Status:** accepted

**Context.** The prototype must run with one command on a reviewer's laptop. Postgres, Redis,
queues and object storage add setup friction and hide the orchestration logic.

**Decision.** SQLite in WAL mode with `BEGIN IMMEDIATE`, one short-lived connection per
operation, a bounded thread pool for parallel stages, and a local artifact workspace.

**Consequences.**
- Zero-infrastructure review; transactional guarantees are real.
- Single writer; no horizontal scale; no crash recovery mid-stage.
- Production path: Postgres + queue workers with leases + object storage. The store/engine
  interfaces isolate the change.

**Rejected.** Postgres + Celery (too much setup), file-only JSON state (no atomic transitions).

---

## ADR-004 — Append-only hash-chained audit log

**Status:** accepted

**Context.** The assignment requires audit-grade observability and traceability, not just
structured logs. A mutable table cannot demonstrate tamper evidence.

**Decision.** Each audit event commits to the previous event's SHA-256 digest over canonical
JSON. Verification walks the global chain and reports the first bad sequence. Every state
transition, gate, policy evaluation, approval, retry, rollback and re-plan is an event.

**Consequences.**
- Tampering and deletion are detectable (tested).
- Appends serialize under a lock; throughput is bounded by audit writes.
- Production path: WORM/append-only ledger with periodic external anchoring.
- Payloads are canonicalized, so whitespace-only changes are not distinguished; content changes
  are.

**Rejected.** Plain structured logs (no tamper evidence), database triggers (harder to verify
and reason about).

---

## ADR-005 — Policy as named rules with retryability and obligations

**Status:** accepted

**Context.** Security and change control must be enforcement, not warnings. Agents can produce
secrets, PII, unsafe designs or deploy without evidence.

**Decision.** A small rule engine evaluates named rules (`SEC-001`, `SEC-002`, `SEC-003`,
`LIC-001`, `CHG-001`, `CHG-002`, `REL-001`) against stage context and artifacts. Decisions carry
severity, retryability and obligations. Denials are persisted as decisions and audit events;
retryable denials consume the stage's retry budget.

**Consequences.**
- Brownfield demonstrates a real secret leak blocked and remediated on retry.
- Policy is testable and explainable; not a general-purpose DSL.
- Rules are code, so policy changes require review and deployment (appropriate for the
  prototype; production would externalize with versioned policy bundles).

**Rejected.** Prompt-only guardrails (unverifiable), OPA/Rego (extra runtime; worthwhile at
scale but overkill here).

---

## ADR-006 — Human approval for high-impact actions; auto-approve constrained

**Status:** accepted

**Context.** Controlled autonomy requires humans to own high-impact decisions. A demo that
requires manual clicking is hard to reproduce; a demo that auto-approves everything is not
governed.

**Decision.** Stages declare `requires_approval`. Approvals record actor, rationale and
timestamp. Auto-approval is a demo/test aid that records a `demo-reviewer` actor and is rejected
when `ASDLC_ENV=production`. Re-plans can also require approval.

**Consequences.**
- Runs pause visibly at gates; dashboard/CLI resume them.
- Production cannot silently auto-approve.
- Approval identity is the API key label in the prototype, not a real IdP.

**Rejected.** No approvals (fails the assignment), mandatory manual approval for every demo
(poor reviewer experience).

---

## ADR-007 — Re-plan by reverse-dependency closure over versioned artifacts

**Status:** accepted

**Context.** Upstream decisions change. Re-running the whole pipeline wastes work; ignoring
change produces stale artifacts. The assignment requires dynamic re-planning with lineage.

**Decision.** Compute the reverse-dependency closure of a changed stage, reset impacted stages to
`PENDING/STALE`, preserve prior artifacts as immutable versions, and re-execute. New artifacts
carry `parent_artifact_id` and increment `version`; the superseded row is marked
`superseded_by`. Re-plan can be approval-gated and every action is audited.

**Consequences.**
- Ambiguous scenario regenerates implementation/tests/docs as `v2`.
- Reviewers can compare versions and see why the change happened.
- Re-plan is not incremental at the file level; stages are the unit of reuse.

**Rejected.** Full pipeline rerun (simple, wasteful, loses lineage), artifact diff patching
(complex, premature).

---

## ADR-008 — Vanilla single-page dashboard, no build step

**Status:** accepted

**Context.** Operators need to see the DAG, approvals, artifacts, audit and metrics. A React
build pipeline adds tooling without improving the prototype's core evaluation.

**Decision.** A static `index.html`, one CSS file and one dependency-free JS file served by
FastAPI. It polls the REST API, renders the dependency levels, draws SVG edges, and exposes
approvals and metrics.

**Consequences.**
- `make serve` is the only setup; no node/npm.
- Polling, not push; no component ecosystem.
- Production path: React/Next or SSE/WebSocket for live updates.

**Rejected.** React + Vite (build overhead), server-rendered templates (harder rich interactions).

---

## ADR-009 — API-key auth and in-process rate limiting for the prototype

**Status:** accepted

**Context.** The prototype must demonstrate control boundaries without an identity provider or
Redis.

**Decision.** Management routes require `X-API-Key` (constant-time compare). A token-bucket
limiter protects management and redirect paths. The key is configurable; the dashboard defaults
to the dev key.

**Consequences.**
- Simple, testable auth and 401/429 behavior.
- Not production-grade identity, rotation or shared rate limits.
- Production path: OAuth2/OIDC, mTLS, Vault, Redis.

**Rejected.** No auth (unrealistic), full OIDC setup (scope creep).
