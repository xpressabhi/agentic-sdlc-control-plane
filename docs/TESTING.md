# Testing Approach

## 1. Strategy

The prototype uses a test pyramid with an emphasis on behavior that matters for governance:

1. **Unit tests** — pure logic: graph validation, URL safety, policy rules, audit hashing,
   codec, configuration.
2. **Integration tests** — the engine and API with real SQLite, synthetic agents and a real
   HTTP client. These cover retries, joins, approvals, rollback, safe-stop, re-plan and
   concurrency.
3. **End-to-end scenario tests** — the three real scenarios with the reference agents, including
   generated-code execution, policy remediation and approval flows.
4. **Static checks** — ruff (imports, bugs, modernization, line hygiene).

Agents are deterministic, so the suite has no model/network dependency. Each test gets an
isolated temporary SQLite database and artifact directory.

```bash
make test          # 100 tests
make cov           # coverage report
make verify        # ruff + tests
```

## 2. Coverage

Latest run: **100 tests passing, 90% statement coverage** across 2,459 statements.

| Area | Coverage | Notes |
|---|---:|---|
| `orchestrator/policy.py` | 100% | every rule has allow/deny tests |
| `shortener/codec.py` | 100% | round-trip, invalid input, reserved aliases |
| `orchestrator/audit.py` | 96% | tamper and deletion detection |
| `orchestrator/engine.py` | 84% | all core paths; some operator edge branches remain |
| `orchestrator/metrics.py` | 97% | summary + Prometheus |
| `shortener/api.py` | 97% | auth, create, redirect, analytics, delete, rate limit |
| `orchestrator/store.py` | 89% | persistence and transitions |
| CLI | 89% | scenario list, run/status/approve, demo, metrics, audit |

The remaining gaps are mostly defensive branches (invalid settings parsing, malformed API
payloads, rare operator error paths). They are intentionally low-risk; adding tests there would
increase coverage without increasing confidence.

## 3. Test inventory

### Unit tests

| File | Focus |
|---|---|
| `test_graph.py` | topological levels, cycle/duplicate/missing-dep rejection, impact closure, compensation order |
| `test_policy.py` | secrets, PII, GPL, SSRF design control, production approval/test evidence, change freeze, rollback docs |
| `test_audit.py` | append/link integrity, payload tamper, record deletion |
| `test_security.py` | URL rejection matrix (loopback, private, IPv6, integer/hex, credentials, control chars), alias/codec rules, visitor hashing |
| `test_config.py` | environment parsing, derived run directory, UTC format |
| `test_llm.py` | adapter fallback, JSON parsing, malformed response, fenced JSON |

### Integration tests

| File | Focus |
|---|---|
| `test_shortener.py` | create/resolve, aliases, idempotency, TTL, soft delete, analytics, persistence |
| `test_api.py` | auth, 201/302/204/404/409/429, idempotency header, analytics, security headers, metrics |
| `test_engine.py` | linear run, parallel peak = 2, join sync, retry, policy retry, approval pause/resume/reject, rollback compensation, safe-stop/resume, re-plan with lineage, re-plan approval, wall-clock budget, unknown agent |
| `test_scenarios.py` | greenfield, brownfield, ambiguous end-to-end; human approval flow; audit/artifact endpoints; safe-stop; re-plan; metrics; production auto-approve denial |
| `test_cli.py` | scenario listing, run → pause → status → approve, demo, metrics, audit verification, replan |

### What the tests prove

- **DAG correctness:** cycles and unknown dependencies are rejected before execution.
- **Parallelism and synchronization:** a test measures two slow branches running concurrently
  (peak active = 2) and a join only running afterwards.
- **Approval governance:** a run pauses, resume works, rejection fails the run, and the actor and
  rationale are recorded.
- **Policy enforcement:** a secret-bearing artifact is denied, the stage retries, and attempt 2
  succeeds — with a persisted denial decision.
- **Rollback:** compensation runs in reverse topological order; success marks stages
  `ROLLED_BACK`; a failed compensation escalates to safe-stop.
- **Safe-stop:** state is preserved and a resume returns to the approval gate.
- **Re-plan lineage:** downstream stages re-run, artifact versions become `v2`, and re-plan
  decisions are recorded; re-plan can require approval.
- **Audit integrity:** modifying a payload or deleting an event fails verification at the right
  sequence number.
- **Generated-code validation:** the validation agent executes generated tests in a subprocess
  and only passes when the process exits 0.
- **API safety:** all management routes require the API key; the redirect route is public;
  oversized/unsafe URLs are rejected; rate limiting returns 429 with `Retry-After`.
- **Production guardrail:** `auto_approve` is rejected when the environment is production.

## 4. Determinism and isolation

- Every test uses `tmp_path` for the database and artifact workspace.
- The reference agents contain no network calls. The LLM adapter is tested with a mocked
  `urlopen`.
- Retry delays in tests are 1ms, keeping the suite fast.
- Audit chain verification is part of every scenario test.
- No test order dependency; the suite passes in random order (no shared state).

## 5. Running specific layers

```bash
uv run pytest tests/test_engine.py -q
uv run pytest tests/test_scenarios.py -q
uv run pytest -k "policy or audit"
uv run pytest --cov=agentic_sdlc --cov-report=html
```

## 6. Performance and load testing (not included)

The prototype validates correctness and governance, not throughput. A production readiness
plan would add:

- redirect load test (k6/Locust) against p95 < 50 ms
- analytics query latency under 1M events
- concurrent run execution with Postgres and worker leases
- audit chain verification at 10M+ events with periodic checkpoints
- chaos tests: worker death mid-stage, database failover, queue duplication

These are documented in [RISKS.md](RISKS.md) as open validation work.

## 7. Known test gaps

- No browser automation is committed. The dashboard was verified manually via Chrome
  screenshots; API behavior underneath it is fully tested.
- The LLM adapter has unit tests but no live-provider contract test.
- No property-based tests yet for graph closure or URL parsing (candidate for Hypothesis).
- MTTR is timestamp-derived; no incident-management integration.
- Rollback tests use synthetic agents; no filesystem/DB compensation scenario.

## 8. Test commands in CI

`.github/workflows/ci.yml` runs on Python 3.12:

```bash
uv sync --all-extras
uv run ruff check src tests
uv run pytest -q
```

The workflow is deliberately fast (no services, no secrets) so every push is verifiable.
