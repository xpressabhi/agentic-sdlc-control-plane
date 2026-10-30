from __future__ import annotations

from agentic_sdlc.config import Settings, utcnow_iso


def test_settings_load_from_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ASDLC_DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("ASDLC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASDLC_API_KEY", "secret")
    monkeypatch.setenv("ASDLC_MAX_PARALLEL", "7")
    monkeypatch.setenv("ASDLC_RETRY_BASE_DELAY", "0.5")
    monkeypatch.setenv("ASDLC_CHANGE_FREEZE", "true")
    monkeypatch.setenv("ASDLC_ENV", "production")
    settings = Settings.load()
    assert settings.db_path == tmp_path / "x.db"
    assert settings.api_key == "secret"
    assert settings.max_parallel == 7
    assert settings.retry_base_delay == 0.5
    assert settings.change_freeze is True
    assert settings.environment == "production"
    assert settings.runs_dir == tmp_path.resolve() / "runs"


def test_utcnow_has_z_suffix() -> None:
    value = utcnow_iso()
    assert value.endswith("Z")
    assert "T" in value
