from __future__ import annotations

from agentic_sdlc.db import Database
from agentic_sdlc.orchestrator.audit import AuditLog, canonical_json


def test_audit_chain_appends_and_verifies(tmp_path) -> None:
    db = Database(tmp_path / "audit.db")
    log = AuditLog(db)
    first = log.append(run_id="run_1", event_type="run.created", actor="tester", payload={"a": 1})
    second = log.append(run_id="run_1", event_type="stage.started", actor="tester", payload={"b": 2})
    assert first["prev_hash"] == "0" * 64
    assert second["prev_hash"] == first["hash"]
    assert log.verify()["valid"] is True
    assert log.verify(run_id="run_1")["valid"] is True


def test_tampering_is_detected(tmp_path) -> None:
    db = Database(tmp_path / "audit.db")
    log = AuditLog(db)
    log.append(run_id="run_1", event_type="run.created", actor="tester", payload={"a": 1})
    log.append(run_id="run_1", event_type="stage.started", actor="tester", payload={"b": 2})
    with db.tx() as conn:
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE seq = 1",
            (canonical_json({"a": 99}),),
        )
    result = log.verify()
    assert result["valid"] is False
    assert result["first_bad_seq"] == 1


def test_deleting_a_record_breaks_the_chain(tmp_path) -> None:
    db = Database(tmp_path / "audit.db")
    log = AuditLog(db)
    for index in range(3):
        log.append(run_id="run_1", event_type=f"e{index}", actor="tester", payload={})
    with db.tx() as conn:
        conn.execute("DELETE FROM audit_events WHERE seq = 2")
    assert log.verify()["valid"] is False
