"""Agent contract and registry."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import AgentContext, AgentResult


@runtime_checkable
class Agent(Protocol):
    name: str

    def execute(self, context: AgentContext) -> AgentResult: ...

    def compensate(self, context: AgentContext) -> None:
        """Undo side effects. Best effort; engines escalate if this raises."""
        return None


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, name: str, agent: Agent) -> None:
        self._agents[name] = agent

    def get(self, name: str) -> Agent | None:
        return self._agents.get(name)

    def require(self, name: str) -> Agent:
        agent = self.get(name)
        if agent is None:
            raise KeyError(f"no agent registered under {name!r}")
        return agent

    def names(self) -> list[str]:
        return sorted(self._agents)
