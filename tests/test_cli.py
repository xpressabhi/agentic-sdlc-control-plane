from __future__ import annotations

from pathlib import Path

import pytest

from agentic_sdlc.cli import main


@pytest.fixture
def cli_env(monkeypatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("ASDLC_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("ASDLC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASDLC_SCENARIOS_DIR", str(Path.cwd() / "scenarios"))
    monkeypatch.setenv("ASDLC_RETRY_BASE_DELAY", "0.001")
    return tmp_path


def test_cli_lists_scenarios(cli_env: Path, capsys) -> None:
    assert main(["scenarios"]) == 0
    output = capsys.readouterr().out
    assert "greenfield" in output and "brownfield" in output and "ambiguous" in output


def test_cli_run_status_approve_and_metrics(cli_env: Path, capsys) -> None:
    assert main(["run", "greenfield"]) == 0
    output = capsys.readouterr().out
    assert "AWAITING_APPROVAL" in output
    run_id = None
    for line in output.splitlines():
        if line.startswith("Run     :"):
            run_id = line.split(":", 1)[1].strip()
    assert run_id

    assert main(["status", run_id]) == 0
    assert "publish" in capsys.readouterr().out

    assert main(["approve", run_id, "--rationale", "reviewed"]) == 0
    assert "SUCCEEDED" in capsys.readouterr().out

    assert main(["metrics"]) == 0
    assert "success_rate" in capsys.readouterr().out
    assert main(["audit-verify", run_id]) == 0
    assert '"valid": true' in capsys.readouterr().out


def test_cli_demo_and_metrics(cli_env: Path, capsys) -> None:
    assert main(["demo"]) == 0
    output = capsys.readouterr().out
    assert "Demo complete." in output
    assert "SUCCEEDED" in output
    assert main(["metrics"]) == 0


def test_cli_rejects_unknown_run(cli_env: Path) -> None:
    assert main(["status", "run_missing"]) == 2


def test_cli_replan(cli_env: Path, capsys) -> None:
    assert main(["run", "greenfield", "--auto-approve"]) == 0
    output = capsys.readouterr().out
    run_id = next(
        line.split(":", 1)[1].strip() for line in output.splitlines() if line.startswith("Run     :")
    )
    assert (
        main(
            [
                "replan",
                run_id,
                "--stage",
                "requirements",
                "--reason",
                "scope changed",
                "--require-approval",
            ]
        )
        == 0
    )
    assert "AWAITING_APPROVAL" in capsys.readouterr().out
