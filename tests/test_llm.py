from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from agentic_sdlc.config import Settings
from agentic_sdlc.errors import StageExecutionError
from agentic_sdlc.orchestrator.agents.llm import LLMAgent
from agentic_sdlc.orchestrator.models import (
    AgentContext,
    AgentResult,
    ArtifactDraft,
)


class Wrapped:
    name = "wrapped"

    def __init__(self) -> None:
        self.called = 0

    def execute(self, context: AgentContext) -> AgentResult:
        self.called += 1
        return AgentResult(
            summary="fallback",
            artifacts=[ArtifactDraft("fallback.md", "markdown", "fallback")],
        )

    def compensate(self, context: AgentContext) -> None:
        return None


def context() -> AgentContext:
    return AgentContext(
        run_id="run_1",
        stage_id="implementation",
        scenario="greenfield",
        requirement="build",
        attempt=1,
        mode="llm",
    )


def settings(**overrides) -> Settings:
    defaults = dict(
        db_path=__import__("pathlib").Path("unused.db"),
        data_dir=__import__("pathlib").Path("unused"),
        llm_api_key="test-key",
        llm_base_url="http://llm.local/v1",
        llm_model="test-model",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_falls_back_without_api_key() -> None:
    wrapped = Wrapped()
    agent = LLMAgent(wrapped, settings(llm_api_key=""))
    result = agent.execute(context())
    assert result.summary == "fallback"
    assert wrapped.called == 1


def test_parses_model_json_response() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "model output",
                            "artifacts": [
                                {"name": "generated/model.py", "kind": "code", "content": "x = 1\n"}
                            ],
                            "decisions": [
                                {"topic": "t", "decision": "d", "rationale": "r"}
                            ],
                            "metadata": {"runner": "llm"},
                        }
                    )
                }
            }
        ]
    }
    wrapped = Wrapped()
    agent = LLMAgent(wrapped, settings())
    with patch(
        "agentic_sdlc.orchestrator.agents.llm.urllib.request.urlopen",
        return_value=io.BytesIO(json.dumps(payload).encode()),
    ):
        result = agent.execute(context())
    assert result.summary == "model output"
    assert result.artifacts[0].name == "generated/model.py"
    assert result.decisions[0].topic == "t"
    assert result.metadata["runner"] == "llm"


def test_malformed_model_response_is_retryable() -> None:
    wrapped = Wrapped()
    agent = LLMAgent(wrapped, settings())
    with patch(
        "agentic_sdlc.orchestrator.agents.llm.urllib.request.urlopen",
        return_value=io.BytesIO(b"not json"),
    ):
        with pytest.raises(StageExecutionError) as exc:
            agent.execute(context())
    assert exc.value.retryable is True


def test_extract_json_handles_fenced_code() -> None:
    assert LLMAgent._extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    with pytest.raises(ValueError):
        LLMAgent._extract_json("no json here")
