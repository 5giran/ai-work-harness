from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_work_harness.decision.canonical import canonical_json_bytes, sha256_bytes
from ai_work_harness.decision.models import ArtifactEnvelope, OperationRecord
from ai_work_harness.decision.store import DecisionStore
from ai_work_harness.errors import HarnessError


@dataclass
class FixedClock:
    instant: datetime = datetime(2026, 8, 11, 1, 2, 3, tzinfo=UTC)

    def now(self) -> datetime:
        return self.instant


@dataclass
class FixedIds:
    value: str = "12345678-1234-4234-9234-123456789abc"

    def new_id(self) -> str:
        return self.value


def make_store(
    root: Path,
    *,
    session_id: str = "decision-1",
    lock_timeout: float = 5.0,
) -> DecisionStore:
    return DecisionStore(
        root,
        session_id,
        clock=FixedClock(),
        id_source=FixedIds(),
        lock_timeout=lock_timeout,
    )


def put_frame(store: DecisionStore, *, parents: dict[str, str] | None = None) -> str:
    return store.put_artifact(
        ArtifactEnvelope.create(
            artifact_type="decision-frame",
            session_id=store.session_id,
            producer={"kind": "local_operator"},
            parents=parents,
            payload={
                "user_statement_verbatim": "Choose an auditable option.",
                "ai_initial_interpretation": "The choice requires evidence.",
                "business_user": None,
                "blocked_decision": None,
                "problem_statement": None,
                "scope_in": [],
                "scope_out": [],
                "assumptions": [],
                "open_questions": [],
            },
        )
    )


def put_candidates(store: DecisionStore) -> str:
    return store.put_artifact(
        ArtifactEnvelope.create(
            artifact_type="candidate-set",
            session_id=store.session_id,
            producer={"kind": "fixture"},
            payload={
                "candidates": [
                    {
                        "candidate_id": candidate_id,
                        "title": candidate_id.title(),
                        "summary": f"Use {candidate_id}.",
                        "proposed_by": "fixture",
                        "benefits": [],
                        "drawbacks": [],
                        "risks": [],
                        "uncertainties": [],
                    }
                    for candidate_id in ("one", "two")
                ]
            },
        )
    )


def test_initialize_writes_canonical_snapshot_and_pointer(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    initialized = store.initialize()

    assert initialized.generation == 0
    assert initialized.refs == {}
    assert initialized.parent_snapshot_sha256 is None
    assert store.get_current().sha256 == initialized.sha256
    assert store.paths.current_pointer.read_bytes() == canonical_json_bytes(
        store.read_pointer().to_document()
    )
    assert store.paths.snapshot_path(initialized.sha256).read_bytes() == canonical_json_bytes(
        initialized.snapshot.to_document()
    )
    assert store.verify().ok is True


def test_artifact_commit_is_content_addressed_and_preserves_complete_refs(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    initial = store.initialize()
    frame_sha = put_frame(store)
    assert put_frame(store) == frame_sha

    committed = store.commit(
        expected_parent=initial.sha256,
        refs={"decision_frame": frame_sha},
        operation="frame.import",
    )

    assert committed.generation == 1
    assert dict(committed.refs) == {"decision_frame": frame_sha}
    assert committed.parent_snapshot_sha256 == initial.sha256
    assert store.read_artifact(frame_sha).payload["user_statement_verbatim"].startswith("Choose")
    assert store.verify().ok is True


def test_commit_rejects_reference_and_parent_type_confusion(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    initial = store.initialize()
    candidate_sha = put_candidates(store)

    with pytest.raises(HarnessError) as wrong_ref:
        store.commit(
            expected_parent=initial.sha256,
            refs={"decision_frame": candidate_sha},
            operation="frame.import",
        )
    assert wrong_ref.value.code == "INVALID_SNAPSHOT_REFS"
    assert wrong_ref.value.details["first_issue"] == "ARTIFACT_TYPE_MISMATCH"

    frame_sha = put_frame(store, parents={"source_manifest": candidate_sha})
    with pytest.raises(HarnessError) as wrong_parent:
        store.commit(
            expected_parent=initial.sha256,
            refs={"decision_frame": frame_sha},
            operation="frame.import",
        )
    assert wrong_parent.value.code == "INVALID_SNAPSHOT_REFS"
    assert wrong_parent.value.details["first_issue"] == "ARTIFACT_TYPE_MISMATCH"
    assert store.get_current().sha256 == initial.sha256


def test_expected_parent_conflict_does_not_change_pointer(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    initial = store.initialize()
    frame_sha = put_frame(store)
    committed = store.commit(
        expected_parent=initial.sha256,
        refs={"decision_frame": frame_sha},
        operation="frame.import",
    )
    pointer_before = store.paths.current_pointer.read_bytes()

    with pytest.raises(HarnessError) as caught:
        store.commit(
            expected_parent=initial.sha256,
            refs={},
            operation="criteria.import",
        )

    assert caught.value.code == "WRITE_CONFLICT"
    assert store.paths.current_pointer.read_bytes() == pointer_before
    assert store.get_current().sha256 == committed.sha256


def test_commit_rejects_missing_artifact_without_pointer_change(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    initial = store.initialize()
    pointer_before = store.paths.current_pointer.read_bytes()

    with pytest.raises(HarnessError) as caught:
        store.commit(
            expected_parent=initial.sha256,
            refs={"criteria_set": "f" * 64},
            operation="criteria.import",
        )

    assert caught.value.code == "INVALID_SNAPSHOT_REFS"
    assert store.paths.current_pointer.read_bytes() == pointer_before


def test_object_tamper_is_detected_by_read_and_verify(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    initial = store.initialize()
    frame_sha = put_frame(store)
    store.commit(
        expected_parent=initial.sha256,
        refs={"decision_frame": frame_sha},
        operation="frame.import",
    )
    store.paths.object_path(frame_sha).write_bytes(b"tampered")

    with pytest.raises(HarnessError) as caught:
        store.read_artifact(frame_sha)
    assert caught.value.code == "OBJECT_INTEGRITY_FAILED"
    report = store.verify()
    assert report.ok is False
    assert any(issue.code == "OBJECT_INTEGRITY_FAILED" for issue in report.issues)


def test_snapshot_tamper_is_detected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    initialized = store.initialize()
    path = store.paths.snapshot_path(initialized.sha256)
    path.write_bytes(path.read_bytes() + b" ")

    with pytest.raises(HarnessError) as caught:
        store.get_current()
    assert caught.value.code == "SNAPSHOT_INTEGRITY_FAILED"
    assert store.verify().ok is False


def test_pointer_generation_tamper_is_detected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    initialized = store.initialize()
    pointer = store.read_pointer().to_document()
    pointer["generation"] = 1
    store.paths.current_pointer.write_bytes(canonical_json_bytes(pointer))

    with pytest.raises(HarnessError) as caught:
        store.get_current()
    assert caught.value.code == "POINTER_INTEGRITY_FAILED"
    report = store.verify()
    assert report.ok is False
    assert report.snapshot_sha256 == initialized.sha256


def test_dangling_pointer_is_detected_as_integrity_failure(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.initialize()
    pointer = store.read_pointer().to_document()
    pointer["snapshot_sha256"] = "f" * 64
    store.paths.current_pointer.write_bytes(canonical_json_bytes(pointer))

    with pytest.raises(HarnessError) as caught:
        store.get_current()
    assert caught.value.code == "SNAPSHOT_MISSING"
    report = store.verify()
    assert report.ok is False
    assert report.issues[0].code == "SNAPSHOT_MISSING"


def test_noncanonical_pointer_is_rejected(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.initialize()
    pointer = store.read_pointer().to_document()
    store.paths.current_pointer.write_text(json.dumps(pointer, indent=2), encoding="utf-8")

    with pytest.raises(HarnessError) as caught:
        store.read_pointer()
    assert caught.value.code == "POINTER_INTEGRITY_FAILED"


def test_doctor_reports_unreferenced_objects_without_treating_them_as_corruption(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    store.initialize()
    orphan_sha = store.put_blob(b"orphan after a safe interrupted transaction")

    report = store.doctor()

    assert report.ok is True
    assert report.orphan_objects == (orphan_sha,)
    assert report.issues == ()


def test_doctor_uses_project_global_object_reachability_but_session_local_snapshots(
    tmp_path: Path,
) -> None:
    selected = make_store(tmp_path, session_id="decision-1")
    selected_current = selected.initialize()
    other = make_store(tmp_path, session_id="decision-2")
    other_initial = other.initialize()
    other_blob_sha = other.put_blob(b"other session source\n")
    other_manifest_sha = other.put_artifact(
        ArtifactEnvelope.create(
            artifact_type="source-manifest",
            session_id=other.session_id,
            producer={"kind": "local_operator"},
            payload={
                "sources": [
                    {
                        "source_id": "other-source",
                        "media_type": "text/plain",
                        "bytes": 21,
                        "blob_sha256": other_blob_sha,
                    }
                ]
            },
        )
    )
    other.commit(
        expected_parent=other_initial.sha256,
        refs={"source_manifest": other_manifest_sha},
        operation="source.capture",
    )
    actual_orphan_sha = selected.put_blob(b"unreferenced")

    report = selected.doctor()

    assert report.ok is True
    assert report.reachable_snapshots == (selected_current.sha256,)
    assert report.orphan_snapshots == ()
    assert other_blob_sha in report.referenced_objects
    assert other_manifest_sha in report.referenced_objects
    assert report.orphan_objects == (actual_orphan_sha,)


def test_doctor_reports_corrupt_other_session_and_does_not_mark_its_objects_live(
    tmp_path: Path,
) -> None:
    selected = make_store(tmp_path, session_id="decision-1")
    selected.initialize()
    other = make_store(tmp_path, session_id="decision-2")
    other_initial = other.initialize()
    other_frame_sha = put_frame(other)
    other_current = other.commit(
        expected_parent=other_initial.sha256,
        refs={"decision_frame": other_frame_sha},
        operation="frame.import",
    )
    other.paths.snapshot_path(other_current.sha256).write_bytes(b"corrupt")

    report = selected.doctor()

    assert report.ok is False
    assert any(
        issue.code == "SNAPSHOT_INTEGRITY_FAILED" and "decision-2" in issue.message
        for issue in report.issues
    )
    assert other_frame_sha not in report.referenced_objects
    assert other_frame_sha in report.orphan_objects


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink creation is privilege-dependent")
def test_doctor_rejects_symlinked_other_session_without_traversing_it(tmp_path: Path) -> None:
    selected = make_store(tmp_path)
    selected.initialize()
    outside = tmp_path / "outside-session"
    outside.mkdir()
    escaped_session = selected.paths.v2_root / "sessions" / "escaped-session"
    escaped_session.symlink_to(outside, target_is_directory=True)

    report = selected.doctor()

    assert report.ok is False
    assert any(
        issue.code == "UNSAFE_STORAGE_PATH" and issue.path == str(escaped_session)
        for issue in report.issues
    )


def test_source_manifest_blob_is_a_reachable_verified_object(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    initial = store.initialize()
    blob_sha = store.put_blob(b"line one\n")
    manifest_sha = store.put_artifact(
        ArtifactEnvelope.create(
            artifact_type="source-manifest",
            session_id=store.session_id,
            producer={"kind": "local_operator"},
            payload={
                "sources": [
                    {
                        "source_id": "brief",
                        "media_type": "text/plain",
                        "bytes": 9,
                        "blob_sha256": blob_sha,
                    }
                ]
            },
        )
    )
    store.commit(
        expected_parent=initial.sha256,
        refs={"source_manifest": manifest_sha},
        operation="source.capture",
    )

    report = store.doctor()
    assert blob_sha in report.referenced_objects
    assert blob_sha not in report.orphan_objects

    store.paths.object_path(blob_sha).write_bytes(b"tampered\n")
    verification = store.verify()
    assert verification.ok is False
    assert any(issue.code == "OBJECT_INTEGRITY_FAILED" for issue in verification.issues)


def test_verification_rejects_false_blob_sizes_and_duplicate_source_ids(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    initial = store.initialize()
    blob_sha = store.put_blob(b"x")
    manifest_sha = store.put_artifact(
        ArtifactEnvelope.create(
            artifact_type="source-manifest",
            session_id=store.session_id,
            producer={"kind": "local_operator"},
            payload={
                "sources": [
                    {
                        "source_id": "same",
                        "media_type": "text/plain",
                        "bytes": 999,
                        "blob_sha256": blob_sha,
                    },
                    {
                        "source_id": "same",
                        "media_type": "text/markdown",
                        "bytes": 1,
                        "blob_sha256": blob_sha,
                    },
                ]
            },
        )
    )

    with pytest.raises(HarnessError) as caught:
        store.commit(
            expected_parent=initial.sha256,
            refs={"source_manifest": manifest_sha},
            operation="source.capture",
        )

    assert caught.value.code == "INVALID_SNAPSHOT_REFS"
    issue_codes = {item["code"] for item in caught.value.details["issues"]}
    assert issue_codes == {"BLOB_SIZE_MISMATCH", "DUPLICATE_SOURCE_ID"}
    assert store.get_current().sha256 == initial.sha256


@pytest.mark.parametrize("artifact_type", ["source-manifest", "migration-report"])
def test_verification_rejects_non_utf8_text_blobs_even_with_valid_digest_and_size(
    tmp_path: Path,
    artifact_type: str,
) -> None:
    store = make_store(tmp_path)
    initial = store.initialize()
    raw = b"\xff\xfe"
    blob_sha = store.put_blob(raw)
    if artifact_type == "source-manifest":
        ref_name = "source_manifest"
        payload = {
            "sources": [
                {
                    "source_id": "bad-text",
                    "media_type": "text/plain",
                    "bytes": len(raw),
                    "blob_sha256": blob_sha,
                }
            ]
        }
    else:
        ref_name = "migration_report"
        payload = {
            "v1_fingerprint": "a" * 64,
            "v1_artifacts": [],
            "migrated_sources": [
                {
                    "source_id": "bad-text",
                    "v1_logical_id": "input.txt",
                    "blob_sha256": blob_sha,
                    "bytes": len(raw),
                }
            ],
            "frame_migrated": False,
            "confirmations_promoted": False,
            "approvals_promoted": False,
        }
    artifact_sha = store.put_artifact(
        ArtifactEnvelope.create(
            artifact_type=artifact_type,
            session_id=store.session_id,
            producer={
                "kind": ("migration" if artifact_type == "migration-report" else "local_operator")
            },
            payload=payload,
        )
    )

    with pytest.raises(HarnessError) as caught:
        store.commit(
            expected_parent=initial.sha256,
            refs={ref_name: artifact_sha},
            operation="migration" if artifact_type == "migration-report" else "source.capture",
        )

    assert caught.value.code == "INVALID_SNAPSHOT_REFS"
    assert {item["code"] for item in caught.value.details["issues"]} == {"BLOB_ENCODING_INVALID"}
    assert store.get_current().sha256 == initial.sha256


def test_source_blob_that_looks_like_an_artifact_remains_opaque(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    initial = store.initialize()
    foreign_envelope = ArtifactEnvelope.create(
        artifact_type="decision-frame",
        session_id="foreign-session",
        producer={"kind": "fixture"},
        payload={
            "user_statement_verbatim": "Opaque source text.",
            "ai_initial_interpretation": "This is source content, not an active artifact.",
            "business_user": None,
            "blocked_decision": None,
            "problem_statement": None,
            "scope_in": [],
            "scope_out": [],
            "assumptions": [],
            "open_questions": [],
        },
    )
    raw = canonical_json_bytes(foreign_envelope.to_document())
    blob_sha = store.put_blob(raw)
    manifest_sha = store.put_artifact(
        ArtifactEnvelope.create(
            artifact_type="source-manifest",
            session_id=store.session_id,
            producer={"kind": "local_operator"},
            payload={
                "sources": [
                    {
                        "source_id": "artifact-shaped-source",
                        "media_type": "text/plain",
                        "bytes": len(raw),
                        "blob_sha256": blob_sha,
                    }
                ]
            },
        )
    )
    store.commit(
        expected_parent=initial.sha256,
        refs={"source_manifest": manifest_sha},
        operation="source.capture",
    )

    assert store.verify().ok is True
    assert blob_sha in store.doctor().referenced_objects


def test_writer_lock_times_out_without_changing_pointer(tmp_path: Path) -> None:
    store = make_store(tmp_path, lock_timeout=0.01)
    initial = store.initialize()
    pointer_before = store.paths.current_pointer.read_bytes()
    store.paths.writer_lock.write_text("held", encoding="utf-8")

    with pytest.raises(HarnessError) as caught:
        store.commit(
            expected_parent=initial.sha256,
            refs={},
            operation="frame.import",
        )

    assert caught.value.code == "WRITE_LOCK_TIMEOUT"
    assert store.paths.current_pointer.read_bytes() == pointer_before


def test_two_writers_with_one_expected_parent_allow_exactly_one_commit(tmp_path: Path) -> None:
    first_store = make_store(tmp_path)
    second_store = make_store(tmp_path)
    initial = first_store.initialize()
    barrier = threading.Barrier(2)

    def write(store: DecisionStore, operation: str) -> str:
        barrier.wait()
        try:
            return store.commit(
                expected_parent=initial.sha256,
                refs={},
                operation=operation,
            ).sha256
        except HarnessError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda item: write(*item),
                [(first_store, "frame.import"), (second_store, "criteria.import")],
            )
        )

    assert outcomes.count("WRITE_CONFLICT") == 1
    assert len([value for value in outcomes if value != "WRITE_CONFLICT"]) == 1
    assert first_store.verify().ok is True


def test_failure_after_snapshot_write_leaves_pointer_old_and_snapshot_orphaned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    initial = store.initialize()

    def fail_pointer_write(_stored: object) -> None:
        raise OSError("injected pointer failure")

    monkeypatch.setattr(store, "_write_pointer", fail_pointer_write)
    with pytest.raises(OSError, match="injected pointer failure"):
        store.commit(
            expected_parent=initial.sha256,
            refs={},
            operation="frame.import",
        )

    assert store.get_current().sha256 == initial.sha256
    report = store.doctor()
    assert report.ok is True
    assert len(report.orphan_snapshots) == 1


def test_object_and_snapshot_write_failures_leave_pointer_byte_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = make_store(tmp_path)
    initial = store.initialize()
    pointer_before = store.paths.current_pointer.read_bytes()

    original_write = store._write_immutable

    def fail_object(path: Path, value: bytes) -> None:
        if path.parent.parent == store.paths.objects_root:
            raise OSError("injected object failure")
        original_write(path, value)

    monkeypatch.setattr(store, "_write_immutable", fail_object)
    with pytest.raises(OSError, match="object failure"):
        put_frame(store)
    assert store.paths.current_pointer.read_bytes() == pointer_before

    monkeypatch.setattr(store, "_write_immutable", original_write)
    monkeypatch.setattr(
        store,
        "_store_snapshot",
        lambda _snapshot: (_ for _ in ()).throw(OSError("injected snapshot failure")),
    )
    with pytest.raises(OSError, match="snapshot failure"):
        store.commit(expected_parent=initial.sha256, refs={}, operation="frame.import")
    assert store.paths.current_pointer.read_bytes() == pointer_before
    assert store.get_current().sha256 == initial.sha256


def test_idempotency_replay_returns_original_snapshot_and_collision_fails(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    initial = store.initialize()
    request_sha = sha256_bytes(b"request-one")
    operation = OperationRecord(
        name="frame.import",
        idempotency_key="request-1",
        request_sha256=request_sha,
    )
    first = store.commit(expected_parent=initial.sha256, refs={}, operation=operation)
    second = store.commit(expected_parent=first.sha256, refs={}, operation="status.touch")

    replay = store.commit(expected_parent=initial.sha256, refs={}, operation=operation)
    assert replay.sha256 == first.sha256
    assert store.get_current().sha256 == second.sha256

    with pytest.raises(HarnessError) as caught:
        store.commit(
            expected_parent=second.sha256,
            refs={},
            operation=OperationRecord(
                name="frame.import",
                idempotency_key="request-1",
                request_sha256=sha256_bytes(b"different-request"),
            ),
        )
    assert caught.value.code == "IDEMPOTENCY_KEY_REUSE"

    with pytest.raises(HarnessError) as different_operation:
        store.commit(
            expected_parent=second.sha256,
            refs={},
            operation=OperationRecord(
                name="criteria.import",
                idempotency_key="request-1",
                request_sha256=request_sha,
            ),
        )
    assert different_operation.value.code == "IDEMPOTENCY_KEY_REUSE"


def test_safe_session_id_and_injected_id_source(tmp_path: Path) -> None:
    with pytest.raises(HarnessError) as caught:
        DecisionStore(tmp_path, "../escape")
    assert caught.value.code == "UNSAFE_ID"
    assert make_store(tmp_path).new_event_id() == "12345678-1234-4234-9234-123456789abc"
