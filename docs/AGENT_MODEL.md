# Agents and Artifacts

## Agent contract

Every stage resolves to an agent implementing:

```python
class Agent(Protocol):
    name: str
    def execute(self, context: AgentContext) -> AgentResult: ...
    def compensate(self, context: AgentContext) -> None: ...
```

`AgentContext` carries the requirement, attempt number, approval record, re-plan reason,
upstream artifacts, prior decisions and a workspace path. `AgentResult` carries artifacts,
decisions, metadata and an explicit success/retryable error.

This is deliberately small: agents produce evidence; the engine owns scheduling, gates,
policy, retries, audit and state. An agent cannot mark itself approved or bypass a gate.

## Reference agents

| Agent | Stage(s) | Produces | Real work |
|---|---|---|---|
| `requirements` | requirements | `requirements.md` | normalizes intent, acceptance criteria, NFRs, ambiguity |
| `ambiguous_requirements` | intake | `requirements.md` | lists ambiguities, blocking questions, assumptions |
| `clarification` | clarification | `clarification.md` | records human-approved interpretation |
| `architecture` | architecture / design | `architecture.md`, `openapi.yaml`, `data-model.sql` | API contract, schema, SSRF controls, trade-offs |
| `threat_model` | threat model | `threat-model.md` | STRIDE, abuse cases, residual risk |
| `implementation` | implementation | `generated/shortener_core.py`, `generated/test_shortener_core.py` | working Base62/URL validation module + tests |
| `test_plan` | test plan | `test-plan.md` | risk-based test matrix and exit criteria |
| `validation` | validation / regression | `test-report.md`, `validation-log.txt` | writes generated tests and executes them in a subprocess |
| `documentation` | documentation | `runbook.md` | rollout, rollback, SLOs, limitations |
| `release` | readiness/deploy | `release-checklist.md` or `deployment-record.md` | change control, rollback, observability evidence |
| `impact_analysis` | impact analysis | `impact-analysis.md` | scans real shortener modules, maps APIs/data flows |
| `api_contract` | API review | `api-compatibility.md` | additive compatibility review |
| `migration` | migration | `002_aliases_analytics_ttl.sql`, verification log | applies migration in a sandbox SQLite DB and verifies |
| `brownfield_implementation` | extension | extension module + tests | alias/TTL/analytics helpers; intentionally leaks a secret on attempt 1 to demonstrate policy retry |

## LLM adapter

`ASDLC_AGENT_MODE=llm` with `ASDLC_LLM_API_KEY` wraps any reference agent with an
OpenAI-compatible chat-completions call. The model is asked for a strict JSON shape; parsing
failures raise a retryable stage error, so the engine's bounded retry, fallback and safe-stop
semantics still apply. The reference agent remains the fallback and the default.

## Adding an agent

1. Implement `execute` (and `compensate` if the stage has side effects).
2. Register it in `build_reference_registry()` or a custom `AgentRegistry`.
3. Reference the agent name in a scenario YAML stage and declare gates/policy tags.
4. Add a synthetic test in `tests/test_engine.py` for failure/retry behavior.
5. Add the artifact to the stage's exit gate expectation.

Because the engine is policy- and gate-driven, a new agent inherits retries, approvals,
audit, metrics and safe-stop without changes to the scheduler.
