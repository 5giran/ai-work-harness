"""Read-only workflow policy for schema-validated, pinned artifact documents."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ai_work_harness.errors import HarnessError

from .domain import (
    derive_comparison,
    frame_is_confirmable,
    recommendation_relation,
    require_comparison_reviews,
    validate_candidates,
    validate_criteria,
    validate_evaluations,
    validate_evidence,
    validate_final_decision,
    validate_frame,
    validate_recommendation,
    validate_reviews,
)

# Parent *presence* is a workflow rule, distinct from checking that declared
# parents resolve to current digests. Optional/consumed parents are handled below.
_REQUIRED_PARENTS = {
    "source_manifest": set(),
    "decision_frame": {"source_manifest"},
    "frame_confirmation": {"decision_frame"},
    "candidate_set": {"decision_frame", "frame_confirmation"},
    "candidate_confirmation": {"candidate_set"},
    "criteria_set": {"candidate_set", "candidate_confirmation"},
    "criteria_confirmation": {"criteria_set"},
    "evidence_set": {"source_manifest", "criteria_set", "criteria_confirmation"},
    "evaluation_set": {"candidate_set", "criteria_set", "evidence_set"},
    "evaluation_review_set": {"evaluation_set"},
    "comparison": {"evaluation_set"},
    "recommendation": {"comparison"},
    "final_decision": {"comparison"},
    "approval_challenge": {"decision_bundle", "final_decision"},
    "human_approval": {"approval_challenge", "decision_bundle", "final_decision"},
}


def _verify_dependencies(
    refs: Mapping[str, str], artifacts: Mapping[str, Mapping[str, Any]]
) -> None:
    for ref, required in _REQUIRED_PARENTS.items():
        if ref not in artifacts:
            continue
        expected = set(required)
        if ref == "comparison" and "evaluation_review_set" in refs:
            expected.add("evaluation_review_set")
        if ref == "final_decision" and "recommendation" in refs:
            expected.add("recommendation")
        actual = set(artifacts[ref]["parents"])
        active_required = expected - ({"approval_challenge"} if ref == "human_approval" else set())
        if actual != expected or not active_required.issubset(refs):
            raise HarnessError(
                "WORKFLOW_DEPENDENCY_MISSING",
                f"{ref} does not bind all required workflow parents",
                details={"artifact": ref, "expected_parents": sorted(expected)},
            )
    if "decision_bundle" in artifacts:
        expected_refs = {
            key: value
            for key, value in refs.items()
            if key not in {"decision_bundle", "approval_challenge", "human_approval"}
        }
        bundle = artifacts["decision_bundle"]
        if (
            "final_decision" not in expected_refs
            or bundle["parents"] != expected_refs
            or bundle["payload"]["refs"] != expected_refs
        ):
            raise HarnessError("DECISION_BUNDLE_MISMATCH", "Decision bundle omits active inputs")


def verify_workflow(
    *,
    refs: Mapping[str, str],
    artifacts: Mapping[str, Mapping[str, Any]],
    excerpt_hashes: Mapping[tuple[str, int, int], str],
) -> None:
    """Recompute reached-stage policy without writes or mutation of caller data.

    Missing downstream stages are valid progress. Once a stage exists, every
    dependency and policy necessary to create that stage must still hold.
    Hash/schema/session checks and historical approval/consent bindings remain
    the store and service verifier's responsibility.
    """

    _verify_dependencies(refs, artifacts)
    payloads = {key: deepcopy(artifact["payload"]) for key, artifact in artifacts.items()}
    if "decision_frame" in payloads:
        if not payloads["source_manifest"]["sources"]:
            raise HarnessError("SOURCE_REQUIRED", "A decision frame requires captured sources")
        frame = validate_frame(payloads["decision_frame"])
        if "frame_confirmation" in payloads and not frame_is_confirmable(frame):
            raise HarnessError("FRAME_INCOMPLETE", "A confirmed frame must be complete")
    if "candidate_set" in payloads:
        validate_candidates(payloads["candidate_set"])
    if "criteria_set" in payloads:
        validate_criteria(payloads["criteria_set"])
    if "evidence_set" not in payloads:
        return

    evidence = validate_evidence(
        payloads["evidence_set"],
        source_ids={item["source_id"] for item in payloads["source_manifest"]["sources"]},
        excerpt_hashes=excerpt_hashes,
    )["evidence"]
    if "evaluation_set" not in payloads:
        return
    candidates = payloads["candidate_set"]["candidates"]
    criteria = payloads["criteria_set"]["criteria"]
    evidence_by_id = {item["evidence_id"]: item for item in evidence}
    producer_kind = artifacts["evaluation_set"]["producer"]["kind"]
    if producer_kind not in {"local_operator", "agent_import", "fixture", "openai"}:
        raise HarnessError("INVALID_PRODUCER", "Unsupported evaluation producer")
    cells = validate_evaluations(
        payloads["evaluation_set"],
        candidate_ids={item["candidate_id"] for item in candidates},
        criterion_ids={item["criterion_id"] for item in criteria},
        evidence_by_id=evidence_by_id,
    )["cells"]
    reviews: list[dict[str, Any]] = []
    if "evaluation_review_set" in payloads:
        reviews = validate_reviews(
            payloads["evaluation_review_set"],
            evaluation_cells={(item["candidate_id"], item["criterion_id"]): item for item in cells},
            criteria={item["criterion_id"]: item for item in criteria},
            producer_kind=producer_kind,
            evidence_by_id=evidence_by_id,
        )["reviews"]
    if "comparison" not in payloads:
        return
    require_comparison_reviews(
        cells=cells, criteria=criteria, reviews=reviews, producer_kind=producer_kind
    )
    comparison = derive_comparison(
        candidates=candidates,
        criteria=criteria,
        cells=cells,
        reviews=reviews,
        producer_kind=producer_kind,
    ).payload
    if payloads["comparison"] != comparison:
        raise HarnessError(
            "COMPARISON_MISMATCH", "Stored comparison differs from its reviewed evaluations"
        )
    recommendation = payloads.get("recommendation")
    if recommendation is not None:
        validate_recommendation(
            recommendation, comparison=comparison, evidence_ids=set(evidence_by_id)
        )
    if "final_decision" not in payloads:
        return
    final = payloads["final_decision"]
    validate_final_decision(
        {
            key: final[key]
            for key in ("disposition", "candidate_id", "reason", "risk_acknowledgements")
        },
        comparison=comparison,
        producer_kind=producer_kind,
    )
    if final["recommendation_relation"] != recommendation_relation(final, recommendation):
        raise HarnessError(
            "RECOMMENDATION_RELATION_MISMATCH",
            "Final decision misstates its recommendation relation",
        )
    if (
        "human_approval" in payloads
        and payloads["human_approval"]["disposition"] == "approved"
        and final["disposition"] not in {"select", "reject_all"}
    ):
        raise HarnessError("DECISION_NOT_APPROVABLE", "An approved decision cannot be provisional")
