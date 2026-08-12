from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_work_harness.cli import main
from ai_work_harness.decision.canonical import sha256_bytes
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


def test_guided_excerpt_read_is_pinned_hashed_and_read_only(tmp_path: Path) -> None:
    service = DecisionService(tmp_path, "operator")
    parent = str(service.initialize()["snapshot_sha256"])
    source = tmp_path / "facts.md"
    source.write_text("first\nsecond\nthird\n", encoding="utf-8")
    captured = service.capture_source(
        source_id="facts",
        source=source,
        expected_parent=parent,
        media_type="text/markdown",
    )
    pinned = str(captured["snapshot_sha256"])
    before = service.store.paths.current_pointer.read_bytes()

    excerpt = service.read_source_excerpt(
        snapshot_sha256=pinned,
        source_id="facts",
        start_line=2,
        end_line=3,
    )

    assert excerpt["excerpt"] == "second\nthird\n"
    assert excerpt["excerpt_sha256"] == sha256_bytes(b"second\nthird\n")
    assert excerpt["snapshot_sha256"] == pinned
    assert service.store.paths.current_pointer.read_bytes() == before

    with pytest.raises(HarnessError) as out_of_range:
        service.read_source_excerpt(
            snapshot_sha256=pinned,
            source_id="facts",
            start_line=2,
            end_line=4,
        )
    assert out_of_range.value.code == "INVALID_SOURCE_LOCATOR"
