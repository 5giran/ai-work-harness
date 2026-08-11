"""Pure, immutable next-step planning for the local decision operator.

The service adapter is responsible for verifying a pinned snapshot before it
constructs :class:`ValidatedOperatorState`.  This module deliberately has no
integrity-failure branch: an integrity error must fail closed in the service
instead of being converted into operator guidance.

``artifacts`` is keyed by active ref name.  Each value is an immutable-friendly
mapping with the artifact envelope fields used by the planner::

    {
        "artifact_type": "evaluation-set",
        "producer": {"kind": "fixture"},
        "payload": {...},
    }

The planner also accepts ``producer_kind`` as a convenience for adapters.  It
never writes to the repository and its only time input is the explicit
``observed_at`` argument.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .domain import (
    AGENT_PRODUCER_KINDS,
    review_completion_state,
)
from .domain import final_decision_constraints as derive_final_decision_constraints

OPERATOR_PLAN_SCHEMA_VERSION = "operator-plan.v1"
AGENT_PRODUCERS = AGENT_PRODUCER_KINDS


class OperatorStage(StrEnum):
    """Stable public identifiers for the operator's current semantic stage."""

    SOURCE = "source"
    FRAME = "frame"
    CANDIDATES = "candidates"
    CRITERIA = "criteria"
    EVIDENCE = "evidence"
    EVALUATION = "evaluation"
    REVIEW = "review"
    COMPARISON = "comparison"
    RECOMMENDATION = "recommendation"
    FINAL_DECISION = "final_decision"
    APPROVAL = "approval"
    COMPLETE = "complete"


class OperatorAction(StrEnum):
    """Stable public identifiers for actions an operator may choose."""

    CAPTURE_SOURCE = "capture_source"
    IMPORT_FRAME = "import_frame"
    CONFIRM_FRAME = "confirm_frame"
    IMPORT_CANDIDATES = "import_candidates"
    CONFIRM_CANDIDATES = "confirm_candidates"
    IMPORT_CRITERIA = "import_criteria"
    CONFIRM_CRITERIA = "confirm_criteria"
    IMPORT_EVIDENCE = "import_evidence"
    IMPORT_EVALUATIONS = "import_evaluations"
    GENERATE_EVALUATIONS = "generate_evaluations"
    RETRY_EVALUATIONS = "retry_evaluations"
    REVIEW_EVALUATIONS = "review_evaluations"
    REVISE_EVALUATIONS = "revise_evaluations"
    DERIVE_COMPARISON = "derive_comparison"
    IMPORT_RECOMMENDATION = "import_recommendation"
    GENERATE_RECOMMENDATION = "generate_recommendation"
    RETRY_RECOMMENDATION = "retry_recommendation"
    RECORD_FINAL_DECISION = "record_final_decision"
    CREATE_APPROVAL_CHALLENGE = "create_approval_challenge"
    COMMIT_APPROVAL = "commit_approval"
    REISSUE_APPROVAL_CHALLENGE = "reissue_approval_challenge"
    REFRESH_STATE = "refresh_state"
    EXPORT_DECISION = "export_decision"


# Short aliases are useful to UI adapters without creating a second identifier
# registry.  They intentionally refer to the same enum classes.
StageId = OperatorStage
ActionId = OperatorAction


class PendingReviewStatus(StrEnum):
    PENDING = "pending"
    REVISION_REQUIRED = "revision_required"


class ChallengeStatus(StrEnum):
    NOT_YET_VALID = "not_yet_valid"
    ACTIVE = "active"
    EXPIRED = "expired"


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze(child) for child in value), key=repr))
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    if isinstance(value, StrEnum):
        return value.value
    return value


def _as_utc(value: datetime, *, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _require_nonblank(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonblank string")
    return value


@dataclass(frozen=True, slots=True)
class ApprovalChallengeBinding:
    """Full internal binding needed to commit an active approval challenge.

    No serializer is provided on purpose.  ``OperatorPlan.to_public_dict`` emits
    only a separately constructed, sanitized challenge summary.
    """

    challenge_id: str
    nonce: str
    decision_bundle_sha256: str
    challenge_sha256: str
    challenge_snapshot_sha256: str
    parent_snapshot_sha256: str
    proposed_disposition: str
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for label in (
            "challenge_id",
            "nonce",
            "decision_bundle_sha256",
            "challenge_sha256",
            "challenge_snapshot_sha256",
            "parent_snapshot_sha256",
            "proposed_disposition",
        ):
            _require_nonblank(getattr(self, label), label=label)
        issued_at = _as_utc(self.issued_at, label="issued_at")
        expires_at = _as_utc(self.expires_at, label="expires_at")
        if expires_at <= issued_at:
            raise ValueError("expires_at must be later than issued_at")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        challenge_sha256: str,
        challenge_snapshot_sha256: str,
    ) -> ApprovalChallengeBinding:
        """Build a binding from a validated approval-challenge payload."""

        def parsed_timestamp(name: str) -> datetime:
            raw = value[name]
            if not isinstance(raw, str):
                raise ValueError(f"{name} must be an ISO-8601 string")
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{name} must be an ISO-8601 string") from exc

        return cls(
            challenge_id=str(value["challenge_id"]),
            nonce=str(value["nonce"]),
            decision_bundle_sha256=str(value["decision_bundle_sha256"]),
            challenge_sha256=challenge_sha256,
            challenge_snapshot_sha256=challenge_snapshot_sha256,
            parent_snapshot_sha256=str(value["parent_snapshot_sha256"]),
            proposed_disposition=str(value["proposed_disposition"]),
            issued_at=parsed_timestamp("issued_at"),
            expires_at=parsed_timestamp("expires_at"),
        )


@dataclass(frozen=True, slots=True)
class ValidatedOperatorState:
    """Immutable adapter result for one service-verified pinned snapshot."""

    session_id: str
    generation: int
    pinned_snapshot_sha256: str
    lifecycle_state: str
    refs: Mapping[str, str]
    artifacts: Mapping[str, Mapping[str, Any]]
    decision_complete: bool = False
    ready: bool = False
    stale_reasons: tuple[str, ...] = ()
    active_challenge: ApprovalChallengeBinding | None = None
    outbound_consent_operation: str | None = None

    def __post_init__(self) -> None:
        _require_nonblank(self.session_id, label="session_id")
        _require_nonblank(self.pinned_snapshot_sha256, label="pinned_snapshot_sha256")
        _require_nonblank(self.lifecycle_state, label="lifecycle_state")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool):
            raise ValueError("generation must be an integer")
        if self.generation < 0:
            raise ValueError("generation must not be negative")
        if not isinstance(self.refs, Mapping):
            raise TypeError("refs must be a mapping")
        if not isinstance(self.artifacts, Mapping):
            raise TypeError("artifacts must be a mapping")
        refs: dict[str, str] = {}
        for key, digest in self.refs.items():
            if not isinstance(key, str) or not key or not isinstance(digest, str) or not digest:
                raise ValueError("refs must map nonblank strings to nonblank strings")
            refs[key] = digest
        artifacts: dict[str, Mapping[str, Any]] = {}
        for key, artifact in self.artifacts.items():
            if not isinstance(key, str) or not key or not isinstance(artifact, Mapping):
                raise ValueError("artifacts must map nonblank ref names to mappings")
            artifacts[key] = _freeze(artifact)
        if not isinstance(self.decision_complete, bool) or not isinstance(self.ready, bool):
            raise TypeError("decision_complete and ready must be booleans")
        stale_reasons = tuple(sorted(set(self.stale_reasons)))
        if any(not isinstance(reason, str) or not reason for reason in stale_reasons):
            raise ValueError("stale_reasons must contain nonblank strings")
        if self.outbound_consent_operation not in {None, "evaluations", "recommendation"}:
            raise ValueError("outbound_consent_operation must be evaluations or recommendation")
        has_challenge_ref = "approval_challenge" in refs
        if has_challenge_ref != (self.active_challenge is not None):
            raise ValueError("approval_challenge ref and active_challenge must be present together")
        if (
            self.active_challenge is not None
            and self.active_challenge.challenge_snapshot_sha256
            != self.pinned_snapshot_sha256
        ):
            raise ValueError("active challenge must bind the pinned challenge snapshot")
        object.__setattr__(self, "refs", MappingProxyType(refs))
        object.__setattr__(self, "artifacts", MappingProxyType(artifacts))
        object.__setattr__(self, "stale_reasons", stale_reasons)


@dataclass(frozen=True, slots=True)
class PendingReview:
    cell_id: str
    candidate_id: str
    candidate_title: str
    criterion_id: str
    criterion_title: str
    priority: str
    assessment: str
    rationale: str
    confidence: str
    evidence_ids: tuple[str, ...]
    uncertainties: tuple[str, ...]
    status: PendingReviewStatus
    revision_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_ids", tuple(self.evidence_ids))
        object.__setattr__(self, "uncertainties", tuple(self.uncertainties))

    def to_public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "cell_id": self.cell_id,
            "candidate_id": self.candidate_id,
            "candidate_title": self.candidate_title,
            "criterion_id": self.criterion_id,
            "criterion_title": self.criterion_title,
            "priority": self.priority,
            "assessment": self.assessment,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "evidence_ids": list(self.evidence_ids),
            "uncertainties": list(self.uncertainties),
            "status": self.status.value,
        }
        if self.revision_reason is not None:
            result["revision_reason"] = self.revision_reason
        return result


@dataclass(frozen=True, slots=True)
class FinalDecisionConstraints:
    eligible_candidate_ids: tuple[str, ...]
    required_by_candidate: Mapping[str, tuple[str, ...]]
    reject_all_required_acknowledgements: tuple[str, ...]
    reject_all_shared_risk_required: bool
    reject_all_shared_risk_feasible: bool
    reject_all_shared_risk_criterion_ids: tuple[str, ...]
    provisional_dispositions: tuple[str, ...] = ("defer", "request_more_evidence")

    def __post_init__(self) -> None:
        object.__setattr__(self, "eligible_candidate_ids", tuple(self.eligible_candidate_ids))
        object.__setattr__(
            self,
            "required_by_candidate",
            MappingProxyType(
                {
                    key: tuple(value)
                    for key, value in sorted(self.required_by_candidate.items())
                }
            ),
        )
        object.__setattr__(
            self,
            "reject_all_required_acknowledgements",
            tuple(self.reject_all_required_acknowledgements),
        )
        object.__setattr__(
            self,
            "reject_all_shared_risk_criterion_ids",
            tuple(self.reject_all_shared_risk_criterion_ids),
        )
        object.__setattr__(self, "provisional_dispositions", tuple(self.provisional_dispositions))

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "allowed_dispositions": [
                "select",
                "reject_all",
                *self.provisional_dispositions,
            ],
            "approvable_dispositions": ["select", "reject_all"],
            "eligible_candidate_ids": list(self.eligible_candidate_ids),
            "select": {
                "required_risk_acknowledgements_by_candidate": {
                    key: list(value) for key, value in self.required_by_candidate.items()
                }
            },
            "reject_all": {
                "required_risk_acknowledgements": list(
                    self.reject_all_required_acknowledgements
                ),
                "shared_risk_condition": {
                    "required": self.reject_all_shared_risk_required,
                    "feasible": self.reject_all_shared_risk_feasible,
                    "criterion_ids": list(self.reject_all_shared_risk_criterion_ids),
                },
            },
            "provisional_dispositions": list(self.provisional_dispositions),
        }


@dataclass(frozen=True, slots=True)
class OperatorPlan:
    """Immutable plan; public serialization intentionally omits sensitive pins."""

    session_id: str
    generation: int
    pinned_snapshot_sha256: str
    lifecycle_state: str
    observed_at: datetime
    stage: OperatorStage
    recommended_action: OperatorAction
    available_actions: tuple[OperatorAction, ...]
    summary: Mapping[str, Any]
    pending_reviews: tuple[PendingReview, ...]
    final_decision_constraints: FinalDecisionConstraints | None
    public_challenge: Mapping[str, Any] | None
    decision_complete: bool
    ready: bool
    stale_reasons: tuple[str, ...]
    semantic_artifacts: Mapping[str, Mapping[str, Any]] = field(repr=False)
    challenge_binding: ApprovalChallengeBinding | None = field(default=None, repr=False)
    schema_version: str = OPERATOR_PLAN_SCHEMA_VERSION

    def __post_init__(self) -> None:
        observed_at = _as_utc(self.observed_at, label="observed_at")
        available = tuple(self.available_actions)
        if self.recommended_action not in available:
            raise ValueError("recommended_action must be one of available_actions")
        if len(set(available)) != len(available):
            raise ValueError("available_actions must not contain duplicates")
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "available_actions", available)
        object.__setattr__(self, "summary", _freeze(self.summary))
        object.__setattr__(self, "pending_reviews", tuple(self.pending_reviews))
        object.__setattr__(self, "stale_reasons", tuple(self.stale_reasons))
        object.__setattr__(self, "semantic_artifacts", _freeze(self.semantic_artifacts))
        if self.public_challenge is not None:
            object.__setattr__(self, "public_challenge", _freeze(self.public_challenge))

    def to_public_dict(self) -> dict[str, Any]:
        """Return the stable JSON value used under the raw command envelope."""

        return {
            "schema_version": self.schema_version,
            "observed_at": _timestamp(self.observed_at),
            "stage": self.stage.value,
            "recommended_action": self.recommended_action.value,
            "available_actions": [item.value for item in self.available_actions],
            "summary": _thaw(self.summary),
            "pending_reviews": [item.to_public_dict() for item in self.pending_reviews],
            "final_decision_constraints": (
                self.final_decision_constraints.to_public_dict()
                if self.final_decision_constraints is not None
                else None
            ),
            "challenge": _thaw(self.public_challenge),
            "decision_complete": self.decision_complete,
            "ready": self.ready,
            "stale_reasons": list(self.stale_reasons),
        }


def _artifact(state: ValidatedOperatorState, ref_name: str) -> Mapping[str, Any]:
    value = state.artifacts.get(ref_name)
    return value if isinstance(value, Mapping) else MappingProxyType({})


def _payload(state: ValidatedOperatorState, ref_name: str) -> Mapping[str, Any]:
    artifact = _artifact(state, ref_name)
    payload = artifact.get("payload")
    return payload if isinstance(payload, Mapping) else artifact


def _records(payload: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    value = payload.get(key, ())
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _producer_kind(state: ValidatedOperatorState, ref_name: str) -> str:
    artifact = _artifact(state, ref_name)
    direct = artifact.get("producer_kind")
    if isinstance(direct, str) and direct:
        return direct
    producer = artifact.get("producer")
    if isinstance(producer, str) and producer:
        return producer
    if isinstance(producer, Mapping):
        kind = producer.get("kind")
        if isinstance(kind, str) and kind:
            return kind
    return "unknown"


def _artifact_type(state: ValidatedOperatorState, ref_name: str) -> str:
    value = _artifact(state, ref_name).get("artifact_type")
    return value if isinstance(value, str) and value else ref_name.replace("_", "-")


def _title_by_id(
    records: Sequence[Mapping[str, Any]],
    *,
    id_key: str,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for record in records:
        identifier = record.get(id_key)
        if not isinstance(identifier, str):
            continue
        title = record.get("title")
        result[identifier] = title if isinstance(title, str) and title else identifier
    return result


def _pending_reviews(state: ValidatedOperatorState) -> tuple[PendingReview, ...]:
    if "evaluation_set" not in state.refs:
        return ()
    cells = _records(_payload(state, "evaluation_set"), "cells")
    criteria = _records(_payload(state, "criteria_set"), "criteria")
    candidates = _records(_payload(state, "candidate_set"), "candidates")
    reviews = _records(_payload(state, "evaluation_review_set"), "reviews")
    criterion_by_id = {
        str(record.get("criterion_id")): record
        for record in criteria
        if isinstance(record.get("criterion_id"), str)
    }
    candidate_titles = _title_by_id(candidates, id_key="candidate_id")
    criterion_titles = _title_by_id(criteria, id_key="criterion_id")
    evaluation_cells = {
        (str(record.get("candidate_id")), str(record.get("criterion_id"))): record
        for record in cells
        if isinstance(record.get("candidate_id"), str)
        and isinstance(record.get("criterion_id"), str)
    }
    review_by_key = {
        (str(record.get("candidate_id")), str(record.get("criterion_id"))): record
        for record in reviews
        if isinstance(record.get("candidate_id"), str)
        and isinstance(record.get("criterion_id"), str)
    }
    producer_kind = _producer_kind(state, "evaluation_set")
    completion = review_completion_state(
        evaluation_cells=evaluation_cells,
        criteria=criterion_by_id,
        producer_kind=producer_kind,
        reviews=reviews,
    )
    pending_ids = set(completion.pending_cell_ids)
    revision_ids = set(completion.revision_required_cell_ids)
    result: list[PendingReview] = []
    for cell in sorted(
        cells,
        key=lambda item: (str(item.get("candidate_id", "")), str(item.get("criterion_id", ""))),
    ):
        candidate_id = cell.get("candidate_id")
        criterion_id = cell.get("criterion_id")
        if not isinstance(candidate_id, str) or not isinstance(criterion_id, str):
            continue
        criterion = criterion_by_id.get(criterion_id, {})
        priority = criterion.get("priority")
        priority = priority if isinstance(priority, str) else "unknown"
        review = review_by_key.get((candidate_id, criterion_id))
        cell_id = (candidate_id, criterion_id)
        if cell_id in revision_ids:
            status = PendingReviewStatus.REVISION_REQUIRED
        elif cell_id in pending_ids:
            status = PendingReviewStatus.PENDING
        else:
            continue
        evidence_ids = cell.get("evidence_ids", ())
        uncertainties = cell.get("uncertainties", ())
        revision_reason = review.get("reason") if review is not None else None
        result.append(
            PendingReview(
                cell_id=f"{candidate_id}/{criterion_id}",
                candidate_id=candidate_id,
                candidate_title=candidate_titles.get(candidate_id, candidate_id),
                criterion_id=criterion_id,
                criterion_title=criterion_titles.get(criterion_id, criterion_id),
                priority=priority,
                assessment=str(cell.get("assessment", "unknown")),
                rationale=str(cell.get("rationale", "")),
                confidence=str(cell.get("confidence", "unknown")),
                evidence_ids=tuple(
                    item for item in evidence_ids if isinstance(item, str)
                )
                if isinstance(evidence_ids, Sequence)
                else (),
                uncertainties=tuple(
                    item for item in uncertainties if isinstance(item, str)
                )
                if isinstance(uncertainties, Sequence)
                else (),
                status=status,
                revision_reason=(
                    revision_reason if isinstance(revision_reason, str) else None
                ),
            )
        )
    return tuple(result)


def _final_constraints(state: ValidatedOperatorState) -> FinalDecisionConstraints | None:
    if "comparison" not in state.refs:
        return None
    comparison = _payload(state, "comparison")
    policy = derive_final_decision_constraints(
        comparison=comparison,
        producer_kind=_producer_kind(state, "evaluation_set"),
        disposition="reject_all",
        candidate_id=None,
    )
    return FinalDecisionConstraints(
        eligible_candidate_ids=policy.eligible_candidate_ids,
        required_by_candidate=policy.required_risk_acknowledgement_cell_ids_by_candidate,
        reject_all_required_acknowledgements=(
            policy.required_risk_acknowledgement_cell_ids
        ),
        reject_all_shared_risk_required=bool(policy.relevant_candidate_ids),
        reject_all_shared_risk_feasible=policy.reject_all_shared_risk_feasible,
        reject_all_shared_risk_criterion_ids=(
            policy.reject_all_shared_risk_criterion_ids
        ),
    )


_ARTIFACT_ORDER = (
    "source_manifest",
    "decision_frame",
    "frame_confirmation",
    "candidate_set",
    "candidate_confirmation",
    "criteria_set",
    "criteria_confirmation",
    "evidence_set",
    "evaluation_set",
    "evaluation_review_set",
    "comparison",
    "recommendation",
    "final_decision",
    "decision_bundle",
    "approval_challenge",
    "human_approval",
    "agent_consent",
    "agent_run",
    "migration_report",
)


def _summary(state: ValidatedOperatorState) -> Mapping[str, Any]:
    sources = []
    for source in sorted(
        _records(_payload(state, "source_manifest"), "sources"),
        key=lambda item: str(item.get("source_id", "")),
    ):
        summary = {
            key: source[key]
            for key in ("source_id", "media_type", "bytes")
            if key in source and isinstance(source[key], (str, int))
        }
        sources.append(summary)
    order = {name: index for index, name in enumerate(_ARTIFACT_ORDER)}
    active_refs = sorted(state.refs, key=lambda name: (order.get(name, len(order)), name))
    artifacts = {
        ref_name: {
            "artifact_type": _artifact_type(state, ref_name),
            "fingerprint": state.refs[ref_name][:12],
        }
        for ref_name in active_refs
    }
    producers = {
        ref_name: _producer_kind(state, ref_name)
        for ref_name in active_refs
    }
    return {"sources": sources, "artifacts": artifacts, "producers": producers}


def _public_challenge(
    binding: ApprovalChallengeBinding,
    *,
    observed_at: datetime,
) -> tuple[ChallengeStatus, Mapping[str, Any]]:
    if observed_at < binding.issued_at:
        status = ChallengeStatus.NOT_YET_VALID
    elif observed_at >= binding.expires_at:
        status = ChallengeStatus.EXPIRED
    else:
        status = ChallengeStatus.ACTIVE
    remaining = max(0, math.ceil((binding.expires_at - observed_at).total_seconds()))
    return status, {
        "status": status.value,
        "proposed_disposition": binding.proposed_disposition,
        "issued_at": _timestamp(binding.issued_at),
        "expires_at": _timestamp(binding.expires_at),
        "remaining_seconds": remaining,
        "decision_bundle_fingerprint": binding.decision_bundle_sha256[:12],
    }


def _outbound_operation(state: ValidatedOperatorState) -> str | None:
    if "agent_consent" not in state.refs:
        return None
    if state.outbound_consent_operation is not None:
        return state.outbound_consent_operation
    manifest = _payload(state, "agent_consent").get("outbound_manifest")
    if isinstance(manifest, Mapping):
        operation = manifest.get("operation")
        if operation in {"evaluations", "recommendation"}:
            return str(operation)
    return None


def plan_operator_next(
    state: ValidatedOperatorState,
    *,
    observed_at: datetime,
) -> OperatorPlan:
    """Return the deterministic next operator plan for a verified snapshot."""

    observed_at = _as_utc(observed_at, label="observed_at")
    refs = state.refs
    pending_reviews = _pending_reviews(state)
    constraints = _final_constraints(state)
    challenge_binding = state.active_challenge
    public_challenge: Mapping[str, Any] | None = None
    stage: OperatorStage
    recommended: OperatorAction
    actions: tuple[OperatorAction, ...]

    # Priority is intentional and mirrors the immutable workflow: terminal
    # approval, active challenge, final, comparison/recommendation,
    # evaluation/review/comparison, evidence, criteria, candidates, frame,
    # source.
    if "human_approval" in refs:
        stage = OperatorStage.COMPLETE
        recommended = OperatorAction.EXPORT_DECISION
        actions = (recommended,)
    elif challenge_binding is not None:
        challenge_status, public_challenge = _public_challenge(
            challenge_binding,
            observed_at=observed_at,
        )
        stage = OperatorStage.APPROVAL
        if challenge_status is ChallengeStatus.ACTIVE:
            recommended = OperatorAction.COMMIT_APPROVAL
        elif challenge_status is ChallengeStatus.EXPIRED:
            recommended = OperatorAction.REISSUE_APPROVAL_CHALLENGE
        else:
            recommended = OperatorAction.REFRESH_STATE
        actions = (recommended,)
    elif "final_decision" in refs:
        stage = OperatorStage.APPROVAL
        recommended = OperatorAction.CREATE_APPROVAL_CHALLENGE
        actions = (recommended,)
    elif "recommendation" in refs:
        stage = OperatorStage.FINAL_DECISION
        recommended = OperatorAction.RECORD_FINAL_DECISION
        actions = (recommended,)
    elif "comparison" in refs:
        stage = OperatorStage.RECOMMENDATION
        outbound_operation = _outbound_operation(state)
        if outbound_operation == "recommendation":
            recommended = OperatorAction.RETRY_RECOMMENDATION
            actions = (
                recommended,
                OperatorAction.IMPORT_RECOMMENDATION,
                OperatorAction.RECORD_FINAL_DECISION,
            )
        else:
            recommended = OperatorAction.GENERATE_RECOMMENDATION
            actions = (
                recommended,
                OperatorAction.IMPORT_RECOMMENDATION,
                OperatorAction.RECORD_FINAL_DECISION,
            )
    elif "evaluation_set" in refs:
        revision_required = any(
            item.status is PendingReviewStatus.REVISION_REQUIRED for item in pending_reviews
        )
        review_required = any(
            item.status is PendingReviewStatus.PENDING for item in pending_reviews
        )
        if revision_required:
            stage = OperatorStage.EVALUATION
            recommended = OperatorAction.REVISE_EVALUATIONS
            actions = (recommended, OperatorAction.IMPORT_EVALUATIONS)
        elif review_required:
            stage = OperatorStage.REVIEW
            recommended = OperatorAction.REVIEW_EVALUATIONS
            actions = (recommended,)
        else:
            stage = OperatorStage.COMPARISON
            recommended = OperatorAction.DERIVE_COMPARISON
            actions = (recommended,)
    elif "evidence_set" in refs:
        stage = OperatorStage.EVALUATION
        outbound_operation = _outbound_operation(state)
        if outbound_operation == "evaluations":
            recommended = OperatorAction.RETRY_EVALUATIONS
            actions = (recommended, OperatorAction.IMPORT_EVALUATIONS)
        else:
            recommended = OperatorAction.IMPORT_EVALUATIONS
            actions = (recommended, OperatorAction.GENERATE_EVALUATIONS)
    elif "criteria_confirmation" in refs:
        stage = OperatorStage.EVIDENCE
        recommended = OperatorAction.IMPORT_EVIDENCE
        actions = (recommended,)
    elif "criteria_set" in refs:
        stage = OperatorStage.CRITERIA
        recommended = OperatorAction.CONFIRM_CRITERIA
        actions = (recommended, OperatorAction.IMPORT_CRITERIA)
    elif "candidate_confirmation" in refs:
        stage = OperatorStage.CRITERIA
        recommended = OperatorAction.IMPORT_CRITERIA
        actions = (recommended,)
    elif "candidate_set" in refs:
        stage = OperatorStage.CANDIDATES
        recommended = OperatorAction.CONFIRM_CANDIDATES
        actions = (recommended, OperatorAction.IMPORT_CANDIDATES)
    elif "frame_confirmation" in refs:
        stage = OperatorStage.CANDIDATES
        recommended = OperatorAction.IMPORT_CANDIDATES
        actions = (recommended,)
    elif "decision_frame" in refs:
        stage = OperatorStage.FRAME
        recommended = OperatorAction.CONFIRM_FRAME
        actions = (recommended, OperatorAction.IMPORT_FRAME)
    elif "source_manifest" in refs:
        stage = OperatorStage.FRAME
        recommended = OperatorAction.IMPORT_FRAME
        actions = (recommended, OperatorAction.CAPTURE_SOURCE)
    else:
        stage = OperatorStage.SOURCE
        recommended = OperatorAction.CAPTURE_SOURCE
        actions = (recommended,)

    return OperatorPlan(
        session_id=state.session_id,
        generation=state.generation,
        pinned_snapshot_sha256=state.pinned_snapshot_sha256,
        lifecycle_state=state.lifecycle_state,
        observed_at=observed_at,
        stage=stage,
        recommended_action=recommended,
        available_actions=actions,
        summary=_summary(state),
        pending_reviews=pending_reviews,
        final_decision_constraints=constraints,
        public_challenge=public_challenge,
        decision_complete=state.decision_complete,
        ready=state.ready,
        stale_reasons=state.stale_reasons,
        semantic_artifacts=state.artifacts,
        challenge_binding=challenge_binding,
    )
