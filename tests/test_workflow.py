from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import PreparedProject

from ai_work_harness.errors import HarnessError
from ai_work_harness.io_utils import atomic_write_json, read_json
from ai_work_harness.project import (
    ProjectPaths,
    capture_input,
    create_approval,
    create_route,
    init_project,
    project_status,
    verify_project,
    write_frame,
)
from ai_work_harness.routing import select_route


def _approval_status(project: PreparedProject) -> dict:
    statuses = project_status(project.root)["approvals"]
    return next(item for item in statuses if item["approval_id"] == project.approval_id)


def test_manifest_contains_only_safe_content_metadata(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = tmp_path / "private-name.txt"
    source.write_text("synthetic content\n", encoding="utf-8")
    init_project(root)
    capture_input(root, logical_id="nested/input.txt", source=source)

    manifest = read_json(ProjectPaths.from_root(root).manifest)
    entry = manifest["inputs"][0]
    assert set(entry) == {"logical_id", "bytes", "sha256"}
    assert entry["logical_id"] == "nested/input.txt"
    serialized = json.dumps(manifest)
    assert str(source.resolve()) not in serialized
    assert "timestamp" not in serialized
    assert "preview" not in serialized


@pytest.mark.parametrize("logical_id", ["../escape", "/absolute", "a/../b", "a\\b"])
def test_capture_rejects_unsafe_logical_ids(tmp_path: Path, logical_id: str) -> None:
    root = tmp_path / "project"
    source = tmp_path / "source.txt"
    source.write_text("data", encoding="utf-8")
    init_project(root)
    with pytest.raises(HarnessError, match="safe relative") as caught:
        capture_input(root, logical_id=logical_id, source=source)
    assert caught.value.code == "UNSAFE_LOGICAL_ID"


def test_capture_rejects_symlink_source(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = tmp_path / "source.txt"
    source.write_text("data", encoding="utf-8")
    symlink = tmp_path / "source-link.txt"
    symlink.symlink_to(source)
    init_project(root)

    with pytest.raises(HarnessError) as caught:
        capture_input(root, logical_id="input.txt", source=symlink)

    assert caught.value.code == "INVALID_SOURCE"


def test_capture_rejects_source_from_managed_storage(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = tmp_path / "source.txt"
    source.write_text("data", encoding="utf-8")
    init_project(root)
    capture_input(root, logical_id="first.txt", source=source)
    managed_source = ProjectPaths.from_root(root).inputs / "first.txt"

    with pytest.raises(HarnessError) as caught:
        capture_input(root, logical_id="second.txt", source=managed_source)

    assert caught.value.code == "SOURCE_IN_MANAGED_STORAGE"


def test_same_profile_produces_byte_identical_route(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = tmp_path / "source.csv"
    source.write_text("id,value\n1,2\n", encoding="utf-8")
    init_project(root)
    capture_input(root, logical_id="input.csv", source=source)
    write_frame(
        root,
        user_statement="Process the records.",
        ai_interpretation="Use deterministic batch processing.",
        user_confirmed=True,
        agreed_frame="Create one local batch artifact.",
    )
    arguments = {
        "input_source": "local_files",
        "modalities": ["json", "csv"],
        "capabilities": ["transform", "aggregate"],
        "objective": "Create output.",
    }
    create_route(root, **arguments)
    route_path = ProjectPaths.from_root(root).route
    first = route_path.read_bytes()
    create_route(
        root,
        input_source="local_files",
        modalities=list(reversed(arguments["modalities"])),
        capabilities=list(reversed(arguments["capabilities"])),
        objective=arguments["objective"],
    )
    second = route_path.read_bytes()
    assert first == second
    assert b"generated_at" not in first


def test_registered_route_fixtures_are_deterministic() -> None:
    cases_path = Path(__file__).parent / "data" / "route_cases.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    for case in cases:
        profile = {
            "input_source": case["input_source"],
            "modalities": case["modalities"],
            "capabilities": case["capabilities"],
        }
        assert select_route(profile) == case["expected_route"]
        assert select_route(profile) == select_route(profile)


def test_input_change_marks_existing_approval_stale(prepared_project: PreparedProject) -> None:
    prepared_project.source.write_bytes(b"id,value\n1,9\n")
    capture_input(
        prepared_project.root,
        logical_id="records.csv",
        source=prepared_project.source,
    )
    status = _approval_status(prepared_project)
    assert status["valid"] is False
    assert "input_changed" in status["stale_reasons"]


@pytest.mark.parametrize(
    ("artifact", "field", "value", "reason"),
    [
        ("frame", "agreed_frame", "Changed frame.", "frame_changed"),
        ("profile", "objective", "Changed objective.", "profile_changed"),
        ("route", "route", "rule_decision", "route_changed"),
    ],
)
def test_artifact_change_marks_existing_approval_stale(
    prepared_project: PreparedProject,
    artifact: str,
    field: str,
    value: str,
    reason: str,
) -> None:
    path = getattr(prepared_project.paths, artifact)
    document = read_json(path)
    document[field] = value
    atomic_write_json(path, document)
    status = _approval_status(prepared_project)
    assert status["valid"] is False
    assert reason in status["stale_reasons"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("decision", "Changed decision.", "decision_changed"),
        ("reason", "Changed reason.", "reason_changed"),
    ],
)
def test_decision_and_reason_are_bound(
    prepared_project: PreparedProject,
    field: str,
    value: str,
    reason: str,
) -> None:
    approval_path = prepared_project.paths.approvals / f"{prepared_project.approval_id}.json"
    document = read_json(approval_path)
    document[field] = value
    atomic_write_json(approval_path, document)
    status = _approval_status(prepared_project)
    assert status["valid"] is False
    assert reason in status["stale_reasons"]
    assert "approval_binding_changed" in status["stale_reasons"]


def test_old_approval_is_retained_after_new_approval(prepared_project: PreparedProject) -> None:
    prepared_project.source.write_bytes(b"id,value\n1,8\n")
    capture_input(
        prepared_project.root,
        logical_id="records.csv",
        source=prepared_project.source,
    )
    write_frame(
        prepared_project.root,
        user_statement="Group the changed records.",
        ai_interpretation="The local batch route still fits.",
        user_confirmed=True,
        agreed_frame="Create a replacement local artifact.",
    )
    create_route(
        prepared_project.root,
        input_source="local_files",
        modalities=["csv"],
        capabilities=["aggregate"],
        objective="Create replacement output.",
    )
    second = create_approval(
        prepared_project.root,
        decision="Approve the replacement route.",
        reason="The changed input has a new confirmed frame.",
    )
    status = project_status(prepared_project.root)
    by_id = {item["approval_id"]: item for item in status["approvals"]}
    assert set(by_id) == {prepared_project.approval_id, second["approval_id"]}
    assert by_id[prepared_project.approval_id]["valid"] is False
    assert by_id[second["approval_id"]]["valid"] is True
    assert verify_project(prepared_project.root)["valid_approvals"] == [second["approval_id"]]


def test_unconfirmed_frame_is_rejected_before_routing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    source = tmp_path / "source.txt"
    source.write_text("data", encoding="utf-8")
    init_project(root)
    capture_input(root, logical_id="input.txt", source=source)
    write_frame(
        root,
        user_statement="Process the file.",
        ai_interpretation="A batch route may fit.",
        user_confirmed=False,
        agreed_frame="Create a local artifact.",
    )
    with pytest.raises(HarnessError) as caught:
        create_route(
            root,
            input_source="local_files",
            modalities=["text"],
            capabilities=["transform"],
            objective="Create output.",
        )
    assert caught.value.code == "FRAME_UNCONFIRMED"


def test_complete_chain_verifies(prepared_project: PreparedProject) -> None:
    result = verify_project(prepared_project.root)
    assert result["ok"] is True
    assert result["route"] == "batch_pipeline"
    assert result["valid_approvals"] == [prepared_project.approval_id]
