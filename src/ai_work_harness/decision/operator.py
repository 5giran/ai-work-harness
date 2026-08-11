"""Thin, pinned orchestration for guided decision interfaces.

This module owns no workflow policy and performs no prompting.  It keeps the
snapshot an operator actually reviewed, injects that full digest into service
mutations, and asks the pure guidance planner for the next view.  A failed
mutation never refreshes or advances the cursor.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_work_harness.errors import HarnessError

from .guidance import (
    ApprovalChallengeBinding,
    OperatorPlan,
    ValidatedOperatorState,
    plan_operator_next,
)
from .service import SUBJECT_REFS, DecisionService

Mutation = Callable[..., Mapping[str, Any]]
Planner = Callable[..., OperatorPlan]


@dataclass(frozen=True, slots=True)
class OperatorCursor:
    """The exact verified snapshot reviewed by one guided operator process."""

    session_id: str
    generation: int
    pinned_snapshot_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session_id must be a nonblank string")
        if (
            not isinstance(self.generation, int)
            or isinstance(self.generation, bool)
            or self.generation < 0
        ):
            raise ValueError("generation must be a nonnegative integer")
        if (
            not isinstance(self.pinned_snapshot_sha256, str)
            or not self.pinned_snapshot_sha256.strip()
        ):
            raise ValueError("pinned_snapshot_sha256 must be a nonblank string")

    @classmethod
    def from_state(cls, state: ValidatedOperatorState) -> OperatorCursor:
        return cls(
            session_id=state.session_id,
            generation=state.generation,
            pinned_snapshot_sha256=state.pinned_snapshot_sha256,
        )

    @property
    def expected_parent(self) -> str:
        return self.pinned_snapshot_sha256

    def advance(self, result: Mapping[str, Any]) -> OperatorCursor:
        """Validate a successful mutation result and return its resulting cursor.

        Content-identical imports are deterministic service no-ops: they return
        the same generation and digest.  Every other successful mutation must
        create exactly one new snapshot.
        """

        if not isinstance(result, Mapping) or result.get("ok") is not True:
            raise ValueError("guided mutations must return a successful service result")
        if result.get("session_id") != self.session_id:
            raise ValueError("mutation result belongs to a different session")
        generation = result.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool):
            raise ValueError("mutation result generation must be an integer")
        snapshot_sha256 = result.get("snapshot_sha256")
        if not isinstance(snapshot_sha256, str) or not snapshot_sha256.strip():
            raise ValueError("mutation result is missing snapshot_sha256")
        if generation == self.generation and snapshot_sha256 == self.pinned_snapshot_sha256:
            return self
        if generation != self.generation + 1:
            raise ValueError("guided mutations must advance by exactly one generation")
        if snapshot_sha256 == self.pinned_snapshot_sha256:
            raise ValueError("a new generation must have a new snapshot digest")
        return OperatorCursor(
            session_id=self.session_id,
            generation=generation,
            pinned_snapshot_sha256=snapshot_sha256,
        )


class GuidedOperator:
    """Pinned DecisionService adapter shared by guided UI controllers."""

    def __init__(
        self,
        service: DecisionService,
        *,
        planner: Planner = plan_operator_next,
    ) -> None:
        self._service = service
        self._planner = planner
        state = service.read_operator_state()
        plan = planner(state, observed_at=service.clock())
        self._cursor = OperatorCursor.from_state(state)
        self._state = state
        self._plan = plan

    @property
    def service(self) -> DecisionService:
        return self._service

    @property
    def cursor(self) -> OperatorCursor:
        return self._cursor

    @property
    def state(self) -> ValidatedOperatorState:
        return self._state

    @property
    def plan(self) -> OperatorPlan:
        return self._plan

    def public_plan(self) -> dict[str, Any]:
        """Return the planner's sanitized value, never its internal challenge binding."""

        return self._plan.to_public_dict()

    def semantic_artifact(self, ref_name: str) -> Mapping[str, Any] | None:
        """Return an immutable active artifact view needed for semantic rendering.

        Approval-challenge payloads contain the nonce used by ``commit_approval``
        and are intentionally available only through the internal binding.
        """

        if ref_name == "approval_challenge":
            return None
        artifact = self._plan.semantic_artifacts.get(ref_name)
        return artifact if isinstance(artifact, Mapping) else None

    def semantic_payload(self, ref_name: str) -> Mapping[str, Any] | None:
        artifact = self.semantic_artifact(ref_name)
        if artifact is None:
            return None
        payload = artifact.get("payload")
        return payload if isinstance(payload, Mapping) else artifact

    def reload(self) -> OperatorPlan:
        """Explicitly adopt current state after the controller asks to refresh.

        Conflict handling never calls this method automatically, so a stale
        mutation cannot be silently retried or rebased.
        """

        state = self._service.read_operator_state()
        plan = self._planner(state, observed_at=self._service.clock())
        cursor = OperatorCursor.from_state(state)
        self._cursor = cursor
        self._state = state
        self._plan = plan
        return plan

    def _validate_pinned_read(self, result: Mapping[str, Any]) -> None:
        if result.get("ok") is not True or result.get("session_id") != self._cursor.session_id:
            raise ValueError("guided reads must return this operator session")
        if (
            result.get("generation") != self._cursor.generation
            or result.get("snapshot_sha256") != self._cursor.pinned_snapshot_sha256
        ):
            raise HarnessError(
                "WRITE_CONFLICT",
                "Current snapshot moved while the guided operator was reviewing it",
                details={
                    "expected": self._cursor.pinned_snapshot_sha256,
                    "current": result.get("snapshot_sha256"),
                },
                exit_code=3,
            )

    def mutate(
        self,
        operation: Mutation,
        /,
        *args: Any,
        **kwargs: Any,
    ) -> OperatorPlan:
        """Run one service mutation against the pinned parent without retrying."""

        if "expected_parent" in kwargs:
            raise TypeError("GuidedOperator owns expected_parent")
        previous_cursor = self._cursor
        result = operation(
            *args,
            expected_parent=previous_cursor.expected_parent,
            **kwargs,
        )
        next_cursor = previous_cursor.advance(result)
        next_state = self._service.read_operator_state()
        if next_state.pinned_snapshot_sha256 != next_cursor.pinned_snapshot_sha256:
            raise HarnessError(
                "WRITE_CONFLICT",
                "Current snapshot moved before the guided mutation could be reloaded",
                details={
                    "expected": next_cursor.pinned_snapshot_sha256,
                    "current": next_state.pinned_snapshot_sha256,
                },
                exit_code=3,
            )
        if next_state.generation != next_cursor.generation:
            raise HarnessError(
                "WRITE_CONFLICT",
                "Current snapshot generation differs from the guided mutation result",
                details={
                    "expected_generation": next_cursor.generation,
                    "current_generation": next_state.generation,
                },
                exit_code=3,
            )
        next_plan = self._planner(next_state, observed_at=self._service.clock())
        self._cursor = next_cursor
        self._state = next_state
        self._plan = next_plan
        return next_plan

    def capture_source(
        self,
        *,
        source_id: str,
        source: Path,
        media_type: str | None = None,
    ) -> OperatorPlan:
        return self.mutate(
            self._service.capture_source,
            source_id=source_id,
            source=source,
            media_type=media_type,
        )

    def import_frame(
        self,
        payload: Any,
        *,
        producer_kind: str = "local_operator",
    ) -> OperatorPlan:
        return self.mutate(
            self._service.import_frame,
            payload,
            producer_kind=producer_kind,
        )

    def confirm(self, subject_type: str) -> OperatorPlan:
        ref_name = SUBJECT_REFS.get(subject_type)
        if ref_name is None:
            raise HarnessError(
                "INVALID_CONFIRMATION_SUBJECT",
                "Unknown confirmation subject",
            )
        artifact_sha256 = self._state.refs.get(ref_name)
        if artifact_sha256 is None:
            raise HarnessError(
                "WORKFLOW_GATE_REQUIRED",
                f"Required current artifact is missing: {ref_name}",
            )
        return self.mutate(
            self._service.confirm,
            subject_type,
            expected_artifact_sha=artifact_sha256,
            method="guided_semantic_review",
        )

    def import_candidates(
        self,
        payload: Any,
        *,
        producer_kind: str = "local_operator",
    ) -> OperatorPlan:
        return self.mutate(
            self._service.import_candidates,
            payload,
            producer_kind=producer_kind,
        )

    def import_criteria(
        self,
        payload: Any,
        *,
        producer_kind: str = "local_operator",
    ) -> OperatorPlan:
        return self.mutate(
            self._service.import_criteria,
            payload,
            producer_kind=producer_kind,
        )

    def import_evidence(self, payload: Any) -> OperatorPlan:
        return self.mutate(self._service.import_evidence, payload)

    def import_evaluations(
        self,
        payload: Any,
        *,
        producer_kind: str = "local_operator",
        producer_extra: Mapping[str, Any] | None = None,
    ) -> OperatorPlan:
        return self.mutate(
            self._service.import_evaluations,
            payload,
            producer_kind=producer_kind,
            producer_extra=producer_extra,
        )

    def import_reviews(self, payload: Any) -> OperatorPlan:
        return self.mutate(self._service.import_reviews, payload)

    def preview_agent(
        self,
        *,
        operation: str,
        provider: str = "openai",
        model: str | None = None,
    ) -> Mapping[str, Any]:
        """Preview outbound data against the operator's pinned snapshot."""

        result = self._service.preview_agent(
            operation=operation,
            provider=provider,
            model=model,
        )
        self._validate_pinned_read(result)
        return result

    def consent_agent(
        self,
        *,
        operation: str,
        preview: Mapping[str, Any],
    ) -> OperatorPlan:
        """Bind a reviewed preview without exposing its full digest to the user."""

        self._validate_pinned_read(preview)
        manifest = preview.get("outbound_manifest")
        manifest_sha = preview.get("outbound_manifest_sha256")
        if not isinstance(manifest, Mapping) or not isinstance(manifest_sha, str):
            raise ValueError("agent preview is missing its outbound manifest binding")
        if manifest.get("operation") != operation:
            raise ValueError("agent preview operation does not match the consent request")
        provider = manifest.get("provider")
        model = manifest.get("model")
        if not isinstance(provider, str) or not isinstance(model, str):
            raise ValueError("agent preview provider configuration is invalid")
        return self.mutate(
            self._service.consent_agent,
            operation=operation,
            provider=provider,
            model=model,
            expected_manifest_sha=manifest_sha,
            method="guided_exact_phrase",
        )

    def run_openai_agent(
        self,
        *,
        operation: str,
        provider: Any | None = None,
    ) -> OperatorPlan:
        """Run only the active consented request and advance on a successful commit."""

        return self.mutate(
            self._service.run_openai_agent,
            operation=operation,
            provider=provider,
        )

    def compare(self) -> OperatorPlan:
        return self.mutate(self._service.compare)

    def record_recommendation(
        self,
        payload: Any,
        *,
        producer_kind: str,
        producer_extra: Mapping[str, Any] | None = None,
    ) -> OperatorPlan:
        return self.mutate(
            self._service.record_recommendation,
            payload,
            producer_kind=producer_kind,
            producer_extra=producer_extra,
        )

    def import_final_decision(self, payload: Any) -> OperatorPlan:
        return self.mutate(self._service.import_final_decision, payload)

    def create_approval_challenge(self, *, disposition: str) -> OperatorPlan:
        return self.mutate(
            self._service.create_approval_challenge,
            disposition=disposition,
        )

    def commit_approval(self, *, reason: str) -> OperatorPlan:
        binding = self._active_challenge_binding()
        return self.mutate(
            self._service.commit_approval,
            challenge_id=binding.challenge_id,
            nonce=binding.nonce,
            expected_bundle_sha=binding.decision_bundle_sha256,
            reason=reason,
        )

    def _active_challenge_binding(self) -> ApprovalChallengeBinding:
        binding = self._state.active_challenge
        if binding is None:
            raise HarnessError(
                "WORKFLOW_GATE_REQUIRED",
                "An active approval challenge is required",
            )
        if binding.challenge_snapshot_sha256 != self._cursor.pinned_snapshot_sha256:
            raise HarnessError(
                "CHALLENGE_STALE",
                "The approval challenge does not bind the pinned operator snapshot",
                exit_code=3,
            )
        return binding
