from __future__ import annotations

from pathlib import Path

import pytest

from ai_work_harness.decision.migration import inspect_v1_migration_source, migrate_v1
from ai_work_harness.decision.store import DecisionStore
from ai_work_harness.errors import HarnessError
from ai_work_harness.io_utils import atomic_write_json, read_json
from ai_work_harness.project import capture_input, init_project, write_frame

_SOURCE_LIMIT = 10 * 1024 * 1024


def _minimal_v1(root: Path, source: Path, *, statement: str = "Choose.") -> None:
    init_project(root)
    capture_input(root, logical_id="facts.md", source=source)
    write_frame(
        root,
        user_statement=statement,
        ai_interpretation="Compare bounded options.",
        user_confirmed=True,
        agreed_frame="Choose a bounded local option.",
    )


def test_migration_rejects_a_valid_but_changed_v1_fingerprint(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = tmp_path / "facts.md"
    source.write_text("first\n", encoding="utf-8")
    _minimal_v1(root, source)
    first = migrate_v1(root, "fingerprint")

    source.write_text("second\n", encoding="utf-8")
    capture_input(root, logical_id="facts.md", source=source)
    write_frame(
        root,
        user_statement="Choose after the source changed.",
        ai_interpretation="Compare bounded options again.",
        user_confirmed=True,
        agreed_frame="Choose a bounded local option again.",
    )
    assert inspect_v1_migration_source(root).fingerprint != first["v1_fingerprint"]

    with pytest.raises(HarnessError) as caught:
        migrate_v1(root, "fingerprint")
    assert caught.value.code == "V1_MIGRATION_FINGERPRINT_CHANGED"
    assert caught.value.exit_code == 3


def test_migration_rejects_target_that_progressed_after_migration(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = tmp_path / "facts.md"
    source.write_text("facts\n", encoding="utf-8")
    _minimal_v1(root, source)
    result = migrate_v1(root, "progressed")
    store = DecisionStore(root, "progressed")
    current = store.get_current()
    store.commit(
        expected_parent=result["snapshot_sha256"],
        refs=dict(current.refs),
        operation="post-migration-change",
    )

    with pytest.raises(HarnessError) as caught:
        migrate_v1(root, "progressed")
    assert caught.value.code == "V1_MIGRATION_TARGET_CONFLICT"
    assert caught.value.details["generation"] == 2


def test_migration_rejects_non_utf8_and_oversized_managed_input(tmp_path: Path) -> None:
    for name, raw in (("binary", b"\xff"), ("oversized", b"x" * (_SOURCE_LIMIT + 1))):
        root = tmp_path / name
        source = tmp_path / f"{name}.txt"
        source.write_bytes(b"initial")
        init_project(root)
        capture_input(root, logical_id="facts.txt", source=source)
        managed = root / ".ai-work-harness" / "inputs" / "facts.txt"
        managed.write_bytes(raw)
        manifest_path = root / ".ai-work-harness" / "input-manifest.json"
        manifest = read_json(manifest_path)
        from ai_work_harness.io_utils import sha256_file

        manifest["inputs"][0]["bytes"] = len(raw)
        manifest["inputs"][0]["sha256"] = sha256_file(managed)
        atomic_write_json(manifest_path, manifest)

        with pytest.raises(HarnessError) as caught:
            inspect_v1_migration_source(root)
        assert caught.value.code == "V1_MIGRATION_SOURCE_INVALID"
        expected = "UTF-8" if name == "binary" else "exceeds"
        assert expected in caught.value.message


def test_migration_preflights_oversized_input_before_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    source = tmp_path / "facts.txt"
    source.write_bytes(b"initial")
    init_project(root)
    capture_input(root, logical_id="facts.txt", source=source)
    managed = root / ".ai-work-harness" / "inputs" / "facts.txt"
    managed.write_bytes(b"x" * (_SOURCE_LIMIT + 1))
    manifest_path = root / ".ai-work-harness" / "input-manifest.json"
    manifest = read_json(manifest_path)
    from ai_work_harness.io_utils import sha256_file

    manifest["inputs"][0]["bytes"] = managed.stat().st_size
    manifest["inputs"][0]["sha256"] = sha256_file(managed)
    atomic_write_json(manifest_path, manifest)

    original = Path.read_bytes

    def guarded_read(path: Path) -> bytes:
        if path == managed:
            raise AssertionError("oversized managed input must not be read")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)

    with pytest.raises(HarnessError) as caught:
        inspect_v1_migration_source(root)
    assert caught.value.code == "V1_MIGRATION_SOURCE_INVALID"
    assert "exceeds" in caught.value.message
    assert caught.value.details["bytes"] == _SOURCE_LIMIT + 1


def test_migration_rechecks_limit_after_managed_input_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    source = tmp_path / "facts.txt"
    source.write_bytes(b"bounded\n")
    _minimal_v1(root, source)
    managed = root / ".ai-work-harness" / "inputs" / "facts.md"
    original = Path.read_bytes

    def grow_during_read(path: Path) -> bytes:
        if path == managed:
            return b"x" * (_SOURCE_LIMIT + 1)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", grow_during_read)

    with pytest.raises(HarnessError) as caught:
        inspect_v1_migration_source(root)
    assert caught.value.code == "V1_MIGRATION_SOURCE_INVALID"
    assert "exceeds" in caught.value.message
    assert caught.value.details["bytes"] == _SOURCE_LIMIT + 1


def test_migration_rejects_symlinked_v1_artifact(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = tmp_path / "facts.md"
    source.write_text("facts\n", encoding="utf-8")
    _minimal_v1(root, source)
    frame = root / ".ai-work-harness" / "frame.json"
    copy = tmp_path / "frame-copy.json"
    copy.write_bytes(frame.read_bytes())
    frame.unlink()
    frame.symlink_to(copy)

    with pytest.raises(HarnessError) as caught:
        inspect_v1_migration_source(root)
    assert caught.value.code == "V1_MIGRATION_SOURCE_INVALID"
