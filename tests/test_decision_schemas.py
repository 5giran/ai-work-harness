from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ai_work_harness.decision.canonical import (
    MAX_SAFE_INTEGER,
    canonical_json_bytes,
    parse_json_bytes,
)
from ai_work_harness.decision.models import (
    PUBLIC_ARTIFACT_TYPES,
    ArtifactEnvelope,
    OperationRecord,
    SnapshotRecord,
    discover_v2_payload_schemas,
    validate_all_v2_schemas,
    validate_safe_id,
    validate_v2_document,
)
from ai_work_harness.errors import HarnessError


def test_all_v2_schemas_are_packaged_and_valid() -> None:
    expected = [
        "artifact-envelope",
        "snapshot",
        "current-pointer",
        "decision-view-v1",
        *(f"payload:{item}" for item in sorted(PUBLIC_ARTIFACT_TYPES)),
    ]
    assert validate_all_v2_schemas() == expected
    assert set(discover_v2_payload_schemas()) == PUBLIC_ARTIFACT_TYPES


def test_internal_ref_and_dotted_operation_names_are_distinct_from_human_ids() -> None:
    snapshot = SnapshotRecord(
        session_id="decision-1",
        generation=1,
        parent_snapshot_sha256="0" * 64,
        created_at="2026-08-11T01:02:03.000000Z",
        refs={"decision_frame": "1" * 64},
        operation=OperationRecord(name="frame.import"),
    )
    validate_v2_document("snapshot", snapshot.to_document())
    with pytest.raises(HarnessError, match="path-safe"):
        validate_safe_id("decision_frame")


def test_artifact_envelope_has_exact_fields_and_closed_producer() -> None:
    envelope = ArtifactEnvelope.create(
        artifact_type="decision-frame",
        session_id="decision-1",
        producer={"kind": "local_operator"},
        payload={
            "user_statement_verbatim": "Choose safely.",
            "ai_initial_interpretation": "An auditable choice is needed.",
            "business_user": None,
            "blocked_decision": None,
            "problem_statement": None,
            "scope_in": ["included"],
            "scope_out": [],
            "assumptions": [],
            "open_questions": [],
        },
    )
    assert set(envelope.to_document()) == {
        "schema_version",
        "artifact_type",
        "session_id",
        "producer",
        "parents",
        "payload",
    }
    invalid = envelope.to_document()
    invalid["producer"]["model"] = "must-live-in-agent-run"
    with pytest.raises(HarnessError) as caught:
        validate_v2_document("artifact-envelope", invalid)
    assert caught.value.code == "SCHEMA_VALIDATION_FAILED"


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b'{"same":1,"same":2}', "DUPLICATE_JSON_KEY"),
        (b'{"number":1.0}', "INVALID_IJSON"),
        (b'{"number":NaN}', "INVALID_IJSON"),
        (f'{{"number":{MAX_SAFE_INTEGER + 1}}}'.encode(), "INVALID_IJSON"),
        (b'"\\ud800"', "INVALID_IJSON"),
        (b"\xef\xbb\xbf{}", "INVALID_JSON"),
        (b'"\xff"', "INVALID_JSON"),
    ],
)
def test_strict_parser_rejects_non_ijson(raw: bytes, code: str) -> None:
    with pytest.raises(HarnessError) as caught:
        parse_json_bytes(raw)
    assert caught.value.code == code


def test_rfc8785_subset_uses_utf16_property_order_and_no_trailing_newline() -> None:
    # U+10000 starts with UTF-16 D800 and therefore sorts before U+E000 under RFC 8785.
    document = {"\ue000": 2, "\U00010000": 1, "escaped": "line\n"}
    encoded = canonical_json_bytes(document)
    assert encoded == '{"escaped":"line\\n","𐀀":1,"":2}'.encode()
    assert not encoded.endswith(b"\n")


def test_artifact_payload_is_deeply_frozen() -> None:
    envelope = ArtifactEnvelope.create(
        artifact_type="decision-frame",
        session_id="decision-1",
        producer={"kind": "core"},
        payload={
            "user_statement_verbatim": "Choose safely.",
            "ai_initial_interpretation": "An auditable choice is needed.",
            "business_user": None,
            "blocked_decision": None,
            "problem_statement": None,
            "scope_in": ["included"],
            "scope_out": [],
            "assumptions": [],
            "open_questions": [],
        },
    )
    with pytest.raises(TypeError):
        envelope.payload["new"] = True  # type: ignore[index]
    with pytest.raises(TypeError):
        envelope.payload["scope_in"][0] = "changed"  # type: ignore[index]


def test_unknown_artifact_type_is_fail_closed() -> None:
    with pytest.raises(HarnessError) as caught:
        ArtifactEnvelope.create(
            artifact_type="undeclared-artifact",
            session_id="decision-1",
            producer={"kind": "core"},
            payload={},
        )
    assert caught.value.code == "UNKNOWN_ARTIFACT_TYPE"


def test_migration_agent_run_and_outbound_consent_payload_contracts() -> None:
    digest = "a" * 64
    event_id = "12345678-1234-4234-9234-123456789abc"
    validate_v2_document(
        "payload:migration-report",
        {
            "v1_fingerprint": digest,
            "v1_artifacts": [{"path": "manifest.json", "sha256": digest, "bytes": 10}],
            "migrated_sources": [
                {
                    "source_id": "brief",
                    "v1_logical_id": "inputs/brief.txt",
                    "blob_sha256": digest,
                    "bytes": 10,
                }
            ],
            "frame_migrated": True,
            "confirmations_promoted": False,
            "approvals_promoted": False,
        },
    )
    validate_v2_document(
        "payload:human-confirmation",
        {
            "confirmation_id": event_id,
            "subject_type": "outbound-manifest",
            "subject_sha256": digest,
            "actor_label": "local_operator",
            "identity_verified": False,
            "method": "digest_challenge",
            "confirmed_at": "2026-08-11T01:02:03Z",
            "outbound_manifest": {
                "provider": "openai",
                "model": "gpt-5.6-luna",
                "operation": "evaluations",
                "prompt_id": "evaluation-v1",
                "prompt_sha256": digest,
                "input_sha256": digest,
                "input_snapshot_sha256": digest,
                "source_sha256s": [digest],
                "evidence_count": 1,
                "source_excerpt_bytes": 10,
                "context_bytes": 100,
                "reasoning_effort": "medium",
                "max_tool_rounds": 12,
                "max_output_tokens": 8000,
                "timeout_seconds": 60,
                "max_retries": 2,
                "max_context_bytes": 1000000,
                "max_lookup_bytes": 1000000,
            },
        },
    )
    validate_v2_document(
        "payload:agent-run",
        {
            "run_id": event_id,
            "provider": "openai",
            "model": "gpt-5.6-luna",
            "operation": "evaluations",
            "prompt_id": "evaluation-v1",
            "prompt_sha256": digest,
            "input_sha256": digest,
            "input_snapshot_sha256": digest,
            "outbound_manifest_sha256": digest,
            "transcript_sha256": digest,
            "tool_name": "submit_evaluations",
            "response_id": None,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "result_sha256": digest,
            "status": "succeeded",
            "error_code": None,
            "started_at": "2026-08-11T01:02:03Z",
            "completed_at": "2026-08-11T01:02:04Z",
        },
    )


def test_timestamp_input_is_ijson_but_semantic_schema_is_strict() -> None:
    with pytest.raises(HarnessError) as caught:
        SnapshotRecord(
            session_id="decision-1",
            generation=0,
            parent_snapshot_sha256=None,
            created_at=datetime.now(UTC).isoformat(),
            refs={},
        )
    assert caught.value.code == "SCHEMA_VALIDATION_FAILED"
