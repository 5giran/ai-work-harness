from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_work_harness.decision.canonical import sha256_bytes
from ai_work_harness.decision.operator import GuidedOperator
from ai_work_harness.decision.providers import FixtureProvider
from ai_work_harness.decision.service import DecisionService
from ai_work_harness.decision.store import DecisionStore


@dataclass(frozen=True)
class FixedClock:
    value: datetime

    def now(self) -> datetime:
        return self.value

    def __call__(self) -> datetime:
        return self.value


class SequenceIds:
    def __init__(self) -> None:
        self.counter = 0

    def new_id(self) -> str:
        self.counter += 1
        return f"00000000-0000-4000-8000-{self.counter:012x}"


def _service(project_root: Path) -> DecisionService:
    project_root.mkdir()
    clock = FixedClock(datetime(2026, 8, 11, 3, 0, tzinfo=UTC))
    store = DecisionStore(
        project_root,
        "equivalent-fixture",
        clock=clock,
        id_source=SequenceIds(),
    )
    return DecisionService(
        project_root,
        "equivalent-fixture",
        store=store,
        clock=clock,
    )


def _frame() -> dict[str, object]:
    return {
        "user_statement_verbatim": "문의 분류 운영 방식을 정해야 한다.",
        "ai_initial_interpretation": "운영 통제와 품질을 함께 비교한다.",
        "business_user": "고객지원 운영 책임자",
        "blocked_decision": "어떤 triage 방식을 운영할지 선택하지 못함",
        "problem_statement": "소규모 한국어 문의 triage 운영 방식을 선택한다.",
        "scope_in": ["synthetic text triage"],
        "scope_out": ["production customer data"],
        "assumptions": ["The demo source is synthetic."],
        "open_questions": [],
    }


def _candidates() -> dict[str, object]:
    candidates = (
        ("rules", "Rules", "Operate triage with explicit rules."),
        ("classical-ml", "Classical ML", "Use a supervised text classifier."),
        ("llm-assisted", "LLM assisted", "Use an LLM draft with human review."),
    )
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
            for candidate_id, title, summary in candidates
        ]
    }


def _criteria() -> dict[str, object]:
    criteria = (
        ("privacy", "Privacy", "Customer text stays within the allowed boundary.", "must"),
        ("auditability", "Auditability", "An operator can explain each result.", "must"),
        (
            "classification-quality",
            "Classification quality",
            "The system routes the synthetic requests correctly.",
            "high",
        ),
        ("latency", "Latency", "The workflow responds within the operating target.", "medium"),
        (
            "operating-cost",
            "Operating cost",
            "The recurring operating burden is acceptable.",
            "medium",
        ),
    )
    return {
        "criteria": [
            {
                "criterion_id": criterion_id,
                "title": title,
                "definition": definition,
                "priority": priority,
            }
            for criterion_id, title, definition, priority in criteria
        ]
    }


def _evidence(excerpt_sha256: str) -> dict[str, object]:
    candidate_ids = ("rules", "classical-ml", "llm-assisted")
    criterion_ids = (
        "privacy",
        "auditability",
        "classification-quality",
        "latency",
        "operating-cost",
    )
    return {
        "evidence": [
            {
                "evidence_id": f"ev-{candidate_id}-{criterion_id}",
                "claim": f"Synthetic observation for {candidate_id} against {criterion_id}.",
                "provenance": "source_observation",
                "source": {
                    "source_id": "triage-source",
                    "start_line": 1,
                    "end_line": 1,
                    "excerpt_sha256": excerpt_sha256,
                },
            }
            for candidate_id in candidate_ids
            for criterion_id in criterion_ids
        ]
    }


def _reviews() -> dict[str, object]:
    return {
        "reviews": [
            {
                "candidate_id": candidate_id,
                "criterion_id": criterion_id,
                "outcome": "concur",
                "reason": "The local operator reviewed the cited synthetic observation.",
            }
            for candidate_id in ("rules", "classical-ml", "llm-assisted")
            for criterion_id in ("privacy", "auditability", "classification-quality")
        ]
    }


def _final_decision() -> dict[str, object]:
    return {
        "disposition": "select",
        "candidate_id": "rules",
        "reason": "Rules are easier for this small team to operate and inspect.",
        "risk_acknowledgements": [
            "rules/classification-quality",
            "rules/latency",
            "rules/operating-cost",
        ],
    }


def _snapshot_sha(result: Mapping[str, object]) -> str:
    snapshot_sha256 = result["snapshot_sha256"]
    assert isinstance(snapshot_sha256, str)
    return snapshot_sha256


def _write_source(project_root: Path) -> Path:
    source = project_root / "triage.md"
    source.write_text(
        "Synthetic triage evidence for every configured fixture cell.\n",
        encoding="utf-8",
    )
    return source


def _build_raw(service: DecisionService, source: Path) -> None:
    parent = _snapshot_sha(service.initialize())
    parent = _snapshot_sha(
        service.capture_source(
            source_id="triage-source",
            source=source,
            expected_parent=parent,
            media_type="text/markdown",
        )
    )
    frame = service.import_frame(_frame(), expected_parent=parent)
    parent = _snapshot_sha(frame)
    parent = _snapshot_sha(
        service.confirm(
            "decision-frame",
            expected_artifact_sha=str(frame["artifact_sha256"]),
            expected_parent=parent,
        )
    )
    candidates = service.import_candidates(_candidates(), expected_parent=parent)
    parent = _snapshot_sha(candidates)
    parent = _snapshot_sha(
        service.confirm(
            "candidate-set",
            expected_artifact_sha=str(candidates["artifact_sha256"]),
            expected_parent=parent,
        )
    )
    criteria = service.import_criteria(_criteria(), expected_parent=parent)
    parent = _snapshot_sha(criteria)
    parent = _snapshot_sha(
        service.confirm(
            "criteria-set",
            expected_artifact_sha=str(criteria["artifact_sha256"]),
            expected_parent=parent,
        )
    )
    parent = _snapshot_sha(
        service.import_evidence(
            _evidence(sha256_bytes(source.read_bytes())),
            expected_parent=parent,
        )
    )
    fixture = FixtureProvider()
    parent = _snapshot_sha(
        service.generate_evaluations(
            fixture.evaluation_provider(),
            expected_parent=parent,
            producer_kind="fixture",
        )
    )
    parent = _snapshot_sha(service.import_reviews(_reviews(), expected_parent=parent))
    parent = _snapshot_sha(service.compare(expected_parent=parent))
    parent = _snapshot_sha(
        service.generate_recommendation(
            fixture.recommendation_provider(),
            expected_parent=parent,
            producer_kind="fixture",
        )
    )
    parent = _snapshot_sha(service.import_final_decision(_final_decision(), expected_parent=parent))
    challenge = service.create_approval_challenge(
        disposition="approved",
        expected_parent=parent,
    )
    parent = _snapshot_sha(challenge)
    service.commit_approval(
        challenge_id=str(challenge["challenge_id"]),
        nonce=str(challenge["nonce"]),
        expected_bundle_sha=str(challenge["decision_bundle_sha256"]),
        reason="I reviewed the full synthetic decision bundle.",
        expected_parent=parent,
    )


def _build_guided(service: DecisionService, source: Path) -> GuidedOperator:
    service.initialize()
    operator = GuidedOperator(service)
    operator.capture_source(
        source_id="triage-source",
        source=source,
        media_type="text/markdown",
    )
    operator.import_frame(_frame())
    operator.confirm("decision-frame")
    operator.import_candidates(_candidates())
    operator.confirm("candidate-set")
    operator.import_criteria(_criteria())
    operator.confirm("criteria-set")
    operator.import_evidence(_evidence(sha256_bytes(source.read_bytes())))
    fixture = FixtureProvider()
    operator.mutate(
        service.generate_evaluations,
        fixture.evaluation_provider(),
        producer_kind="fixture",
    )
    operator.import_reviews(_reviews())
    operator.compare()
    operator.mutate(
        service.generate_recommendation,
        fixture.recommendation_provider(),
        producer_kind="fixture",
    )
    operator.import_final_decision(_final_decision())
    operator.create_approval_challenge(disposition="approved")
    operator.commit_approval(reason="I reviewed the full synthetic decision bundle.")
    return operator


def _active_documents(service: DecisionService) -> dict[str, dict[str, Any]]:
    refs = service.status()["refs"]
    assert isinstance(refs, Mapping)
    return {
        ref_name: service.store.read_artifact(digest).to_document()
        for ref_name, digest in refs.items()
        if isinstance(ref_name, str) and isinstance(digest, str)
    }


def _graph_shape(documents: Mapping[str, Mapping[str, Any]]) -> dict[str, object]:
    return {
        ref_name: (
            document["artifact_type"],
            document["producer"]["kind"],
            tuple(sorted(document["parents"])),
        )
        for ref_name, document in documents.items()
    }


def test_raw_and_guided_fixture_workflows_have_equivalent_semantic_graphs(
    tmp_path: Path,
) -> None:
    raw_service = _service(tmp_path / "raw")
    guided_service = _service(tmp_path / "guided")
    _build_raw(raw_service, _write_source(raw_service.project_root))
    guided_operator = _build_guided(
        guided_service,
        _write_source(guided_service.project_root),
    )

    raw_documents = _active_documents(raw_service)
    guided_documents = _active_documents(guided_service)

    # Digest values intentionally diverge after the first guided confirmation,
    # but both interfaces must produce the same active artifact topology.
    assert _graph_shape(raw_documents) == _graph_shape(guided_documents)

    domain_refs = (
        "source_manifest",
        "decision_frame",
        "candidate_set",
        "criteria_set",
        "evidence_set",
        "evaluation_set",
        "evaluation_review_set",
        "comparison",
        "recommendation",
        "final_decision",
    )
    for ref_name in domain_refs:
        assert raw_documents[ref_name]["payload"] == guided_documents[ref_name]["payload"]

    subject_refs = {
        "frame_confirmation": ("decision_frame", "decision-frame"),
        "candidate_confirmation": ("candidate_set", "candidate-set"),
        "criteria_confirmation": ("criteria_set", "criteria-set"),
    }
    for confirmation_ref, (subject_ref, subject_type) in subject_refs.items():
        raw_confirmation = raw_documents[confirmation_ref]["payload"]
        guided_confirmation = guided_documents[confirmation_ref]["payload"]
        assert raw_confirmation["subject_sha256"] == raw_service.status()["refs"][subject_ref]
        assert guided_confirmation["subject_sha256"] == guided_service.status()["refs"][subject_ref]
        assert raw_confirmation["subject_type"] == subject_type
        assert guided_confirmation["subject_type"] == subject_type
        assert raw_confirmation["actor_label"] == guided_confirmation["actor_label"]
        assert raw_confirmation["identity_verified"] is False
        assert guided_confirmation["identity_verified"] is False
        assert raw_confirmation["method"] == "digest_challenge"
        assert guided_confirmation["method"] == "guided_semantic_review"
        assert raw_documents[confirmation_ref]["schema_version"] == "2.0"
        assert guided_documents[confirmation_ref]["schema_version"] == "2.0"

    # Challenge nonces and digest bindings are deliberately different.  Their
    # non-secret semantics and each graph's internal bindings must still agree.
    raw_approval = raw_documents["human_approval"]["payload"]
    guided_approval = guided_documents["human_approval"]["payload"]
    for field in ("disposition", "reason", "actor_label", "identity_verified"):
        assert raw_approval[field] == guided_approval[field]
    raw_challenge = raw_service.store.read_artifact(raw_approval["challenge_sha256"]).to_document()
    guided_challenge = guided_service.store.read_artifact(
        guided_approval["challenge_sha256"]
    ).to_document()
    for field in ("proposed_disposition", "actor_label", "identity_verified"):
        assert raw_challenge["payload"][field] == guided_challenge["payload"][field]
    assert raw_challenge["artifact_type"] == guided_challenge["artifact_type"]
    assert set(raw_challenge["parents"]) == set(guided_challenge["parents"])
    assert raw_approval["decision_bundle_sha256"] == raw_service.status()["refs"]["decision_bundle"]
    assert (
        guided_approval["decision_bundle_sha256"] == guided_operator.state.refs["decision_bundle"]
    )

    for service in (raw_service, guided_service):
        status = service.status()
        assert status["decision_complete"] is True
        assert status["ready"] is True
        assert service.verify(status["snapshot_sha256"])["verified"] is True
