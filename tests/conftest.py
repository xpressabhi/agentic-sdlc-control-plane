from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentic_sdlc.api import create_app
from agentic_sdlc.config import Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        data_dir=tmp_path / "data",
        api_key="test-key",
        analytics_salt="test-salt",
        max_parallel=4,
        max_wall_seconds=120.0,
        max_stage_attempts=200,
        retry_base_delay=0.001,
        retry_max_delay=0.005,
        environment="test",
        rate_limit_per_minute=1000,
        redirect_rate_limit_per_minute=1000,
    )


@pytest.fixture
def app(settings: Settings):
    return create_app(settings)


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth() -> dict[str, str]:
    return {"X-API-Key": "test-key"}
