"""Optional LLM-backed agent adapter.

The deterministic reference agent is the default and the fallback. When
``ASDLC_AGENT_MODE=llm`` and an API key is configured, this adapter asks an
OpenAI-compatible endpoint to produce artifacts as JSON. Any transport, schema
or content failure raises a retryable stage error; the orchestrator's bounded
retry and fallback semantics then apply.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ...config import Settings
from ...errors import StageExecutionError
from ..models import AgentContext, AgentResult, ArtifactDraft, DecisionDraft
from .base import Agent

SYSTEM_PROMPT = """You are a senior software engineer executing one SDLC stage.
Return ONLY a JSON object with this shape:
{
  "summary": "one line",
  "artifacts": [{"name": "path.md", "kind": "markdown", "content": "..."}],
  "decisions": [{"topic": "...", "decision": "...", "rationale": "..."}],
  "metadata": {}
}
Do not include secrets, credentials or personal data. Be concrete and concise."""


class LLMAgent:
    def __init__(self, wrapped: Agent, settings: Settings) -> None:
        self.wrapped = wrapped
        self.settings = settings
        self.name = wrapped.name

    def execute(self, context: AgentContext) -> AgentResult:
        if not self.settings.llm_api_key:
            return self.wrapped.execute(context)
        upstream = {
            stage: [
                {"name": artifact.name, "kind": artifact.kind, "content": artifact.content[:6000]}
                for artifact in artifacts
            ]
            for stage, artifacts in context.upstream.items()
        }
        user_prompt = json.dumps(
            {
                "stage": context.stage_id,
                "scenario": context.scenario,
                "attempt": context.attempt,
                "requirement": context.requirement,
                "upstream": upstream,
                "replan_reason": context.replan_reason,
            },
            indent=2,
        )
        payload = {
            "model": self.settings.llm_model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        }
        request = urllib.request.Request(
            f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.settings.llm_api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.settings.llm_timeout_seconds
            ) as response:
                body = json.loads(response.read().decode())
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(self._extract_json(content))
            artifacts = [
                ArtifactDraft(
                    name=str(item["name"]),
                    kind=str(item.get("kind", "markdown")),
                    content=str(item["content"]),
                    metadata=item.get("metadata") or {},
                )
                for item in parsed.get("artifacts", [])
            ]
            decisions = [
                DecisionDraft(
                    topic=str(item["topic"]),
                    decision=str(item["decision"]),
                    rationale=str(item.get("rationale", "")),
                )
                for item in parsed.get("decisions", [])
            ]
            if not artifacts:
                raise StageExecutionError("LLM produced no artifacts", retryable=True)
            return AgentResult(
                summary=str(parsed.get("summary", "LLM stage completed")),
                artifacts=artifacts,
                decisions=decisions,
                metadata=parsed.get("metadata") or {},
            )
        except StageExecutionError:
            raise
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, TypeError) as exc:
            raise StageExecutionError(
                f"LLM adapter failed: {type(exc).__name__}: {exc}",
                retryable=True,
                code="llm_adapter_error",
            ) from exc

    def compensate(self, context: AgentContext) -> None:
        return self.wrapped.compensate(context)

    @staticmethod
    def _extract_json(content: str) -> str:
        content = content.strip()
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end < 0:
            raise ValueError("no JSON object in model response")
        return content[start : end + 1]
