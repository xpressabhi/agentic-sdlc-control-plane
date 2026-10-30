# Risks, Trade-offs and Validation

## 1. Risk register

| ID | Risk | Likelihood | Impact | Mitigation | Validation | Residual |
|---|---|---|---|---|---|---|
| R1 | Agent generates insecure code (secrets, PII) | Medium | High | `SEC-001`/`SEC-002` scan code/sql/test artifacts; retryable denial; audit | brownfield scenario leaks a key on attempt 1, denied, attempt 2 clean | Novel secret formats not matched by regex |
| R2 | Design omits SSRF/private-address controls | Medium | High | `SEC-003` requires documented controls on architecture artifacts | policy unit tests + greenfield run | Prose can claim controls not implemented |
| R3 | Production deploy without human approval | Low | Critical | `CHG-001` denies unless an APPROVED approval exists; `requires_approval` gate | API + engine tests; auto-approve forbidden in production | Forged API key in prototype |
| R4 | Deploy during change freeze | Low | High | `CHG-002` denies when `ASDLC_CHANGE_FREEZE=1` | policy test | Freeze flag is process config, not a calendar service |
| R5 | Secret leaked in release artifacts | Low | High | Scan runs after every code/test/sql artifact; denials persisted | brownfield evidence | Markdown/docs are not scanned for secrets |
| R6 | Agent failure or timeout hangs the run | Medium | High | per-stage timeout, bounded retries, deadlock detector, safe-stop | engine timeout/unknown-agent tests | Worker thread cannot be forcibly killed |
| R7 | Partial write leaves inconsistent state | Low | High | transactions, conditional updates, WAL, per-run lock | store/engine tests | Crash between artifact write and DB commit |
| R8 | Compensation fails during rollback | Low | High | compensation failure escalates to `SAFE_STOPPED`; artifacts preserved | rollback test (success path); escalation unit path | No automatic retry of compensation |
| R9 | Audit log tampering | Low | Critical | SHA-256 chain; verification endpoint; tests for payload edit and deletion | `test_audit.py` | A writer with DB access can rewrite the whole chain undetected without external anchoring |
| R10 | Re-plan reuses stale upstream artifacts | Medium | Medium | reverse-dependency closure; explicit targets; version bumps | ambiguous scenario `v2` artifacts; re-plan test | Artifact-level staleness is not computed |
| R11 | Approval reused after re-plan | Medium | High | `approval_id` cleared on reset; approvals scoped to stage epoch | re-plan approval tests | Manual DB edits could reintroduce |
| R12 | SSRF via DNS rebinding | Low | High | literal private/loopback/link-local/int/hex blocking; no server-side fetch | URL matrix tests | DNS name resolving to private IP is not resolved at creation; documented |
| R13 | Abuse: phishing/malware destinations | Medium | High | URL validation, rate limits, reserved aliases | unit tests | No reputation feed, malware scan or takedown workflow |
| R14 | Rate limiting bypass across processes | Medium | Medium | per-process token bucket | API test | Not shared; production needs Redis |
| R15 | Analytics PII exposure | Low | High | salted visitor hashes; no raw IP persisted | analytics test | Salt is config, not KMS/HSM |
| R16 | Idempotency conflict or duplicate link | Low | Medium | unique keys/aliases; 409 on different target | API/idempotency tests | Key scope is global, not per-tenant |
| R17 | Scheduler starvation / unfairness | Low | Medium | bounded batches, deterministic order, budget | parallel/join test | No priority or tenant fairness |
| R18 | Run wall-clock budget too low | Medium | Low | configurable `ASDLC_MAX_WALL_SECONDS`; safe-stop resumable | budget test | Resume resets wall clock |
| R19 | Dashboard/API unavailable | Low | Medium | health/readiness; static dashboard; CLI fallback | manual browser + API tests | Single process |
| R20 | Model adapter returns invalid structure | Medium | Medium | schema checks; retryable error; reference fallback | LLM adapter tests | No semantic quality evaluation |

## 2. Failure scenarios and expected behavior

| Scenario | Expected behavior | Evidence |
|---|---|---|
| Agent raises a retryable error | attempt audited, bounded backoff, retry; fail after budget | `test_bounded_retry_then_success` |
| Agent raises a non-retryable error | no retry; stage failed; run policy applied | engine failure path |
| Policy denies code artifact | denial persisted, retry; clean retry succeeds | brownfield scenario |
| Exit gate fails | retry consumed; failure if budget exhausted | release gate test path |
| Approval rejected | stage cancelled, pending stages cancelled, run FAILED | `test_approval_rejection_fails_run` |
| Approval never arrives | run remains AWAITING; no downstream execution | scenario test |
| Deploy without test evidence | `CHG-001` denies | policy test |
| Compensation success | stages ROLLED_BACK, run ROLLED_BACK | `test_rollback_compensates_succeeded_stages` |
| Compensation failure | `SAFE_STOPPED`, human required | engine branch |
| Operator safe-stop | pending stages cancelled, reason recorded, resumable | `test_safe_stop_and_resume` |
| Wall-clock budget | `SAFE_STOPPED: wall_clock_budget_exceeded` | `test_wall_clock_budget_safe_stops` |
| Unknown agent | stage fails with recorded error; no hang | `test_unknown_agent_fails_without_hanging` |
| Re-plan on active run | 409, no mutation | engine guard |
| Re-plan with no downstream | 409, no mutation | engine guard |
| Audit payload altered | verification false at first bad seq | `test_tampering_is_detected` |
| Audit event deleted | verification false | `test_deleting_a_record_breaks_the_chain` |

## 3. Trade-offs accepted

| Decision | Benefit | Cost | Why acceptable |
|---|---|---|---|
| Custom engine | transparent governance, testability | no durable/distributed execution | interview prototype, single node |
| SQLite | zero-infra, transactional | single writer, no replication | reviewability first |
| Local artifact files | inspectable, simple | no durability guarantees | artifacts also stored in DB |
| Deterministic agents | reproducible evidence | less "generative" appearance | assignment evaluates orchestration; LLM adapter exists |
| In-process threads | simple parallel batches | no isolation, kill only by timeout | agents are fast and trusted |
| API key | simple auth demo | no identity/rotation | control boundary is demonstrated; production path documented |
| Polling dashboard | no build step | not real-time | 2.6s is sufficient for demos |
| Regex secret scan | fast, explainable | false positives/negatives | policy is defense-in-depth, not sole control |
| Best-effort rollback | simple compensation | cannot guarantee external undo | failure escalates to safe-stop |
| Hash chain in same DB | tamper-evident | writer can rewrite whole chain | production anchors externally |

## 4. Validation approach

- **Policy:** unit tests for every rule, plus the brownfield end-to-end policy retry.
- **Approvals:** pause/resume, reject, production auto-approve denial, re-plan approval.
- **Resilience:** retry, timeout, unknown agent, rollback, safe-stop, budgets, deadlock detector.
- **Audit:** chain valid after all scenarios; tamper and deletion fail verification.
- **Real output:** generated module tests execute in a subprocess; migration applied to a sandbox
  SQLite DB; impact analysis reads actual source files.
- **API:** auth, validation, conflicts, rate limits, redirects, analytics, security headers.
- **Determinism:** 100 tests, isolated temp DB/artifacts, no network.

## 5. Security review notes

- Redirect responses set `Cache-Control: no-store` and `X-Robots-Tag: noindex`.
- Management routes are API-key protected; redirects are public by design.
- Security headers: `nosniff`, `DENY` framing, no-referrer, restrictive CSP.
- Request IDs are returned for correlation.
- Error handlers do not leak stack traces; CLI prints exception type/message.
- Raw IPs are never persisted; the salt must be rotated with care (rotation breaks uniqueness).

## 6. Production hardening backlog (prioritized)

1. Replace SQLite with Postgres and add migrations/backups.
2. Move stage execution to queue workers with leases, heartbeats and crash recovery.
3. Move artifacts to object storage with immutable retention and checksums.
4. Add OAuth2/OIDC, mTLS, secret rotation and per-tenant authorization.
5. Add Redis rate limiting and distributed idempotency.
6. Add an egress proxy and DNS pinning for redirect destinations.
7. Anchor the audit chain externally (WORM/SIEM) and add checkpoints.
8. Add malware/reputation scanning and a takedown workflow.
9. Add SSE/WebSocket live updates and multi-user approval views.
10. Add load, chaos, and model-evaluation tests.

## 7. Assumptions

- The prototype is single-tenant and single-node.
- The operator controls the API key and the host.
- Generated artifacts are trusted only after gates and policy; they are not executed outside the
  validation sandbox.
- "Production" in scenarios means a high-impact release action; no real deployment occurs.
- The environment provides Python 3.11+; no external services are required.
