from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_work_harness.decision.migration import migrate_v1
from ai_work_harness.decision.service import DecisionService
from ai_work_harness.decision.store import DecisionStore
from ai_work_harness.errors import HarnessError
from ai_work_harness.project import ProjectPaths, init_project


def _v1_bytes(paths: ProjectPaths) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for path in paths.state.rglob("*"):
        if path.is_file() and "v2" not in path.relative_to(paths.state).parts:
            result[path.relative_to(paths.state).as_posix()] = path.read_bytes()
    return result


def test_complete_v1_migration_is_one_way_and_idempotent(prepared_project) -> None:
    before = _v1_bytes(prepared_project.paths)

    first = migrate_v1(prepared_project.root, "migration-demo")
    store = DecisionStore(prepared_project.root, "migration-demo")
    current = store.get_current()
    report = store.read_artifact(current.refs["migration_report"])

    assert first["already_migrated"] is False
    assert set(current.refs) == {"source_manifest", "decision_frame", "migration_report"}
    assert report.payload["confirmations_promoted"] is False
    assert report.payload["approvals_promoted"] is False
    assert _v1_bytes(prepared_project.paths) == before

    second = migrate_v1(prepared_project.root, "migration-demo")
    assert second["already_migrated"] is True
    assert second["snapshot_sha256"] == first["snapshot_sha256"]
    assert _v1_bytes(prepared_project.paths) == before


def test_concurrent_identical_migrations_converge_on_one_snapshot(
    prepared_project,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_work_harness.decision.migration as migration_module

    barrier = threading.Barrier(2)
    original = migration_module._existing_migration

    def synchronize_initial_inspection(store: DecisionStore, fingerprint: str):
        result = original(store, fingerprint)
        if result is None:
            barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(
        migration_module,
        "_existing_migration",
        synchronize_initial_inspection,
    )

    def migrate() -> dict[str, object]:
        return migrate_v1(prepared_project.root, "concurrent-migration")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: migrate(), range(2)))

    assert sorted(result["already_migrated"] for result in results) == [False, True]
    assert len({result["snapshot_sha256"] for result in results}) == 1
    current = DecisionStore(prepared_project.root, "concurrent-migration").get_current()
    assert current.generation == 1
    assert current.snapshot_sha256 == results[0]["snapshot_sha256"]


def test_empty_v1_migrates_only_a_report(tmp_path: Path) -> None:
    root = tmp_path / "project"
    init_project(root)

    result = migrate_v1(root, "empty-migration")
    current = DecisionStore(root, "empty-migration").get_current()

    assert result["sources_migrated"] == 0
    assert result["frame_migrated"] is False
    assert set(current.refs) == {"migration_report"}


def test_invalid_v1_fails_before_creating_a_target_session(prepared_project) -> None:
    managed = prepared_project.paths.inputs / "records.csv"
    managed.write_text("changed\n", encoding="utf-8")

    with pytest.raises(HarnessError) as caught:
        migrate_v1(prepared_project.root, "invalid-migration")

    assert caught.value.code == "V1_MIGRATION_SOURCE_INVALID"
    assert not (
        prepared_project.paths.state / "v2" / "sessions" / "invalid-migration" / "current.json"
    ).exists()


def test_migration_resumes_an_initialized_target_with_orphans(prepared_project) -> None:
    store = DecisionStore(prepared_project.root, "resume-migration")
    initial = store.initialize()
    orphan_sha = store.put_blob(b"unreferenced crash remainder")

    result = migrate_v1(prepared_project.root, "resume-migration", store=store)

    assert result["already_migrated"] is False
    assert result["generation"] == initial.generation + 1
    assert orphan_sha in store.doctor().orphan_objects


def test_v1_change_after_migration_is_not_auto_applied(prepared_project) -> None:
    migrate_v1(prepared_project.root, "changed-migration")
    source = prepared_project.root.parent / "replacement.csv"
    source.write_text("id,value\n1,7\n", encoding="utf-8")
    from ai_work_harness.project import capture_input

    capture_input(prepared_project.root, logical_id="records.csv", source=source)

    with pytest.raises(HarnessError) as caught:
        migrate_v1(prepared_project.root, "changed-migration")

    assert caught.value.code == "V1_MIGRATION_SOURCE_INVALID"


def test_migrated_draft_can_be_replaced_without_leaving_a_stale_report(
    prepared_project,
) -> None:
    migrated = migrate_v1(prepared_project.root, "migration-continue")
    service = DecisionService(prepared_project.root, "migration-continue")

    frame = service.import_frame(
        {
            "user_statement_verbatim": "Choose a safe processing route.",
            "ai_initial_interpretation": "Compare bounded route options.",
            "business_user": "local operator",
            "blocked_decision": "which route to choose",
            "problem_statement": "Choose one bounded processing route.",
            "scope_in": ["captured synthetic input"],
            "scope_out": ["production deployment"],
            "assumptions": ["The migrated input is still authoritative."],
            "open_questions": [],
        },
        expected_parent=migrated["snapshot_sha256"],
    )

    assert "migration_report" not in service.status()["refs"]
    assert service.verify(frame["snapshot_sha256"])["verified"] is True
