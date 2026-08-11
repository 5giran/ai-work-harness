from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_work_harness.cli import main
from ai_work_harness.decision.service import DecisionService
from ai_work_harness.errors import HarnessError


def test_operator_plan_is_read_only_and_uses_the_raw_next_envelope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    service = DecisionService(
        tmp_path,
        "operator",
        clock=lambda: datetime(2026, 8, 12, 1, 2, 3, tzinfo=UTC),
    )
    initialized = service.initialize()
    pointer = service.store.paths.current_pointer
    before = pointer.read_bytes()

    plan = service.operator_plan()

    assert pointer.read_bytes() == before
    assert plan["snapshot_sha256"] == initialized["snapshot_sha256"]
    assert plan["schema_version"] == "operator-plan.v1"
    assert plan["observed_at"] == "2026-08-12T01:02:03Z"
    assert plan["stage"] == "source"
    assert plan["recommended_action"] == "capture_source"

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "decision",
            "next",
            "--session-id",
            "operator",
        ]
    )
    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["command"] == "decision next"
    assert output["generation"] == 0
    assert output["snapshot_sha256"] == initialized["snapshot_sha256"]
    assert output["result"]["schema_version"] == "operator-plan.v1"
    assert pointer.read_bytes() == before


def test_read_operator_state_remains_pinned_when_current_moves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = DecisionService(tmp_path, "operator")
    initialized = service.initialize()
    pinned = str(initialized["snapshot_sha256"])
    real_verify = service.verify
    moved = False

    def verify_and_move(snapshot_sha256: str | None = None) -> dict[str, object]:
        nonlocal moved
        result = real_verify(snapshot_sha256)
        if not moved:
            moved = True
            service.store.commit(
                expected_parent=pinned,
                refs={},
                operation="test.concurrent-pointer-move",
            )
        return result

    monkeypatch.setattr(service, "verify", verify_and_move)
    state = service.read_operator_state()

    assert state.pinned_snapshot_sha256 == pinned
    assert state.generation == 0
    assert state.refs == {}
    assert service.store.get_current().snapshot_sha256 != pinned


def test_operator_plan_fails_closed_on_integrity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = DecisionService(tmp_path, "operator")
    initialized = service.initialize()

    def fail_integrity(snapshot_sha256: str | None = None) -> dict[str, object]:
        raise HarnessError(
            "INTEGRITY_VERIFICATION_FAILED",
            "Pinned snapshot is damaged",
            exit_code=5,
        )

    monkeypatch.setattr(service, "verify", fail_integrity)
    with pytest.raises(HarnessError) as caught:
        service.operator_plan()

    assert caught.value.code == "INTEGRITY_VERIFICATION_FAILED"
    assert caught.value.exit_code == 5
    assert service.store.get_current().snapshot_sha256 == initialized["snapshot_sha256"]

