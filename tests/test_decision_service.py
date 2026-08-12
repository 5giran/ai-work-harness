from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_work_harness.decision.canonical import digest_json, parse_json_bytes, sha256_bytes
from ai_work_harness.decision.models import ArtifactEnvelope, validate_v2_document
from ai_work_harness.decision.openai_provider import OpenAIProvider
from ai_work_harness.decision.providers import FixtureProvider
from ai_work_harness.decision.service import DecisionService
from ai_work_harness.decision.store import DecisionStore
from ai_work_harness.errors import HarnessError


@dataclass
class MutableClock:
    value: datetime

    def now(self) -> datetime:
        return self.value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class SequenceIds:
    def __init__(self) -> None:
        self.counter = 0

    def new_id(self) -> str:
        self.counter += 1
        return f"00000000-0000-4000-8000-{self.counter:012x}"


def _service(tmp_path: Path) -> DecisionService:
    clock = MutableClock(datetime(2026, 8, 11, 3, 0, tzinfo=UTC))
    store = DecisionStore(
        tmp_path,
        "triage-demo",
        clock=clock,
        id_source=SequenceIds(),
    )
    return DecisionService(
        tmp_path,
        "triage-demo",
        store=store,
        clock=clock,
    )


def _candidates() -> dict[str, object]:
    return {
        "candidates": [
            {
                "candidate_id": candidate_id,
                "title": title,
                "summary": summary,
                "proposed_by": "synthetic-demo",
                "benefits": ["A concrete operating benefit."],
                "drawbacks": ["A concrete operating drawback."],
                "risks": ["A synthetic risk to review."],
                "uncertainties": ["Synthetic evidence has limited scope."],
            }
            for candidate_id, title, summary in (
                ("rules", "Rules", "Operate triage with explicit rules."),
                ("classical-ml", "Classical ML", "Use a supervised text classifier."),
                ("llm-assisted", "LLM assisted", "Use an LLM draft with human review."),
            )
        ]
    }


def _criteria() -> dict[str, object]:
    return {
        "criteria": [
            {
                "criterion_id": criterion_id,
                "title": title,
                "definition": definition,
                "priority": priority,
            }
            for criterion_id, title, definition, priority in (
                ("privacy", "Privacy", "Customer text stays within the allowed boundary.", "must"),
                ("auditability", "Auditability", "An operator can explain each result.", "must"),
                (
                    "classification-quality",
                    "Classification quality",
                    "The system routes the synthetic requests correctly.",
                    "high",
                ),
                (
                    "latency",
                    "Latency",
                    "The workflow responds within the operating target.",
                    "medium",
                ),
                (
                    "operating-cost",
                    "Operating cost",
                    "The recurring operating burden is acceptable.",
                    "medium",
                ),
            )
        ]
    }


def _evidence(excerpt_sha256: str) -> dict[str, object]:
    pairs = (
        (candidate, criterion)
        for candidate in ("rules", "classical-ml", "llm-assisted")
        for criterion in (
            "privacy",
            "auditability",
            "classification-quality",
            "latency",
            "operating-cost",
        )
    )
    return {
        "evidence": [
            {
                "evidence_id": f"ev-{candidate}-{criterion}",
                "claim": f"Synthetic observation for {candidate} against {criterion}.",
                "provenance": "source_observation",
                "source": {
                    "source_id": "triage-source",
                    "start_line": 1,
                    "end_line": 1,
                    "excerpt_sha256": excerpt_sha256,
                },
            }
            for candidate, criterion in pairs
        ]
    }


def _reviews() -> dict[str, object]:
    return {
        "reviews": [
            {
                "candidate_id": candidate,
                "criterion_id": criterion,
                "outcome": "concur",
                "reason": "The local operator reviewed the cited synthetic observation.",
            }
            for candidate in ("rules", "classical-ml", "llm-assisted")
            for criterion in ("privacy", "auditability", "classification-quality")
        ]
    }


def _advance(result: dict[str, object]) -> str:
    value = result["snapshot_sha256"]
    assert isinstance(value, str)
    return value


def _build_approved_demo(service: DecisionService, source: Path) -> dict[str, object]:
    parent = _advance(service.initialize())
    parent = _advance(
        service.capture_source(
            source_id="triage-source",
            source=source,
            expected_parent=parent,
            media_type="text/markdown",
        )
    )
    parent = _advance(
        service.import_frame(
            {
                "user_statement_verbatim": "문의 분류 운영 방식을 정해야 한다.",
                "ai_initial_interpretation": "운영 통제와 품질을 함께 비교한다.",
                "business_user": "고객지원 운영 책임자",
                "blocked_decision": "어떤 triage 방식을 운영할지 선택하지 못함",
                "problem_statement": "소규모 한국어 문의 triage 운영 방식을 선택한다.",
                "scope_in": ["synthetic text triage"],
                "scope_out": ["production customer data"],
                "assumptions": ["The demo source is synthetic."],
                "open_questions": [],
            },
            expected_parent=parent,
        )
    )
    frame_sha = service.status()["refs"]["decision_frame"]
    parent = _advance(
        service.confirm(
            "decision-frame",
            expected_artifact_sha=frame_sha,
            expected_parent=parent,
        )
    )
    parent = _advance(service.import_candidates(_candidates(), expected_parent=parent))
    candidate_sha = service.status()["refs"]["candidate_set"]
    parent = _advance(
        service.confirm(
            "candidate-set",
            expected_artifact_sha=candidate_sha,
            expected_parent=parent,
        )
    )
    parent = _advance(service.import_criteria(_criteria(), expected_parent=parent))
    criteria_sha = service.status()["refs"]["criteria_set"]
    parent = _advance(
        service.confirm(
            "criteria-set",
            expected_artifact_sha=criteria_sha,
            expected_parent=parent,
        )
    )
    excerpt_sha = sha256_bytes(source.read_bytes())
    parent = _advance(service.import_evidence(_evidence(excerpt_sha), expected_parent=parent))
    fixture = FixtureProvider()
    parent = _advance(
        service.generate_evaluations(
            fixture.evaluation_provider(),
            expected_parent=parent,
            producer_kind="fixture",
        )
    )
    parent = _advance(service.import_reviews(_reviews(), expected_parent=parent))
    comparison = service.compare(expected_parent=parent)
    assert comparison["eligible_candidate_ids"] == ["classical-ml", "rules"]
    parent = _advance(comparison)
    recommendation = service.generate_recommendation(
        fixture.recommendation_provider(),
        expected_parent=parent,
        producer_kind="fixture",
    )
    parent = _advance(recommendation)
    decision = service.import_final_decision(
        {
            "disposition": "select",
            "candidate_id": "rules",
            "reason": "Rules are easier for this small team to operate and inspect.",
            "risk_acknowledgements": [
                "rules/classification-quality",
                "rules/latency",
                "rules/operating-cost",
            ],
        },
        expected_parent=parent,
    )
    assert decision["recommendation_relation"] == "different"
    parent = _advance(decision)
    challenge = service.create_approval_challenge(
        disposition="approved",
        expected_parent=parent,
    )
    parent = _advance(challenge)
    return service.commit_approval(
        challenge_id=challenge["challenge_id"],
        nonce=challenge["nonce"],
        expected_bundle_sha=challenge["decision_bundle_sha256"],
        reason="I reviewed the full synthetic decision bundle.",
        expected_parent=parent,
    )


def test_full_fixture_workflow_ready_implies_verify(tmp_path: Path) -> None:
    source = tmp_path / "triage.md"
    source.write_text("Synthetic triage evidence for every configured fixture cell.\n")
    service = _service(tmp_path)

    approved = _build_approved_demo(service, source)
    status = service.status()

    assert status["snapshot_sha256"] == approved["snapshot_sha256"]
    assert status["decision_complete"] is True
    assert status["ready"] is True
    assert service.verify(status["snapshot_sha256"])["verified"] is True

    with pytest.raises(HarnessError) as unknown_evidence:
        service.record_recommendation(
            {
                "disposition": "select",
                "candidate_id": "rules",
                "rationale": "This draft cites an absent evidence record.",
                "evidence_ids": ["not-captured"],
                "risks": [],
                "uncertainties": [],
            },
            expected_parent=status["snapshot_sha256"],
            producer_kind="agent_import",
        )
    assert unknown_evidence.value.code == "UNKNOWN_REFERENCE"
    assert service.status()["snapshot_sha256"] == status["snapshot_sha256"]

    reconfirmed = service.confirm(
        "decision-frame",
        expected_artifact_sha=status["refs"]["decision_frame"],
        expected_parent=status["snapshot_sha256"],
    )
    reconfirmed_status = service.status()
    assert reconfirmed_status["snapshot_sha256"] == reconfirmed["snapshot_sha256"]
    assert "candidate_set" not in reconfirmed_status["refs"]
    assert reconfirmed_status["verified"] is True
    assert reconfirmed_status["stale_reasons"] == ["frame_confirmation_changed"]


def test_upstream_change_removes_approval_and_reports_stale_reason(tmp_path: Path) -> None:
    source = tmp_path / "triage.md"
    source.write_text("Synthetic triage evidence for every configured fixture cell.\n")
    service = _service(tmp_path)
    approved = _build_approved_demo(service, source)

    changed = _criteria()
    changed["criteria"][2]["definition"] = "A stricter synthetic quality definition."
    result = service.import_criteria(changed, expected_parent=approved["snapshot_sha256"])
    status = service.status()

    assert result["snapshot_sha256"] == status["snapshot_sha256"]
    assert "human_approval" not in status["refs"]
    assert status["ready"] is False
    assert status["verified"] is True
    assert status["stale_reasons"] == ["criteria_set_changed"]


def test_guided_confirmation_method_is_additive_and_raw_default_is_unchanged(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.md"
    source.write_text("Synthetic source.\n", encoding="utf-8")
    service = _service(tmp_path)
    parent = _advance(service.initialize())
    parent = _advance(
        service.capture_source(
            source_id="source",
            source=source,
            expected_parent=parent,
        )
    )
    parent = _advance(
        service.import_frame(
            {
                "user_statement_verbatim": "A decision is blocked.",
                "ai_initial_interpretation": "Compare the available approaches.",
                "business_user": "Operator",
                "blocked_decision": "Choose an approach",
                "problem_statement": "Choose one reviewed approach.",
                "scope_in": [],
                "scope_out": [],
                "assumptions": [],
                "open_questions": [],
            },
            expected_parent=parent,
        )
    )
    frame_sha = service.status()["refs"]["decision_frame"]
    guided = service.confirm(
        "decision-frame",
        expected_artifact_sha=frame_sha,
        expected_parent=parent,
        method="guided_semantic_review",
    )
    confirmation_sha = service.status()["refs"]["frame_confirmation"]
    assert service.store.read_artifact(confirmation_sha).payload["method"] == (
        "guided_semantic_review"
    )

    # Re-importing a changed frame clears the confirmation; raw callers retain
    # their historical digest-challenge method without opting in to guided UX.
    changed_parent = _advance(
        service.import_frame(
            {
                "user_statement_verbatim": "A decision is blocked.",
                "ai_initial_interpretation": "Compare the available approaches.",
                "business_user": "Operator",
                "blocked_decision": "Choose a revised approach",
                "problem_statement": "Choose one reviewed approach.",
                "scope_in": [],
                "scope_out": [],
                "assumptions": [],
                "open_questions": [],
            },
            expected_parent=guided["snapshot_sha256"],
        )
    )
    changed_frame_sha = service.status()["refs"]["decision_frame"]
    service.confirm(
        "decision-frame",
        expected_artifact_sha=changed_frame_sha,
        expected_parent=changed_parent,
    )
    raw_confirmation_sha = service.status()["refs"]["frame_confirmation"]
    assert service.store.read_artifact(raw_confirmation_sha).payload["method"] == (
        "digest_challenge"
    )


def test_same_final_and_active_approval_cannot_be_rechallenged(tmp_path: Path) -> None:
    source = tmp_path / "triage.md"
    source.write_text("Synthetic triage evidence for every configured fixture cell.\n")
    service = _service(tmp_path)
    approved = _build_approved_demo(service, source)
    approved_parent = str(approved["snapshot_sha256"])
    refs = service.status()["refs"]
    final = dict(service.store.read_artifact(refs["final_decision"]).payload)

    with pytest.raises(HarnessError) as duplicate_challenge:
        service.create_approval_challenge(
            disposition="approved",
            expected_parent=approved_parent,
        )
    assert duplicate_challenge.value.code == "ACTIVE_APPROVAL_EXISTS"
    assert service.status()["snapshot_sha256"] == approved_parent

    with pytest.raises(HarnessError) as unchanged:
        service.import_final_decision(
            {
                "disposition": final["disposition"],
                "candidate_id": final["candidate_id"],
                "reason": f"  {final['reason']}  ",
                "risk_acknowledgements": list(reversed(final["risk_acknowledgements"])),
            },
            expected_parent=approved_parent,
        )
    assert unchanged.value.code == "FINAL_DECISION_UNCHANGED"
    assert service.status()["snapshot_sha256"] == approved_parent

    changed = service.import_final_decision(
        {
            "disposition": final["disposition"],
            "candidate_id": final["candidate_id"],
            "reason": "The operator revised the rationale after reviewing the rejection.",
            "risk_acknowledgements": list(final["risk_acknowledgements"]),
        },
        expected_parent=approved_parent,
    )
    changed_refs = service.status()["refs"]
    assert "human_approval" not in changed_refs
    challenge = service.create_approval_challenge(
        disposition="approved",
        expected_parent=str(changed["snapshot_sha256"]),
    )
    assert challenge["challenge_id"]


def test_revision_request_is_recorded_and_requires_a_new_evaluation(tmp_path: Path) -> None:
    source = tmp_path / "triage.md"
    source.write_text("Synthetic triage evidence for every configured fixture cell.\n")
    service = _service(tmp_path)
    approved = _build_approved_demo(service, source)
    refs = service.status()["refs"]
    evaluation = json.loads(
        json.dumps(service.store.read_artifact(refs["evaluation_set"]).to_document()["payload"])
    )
    evaluation["cells"][0]["rationale"] = "A revised fixture rationale requiring review."
    revised = service.import_evaluations(
        evaluation,
        expected_parent=str(approved["snapshot_sha256"]),
        producer_kind="fixture",
    )

    requested = service.import_reviews(
        {
            "reviews": [
                {
                    "candidate_id": evaluation["cells"][0]["candidate_id"],
                    "criterion_id": evaluation["cells"][0]["criterion_id"],
                    "outcome": "request_revision",
                    "reason": "The cited rationale needs a new evaluation draft.",
                }
            ]
        },
        expected_parent=str(revised["snapshot_sha256"]),
    )
    requested_parent = str(requested["snapshot_sha256"])
    assert "evaluation_review_set" in service.status()["refs"]

    with pytest.raises(HarnessError) as comparison_error:
        service.compare(expected_parent=requested_parent)
    assert comparison_error.value.code == "EVALUATION_REVISION_REQUIRED"
    assert service.status()["snapshot_sha256"] == requested_parent

    with pytest.raises(HarnessError) as overwrite_error:
        service.import_reviews(_reviews(), expected_parent=requested_parent)
    assert overwrite_error.value.code == "EVALUATION_REVISION_REQUIRED"
    assert service.status()["snapshot_sha256"] == requested_parent

    evaluation["cells"][0]["rationale"] = "The requested revision is now addressed."
    new_evaluation = service.import_evaluations(
        evaluation,
        expected_parent=requested_parent,
        producer_kind="fixture",
    )
    assert "evaluation_review_set" not in service.status()["refs"]
    assert new_evaluation["snapshot_sha256"] != requested_parent


def test_view_export_preserves_artifact_digests_and_opt_in_excerpts(tmp_path: Path) -> None:
    source = tmp_path / "triage.md"
    source.write_text("Synthetic triage evidence for every configured fixture cell.\n")
    service = _service(tmp_path)
    approved = _build_approved_demo(service, source)

    default_path = tmp_path / "default-view.json"
    service.export_view(default_path, snapshot_sha256=approved["snapshot_sha256"])
    default_bundle = parse_json_bytes(default_path.read_bytes())
    validate_v2_document("decision-view-v1", default_bundle)
    assert default_bundle["cited_excerpts"] == {}
    assert all(
        digest_json(artifact) == default_bundle["digest_map"][name]
        for name, artifact in default_bundle["artifacts"].items()
    )

    cited_path = tmp_path / "cited-view.json"
    service.export_view(
        cited_path,
        snapshot_sha256=approved["snapshot_sha256"],
        include_cited_excerpts=True,
    )
    cited_bundle = parse_json_bytes(cited_path.read_bytes())
    validate_v2_document("decision-view-v1", cited_bundle)
    assert len(cited_bundle["cited_excerpts"]) == 15
    assert set(cited_bundle["cited_excerpts"].values()) == {source.read_text()}
    unsigned = {key: value for key, value in cited_bundle.items() if key != "integrity_sha256"}
    assert digest_json(unsigned) == cited_bundle["integrity_sha256"]


def test_source_capture_rejects_unsafe_inputs_without_advancing_pointer(tmp_path: Path) -> None:
    service = _service(tmp_path)
    parent = _advance(service.initialize())
    missing = tmp_path / "missing.txt"
    binary = tmp_path / "binary.txt"
    binary.write_bytes(b"\xff")
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * (10 * 1024 * 1024 + 1))
    regular = tmp_path / "source.txt"
    regular.write_text("one\n", encoding="utf-8")
    symlink = tmp_path / "source-link.txt"
    symlink.symlink_to(regular)

    for source, media_type, code in (
        (missing, None, "INVALID_SOURCE"),
        (symlink, None, "INVALID_SOURCE"),
        (binary, None, "UNSUPPORTED_SOURCE_FORMAT"),
        (oversized, None, "SOURCE_TOO_LARGE"),
        (regular, "application/json", "UNSUPPORTED_SOURCE_FORMAT"),
    ):
        with pytest.raises(HarnessError) as caught:
            service.capture_source(
                source_id="source",
                source=source,
                expected_parent=parent,
                media_type=media_type,
            )
        assert caught.value.code == code
        assert service.status()["snapshot_sha256"] == parent

    managed_sha = service.store.put_blob(b"managed text\n")
    managed_path = service.store.paths.object_path(managed_sha)
    with pytest.raises(HarnessError) as managed_error:
        service.capture_source(
            source_id="managed",
            source=managed_path,
            expected_parent=parent,
        )
    assert managed_error.value.code == "SOURCE_IN_MANAGED_STORAGE"

    first = service.capture_source(
        source_id="first",
        source=regular,
        expected_parent=parent,
    )
    second_source = tmp_path / "second.md"
    second_source.write_text("two\n", encoding="utf-8")
    second = service.capture_source(
        source_id="second",
        source=second_source,
        expected_parent=first["snapshot_sha256"],
    )
    assert len(second["source"]["blob_sha256"]) == 64
    assert (
        len(
            service.store.read_artifact(service.status()["refs"]["source_manifest"]).payload[
                "sources"
            ]
        )
        == 2
    )

    with pytest.raises(HarnessError) as conflict:
        service.capture_source(
            source_id="stale",
            source=regular,
            expected_parent=parent,
        )
    assert conflict.value.code == "WRITE_CONFLICT"
    assert conflict.value.exit_code == 3


def test_frame_confirmation_is_an_exact_human_gate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    parent = _advance(service.initialize())
    frame = {
        "user_statement_verbatim": "Choose a workflow.",
        "ai_initial_interpretation": "Compare explicit candidates.",
        "business_user": None,
        "blocked_decision": None,
        "problem_statement": None,
        "scope_in": [],
        "scope_out": [],
        "assumptions": [],
        "open_questions": ["Who owns the decision?"],
    }
    with pytest.raises(HarnessError) as missing_source:
        service.import_frame(frame, expected_parent=parent)
    assert missing_source.value.code == "WORKFLOW_GATE_REQUIRED"

    source = tmp_path / "source.txt"
    source.write_text("source\n", encoding="utf-8")
    parent = _advance(
        service.capture_source(source_id="source", source=source, expected_parent=parent)
    )
    imported = service.import_frame(frame, expected_parent=parent)
    parent = _advance(imported)

    with pytest.raises(HarnessError) as unknown_subject:
        service.confirm(
            "unknown",
            expected_artifact_sha=imported["artifact_sha256"],
            expected_parent=parent,
        )
    assert unknown_subject.value.code == "INVALID_CONFIRMATION_SUBJECT"
    with pytest.raises(HarnessError) as mismatch:
        service.confirm(
            "decision-frame",
            expected_artifact_sha="0" * 64,
            expected_parent=parent,
        )
    assert mismatch.value.code == "CONFIRMATION_DIGEST_MISMATCH"
    with pytest.raises(HarnessError) as incomplete:
        service.confirm(
            "decision-frame",
            expected_artifact_sha=imported["artifact_sha256"],
            expected_parent=parent,
        )
    assert incomplete.value.code == "FRAME_INCOMPLETE"

    frame.update(
        {
            "business_user": "operator",
            "blocked_decision": "which workflow",
            "problem_statement": "Select a reviewable workflow.",
        }
    )
    completed = service.import_frame(frame, expected_parent=parent)
    confirmed = service.confirm(
        "decision-frame",
        expected_artifact_sha=completed["artifact_sha256"],
        expected_parent=completed["snapshot_sha256"],
    )
    assert confirmed["identity_verified"] is False


def test_approval_challenge_cannot_be_replayed_after_commit(tmp_path: Path) -> None:
    source = tmp_path / "triage.md"
    source.write_text("Synthetic triage evidence for every configured fixture cell.\n")
    service = _service(tmp_path)
    approved = _build_approved_demo(service, source)
    refs = service.status()["refs"]
    assert "approval_challenge" not in refs
    approval_artifact = service.store.read_artifact(refs["human_approval"])
    challenge_sha = approval_artifact.parents["approval_challenge"]
    challenge = service.store.read_artifact(challenge_sha).payload

    with pytest.raises(HarnessError) as replay:
        service.commit_approval(
            challenge_id=challenge["challenge_id"],
            nonce=challenge["nonce"],
            expected_bundle_sha=challenge["decision_bundle_sha256"],
            reason="Replay attempt.",
            expected_parent=approved["snapshot_sha256"],
        )

    assert replay.value.code == "CHALLENGE_STALE"
    assert service.verify(approved["snapshot_sha256"])["verified"] is True

    with pytest.raises(HarnessError) as duplicate:
        service.create_approval_challenge(
            disposition="approved",
            expected_parent=approved["snapshot_sha256"],
        )
    assert duplicate.value.code == "ACTIVE_APPROVAL_EXISTS"

    final = service.store.read_artifact(refs["final_decision"]).to_document()["payload"]
    changed = service.import_final_decision(
        {
            "disposition": final["disposition"],
            "candidate_id": final["candidate_id"],
            "reason": "A changed rationale permits a fresh approval cycle.",
            "risk_acknowledgements": final["risk_acknowledgements"],
        },
        expected_parent=approved["snapshot_sha256"],
    )
    replacement = service.create_approval_challenge(
        disposition="approved",
        expected_parent=changed["snapshot_sha256"],
    )
    assert isinstance(service.clock, MutableClock)
    service.clock.advance(timedelta(minutes=10))
    with pytest.raises(HarnessError) as expired:
        service.commit_approval(
            challenge_id=replacement["challenge_id"],
            nonce=replacement["nonce"],
            expected_bundle_sha=replacement["decision_bundle_sha256"],
            reason="Boundary replay attempt.",
            expected_parent=replacement["snapshot_sha256"],
        )
    assert expired.value.code == "CHALLENGE_EXPIRED"


class FakeResponses:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def create(self, **request: object) -> dict[str, object]:
        self.requests.append(request)
        return self.response


class FakeOpenAIClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.responses = FakeResponses(response)


def _tool_response(tool_name: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "id": "resp_synthetic",
        "output": [
            {
                "type": "function_call",
                "name": tool_name,
                "arguments": json.dumps(payload),
            }
        ],
        "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    }


def test_openai_evaluation_requires_consent_and_replays_without_network(
    tmp_path: Path,
) -> None:
    source = tmp_path / "triage.md"
    source.write_text("Synthetic triage evidence for every configured fixture cell.\n")
    service = _service(tmp_path)
    approved = _build_approved_demo(service, source)
    parent = _advance(
        service.import_evidence(
            _evidence(sha256_bytes(source.read_bytes())),
            expected_parent=approved["snapshot_sha256"],
        )
    )
    context = service._decision_context()
    draft_payload = FixtureProvider().generate_evaluations(context).as_payload()
    preview = service.preview_agent(operation="evaluations")

    consent = service.consent_agent(
        operation="evaluations",
        expected_manifest_sha=preview["outbound_manifest_sha256"],
        expected_parent=parent,
    )
    raw_consent = service.store.read_artifact(str(consent["artifact_sha256"]))
    assert raw_consent.payload["method"] == "digest_challenge"
    provider = OpenAIProvider(
        client=FakeOpenAIClient(_tool_response("submit_evaluations", draft_payload)),
        model=preview["outbound_manifest"]["model"],
        sleeper=lambda _seconds: None,
    )
    result = service.run_openai_agent(
        operation="evaluations",
        expected_parent=consent["snapshot_sha256"],
        provider=provider,
    )
    replay = service.replay_agent_validate(agent_run_sha=result["agent_run_sha256"])

    assert result["artifact"] == "evaluation_set"
    assert service.verify(result["snapshot_sha256"])["verified"] is True
    assert replay["replay_valid"] is True
    assert all(replay["checks"].values())
    assert len(provider._client.responses.requests) == 1
    assert "agent_consent" not in service.status()["refs"]
    with pytest.raises(HarnessError) as consent_reuse:
        service.run_openai_agent(
            operation="evaluations",
            expected_parent=result["snapshot_sha256"],
            provider=provider,
        )
    assert consent_reuse.value.code == "OUTBOUND_CONSENT_REQUIRED"

    run_artifact = service.store.read_artifact(result["agent_run_sha256"])
    malformed_sha = service.store.put_artifact(
        ArtifactEnvelope.create(
            artifact_type="agent-run",
            session_id=service.session_id,
            producer={"kind": "core"},
            parents={"evaluation_set": result["artifact_sha256"]},
            payload=dict(run_artifact.payload),
        )
    )
    with pytest.raises(HarnessError) as malformed:
        service.replay_agent_validate(agent_run_sha=malformed_sha)
    assert malformed.value.code == "AGENT_REPLAY_INVALID"
    assert malformed.value.exit_code == 5


def test_openai_refusal_does_not_advance_the_pointer(tmp_path: Path) -> None:
    source = tmp_path / "triage.md"
    source.write_text("Synthetic triage evidence for every configured fixture cell.\n")
    service = _service(tmp_path)
    approved = _build_approved_demo(service, source)
    preview = service.preview_agent(operation="recommendation")
    consent = service.consent_agent(
        operation="recommendation",
        expected_manifest_sha=preview["outbound_manifest_sha256"],
        expected_parent=approved["snapshot_sha256"],
    )
    provider = OpenAIProvider(
        client=FakeOpenAIClient(
            {
                "id": "resp_refusal",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "Cannot comply."}],
                    }
                ],
            }
        ),
        model=preview["outbound_manifest"]["model"],
        sleeper=lambda _seconds: None,
    )

    with pytest.raises(HarnessError) as caught:
        service.run_openai_agent(
            operation="recommendation",
            expected_parent=consent["snapshot_sha256"],
            provider=provider,
        )

    assert caught.value.code == "MODEL_REFUSAL"
    assert caught.value.exit_code == 4
    assert service.status()["snapshot_sha256"] == consent["snapshot_sha256"]
