from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_work_harness.decision.canonical import sha256_bytes
from ai_work_harness.decision.models import ArtifactEnvelope
from ai_work_harness.decision.service import DecisionService
from ai_work_harness.decision.store import DecisionStore
from ai_work_harness.errors import HarnessError


@dataclass
class MutableClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def _service(tmp_path: Path, session_id: str = "edges") -> tuple[DecisionService, MutableClock]:
    clock = MutableClock(datetime(2026, 8, 11, 0, 0, tzinfo=UTC))
    store = DecisionStore(tmp_path, session_id)
    return DecisionService(tmp_path, session_id, store=store, clock=clock), clock


def _frame(*, complete: bool = True) -> dict[str, Any]:
    return {
        "user_statement_verbatim": "Choose a local option.",
        "ai_initial_interpretation": "Compare two options.",
        "business_user": "operator" if complete else None,
        "blocked_decision": "which option" if complete else None,
        "problem_statement": "Choose one option." if complete else None,
        "scope_in": ["synthetic evidence"],
        "scope_out": [],
        "assumptions": [],
        "open_questions": [],
    }


def _candidates() -> dict[str, Any]:
    return {
        "candidates": [
            {
                "candidate_id": candidate,
                "title": candidate.title(),
                "summary": f"Use option {candidate}.",
                "proposed_by": "fixture",
                "benefits": [],
                "drawbacks": [],
                "risks": [],
                "uncertainties": [],
            }
            for candidate in ("one", "two")
        ]
    }


def _criteria() -> dict[str, Any]:
    return {
        "criteria": [
            {
                "criterion_id": criterion,
                "title": criterion.title(),
                "definition": f"Assess {criterion}.",
                "priority": priority,
            }
            for criterion, priority in (
                ("privacy", "must"),
                ("quality", "high"),
                ("latency", "medium"),
            )
        ]
    }


def _evaluations(*, shared_quality_risk: bool = False) -> dict[str, Any]:
    assessments = {
        ("one", "privacy"): "meets",
        ("one", "quality"): "partial",
        ("one", "latency"): "meets",
        ("two", "privacy"): "meets",
        ("two", "quality"): "partial" if shared_quality_risk else "meets",
        ("two", "latency"): "meets",
    }
    return {
        "cells": [
            {
                "candidate_id": candidate,
                "criterion_id": criterion,
                "assessment": assessments[(candidate, criterion)],
                "rationale": "The synthetic source supports this draft.",
                "evidence_ids": ["evidence"],
                "confidence": "high",
                "uncertainties": [],
            }
            for candidate in ("one", "two")
            for criterion in ("privacy", "quality", "latency")
        ]
    }


def _reviews(*, request_medium_revision: bool = False) -> dict[str, Any]:
    reviews = [
        {
            "candidate_id": candidate,
            "criterion_id": criterion,
            "outcome": "concur",
            "reason": "The operator reviewed the source observation.",
        }
        for candidate in ("one", "two")
        for criterion in ("privacy", "quality")
    ]
    if request_medium_revision:
        reviews.append(
            {
                "candidate_id": "one",
                "criterion_id": "latency",
                "outcome": "request_revision",
                "reason": "The medium-priority draft needs revision before relying on it.",
            }
        )
    return {"reviews": reviews}


def _build_comparison(
    service: DecisionService,
    tmp_path: Path,
    *,
    producer_kind: str = "fixture",
    request_medium_revision: bool = False,
    shared_quality_risk: bool = False,
) -> str:
    source = tmp_path / f"{service.session_id}.md"
    source.write_text("Synthetic evidence for both options.\n", encoding="utf-8")
    parent = service.initialize()["snapshot_sha256"]
    parent = service.capture_source(
        source_id="facts",
        source=source,
        expected_parent=parent,
        media_type="text/markdown",
    )["snapshot_sha256"]
    frame = service.import_frame(_frame(), expected_parent=parent)
    parent = frame["snapshot_sha256"]
    parent = service.confirm(
        "decision-frame",
        expected_artifact_sha=frame["artifact_sha256"],
        expected_parent=parent,
    )["snapshot_sha256"]
    candidates = service.import_candidates(_candidates(), expected_parent=parent)
    parent = candidates["snapshot_sha256"]
    parent = service.confirm(
        "candidate-set",
        expected_artifact_sha=candidates["artifact_sha256"],
        expected_parent=parent,
    )["snapshot_sha256"]
    criteria = service.import_criteria(_criteria(), expected_parent=parent)
    parent = criteria["snapshot_sha256"]
    parent = service.confirm(
        "criteria-set",
        expected_artifact_sha=criteria["artifact_sha256"],
        expected_parent=parent,
    )["snapshot_sha256"]
    parent = service.import_evidence(
        {
            "evidence": [
                {
                    "evidence_id": "evidence",
                    "claim": "Both options were observed in the synthetic source.",
                    "provenance": "source_observation",
                    "source": {
                        "source_id": "facts",
                        "start_line": 1,
                        "end_line": 1,
                        "excerpt_sha256": sha256_bytes(source.read_bytes()),
                    },
                }
            ]
        },
        expected_parent=parent,
    )["snapshot_sha256"]
    parent = service.import_evaluations(
        _evaluations(shared_quality_risk=shared_quality_risk),
        expected_parent=parent,
        producer_kind=producer_kind,
    )["snapshot_sha256"]
    if producer_kind != "local_operator":
        parent = service.import_reviews(
            _reviews(request_medium_revision=request_medium_revision),
            expected_parent=parent,
        )["snapshot_sha256"]
    return service.compare(expected_parent=parent)["snapshot_sha256"]


def test_source_and_confirmation_gates_leave_pointer_unchanged(tmp_path: Path) -> None:
    service, _clock = _service(tmp_path, "source-gates")
    parent = service.initialize()["snapshot_sha256"]
    source = tmp_path / "source.txt"
    source.write_text("text\n", encoding="utf-8")
    symlink = tmp_path / "source-link.txt"
    symlink.symlink_to(source)
    invalid_utf8 = tmp_path / "invalid.txt"
    invalid_utf8.write_bytes(b"\xff")
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * (10 * 1024 * 1024 + 1))

    for path, media_type, code in (
        (symlink, None, "INVALID_SOURCE"),
        (invalid_utf8, None, "UNSUPPORTED_SOURCE_FORMAT"),
        (oversized, None, "SOURCE_TOO_LARGE"),
        (source, "application/pdf", "UNSUPPORTED_SOURCE_FORMAT"),
    ):
        with pytest.raises(HarnessError) as caught:
            service.capture_source(
                source_id="facts",
                source=path,
                expected_parent=parent,
                media_type=media_type,
            )
        assert caught.value.code == code
        assert service.status()["snapshot_sha256"] == parent

    captured = service.capture_source(
        source_id="facts",
        source=source,
        expected_parent=parent,
    )
    parent = captured["snapshot_sha256"]
    draft = service.import_frame(_frame(complete=False), expected_parent=parent)
    with pytest.raises(HarnessError) as incomplete:
        service.confirm(
            "decision-frame",
            expected_artifact_sha=draft["artifact_sha256"],
            expected_parent=draft["snapshot_sha256"],
        )
    assert incomplete.value.code == "FRAME_INCOMPLETE"
    with pytest.raises(HarnessError) as unknown:
        service.confirm(
            "unknown",
            expected_artifact_sha="a" * 64,
            expected_parent=draft["snapshot_sha256"],
        )
    assert unknown.value.code == "INVALID_CONFIRMATION_SUBJECT"
    with pytest.raises(HarnessError) as mismatch:
        service.confirm(
            "decision-frame",
            expected_artifact_sha="a" * 64,
            expected_parent=draft["snapshot_sha256"],
        )
    assert mismatch.value.code == "CONFIRMATION_DIGEST_MISMATCH"


def test_doctor_fails_closed_when_referenced_content_is_corrupt(tmp_path: Path) -> None:
    service, _clock = _service(tmp_path, "doctor-corruption")
    source = tmp_path / "doctor-source.txt"
    source.write_text("captured evidence\n", encoding="utf-8")
    parent = service.initialize()["snapshot_sha256"]
    service.capture_source(source_id="facts", source=source, expected_parent=parent)
    refs = service.status()["refs"]
    manifest = service.store.read_artifact(refs["source_manifest"])
    blob_sha = manifest.payload["sources"][0]["blob_sha256"]
    service.store.paths.object_path(blob_sha).write_bytes(b"tampered\n")

    with pytest.raises(HarnessError) as caught:
        service.doctor()

    assert caught.value.code == "INTEGRITY_VERIFICATION_FAILED"
    assert caught.value.exit_code == 5
    assert caught.value.details["doctor"]["ok"] is False


def test_forged_confirmation_role_cannot_pass_verify_or_next_mutation(tmp_path: Path) -> None:
    service, clock = _service(tmp_path, "forged-confirmation-role")
    source = tmp_path / "confirmation-source.txt"
    source.write_text("synthetic evidence\n", encoding="utf-8")
    parent = service.initialize()["snapshot_sha256"]
    parent = service.capture_source(
        source_id="facts",
        source=source,
        expected_parent=parent,
    )["snapshot_sha256"]
    frame = service.import_frame(_frame(), expected_parent=parent)
    frame_sha = frame["artifact_sha256"]
    forged_sha = service.store.put_artifact(
        ArtifactEnvelope.create(
            artifact_type="human-confirmation",
            session_id=service.session_id,
            producer={"kind": "core"},
            parents={"decision_frame": frame_sha},
            payload={
                "confirmation_id": service.store.new_event_id(),
                "subject_type": "candidate-set",
                "subject_sha256": frame_sha,
                "actor_label": "local_operator",
                "identity_verified": False,
                "method": "digest_challenge",
                "confirmed_at": clock().isoformat().replace("+00:00", "Z"),
            },
        )
    )
    refs = dict(service.store.get_current().refs)
    refs["frame_confirmation"] = forged_sha
    forged = service.store.commit(
        expected_parent=frame["snapshot_sha256"],
        refs=refs,
        operation="test.forge-confirmation",
    )

    with pytest.raises(HarnessError) as verify_error:
        service.verify(forged.snapshot_sha256)
    assert verify_error.value.code == "CONFIRMATION_STALE"
    assert verify_error.value.exit_code == 5

    with pytest.raises(HarnessError) as mutation_error:
        service.import_candidates(_candidates(), expected_parent=forged.snapshot_sha256)
    assert mutation_error.value.code == "CONFIRMATION_STALE"
    assert service.store.get_current().snapshot_sha256 == forged.snapshot_sha256


def test_hostile_import_shapes_are_structured_input_errors_without_pointer_changes(
    tmp_path: Path,
) -> None:
    service, _clock = _service(tmp_path, "hostile-import-shapes")
    parent = _build_comparison(service, tmp_path)
    operations = (
        lambda: service.import_evidence({"evidence": [1]}, expected_parent=parent),
        lambda: service.import_evaluations(
            {
                "cells": [
                    {
                        "candidate_id": [],
                        "criterion_id": "privacy",
                        "assessment": "meets",
                        "rationale": "Malformed candidate ID.",
                        "evidence_ids": ["evidence"],
                        "confidence": "high",
                        "uncertainties": [],
                    }
                ]
            },
            expected_parent=parent,
            producer_kind="fixture",
        ),
        lambda: service.import_reviews(
            {
                "reviews": [
                    {
                        "candidate_id": [],
                        "criterion_id": "privacy",
                        "outcome": "concur",
                        "reason": "Malformed candidate ID.",
                    }
                ]
            },
            expected_parent=parent,
        ),
    )

    for operation in operations:
        with pytest.raises(HarnessError) as caught:
            operation()
        assert caught.value.code == "INVALID_PAYLOAD"
        assert caught.value.exit_code == 2
        assert service.status()["snapshot_sha256"] == parent


def test_comparison_recommendation_and_final_decision_policy_edges(tmp_path: Path) -> None:
    service, _clock = _service(tmp_path, "decision-policy")
    parent = _build_comparison(service, tmp_path)

    for payload, code in (
        (
            {
                "disposition": "other",
                "candidate_id": None,
                "rationale": "No.",
                "evidence_ids": [],
                "risks": [],
                "uncertainties": [],
            },
            "INVALID_RECOMMENDATION",
        ),
        (
            {
                "disposition": "select",
                "candidate_id": "absent",
                "rationale": "No.",
                "evidence_ids": [],
                "risks": [],
                "uncertainties": [],
            },
            "INELIGIBLE_RECOMMENDATION",
        ),
        (
            {
                "disposition": "abstain",
                "candidate_id": "one",
                "rationale": "No.",
                "evidence_ids": [],
                "risks": [],
                "uncertainties": [],
            },
            "INVALID_RECOMMENDATION",
        ),
    ):
        with pytest.raises(HarnessError) as caught:
            service.record_recommendation(
                payload,
                expected_parent=parent,
                producer_kind="fixture",
            )
        assert caught.value.code == code

    recommendation = service.record_recommendation(
        {
            "disposition": "select",
            "candidate_id": "two",
            "rationale": "Two is eligible.",
            "evidence_ids": ["evidence"],
            "risks": [],
            "uncertainties": [],
        },
        expected_parent=parent,
        producer_kind="fixture",
    )
    parent = recommendation["snapshot_sha256"]
    invalid_finals = (
        (
            {
                "disposition": "unknown",
                "candidate_id": None,
                "reason": "No.",
                "risk_acknowledgements": [],
            },
            "INVALID_FINAL_DECISION",
        ),
        (
            {
                "disposition": "select",
                "candidate_id": "absent",
                "reason": "No.",
                "risk_acknowledgements": [],
            },
            "INELIGIBLE_FINAL_DECISION",
        ),
        (
            {
                "disposition": "defer",
                "candidate_id": "one",
                "reason": "No.",
                "risk_acknowledgements": [],
            },
            "INVALID_FINAL_DECISION",
        ),
        (
            {
                "disposition": "select",
                "candidate_id": "one",
                "reason": "Select one.",
                "risk_acknowledgements": [],
            },
            "RISK_ACKNOWLEDGEMENT_REQUIRED",
        ),
    )
    for payload, code in invalid_finals:
        with pytest.raises(HarnessError) as caught:
            service.import_final_decision(payload, expected_parent=parent)
        assert caught.value.code == code

    final = service.import_final_decision(
        {
            "disposition": "select",
            "candidate_id": "one",
            "reason": "Select one with acknowledged risks.",
            "risk_acknowledgements": ["one/quality", "one/latency"],
        },
        expected_parent=parent,
    )
    assert final["recommendation_relation"] == "different"


def test_stale_generic_provider_request_is_rejected_before_generate(tmp_path: Path) -> None:
    service, _clock = _service(tmp_path, "stale-provider-preflight")
    parent = _build_comparison(service, tmp_path)
    calls: list[Any] = []
    provider = SimpleNamespace(generate=lambda context: calls.append(context))

    for generate in (service.generate_evaluations, service.generate_recommendation):
        with pytest.raises(HarnessError) as caught:
            generate(
                provider,
                expected_parent="a" * 64,
                producer_kind="fixture",
            )
        assert caught.value.code == "WRITE_CONFLICT"

    assert calls == []
    assert service.status()["snapshot_sha256"] == parent


def test_evaluation_provider_context_excludes_prior_results(tmp_path: Path) -> None:
    service, _clock = _service(tmp_path, "operation-context")
    parent = _build_comparison(service, tmp_path)
    snapshot = service.store.load_snapshot(parent)

    evaluation_context = service._agent_context_for_operation(snapshot, "evaluations")
    recommendation_context = service._agent_context_for_operation(snapshot, "recommendation")

    assert evaluation_context.evaluations == ()
    assert dict(evaluation_context.comparison) == {}
    assert recommendation_context.evaluations
    assert recommendation_context.comparison


def test_reject_all_requires_shared_risk_and_is_complete_but_not_ready(tmp_path: Path) -> None:
    service, _clock = _service(tmp_path, "reject-all")
    parent = _build_comparison(service, tmp_path, producer_kind="local_operator")
    with pytest.raises(HarnessError) as missing:
        service.import_final_decision(
            {
                "disposition": "reject_all",
                "candidate_id": None,
                "reason": "Reject both.",
                "risk_acknowledgements": ["one/quality", "two/privacy"],
            },
            expected_parent=parent,
        )
    assert missing.value.code == "REJECT_ALL_SHARED_RISK_REQUIRED"

    shared_service, _shared_clock = _service(tmp_path, "reject-all-shared")
    shared_parent = _build_comparison(
        shared_service,
        tmp_path,
        producer_kind="local_operator",
        shared_quality_risk=True,
    )
    final = shared_service.import_final_decision(
        {
            "disposition": "reject_all",
            "candidate_id": None,
            "reason": "Reject both for a shared operating risk.",
            "risk_acknowledgements": ["one/quality", "two/quality"],
        },
        expected_parent=shared_parent,
    )
    challenge = shared_service.create_approval_challenge(
        disposition="approved",
        expected_parent=final["snapshot_sha256"],
    )
    shared_service.commit_approval(
        challenge_id=challenge["challenge_id"],
        nonce=challenge["nonce"],
        expected_bundle_sha=challenge["decision_bundle_sha256"],
        reason="Reviewed reject-all decision.",
        expected_parent=challenge["snapshot_sha256"],
    )
    status = shared_service.status()
    assert status["decision_complete"] is True
    assert status["ready"] is False


def test_medium_revision_request_blocks_comparison_until_new_evaluation(tmp_path: Path) -> None:
    service, _clock = _service(tmp_path, "medium-revision-risk")
    with pytest.raises(HarnessError) as revision:
        _build_comparison(
            service,
            tmp_path,
            request_medium_revision=True,
        )

    assert revision.value.code == "EVALUATION_REVISION_REQUIRED"
    assert revision.value.details["cells"] == [["one", "latency"]]


def test_intervening_agent_consent_invalidates_approval_challenge(tmp_path: Path) -> None:
    service, _clock = _service(tmp_path, "challenge-agent-consent")
    parent = _build_comparison(service, tmp_path, producer_kind="local_operator")
    final = service.import_final_decision(
        {
            "disposition": "select",
            "candidate_id": "one",
            "reason": "Select one after reviewing the qualitative matrix.",
            "risk_acknowledgements": ["one/quality"],
        },
        expected_parent=parent,
    )
    challenge = service.create_approval_challenge(
        disposition="approved",
        expected_parent=final["snapshot_sha256"],
    )
    preview = service.preview_agent(operation="evaluations")
    consent = service.consent_agent(
        operation="evaluations",
        expected_manifest_sha=preview["outbound_manifest_sha256"],
        expected_parent=challenge["snapshot_sha256"],
    )

    with pytest.raises(HarnessError) as stale:
        service.commit_approval(
            challenge_id=challenge["challenge_id"],
            nonce=challenge["nonce"],
            expected_bundle_sha=challenge["decision_bundle_sha256"],
            reason="This response belongs to the superseded challenge.",
            expected_parent=consent["snapshot_sha256"],
        )

    assert stale.value.code == "CHALLENGE_STALE"
    assert service.status()["ready"] is False


def test_agent_consent_after_approval_keeps_the_active_graph_verifiable(tmp_path: Path) -> None:
    service, _clock = _service(tmp_path, "approved-agent-consent")
    parent = _build_comparison(service, tmp_path, producer_kind="local_operator")
    final = service.import_final_decision(
        {
            "disposition": "select",
            "candidate_id": "one",
            "reason": "Select one after reviewing the qualitative matrix.",
            "risk_acknowledgements": ["one/quality"],
        },
        expected_parent=parent,
    )
    challenge = service.create_approval_challenge(
        disposition="approved",
        expected_parent=final["snapshot_sha256"],
    )
    approval = service.commit_approval(
        challenge_id=challenge["challenge_id"],
        nonce=challenge["nonce"],
        expected_bundle_sha=challenge["decision_bundle_sha256"],
        reason="Reviewed the current bundle.",
        expected_parent=challenge["snapshot_sha256"],
    )
    assert service.status()["ready"] is True

    preview = service.preview_agent(operation="evaluations")
    consent = service.consent_agent(
        operation="evaluations",
        expected_manifest_sha=preview["outbound_manifest_sha256"],
        expected_parent=approval["snapshot_sha256"],
    )
    status = service.status()

    assert status["snapshot_sha256"] == consent["snapshot_sha256"]
    assert status["verified"] is True
    assert status["ready"] is False
    assert "human_approval" not in status["refs"]
    assert "decision_bundle" not in status["refs"]


def test_identical_source_capture_is_a_noop_that_preserves_readiness(tmp_path: Path) -> None:
    service, _clock = _service(tmp_path, "identical-source-noop")
    parent = _build_comparison(service, tmp_path, producer_kind="local_operator")
    final = service.import_final_decision(
        {
            "disposition": "select",
            "candidate_id": "one",
            "reason": "Select one after reviewing its risks.",
            "risk_acknowledgements": ["one/quality"],
        },
        expected_parent=parent,
    )
    challenge = service.create_approval_challenge(
        disposition="approved",
        expected_parent=final["snapshot_sha256"],
    )
    approval = service.commit_approval(
        challenge_id=challenge["challenge_id"],
        nonce=challenge["nonce"],
        expected_bundle_sha=challenge["decision_bundle_sha256"],
        reason="Approve the reviewed bundle.",
        expected_parent=challenge["snapshot_sha256"],
    )
    source = tmp_path / f"{service.session_id}.md"

    repeated = service.capture_source(
        source_id="facts",
        source=source,
        expected_parent=approval["snapshot_sha256"],
        media_type="text/markdown",
    )

    assert repeated["snapshot_sha256"] == approval["snapshot_sha256"]
    assert service.status()["ready"] is True


def test_verifier_rejects_approval_disposition_forged_after_rejected_challenge(
    tmp_path: Path,
) -> None:
    service, _clock = _service(tmp_path, "forged-approval-disposition")
    parent = _build_comparison(service, tmp_path, producer_kind="local_operator")
    final = service.import_final_decision(
        {
            "disposition": "select",
            "candidate_id": "one",
            "reason": "Select one after reviewing the matrix.",
            "risk_acknowledgements": ["one/quality"],
        },
        expected_parent=parent,
    )
    challenge = service.create_approval_challenge(
        disposition="rejected",
        expected_parent=final["snapshot_sha256"],
    )
    rejected = service.commit_approval(
        challenge_id=challenge["challenge_id"],
        nonce=challenge["nonce"],
        expected_bundle_sha=challenge["decision_bundle_sha256"],
        reason="Reject this bundle.",
        expected_parent=challenge["snapshot_sha256"],
    )
    current = service.store.get_current()
    refs = dict(current.refs)
    original = service.store.read_artifact(refs["human_approval"])
    forged_payload = dict(original.payload)
    forged_payload["disposition"] = "approved"
    forged_sha = service.store.put_artifact(
        ArtifactEnvelope.create(
            artifact_type="human-approval",
            session_id=service.session_id,
            producer={"kind": "core"},
            parents=dict(original.parents),
            payload=forged_payload,
        )
    )
    refs["human_approval"] = forged_sha
    forged = service.store.commit(
        expected_parent=rejected["snapshot_sha256"],
        refs=refs,
        operation="test.forge-approval",
    )

    with pytest.raises(HarnessError) as caught:
        service.verify(forged.snapshot_sha256)

    assert caught.value.code == "APPROVAL_BINDING_STALE"
    status = service.status()
    assert status["verified"] is False
    assert status["decision_complete"] is False
    assert status["ready"] is False


def test_approval_challenge_mismatch_expiry_and_provisional_policy(tmp_path: Path) -> None:
    service, clock = _service(tmp_path, "approval-edges")
    parent = _build_comparison(service, tmp_path, producer_kind="local_operator")
    final = service.import_final_decision(
        {
            "disposition": "defer",
            "candidate_id": None,
            "reason": "Wait for more evidence.",
            "risk_acknowledgements": [],
        },
        expected_parent=parent,
    )
    parent = final["snapshot_sha256"]
    with pytest.raises(HarnessError) as invalid:
        service.create_approval_challenge(disposition="other", expected_parent=parent)
    assert invalid.value.code == "INVALID_APPROVAL_DISPOSITION"
    with pytest.raises(HarnessError) as stale:
        service.create_approval_challenge(disposition="rejected", expected_parent="a" * 64)
    assert stale.value.code == "WRITE_CONFLICT"
    with pytest.raises(HarnessError) as provisional:
        service.create_approval_challenge(disposition="approved", expected_parent=parent)
    assert provisional.value.code == "DECISION_NOT_APPROVABLE"

    challenge = service.create_approval_challenge(
        disposition="rejected",
        expected_parent=parent,
    )
    for kwargs in (
        {"challenge_id": "bad"},
        {"nonce": "bad"},
        {"expected_bundle_sha": "b" * 64},
    ):
        arguments = {
            "challenge_id": challenge["challenge_id"],
            "nonce": challenge["nonce"],
            "expected_bundle_sha": challenge["decision_bundle_sha256"],
            "reason": "Reviewed.",
            "expected_parent": challenge["snapshot_sha256"],
            **kwargs,
        }
        with pytest.raises(HarnessError) as mismatch:
            service.commit_approval(**arguments)
        assert mismatch.value.code == "CHALLENGE_MISMATCH"

    clock.advance(timedelta(minutes=11))
    with pytest.raises(HarnessError) as expired:
        service.commit_approval(
            challenge_id=challenge["challenge_id"],
            nonce=challenge["nonce"],
            expected_bundle_sha=challenge["decision_bundle_sha256"],
            reason="Reviewed.",
            expected_parent=challenge["snapshot_sha256"],
        )
    assert expired.value.code == "CHALLENGE_EXPIRED"


def test_approval_rejects_clock_rollback_before_challenge_issuance(tmp_path: Path) -> None:
    service, clock = _service(tmp_path, "approval-clock-rollback")
    parent = _build_comparison(service, tmp_path, producer_kind="local_operator")
    final = service.import_final_decision(
        {
            "disposition": "select",
            "candidate_id": "one",
            "reason": "Select one after review.",
            "risk_acknowledgements": ["one/quality"],
        },
        expected_parent=parent,
    )
    challenge = service.create_approval_challenge(
        disposition="approved",
        expected_parent=final["snapshot_sha256"],
    )
    clock.advance(timedelta(days=-1))

    with pytest.raises(HarnessError) as caught:
        service.commit_approval(
            challenge_id=challenge["challenge_id"],
            nonce=challenge["nonce"],
            expected_bundle_sha=challenge["decision_bundle_sha256"],
            reason="This clock has moved backwards.",
            expected_parent=challenge["snapshot_sha256"],
        )

    assert caught.value.code == "CHALLENGE_CLOCK_REGRESSION"
    assert service.status()["ready"] is False


def test_agent_preview_and_consent_fail_closed_before_external_calls(tmp_path: Path) -> None:
    from ai_work_harness.decision.openai_provider import OpenAIProvider

    service, _clock = _service(tmp_path, "agent-gates")
    parent = _build_comparison(service, tmp_path)
    for operation, provider, code in (
        ("invalid", "openai", "INVALID_AGENT_OPERATION"),
        ("evaluations", "other", "UNSUPPORTED_PROVIDER"),
    ):
        with pytest.raises(HarnessError) as caught:
            service.preview_agent(operation=operation, provider=provider)
        assert caught.value.code == code

    preview = service.preview_agent(operation="evaluations", model="gpt-explicit")
    assert preview["outbound_manifest"]["model"] == "gpt-explicit"
    with pytest.raises(HarnessError) as stale:
        service.consent_agent(
            operation="evaluations",
            expected_manifest_sha=preview["outbound_manifest_sha256"],
            expected_parent="a" * 64,
        )
    assert stale.value.code == "WRITE_CONFLICT"
    with pytest.raises(HarnessError) as mismatch:
        service.consent_agent(
            operation="evaluations",
            expected_manifest_sha="b" * 64,
            expected_parent=parent,
        )
    assert mismatch.value.code == "OUTBOUND_MANIFEST_MISMATCH"

    with pytest.raises(HarnessError) as no_consent:
        service.run_openai_agent(operation="evaluations", expected_parent=parent)
    assert no_consent.value.code == "OUTBOUND_CONSENT_REQUIRED"

    consent = service.consent_agent(
        operation="evaluations",
        provider="openai",
        model="gpt-explicit",
        expected_manifest_sha=preview["outbound_manifest_sha256"],
        expected_parent=parent,
    )
    mismatched_provider = OpenAIProvider(
        client=SimpleNamespace(),
        model="gpt-explicit",
        max_retries=0,
    )
    with pytest.raises(HarnessError) as configuration:
        service.run_openai_agent(
            operation="evaluations",
            expected_parent=consent["snapshot_sha256"],
            provider=mismatched_provider,
        )
    assert configuration.value.code == "OUTBOUND_CONSENT_STALE"


def test_verify_status_doctor_and_export_failure_seams(tmp_path: Path) -> None:
    service, _clock = _service(tmp_path, "reporting")
    parent = _build_comparison(service, tmp_path)
    historical = service.store.load_snapshot(parent).parent_snapshot_sha256
    assert historical is not None
    export = service.export_view(tmp_path / "view.json", snapshot_sha256=historical)
    assert export["snapshot_sha256"] == historical

    pointer_before = service.store.paths.current_pointer.read_bytes()
    with pytest.raises(HarnessError) as managed_path:
        service.export_view(
            service.store.paths.current_pointer,
            snapshot_sha256=historical,
        )
    assert managed_path.value.code == "EXPORT_PATH_MANAGED"
    assert service.store.paths.current_pointer.read_bytes() == pointer_before

    class Report:
        ok = False
        issues = (SimpleNamespace(code="BROKEN", message="broken", path="snapshot"),)

    original_verify = service.store.verify
    service.store.verify = lambda _sha: Report()
    with pytest.raises(HarnessError) as failed:
        service.verify(parent)
    assert failed.value.code == "INTEGRITY_VERIFICATION_FAILED"
    status = service.status()
    assert status["verified"] is False
    assert status["verification_error"]["code"] == "INTEGRITY_VERIFICATION_FAILED"
    service.store.verify = original_verify

    class PlainDoctor:
        def __init__(self) -> None:
            self.ok = True
            self.detail = "plain"

    service.store.doctor = lambda: PlainDoctor()
    doctor = service.doctor()
    assert doctor["doctor"] == {"ok": True, "detail": "plain"}
