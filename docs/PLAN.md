# Implementation Plan — Agentic SDLC Control Plane + URL Shortener

**Spec:** `docs/superpowers/specs/2026-10-30-agentic-sdlc-url-shortener-design.md`

## Task list

| # | Task | Deliverable | Verification |
|---|---|---|---|
| 1 | Scaffold repo, config, SQLite schema | `pyproject.toml`, `config.py`, `db.py`, `Makefile` | `uv sync`, import app |
| 2 | URL shortener domain | repository, service, validation, codec, rate limiting | unit tests |
| 3 | URL shortener API | routes, auth, redirect, analytics, health | API tests |
| 4 | Orchestration primitives | graph, models, audit chain, policy engine | unit tests |
| 5 | Orchestration store | runs, stages, artifacts, approvals, decisions, metrics | store tests |
| 6 | Orchestration engine | scheduler, gates, retries, approval, rollback, safe-stop, replan | engine tests |
| 7 | Reference agents + generated artifacts | requirements, architecture, threat model, implementation, validation, docs, release, impact, migration | scenario tests |
| 8 | Three scenarios | YAML definitions + loader/validator | e2e tests |
| 9 | Orchestrator REST API + CLI | routes, CLI commands, demo | smoke tests |
| 10 | Dashboard | run list, DAG, stage drawer, approvals, metrics | manual + screenshot |
| 11 | Docs | README, architecture, orchestration, scenarios, testing, risk, decisions, final summary, mapping | review |
| 12 | Packaging | Dockerfile, CI, demo script | `make verify` |

## Sequencing

1–3 first (concrete service), then 4–6 (orchestration core), then 7–8 (agents/scenarios),
then 9–10 (interfaces), then 11–12 (docs/packaging). Tests accompany each task; the final
`make verify` runs lint + full suite + all three scenarios.

## Definition of done

- `make verify` green from a clean checkout.
- `make demo` completes greenfield with human-style approval evidence.
- Dashboard loads and shows live runs, DAG state, approvals, metrics.
- Assignment mapping document covers every section of the PDF.

## Status — complete (2026-10-30)

- 100 tests passing, 90% statement coverage, ruff clean.
- All three scenarios pass end-to-end; ambiguous re-plan produces `v2` artifacts.
- Dashboard verified in headless Chrome: run DAG, stage drawer, approvals inbox, metrics.
- `make demo-all` succeeds from a fresh database.

