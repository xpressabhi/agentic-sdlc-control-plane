# API Reference

Interactive OpenAPI documentation is served at `/docs` and the raw schema at
`/openapi.json`. The design-time contract is also generated as an SDLC artifact
(`openapi.yaml` in the greenfield run).

## Authentication

All `/api/v1/*` management routes require:

```http
X-API-Key: dev-key
```

The redirect route (`GET /{code}`) is public by design. The default key is `dev-key`; set
`ASDLC_API_KEY` for any shared environment.

Errors use one envelope:

```json
{"error": "human-readable message", "code": "machine_code"}
```

| HTTP | Code examples | Meaning |
|---|---|---|
| 401 | `invalid_api_key` | missing/invalid key |
| 404 | `link_not_found`, `link_expired`, `run_not_found`, `artifact_not_found` | resource absent |
| 409 | `alias_taken`, `idempotency_conflict`, `run_active`, `no_impact` | state conflict |
| 422 | `unsafe_url`, `invalid_alias`, `invalid_ttl` | validation failure |
| 429 | `rate_limited` | token bucket exhausted; `Retry-After` header |
| 403 | `policy_denied`, `auto_approve_forbidden` | policy guardrail |

## URL shortener

### Create a link

```http
POST /api/v1/links
Content-Type: application/json
X-API-Key: dev-key
Idempotency-Key: optional-client-key

{
  "url": "https://example.com/launch?utm=1",
  "custom_alias": "launch",
  "ttl_seconds": 86400,
  "metadata": {"campaign": "q4"}
}
```

`201 Created`:

```json
{
  "code": "launch",
  "short_url": "http://localhost:8000/launch",
  "target_url": "https://example.com/launch?utm=1",
  "created_at": "2026-10-30T12:00:00.000Z",
  "expires_at": "2026-10-31T12:00:00.000Z",
  "deleted_at": null,
  "click_count": 0,
  "metadata": {"campaign": "q4"}
}
```

Rules: `http`/`https` only; no credentials; no private/loopback/link-local/metadata hosts;
URL ≤ 2048 chars; alias 3-32 chars; TTL 30s–1 year. Replaying an idempotency key with the same
target returns the original record; a different target returns 409.

### Redirect

```http
GET /{code}
```

`302 Location: <target>`. Records referrer, user agent, salted visitor hash and country.
Expired/deleted links return 404. Response headers: `Cache-Control: no-store`,
`X-Robots-Tag: noindex`.

### Metadata / delete

```http
GET    /api/v1/links/{code}
DELETE /api/v1/links/{code}      # 204, soft delete
```

### Analytics

```http
GET /api/v1/links/{code}/analytics?since=2026-10-01T00:00:00Z&until=2026-10-31T23:59:59Z
```

```json
{
  "code": "launch",
  "total_clicks": 42,
  "unique_visitors": 31,
  "first_click_at": "2026-10-30T12:01:00.000Z",
  "last_click_at": "2026-10-30T18:22:00.000Z",
  "daily": [{"day": "2026-10-30", "clicks": 42}],
  "top_referrers": [{"referrer": "direct", "clicks": 20}],
  "top_countries": [{"country": "US", "clicks": 30}]
}
```

## Orchestration

### Scenarios

```http
GET /api/v1/scenarios
```

Returns scenario metadata and stage graphs (ids, dependencies, approval flags, descriptions).

### Launch a run

```http
POST /api/v1/runs
{"scenario": "greenfield", "auto_approve": false}
```

`201 Created` returns the full run snapshot. `auto_approve=true` is rejected when
`ASDLC_ENV=production`.

### Run snapshot

```http
GET /api/v1/runs/{run_id}
```

Includes `run`, enriched `stages` (status, attempt, retry count, dependencies, gates, approval,
artifacts), `levels` (topological levels for the DAG), `artifacts`, `decisions`, `approvals`,
`audit`, `audit_verification`, `metrics` and `actions`.

### Approvals

```http
GET  /api/v1/approvals?status=PENDING
POST /api/v1/runs/{run_id}/approve  {"stage_id": "publish", "rationale": "evidence reviewed"}
POST /api/v1/runs/{run_id}/reject   {"stage_id": "publish", "rationale": "missing rollback plan"}
```

Approving resumes the scheduler synchronously. Rejecting cancels the stage and fails the run.
For a re-plan approval use `stage_id: "__replan__"` (or omit it when it is the only pending).

### Re-plan

```http
POST /api/v1/runs/{run_id}/replan
{
  "changed_stage": "requirements",
  "reason": "scope clarified",
  "targets": ["architecture", "implementation"],
  "require_approval": true
}
```

Targets are validated; default is the reverse-dependency closure minus the changed stage.
Impacted stages are reset to `PENDING/STALE`; re-execution creates new artifact versions.

### Safe-stop / resume

```http
POST /api/v1/runs/{run_id}/safe-stop  {"reason": "operator pause"}
POST /api/v1/runs/{run_id}/resume
```

Safe-stop cancels non-terminal stages, preserves artifacts, and records the reason. Resume
returns cancelled stages to `PENDING` and re-enters the scheduler.

### Audit

```http
GET /api/v1/runs/{run_id}/audit
GET /api/v1/runs/{run_id}/audit/verify
```

Verification walks the global hash chain and returns `{valid, checked, first_bad_seq, message}`.

### Artifacts and metrics

```http
GET /api/v1/artifacts/{artifact_id}
GET /api/v1/metrics/summary
GET /api/v1/metrics/prometheus
GET /metrics
```

## Prometheus metrics

```
asdlc_runs_total
asdlc_runs_succeeded_total
asdlc_run_success_rate
asdlc_stage_retry_frequency
asdlc_run_rollback_frequency
asdlc_mttr_seconds
asdlc_run_latency_p95_seconds
asdlc_pending_approvals
asdlc_policy_violations_total
asdlc_short_links_total
asdlc_clicks_total
```

## Operational endpoints

| Path | Purpose |
|---|---|
| `/healthz` | liveness |
| `/readyz` | database readiness |
| `/metrics` | Prometheus text |
| `/docs` | Swagger UI |
| `/openapi.json` | OpenAPI schema |
