from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_work_harness.decision import canonical
from ai_work_harness.decision.models import (
    ArtifactEnvelope,
    CurrentPointer,
    DoctorReport,
    IntegrityIssue,
    OperationRecord,
    SnapshotRecord,
    SystemClock,
    UUID4IdSource,
    VerificationReport,
    discover_v2_payload_schemas,
    format_timestamp,
    load_v2_schema,
    validate_all_v2_schemas,
    validate_artifact_payload,
    validate_digest,
    validate_event_id,
    validate_ref_key,
    validate_safe_id,
    validate_v2_document,
)
from ai_work_harness.errors import HarnessError


@pytest.mark.parametrize(
    ("value", "path"),
    [
        (1.5, "$"),
        ((1 << 53), "$"),
        ({1: "value"}, "$"),
        ({"bad": "\ud800"}, "$.bad"),
        ({"bad": object()}, "$.bad"),
    ],
)
def test_ijson_rejects_non_portable_values_with_paths(value: object, path: str) -> None:
    with pytest.raises(HarnessError) as caught:
        canonical.validate_ijson(value)
    assert caught.value.code == "INVALID_IJSON"
    assert caught.value.details["path"] == path


def test_ijson_rejects_cycles_and_accepts_scalar_and_nested_sequences() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(HarnessError, match="Cyclic"):
        canonical.validate_ijson({"cycle": cyclic})

    canonical.validate_ijson(
        {"none": None, "yes": True, "no": False, "integer": -1, "items": ["ok", 2]}
    )


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"\xff", "INVALID_JSON"),
        (b'{"a":1,"a":2}', "DUPLICATE_JSON_KEY"),
        (b"\xef\xbb\xbf{}", "INVALID_JSON"),
        (b"{", "INVALID_JSON"),
        (b'{"n":9007199254740992}', "INVALID_IJSON"),
        (b'{"n":1.0}', "INVALID_IJSON"),
        (b'{"n":NaN}', "INVALID_IJSON"),
    ],
)
def test_strict_json_parser_rejects_ambiguous_inputs(raw: bytes, code: str) -> None:
    with pytest.raises(HarnessError) as caught:
        canonical.parse_json_bytes(raw, label="fixture")
    assert caught.value.code == code


def test_read_json_file_and_canonical_encoder_seams(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(HarnessError) as caught:
        canonical.read_json_file(missing)
    assert caught.value.code == "ARTIFACT_MISSING"

    document = tmp_path / "data.json"
    document.write_text('{"z":1,"a":[true,null,"한글"]}', encoding="utf-8")
    assert canonical.read_json_file(document) == {"z": 1, "a": [True, None, "한글"]}
    assert canonical.canonical_json_bytes({"z": 1, "a": [True, None, "한글"]}) == (
        b'{"a":[true,null,"\xed\x95\x9c\xea\xb8\x80"],"z":1}'
    )
    assert canonical.canonical_json_bytes(False, encoder=lambda _value: b"false") == b"false"
    with pytest.raises(TypeError, match="bytes"):
        canonical.canonical_json_bytes({}, encoder=lambda _value: "{}")  # type: ignore[arg-type]
    assert canonical.digest_json({"a": 1}) == canonical.sha256_bytes(b'{"a":1}')


def test_builtin_canonical_encoder_covers_the_supported_ijson_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(canonical, "_rfc8785", None)
    value = {
        "null": None,
        "true": True,
        "false": False,
        "integer": -7,
        "text": "한글",
        "array": [1, "two"],
    }
    encoded = canonical.canonical_json_bytes(value)
    assert encoded == (
        b'{"array":[1,"two"],"false":false,"integer":-7,"null":null,'
        b'"text":"\xed\x95\x9c\xea\xb8\x80","true":true}'
    )


def test_read_json_file_wraps_os_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "blocked.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(Path, "read_bytes", lambda _self: (_ for _ in ()).throw(OSError("no")))
    with pytest.raises(HarnessError) as caught:
        canonical.read_json_file(path, label="blocked")
    assert caught.value.code == "ARTIFACT_READ_FAILED"


def test_schema_registry_and_validation_fail_closed() -> None:
    discovered = discover_v2_payload_schemas()
    assert "decision-frame" in discovered
    assert set(validate_all_v2_schemas()) >= {
        "artifact-envelope",
        "snapshot",
        "current-pointer",
        "payload:decision-frame",
    }
    assert load_v2_schema("payload:source-manifest")["$schema"].endswith("2020-12/schema")

    with pytest.raises(HarnessError) as unknown_payload:
        load_v2_schema("payload:not-real")
    assert unknown_payload.value.code == "UNKNOWN_ARTIFACT_TYPE"
    with pytest.raises(HarnessError) as unknown_schema:
        load_v2_schema("not-real")
    assert unknown_schema.value.code == "UNKNOWN_SCHEMA"
    with pytest.raises(HarnessError) as invalid:
        validate_v2_document("current-pointer", {"schema_version": "2.0"})
    assert invalid.value.code == "SCHEMA_VALIDATION_FAILED"
    assert invalid.value.details["errors"]
    with pytest.raises(HarnessError):
        validate_artifact_payload("source-manifest", {"sources": [], "extra": True})


@pytest.mark.parametrize(
    ("validator", "value", "code"),
    [
        (validate_safe_id, "Bad/Path", "UNSAFE_ID"),
        (validate_ref_key, "bad.path", "UNSAFE_REF_KEY"),
        (validate_digest, "A" * 64, "INVALID_DIGEST"),
        (validate_event_id, "00000000-0000-1000-8000-000000000000", "INVALID_EVENT_ID"),
    ],
)
def test_model_scalar_validators_reject_unsafe_values(validator, value: str, code: str) -> None:
    with pytest.raises(HarnessError) as caught:
        validator(value)
    assert caught.value.code == code


def test_model_round_trips_freeze_payloads_and_reports() -> None:
    digest = "a" * 64
    event_id = UUID4IdSource().new_id()
    validate_event_id(event_id)
    assert SystemClock().now().tzinfo is not None
    assert format_timestamp(datetime(2026, 8, 11, tzinfo=UTC)).endswith("Z")
    with pytest.raises(HarnessError, match="timezone-aware"):
        format_timestamp(datetime(2026, 8, 11))

    artifact = ArtifactEnvelope.create(
        artifact_type="source-manifest",
        session_id="demo",
        producer={"kind": "core"},
        payload={
            "sources": [
                {
                    "source_id": "source",
                    "media_type": "text/plain",
                    "bytes": 1,
                    "blob_sha256": digest,
                }
            ]
        },
    )
    rebuilt = ArtifactEnvelope.from_document(artifact.to_document())
    assert rebuilt.to_document() == artifact.to_document()

    operation = OperationRecord("source.capture", "request-1", digest)
    assert OperationRecord.from_document(operation.to_document()) == operation
    snapshot = SnapshotRecord(
        session_id="demo",
        generation=1,
        parent_snapshot_sha256=digest,
        created_at="2026-08-11T00:00:00.000000Z",
        refs={"source_manifest": digest},
        operation=operation,
    )
    rebuilt_snapshot = SnapshotRecord.from_document(snapshot.to_document())
    assert rebuilt_snapshot.to_document() == snapshot.to_document()
    pointer = CurrentPointer("demo", 1, digest)
    assert CurrentPointer.from_document(pointer.to_document()) == pointer

    issue = IntegrityIssue("BROKEN", "broken", "where")
    verification = VerificationReport(False, digest, 1, (issue,)).as_dict()
    doctor = DoctorReport(False, digest, (digest,), (), (digest,), (), (issue,)).as_dict()
    assert verification["issues"][0]["code"] == "BROKEN"
    assert doctor["reachable_snapshots"] == [digest]
    json.dumps(doctor)


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (("bad operation",), "INVALID_OPERATION_NAME"),
        (("valid", "key", None), "INVALID_IDEMPOTENCY_RECORD"),
        (("valid", "bad key!", "a" * 64), "INVALID_IDEMPOTENCY_KEY"),
    ],
)
def test_operation_record_invariants(arguments: tuple[object, ...], code: str) -> None:
    with pytest.raises(HarnessError) as caught:
        OperationRecord(*arguments)  # type: ignore[arg-type]
    assert caught.value.code == code
