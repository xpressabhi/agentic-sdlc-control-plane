"""Runtime configuration.

Settings are deliberately plain dataclasses rather than a settings framework: the
prototype has a small, explicit surface and tests construct settings directly.
Every value can be overridden with an ``ASDLC_`` environment variable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.getenv(f"ASDLC_{name}", default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings shared by the API, CLI and engine."""

    db_path: Path = Path("var/agentic_sdlc.db")
    data_dir: Path = Path("var")
    api_key: str = "dev-key"
    analytics_salt: str = "dev-only-salt"
    public_base_url: str = "http://localhost:8000"

    # Shortener limits
    max_url_length: int = 2048
    min_alias_length: int = 3
    max_alias_length: int = 32
    min_ttl_seconds: int = 30
    max_ttl_seconds: int = 31_536_000
    rate_limit_per_minute: int = 240
    redirect_rate_limit_per_minute: int = 1200

    # Orchestration
    max_parallel: int = 4
    max_wall_seconds: float = 180.0
    max_stage_attempts: int = 120
    retry_base_delay: float = 0.08
    retry_max_delay: float = 1.0
    environment: str = "development"
    change_freeze: bool = False
    agent_mode: str = "reference"

    # Optional LLM adapter (disabled unless agent_mode == "llm")
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0

    artifact_root: Path | None = None

    @property
    def runs_dir(self) -> Path:
        return (self.artifact_root or (self.data_dir / "runs")).resolve()

    @classmethod
    def load(cls) -> Settings:
        """Build settings from the environment."""
        data_dir = Path(_env("DATA_DIR", "var"))
        artifact_root = _env("ARTIFACT_ROOT", "")
        return cls(
            db_path=Path(_env("DB_PATH", str(data_dir / "agentic_sdlc.db"))),
            data_dir=data_dir,
            api_key=_env("API_KEY", "dev-key"),
            analytics_salt=_env("ANALYTICS_SALT", "dev-only-salt"),
            public_base_url=_env("PUBLIC_BASE_URL", "http://localhost:8000"),
            max_url_length=_env_int("MAX_URL_LENGTH", 2048),
            rate_limit_per_minute=_env_int("RATE_LIMIT_PER_MINUTE", 240),
            redirect_rate_limit_per_minute=_env_int("REDIRECT_RATE_LIMIT_PER_MINUTE", 1200),
            max_parallel=_env_int("MAX_PARALLEL", 4),
            max_wall_seconds=_env_float("MAX_WALL_SECONDS", 180.0),
            max_stage_attempts=_env_int("MAX_STAGE_ATTEMPTS", 120),
            retry_base_delay=_env_float("RETRY_BASE_DELAY", 0.08),
            retry_max_delay=_env_float("RETRY_MAX_DELAY", 1.0),
            environment=_env("ENV", "development"),
            change_freeze=_env_bool("CHANGE_FREEZE", False),
            agent_mode=_env("AGENT_MODE", "reference"),
            llm_base_url=_env("LLM_BASE_URL", "https://api.openai.com/v1"),
            llm_api_key=_env("LLM_API_KEY", ""),
            llm_model=_env("LLM_MODEL", "gpt-4o-mini"),
            llm_timeout_seconds=_env_float("LLM_TIMEOUT_SECONDS", 30.0),
            artifact_root=Path(artifact_root) if artifact_root else None,
        )

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)


def utcnow_iso() -> str:
    """UTC timestamp with millisecond precision, always suffixed with ``Z``."""
    from datetime import datetime

    return (
        datetime.now(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
