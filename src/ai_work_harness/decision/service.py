from __future__ import annotations

import json
import mimetypes
import os
import secrets
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ai_work_harness.decision.canonical import (
    canonical_json_bytes,
    digest_json,
    parse_json_bytes,
    read_json_file,
    sha256_bytes,
)
from ai_work_harness.decision.domain import (
    derive_comparison,
    frame_is_confirmable,
    recommendation_relation,
    require_comparison_reviews,
    require_object,
    require_string,
    validate_candidates,
    validate_criteria,
    validate_evaluations,
    validate_evidence,
    validate_final_decision,
    validate_frame,
    validate_recommendation,
    validate_reviews,
)
from ai_work_harness.errors import HarnessError

if TYPE_CHECKING:
    from ai_work_harness.decision.guidance import ValidatedOperatorState

MAX_SOURCE_BYTES = 10 * 1024 * 1024
CONFIRMATION_REFS = {
    "decision-frame": "frame_confirmation",
    "candidate-set": "candidate_confirmation",
    "criteria-set": "criteria_confirmation",
}
SUBJECT_REFS = {
    "decision-frame": "decision_frame",
    "candidate-set": "candidate_set",
    "criteria-set": "criteria_set",
}
DOWNSTREAM = {
    "source_manifest": {
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
        "migration_report",
    },
    "decision_frame": {
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
        "migration_report",
    },
    "candidate_set": {
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
    },
    "criteria_set": {
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
    },
    "evidence_set": {
        "evaluation_set",
        "evaluation_review_set",
        "comparison",
        "recommendation",
        "final_decision",
        "decision_bundle",
        "approval_challenge",
        "human_approval",
    },
    "evaluation_set": {
        "evaluation_review_set",
        "comparison",
        "recommendation",
        "final_decision",
        "decision_bundle",
        "approval_challenge",
        "human_approval",
    },
    "evaluation_review_set": {
        "comparison",
        "recommendation",
        "final_decision",
        "decision_bundle",
        "approval_challenge",
        "human_approval",
    },
    "comparison": {
        "recommendation",
        "final_decision",
        "decision_bundle",
        "approval_challenge",
        "human_approval",
    },
    "recommendation": {
        "final_decision",
        "decision_bundle",
        "approval_challenge",
        "human_approval",
    },
    "final_decision": {"decision_bundle", "approval_challenge", "human_approval"},
}

for _agent_sensitive_ref in (
    "source_manifest",
    "decision_frame",
    "candidate_set",
    "criteria_set",
    "evidence_set",
    "evaluation_set",
    "evaluation_review_set",
    "comparison",
    "recommendation",
):
    DOWNSTREAM[_agent_sensitive_ref].update({"agent_consent", "agent_run"})

CONFIRMATION_DOWNSTREAM = {
    "decision-frame": DOWNSTREAM["decision_frame"] - {"frame_confirmation"},
    "candidate-set": DOWNSTREAM["candidate_set"] - {"candidate_confirmation"},
    "criteria-set": DOWNSTREAM["criteria_set"] - {"criteria_confirmation"},
}


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _snapshot_sha(snapshot: Any) -> str:
    for field in ("snapshot_sha256", "sha256", "digest"):
        value = getattr(snapshot, field, None)
        if isinstance(value, str):
            return value
    raise TypeError("StoredSnapshot does not expose its digest")


def _snapshot_refs(snapshot: Any) -> dict[str, str]:
    record = getattr(snapshot, "snapshot", snapshot)
    refs = getattr(record, "refs", None)
    if not isinstance(refs, Mapping):
        raise TypeError("StoredSnapshot does not expose refs")
    return dict(refs)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


def _artifact_payload(artifact: Any) -> dict[str, Any]:
    value = getattr(artifact, "payload", None)
    if not isinstance(value, Mapping):
        raise HarnessError("CORRUPT_ARTIFACT", "Artifact payload is not an object", exit_code=5)
    return _thaw(value)


def _artifact_producer_kind(artifact: Any) -> str:
    producer = getattr(artifact, "producer", None)
    if isinstance(producer, str):
        return producer
    if isinstance(producer, Mapping) and isinstance(producer.get("kind"), str):
        return producer["kind"]
    raise HarnessError("CORRUPT_ARTIFACT", "Artifact producer is invalid", exit_code=5)


def _artifact_parents(artifact: Any) -> dict[str, str]:
    parents = getattr(artifact, "parents", None)
    if not isinstance(parents, Mapping):
        raise HarnessError("CORRUPT_ARTIFACT", "Artifact parents are invalid", exit_code=5)
    return dict(parents)


def _artifact_dict(artifact: Any) -> dict[str, Any]:
    for method_name in ("as_dict", "to_dict", "to_document"):
        method = getattr(artifact, method_name, None)
        if callable(method):
            value = method()
            if isinstance(value, dict):
                return value
    value = {
        field: getattr(artifact, field)
        for field in (
            "schema_version",
            "artifact_type",
            "session_id",
            "producer",
            "parents",
            "payload",
        )
    }
    return json.loads(json.dumps(value))


def _final_decision_semantics(payload: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return the human-authored meaning, excluding core timestamps and bindings."""

    acknowledgements = payload.get("risk_acknowledgements")
    normalized_acknowledgements = (
        tuple(sorted(acknowledgements)) if isinstance(acknowledgements, list) else ()
    )
    reason = payload.get("reason")
    return (
        payload.get("disposition"),
        payload.get("candidate_id"),
        reason.strip() if isinstance(reason, str) else reason,
        normalized_acknowledgements,
    )


def _serialized_mutation(method: Callable[..., Any]) -> Callable[..., Any]:
    """Hold the session writer lock across validation, object writes, and commit."""

    @wraps(method)
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        expected_parent = kwargs.get("expected_parent")
        if not isinstance(expected_parent, str):
            raise TypeError("serialized mutations require expected_parent as a keyword")
        with self.store.mutation(expected_parent) as replay:
            if replay is not None:
                render = getattr(self.store, "render_idempotent_result", None)
                if not callable(render):
                    raise HarnessError(
                        "IDEMPOTENCY_REPLAY_UNSUPPORTED",
                        "The store cannot reconstruct an idempotent mutation result",
                        exit_code=5,
                    )
                return render(replay)
            snapshot = self._current()
            self._verify_active_bindings(snapshot)
            self._verify_workflow(snapshot)
            return method(self, *args, **kwargs)

    return wrapped


class DecisionService:
    """Application service that owns every v2 state transition."""

    def __init__(
        self,
        project_root: Path,
        session_id: str,
        *,
        store: Any | None = None,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        if store is None:
            from ai_work_harness.decision.store import DecisionStore

            store = DecisionStore(project_root, session_id)
        self.store = store
        self.project_root = project_root.resolve()
        self.session_id = session_id
        self.clock = clock

    def initialize(self) -> dict[str, Any]:
        snapshot = self.store.initialize()
        return self._result(snapshot, lifecycle_state="initialized")

    def _result(self, snapshot: Any, **result: Any) -> dict[str, Any]:
        record = getattr(snapshot, "snapshot", snapshot)
        return {
            "ok": True,
            "session_id": self.session_id,
            "generation": record.generation,
            "snapshot_sha256": _snapshot_sha(snapshot),
            **result,
        }

    def _current(self) -> Any:
        return self.store.get_current()

    def _require_ref(self, refs: Mapping[str, str], key: str) -> str:
        try:
            return refs[key]
        except KeyError as exc:
            raise HarnessError(
                "WORKFLOW_GATE_REQUIRED",
                f"Required current artifact is missing: {key}",
                details={"artifact": key},
                exit_code=3,
            ) from exc

    def _read(self, refs: Mapping[str, str], key: str) -> Any:
        return self.store.read_artifact(self._require_ref(refs, key))

    def _create_artifact(
        self,
        *,
        artifact_type: str,
        producer_kind: str,
        parents: Mapping[str, str],
        payload: Mapping[str, Any],
        producer_extra: Mapping[str, Any] | None = None,
    ) -> tuple[Any, str]:
        from ai_work_harness.decision.models import ArtifactEnvelope

        # Provider metadata is recorded in a separate agent-run artifact.  The
        # envelope producer remains a deliberately small, stable policy field.
        producer: dict[str, Any] = {"kind": producer_kind}
        artifact = ArtifactEnvelope.create(
            artifact_type=artifact_type,
            session_id=self.session_id,
            producer=producer,
            parents=dict(parents),
            payload=dict(payload),
        )
        return artifact, self.store.put_artifact(artifact)

    def _commit_ref(
        self,
        *,
        expected_parent: str,
        ref_name: str,
        digest: str,
        invalidate_from: str | None,
        operation: str,
    ) -> Any:
        current = self._current()
        refs = _snapshot_refs(current)
        if refs.get(ref_name) == digest:
            return current
        if invalidate_from is not None:
            for key in DOWNSTREAM[invalidate_from]:
                refs.pop(key, None)
        refs[ref_name] = digest
        return self.store.commit(
            expected_parent=expected_parent,
            refs=refs,
            operation=operation,
        )

    @_serialized_mutation
    def capture_source(
        self,
        *,
        source_id: str,
        source: Path,
        expected_parent: str,
        media_type: str | None = None,
    ) -> dict[str, Any]:
        from ai_work_harness.decision.models import validate_safe_id

        validate_safe_id(source_id, label="source_id")
        path = source.resolve()
        if source.is_symlink() or not path.is_file():
            raise HarnessError("INVALID_SOURCE", "Source must be a regular non-symlink file")
        state_root = self.project_root / ".ai-work-harness" / "v2"
        try:
            path.relative_to(state_root.resolve())
        except ValueError:
            pass
        else:
            raise HarnessError("SOURCE_IN_MANAGED_STORAGE", "Source must be outside v2 storage")
        source_bytes = path.stat().st_size
        if source_bytes > MAX_SOURCE_BYTES:
            raise HarnessError(
                "SOURCE_TOO_LARGE",
                "Phase 1 sources are limited to 10 MiB",
                details={"bytes": source_bytes, "limit": MAX_SOURCE_BYTES},
            )
        content = path.read_bytes()
        if len(content) > MAX_SOURCE_BYTES:
            raise HarnessError(
                "SOURCE_TOO_LARGE",
                "Phase 1 sources are limited to 10 MiB",
                details={"bytes": len(content), "limit": MAX_SOURCE_BYTES},
            )
        try:
            content.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise HarnessError("UNSUPPORTED_SOURCE_FORMAT", "Source must be UTF-8 text") from exc
        current = self._current()
        refs = _snapshot_refs(current)
        sources: list[dict[str, Any]] = []
        if "source_manifest" in refs:
            sources = list(_artifact_payload(self._read(refs, "source_manifest"))["sources"])
        by_id = {item["source_id"]: item for item in sources}
        detected = media_type or mimetypes.guess_type(path.name)[0] or "text/plain"
        if detected not in {"text/plain", "text/markdown"}:
            raise HarnessError(
                "UNSUPPORTED_SOURCE_FORMAT",
                "Only UTF-8 text and Markdown are supported",
            )
        blob_sha256 = self.store.put_blob(content)
        by_id[source_id] = {
            "source_id": source_id,
            "media_type": detected,
            "bytes": len(content),
            "blob_sha256": blob_sha256,
        }
        payload = {"sources": [by_id[key] for key in sorted(by_id)]}
        _, digest = self._create_artifact(
            artifact_type="source-manifest",
            producer_kind="local_operator",
            parents={},
            payload=payload,
        )
        snapshot = self._commit_ref(
            expected_parent=expected_parent,
            ref_name="source_manifest",
            digest=digest,
            invalidate_from="source_manifest",
            operation="source.capture",
        )
        return self._result(
            snapshot,
            artifact="source_manifest",
            artifact_sha256=digest,
            source=by_id[source_id],
        )

    @_serialized_mutation
    def import_frame(
        self,
        payload: Any,
        *,
        expected_parent: str,
        producer_kind: str = "local_operator",
    ) -> dict[str, Any]:
        document = validate_frame(payload)
        current = self._current()
        refs = _snapshot_refs(current)
        source_sha = self._require_ref(refs, "source_manifest")
        manifest = _artifact_payload(self._read(refs, "source_manifest"))
        if not manifest["sources"]:
            raise HarnessError("SOURCE_REQUIRED", "Capture at least one source before framing")
        _, digest = self._create_artifact(
            artifact_type="decision-frame",
            producer_kind=producer_kind,
            parents={"source_manifest": source_sha},
            payload=document,
        )
        snapshot = self._commit_ref(
            expected_parent=expected_parent,
            ref_name="decision_frame",
            digest=digest,
            invalidate_from="decision_frame",
            operation="frame.import",
        )
        return self._result(snapshot, artifact="decision_frame", artifact_sha256=digest)

    @_serialized_mutation
    def confirm(
        self,
        subject_type: str,
        *,
        expected_artifact_sha: str,
        expected_parent: str,
        method: str = "digest_challenge",
    ) -> dict[str, Any]:
        if subject_type not in SUBJECT_REFS:
            raise HarnessError("INVALID_CONFIRMATION_SUBJECT", "Unknown confirmation subject")
        if method not in {"digest_challenge", "guided_semantic_review"}:
            raise HarnessError("INVALID_CONFIRMATION_METHOD", "Unknown confirmation method")
        current = self._current()
        refs = _snapshot_refs(current)
        ref_name = SUBJECT_REFS[subject_type]
        subject_sha = self._require_ref(refs, ref_name)
        if expected_artifact_sha != subject_sha:
            raise HarnessError(
                "CONFIRMATION_DIGEST_MISMATCH",
                "The confirmation digest does not match the current artifact",
                exit_code=3,
            )
        if subject_type == "decision-frame" and not frame_is_confirmable(
            _artifact_payload(self.store.read_artifact(subject_sha))
        ):
            raise HarnessError("FRAME_INCOMPLETE", "Complete the frame before confirmation")
        payload = {
            "confirmation_id": self.store.new_event_id(),
            "subject_type": subject_type,
            "subject_sha256": subject_sha,
            "actor_label": "local_operator",
            "identity_verified": False,
            "method": method,
            "confirmed_at": _timestamp(self.clock()),
        }
        _, digest = self._create_artifact(
            artifact_type="human-confirmation",
            producer_kind="core",
            parents={ref_name: subject_sha},
            payload=payload,
        )
        confirmation_ref = CONFIRMATION_REFS[subject_type]
        for key in CONFIRMATION_DOWNSTREAM[subject_type]:
            refs.pop(key, None)
        refs[confirmation_ref] = digest
        snapshot = self.store.commit(
            expected_parent=expected_parent,
            refs=refs,
            operation=f"{ref_name}.confirm",
        )
        return self._result(
            snapshot,
            artifact=confirmation_ref,
            artifact_sha256=digest,
            identity_verified=False,
        )

    @_serialized_mutation
    def import_candidates(
        self,
        payload: Any,
        *,
        expected_parent: str,
        producer_kind: str = "local_operator",
    ) -> dict[str, Any]:
        document = validate_candidates(payload)
        current = self._current()
        refs = _snapshot_refs(current)
        frame_sha = self._require_ref(refs, "decision_frame")
        confirmation_sha = self._require_ref(refs, "frame_confirmation")
        _, digest = self._create_artifact(
            artifact_type="candidate-set",
            producer_kind=producer_kind,
            parents={"decision_frame": frame_sha, "frame_confirmation": confirmation_sha},
            payload=document,
        )
        snapshot = self._commit_ref(
            expected_parent=expected_parent,
            ref_name="candidate_set",
            digest=digest,
            invalidate_from="candidate_set",
            operation="candidates.import",
        )
        return self._result(snapshot, artifact="candidate_set", artifact_sha256=digest)

    @_serialized_mutation
    def import_criteria(
        self,
        payload: Any,
        *,
        expected_parent: str,
        producer_kind: str = "local_operator",
    ) -> dict[str, Any]:
        document = validate_criteria(payload)
        current = self._current()
        refs = _snapshot_refs(current)
        candidates_sha = self._require_ref(refs, "candidate_set")
        confirmation_sha = self._require_ref(refs, "candidate_confirmation")
        _, digest = self._create_artifact(
            artifact_type="criteria-set",
            producer_kind=producer_kind,
            parents={
                "candidate_set": candidates_sha,
                "candidate_confirmation": confirmation_sha,
            },
            payload=document,
        )
        snapshot = self._commit_ref(
            expected_parent=expected_parent,
            ref_name="criteria_set",
            digest=digest,
            invalidate_from="criteria_set",
            operation="criteria.import",
        )
        return self._result(snapshot, artifact="criteria_set", artifact_sha256=digest)

    def _source_excerpt_hashes(
        self,
        manifest: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> dict[tuple[str, int, int], str]:
        sources = {item["source_id"]: item for item in manifest["sources"]}
        requested: set[tuple[str, int, int]] = set()
        items = payload.get("evidence")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict) or item.get("provenance") != "source_observation":
                    continue
                source = item.get("source")
                if not isinstance(source, dict):
                    continue
                source_id = source.get("source_id")
                start = source.get("start_line")
                end = source.get("end_line")
                if (
                    isinstance(source_id, str)
                    and isinstance(start, int)
                    and not isinstance(start, bool)
                    and isinstance(end, int)
                    and not isinstance(end, bool)
                ):
                    requested.add((source_id, start, end))
        result: dict[tuple[str, int, int], str] = {}
        for source_id, start, end in requested:
            if source_id not in sources or not isinstance(start, int) or not isinstance(end, int):
                continue
            raw = self.store.read_object(sources[source_id]["blob_sha256"])
            try:
                lines = raw.decode("utf-8", errors="strict").splitlines(keepends=True)
            except UnicodeDecodeError as exc:  # pragma: no cover - capture already validates
                raise HarnessError("CORRUPT_SOURCE", "Captured source is no longer UTF-8") from exc
            if start < 1 or end < start or end > len(lines):
                continue
            excerpt = "".join(lines[start - 1 : end]).encode("utf-8")
            result[(source_id, start, end)] = sha256_bytes(excerpt)
        return result

    @_serialized_mutation
    def import_evidence(self, payload: Any, *, expected_parent: str) -> dict[str, Any]:
        raw_document = require_object(payload, "evidence set")
        current = self._current()
        refs = _snapshot_refs(current)
        source_sha = self._require_ref(refs, "source_manifest")
        criteria_sha = self._require_ref(refs, "criteria_set")
        criteria_confirmation = self._require_ref(refs, "criteria_confirmation")
        manifest = _artifact_payload(self._read(refs, "source_manifest"))
        source_ids = {item["source_id"] for item in manifest["sources"]}
        excerpt_hashes = self._source_excerpt_hashes(manifest, raw_document)
        document = validate_evidence(
            raw_document,
            source_ids=source_ids,
            excerpt_hashes=excerpt_hashes,
        )
        _, digest = self._create_artifact(
            artifact_type="evidence-set",
            producer_kind="local_operator",
            parents={
                "source_manifest": source_sha,
                "criteria_set": criteria_sha,
                "criteria_confirmation": criteria_confirmation,
            },
            payload=document,
        )
        snapshot = self._commit_ref(
            expected_parent=expected_parent,
            ref_name="evidence_set",
            digest=digest,
            invalidate_from="evidence_set",
            operation="evidence.import",
        )
        return self._result(snapshot, artifact="evidence_set", artifact_sha256=digest)

    def _decision_context(self, snapshot: Any | None = None) -> Any:
        from ai_work_harness.decision.providers import FrozenDecisionContext

        snapshot = snapshot or self._current()
        refs = _snapshot_refs(snapshot)
        candidates = _artifact_payload(self._read(refs, "candidate_set"))["candidates"]
        criteria = _artifact_payload(self._read(refs, "criteria_set"))["criteria"]
        evidence = _artifact_payload(self._read(refs, "evidence_set"))["evidence"]
        evaluations: list[dict[str, Any]] = []
        comparison: dict[str, Any] = {}
        if "evaluation_set" in refs:
            evaluations = _artifact_payload(self._read(refs, "evaluation_set"))["cells"]
        if "comparison" in refs:
            comparison = _artifact_payload(self._read(refs, "comparison"))
        return FrozenDecisionContext(
            session_id=self.session_id,
            snapshot_sha256=_snapshot_sha(snapshot),
            candidates=tuple(candidates),
            criteria=tuple(criteria),
            evidence=tuple(evidence),
            evaluations=tuple(evaluations),
            comparison=comparison,
        )

    def _agent_context(self, snapshot: Any) -> Any:
        from ai_work_harness.decision.providers import FrozenDecisionContext

        base = self._decision_context(snapshot)
        document = base.as_dict()
        refs = _snapshot_refs(snapshot)
        manifest = _artifact_payload(self._read(refs, "source_manifest"))
        sources = {item["source_id"]: item for item in manifest["sources"]}
        enriched_evidence: list[dict[str, Any]] = []
        for item in document["evidence"]:
            enriched = dict(item)
            locator = item.get("source")
            if item.get("provenance") == "source_observation" and isinstance(locator, dict):
                source = sources[locator["source_id"]]
                raw = self.store.read_object(source["blob_sha256"])
                lines = raw.decode("utf-8", errors="strict").splitlines(keepends=True)
                excerpt_bytes = "".join(
                    lines[locator["start_line"] - 1 : locator["end_line"]]
                ).encode("utf-8")
                if sha256_bytes(excerpt_bytes) != locator["excerpt_sha256"]:
                    raise HarnessError(
                        "EVIDENCE_LOCATOR_STALE",
                        "The cited source excerpt no longer matches its evidence locator",
                        exit_code=5,
                    )
                enriched["cited_excerpt"] = excerpt_bytes.decode("utf-8")
            enriched_evidence.append(enriched)
        return FrozenDecisionContext.from_mapping({**document, "evidence": enriched_evidence})

    def _agent_context_for_operation(self, snapshot: Any, operation: str) -> Any:
        from ai_work_harness.decision.providers import FrozenDecisionContext

        context = self._agent_context(snapshot)
        self._agent_operation_contract(operation)
        if operation == "recommendation":
            return context
        document = context.as_dict()
        document["evaluations"] = []
        document["comparison"] = {}
        return FrozenDecisionContext.from_mapping(document)

    @staticmethod
    def _agent_prompt(operation: str) -> Any:
        from ai_work_harness.decision.prompts import (
            EVALUATION_PROMPT,
            RECOMMENDATION_PROMPT,
        )

        if operation == "evaluations":
            return EVALUATION_PROMPT
        if operation == "recommendation":
            return RECOMMENDATION_PROMPT
        raise HarnessError(
            "INVALID_AGENT_OPERATION",
            "Agent operation must be evaluations or recommendation",
        )

    @staticmethod
    def _agent_operation_contract(operation: str) -> tuple[str, str, str]:
        if operation == "evaluations":
            return ("evaluation_set", "evaluation-set", "submit_evaluations")
        if operation == "recommendation":
            return ("recommendation", "recommendation", "submit_recommendation")
        raise HarnessError(
            "INVALID_AGENT_OPERATION",
            "Agent operation must be evaluations or recommendation",
        )

    def _agent_manifest(
        self,
        *,
        operation: str,
        provider: str,
        model: str | None,
        snapshot: Any,
    ) -> dict[str, Any]:
        from ai_work_harness.decision.openai_provider import (
            DEFAULT_MAX_CONTEXT_BYTES,
            DEFAULT_MAX_LOOKUP_BYTES,
            DEFAULT_MAX_OUTPUT_TOKENS,
            DEFAULT_MAX_RETRIES,
            DEFAULT_MAX_TOOL_CALLS,
            DEFAULT_MODEL,
            DEFAULT_REASONING_EFFORT,
            DEFAULT_TIMEOUT_SECONDS,
        )

        if provider != "openai":
            raise HarnessError(
                "UNSUPPORTED_PROVIDER",
                f"Unsupported outbound provider: {provider}",
                details={"provider": provider},
                exit_code=4,
            )
        prompt = self._agent_prompt(operation)
        context = self._agent_context_for_operation(snapshot, operation)
        if operation == "recommendation" and not context.comparison:
            raise HarnessError(
                "WORKFLOW_GATE_REQUIRED",
                "A deterministic comparison is required before recommendation preview",
                exit_code=3,
            )
        if len(context.evidence) > 50:
            raise HarnessError(
                "OUTBOUND_LIMIT_EXCEEDED",
                "Agent runs are limited to 50 evidence records",
                details={"evidence_count": len(context.evidence), "limit": 50},
                exit_code=4,
            )
        refs = _snapshot_refs(snapshot)
        manifest = _artifact_payload(self._read(refs, "source_manifest"))
        source_sha256s = sorted({item["blob_sha256"] for item in manifest["sources"]})
        input_document = context.as_dict()
        context_bytes = len(canonical_json_bytes(input_document))
        if context_bytes > DEFAULT_MAX_CONTEXT_BYTES:
            raise HarnessError(
                "OUTBOUND_LIMIT_EXCEEDED",
                "The frozen decision context exceeds the outbound byte limit",
                details={"context_bytes": context_bytes, "limit": DEFAULT_MAX_CONTEXT_BYTES},
                exit_code=4,
            )
        source_excerpt_bytes = sum(
            len(item.get("cited_excerpt", "").encode("utf-8"))
            for item in input_document["evidence"]
        )
        if source_excerpt_bytes > 100_000:
            raise HarnessError(
                "OUTBOUND_LIMIT_EXCEEDED",
                "Cited source excerpts exceed the 100,000-byte outbound limit",
                details={"source_excerpt_bytes": source_excerpt_bytes, "limit": 100_000},
                exit_code=4,
            )
        return {
            "provider": provider,
            "model": model or os.environ.get("AI_WORK_HARNESS_OPENAI_MODEL") or DEFAULT_MODEL,
            "operation": operation,
            "prompt_id": prompt.prompt_id,
            "prompt_sha256": prompt.sha256,
            "input_sha256": digest_json(input_document),
            "input_snapshot_sha256": _snapshot_sha(snapshot),
            "source_sha256s": source_sha256s,
            "evidence_count": len(context.evidence),
            "source_excerpt_bytes": source_excerpt_bytes,
            "context_bytes": context_bytes,
            "reasoning_effort": DEFAULT_REASONING_EFFORT,
            "max_tool_rounds": DEFAULT_MAX_TOOL_CALLS,
            "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "timeout_seconds": int(DEFAULT_TIMEOUT_SECONDS),
            "max_retries": DEFAULT_MAX_RETRIES,
            "max_context_bytes": DEFAULT_MAX_CONTEXT_BYTES,
            "max_lookup_bytes": DEFAULT_MAX_LOOKUP_BYTES,
        }

    def preview_agent(
        self,
        *,
        operation: str,
        provider: str = "openai",
        model: str | None = None,
    ) -> dict[str, Any]:
        from ai_work_harness.decision.openai_provider import validate_openai_endpoint_environment

        validate_openai_endpoint_environment()
        snapshot = self._current()
        manifest = self._agent_manifest(
            operation=operation,
            provider=provider,
            model=model,
            snapshot=snapshot,
        )
        return self._result(
            snapshot,
            outbound_manifest=manifest,
            outbound_manifest_sha256=digest_json(manifest),
        )

    @_serialized_mutation
    def consent_agent(
        self,
        *,
        operation: str,
        expected_manifest_sha: str,
        expected_parent: str,
        provider: str = "openai",
        model: str | None = None,
        method: str = "digest_challenge",
    ) -> dict[str, Any]:
        from ai_work_harness.decision.openai_provider import validate_openai_endpoint_environment

        validate_openai_endpoint_environment()
        if method not in {"digest_challenge", "guided_exact_phrase"}:
            raise HarnessError(
                "INVALID_CONFIRMATION_METHOD",
                "Unknown outbound consent method",
            )
        current = self._current()
        if _snapshot_sha(current) != expected_parent:
            raise HarnessError(
                "WRITE_CONFLICT",
                "Current snapshot changed after outbound preview",
                details={
                    "expected_parent": expected_parent,
                    "actual_parent": _snapshot_sha(current),
                },
                exit_code=3,
            )
        manifest = self._agent_manifest(
            operation=operation,
            provider=provider,
            model=model,
            snapshot=current,
        )
        manifest_sha = digest_json(manifest)
        if manifest_sha != expected_manifest_sha:
            raise HarnessError(
                "OUTBOUND_MANIFEST_MISMATCH",
                "The confirmed outbound manifest digest is not current",
                details={
                    "expected_manifest_sha": expected_manifest_sha,
                    "actual_manifest_sha": manifest_sha,
                },
                exit_code=3,
            )
        self.verify(expected_parent)
        refs = _snapshot_refs(current)
        parent_keys = ["source_manifest", "candidate_set", "criteria_set", "evidence_set"]
        if operation == "recommendation":
            parent_keys.append("comparison")
        parents = {key: self._require_ref(refs, key) for key in parent_keys}
        payload = {
            "confirmation_id": self.store.new_event_id(),
            "subject_type": "outbound-manifest",
            "subject_sha256": manifest_sha,
            "actor_label": "local_operator",
            "identity_verified": False,
            "method": method,
            "confirmed_at": _timestamp(self.clock()),
            "outbound_manifest": manifest,
        }
        _, consent_sha = self._create_artifact(
            artifact_type="human-confirmation",
            producer_kind="core",
            parents=parents,
            payload=payload,
        )
        updated_refs = dict(refs)
        updated_refs.pop("agent_run", None)
        updated_refs.pop("approval_challenge", None)
        updated_refs.pop("decision_bundle", None)
        updated_refs.pop("human_approval", None)
        updated_refs["agent_consent"] = consent_sha
        snapshot = self.store.commit(
            expected_parent=expected_parent,
            refs=updated_refs,
            operation="agent.consent",
        )
        return self._result(
            snapshot,
            artifact="agent_consent",
            artifact_sha256=consent_sha,
            outbound_manifest_sha256=manifest_sha,
            identity_verified=False,
        )

    @_serialized_mutation
    def import_evaluations(
        self,
        payload: Any,
        *,
        expected_parent: str,
        producer_kind: str = "local_operator",
        producer_extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if producer_kind not in {"local_operator", "agent_import", "fixture", "openai"}:
            raise HarnessError("INVALID_PRODUCER", "Unsupported evaluation producer")
        current = self._current()
        refs = _snapshot_refs(current)
        candidate_sha = self._require_ref(refs, "candidate_set")
        criteria_sha = self._require_ref(refs, "criteria_set")
        evidence_sha = self._require_ref(refs, "evidence_set")
        candidates = _artifact_payload(self._read(refs, "candidate_set"))["candidates"]
        criteria = _artifact_payload(self._read(refs, "criteria_set"))["criteria"]
        evidence = _artifact_payload(self._read(refs, "evidence_set"))["evidence"]
        document = validate_evaluations(
            payload,
            candidate_ids={item["candidate_id"] for item in candidates},
            criterion_ids={item["criterion_id"] for item in criteria},
            evidence_by_id={item["evidence_id"]: item for item in evidence},
        )
        _, digest = self._create_artifact(
            artifact_type="evaluation-set",
            producer_kind=producer_kind,
            producer_extra=producer_extra,
            parents={
                "candidate_set": candidate_sha,
                "criteria_set": criteria_sha,
                "evidence_set": evidence_sha,
            },
            payload=document,
        )
        snapshot = self._commit_ref(
            expected_parent=expected_parent,
            ref_name="evaluation_set",
            digest=digest,
            invalidate_from="evaluation_set",
            operation="evaluations.import",
        )
        return self._result(snapshot, artifact="evaluation_set", artifact_sha256=digest)

    def generate_evaluations(
        self,
        provider: Any,
        *,
        expected_parent: str,
        producer_kind: str,
        producer_extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self._current()
        if _snapshot_sha(current) != expected_parent:
            raise HarnessError(
                "WRITE_CONFLICT",
                "Current snapshot changed before provider generation",
                details={
                    "expected_parent": expected_parent,
                    "actual_parent": _snapshot_sha(current),
                },
                exit_code=3,
            )
        self.verify(expected_parent)
        context = self._agent_context_for_operation(current, "evaluations")
        draft = provider.generate(context)
        draft.validate_against(context)
        return self.import_evaluations(
            draft.as_payload(),
            expected_parent=expected_parent,
            producer_kind=producer_kind,
            producer_extra=producer_extra,
        )

    @_serialized_mutation
    def import_reviews(self, payload: Any, *, expected_parent: str) -> dict[str, Any]:
        current = self._current()
        refs = _snapshot_refs(current)
        evaluation_sha = self._require_ref(refs, "evaluation_set")
        if "evaluation_review_set" in refs:
            active_reviews = _artifact_payload(self._read(refs, "evaluation_review_set"))["reviews"]
            if any(review["outcome"] == "request_revision" for review in active_reviews):
                raise HarnessError(
                    "EVALUATION_REVISION_REQUIRED",
                    "Import a new evaluation before recording another review",
                    details={"evaluation_sha256": evaluation_sha},
                    exit_code=3,
                )
        evaluation_artifact = self._read(refs, "evaluation_set")
        evaluation_payload = _artifact_payload(evaluation_artifact)
        criteria = _artifact_payload(self._read(refs, "criteria_set"))["criteria"]
        evidence = _artifact_payload(self._read(refs, "evidence_set"))["evidence"]
        cells = {
            (item["candidate_id"], item["criterion_id"]): item
            for item in evaluation_payload["cells"]
        }
        document = validate_reviews(
            payload,
            evaluation_cells=cells,
            criteria={item["criterion_id"]: item for item in criteria},
            producer_kind=_artifact_producer_kind(evaluation_artifact),
            evidence_by_id={item["evidence_id"]: item for item in evidence},
        )
        _, digest = self._create_artifact(
            artifact_type="evaluation-review-set",
            producer_kind="local_operator",
            parents={"evaluation_set": evaluation_sha},
            payload=document,
        )
        snapshot = self._commit_ref(
            expected_parent=expected_parent,
            ref_name="evaluation_review_set",
            digest=digest,
            invalidate_from="evaluation_review_set",
            operation="evaluations.review",
        )
        return self._result(snapshot, artifact="evaluation_review_set", artifact_sha256=digest)

    @_serialized_mutation
    def compare(self, *, expected_parent: str) -> dict[str, Any]:
        current = self._current()
        refs = _snapshot_refs(current)
        candidates = _artifact_payload(self._read(refs, "candidate_set"))["candidates"]
        criteria = _artifact_payload(self._read(refs, "criteria_set"))["criteria"]
        evaluation_artifact = self._read(refs, "evaluation_set")
        evaluations = _artifact_payload(evaluation_artifact)["cells"]
        producer_kind = _artifact_producer_kind(evaluation_artifact)
        reviews: list[dict[str, Any]] = []
        review_sha: str | None = None
        if "evaluation_review_set" in refs:
            review_sha = refs["evaluation_review_set"]
            reviews = _artifact_payload(self._read(refs, "evaluation_review_set"))["reviews"]
        require_comparison_reviews(
            cells=evaluations, criteria=criteria, producer_kind=producer_kind, reviews=reviews
        )
        result = derive_comparison(
            candidates=candidates,
            criteria=criteria,
            cells=evaluations,
            reviews=reviews,
            producer_kind=producer_kind,
        )
        parents = {"evaluation_set": refs["evaluation_set"]}
        if review_sha is not None:
            parents["evaluation_review_set"] = review_sha
        _, digest = self._create_artifact(
            artifact_type="comparison",
            producer_kind="core",
            parents=parents,
            payload=result.payload,
        )
        snapshot = self._commit_ref(
            expected_parent=expected_parent,
            ref_name="comparison",
            digest=digest,
            invalidate_from="comparison",
            operation="comparison.derive",
        )
        return self._result(
            snapshot,
            artifact="comparison",
            artifact_sha256=digest,
            eligible_candidate_ids=sorted(result.eligible),
        )

    @_serialized_mutation
    def record_recommendation(
        self,
        payload: Any,
        *,
        expected_parent: str,
        producer_kind: str,
        producer_extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self._current()
        refs = _snapshot_refs(current)
        comparison_sha = self._require_ref(refs, "comparison")
        document = validate_recommendation(
            payload,
            comparison=_artifact_payload(self._read(refs, "comparison")),
            evidence_ids={
                item["evidence_id"]
                for item in _artifact_payload(self._read(refs, "evidence_set"))["evidence"]
            },
        )
        _, digest = self._create_artifact(
            artifact_type="recommendation",
            producer_kind=producer_kind,
            producer_extra=producer_extra,
            parents={"comparison": comparison_sha},
            payload=document,
        )
        snapshot = self._commit_ref(
            expected_parent=expected_parent,
            ref_name="recommendation",
            digest=digest,
            invalidate_from="recommendation",
            operation="recommendation.record",
        )
        return self._result(snapshot, artifact="recommendation", artifact_sha256=digest)

    def generate_recommendation(
        self,
        provider: Any,
        *,
        expected_parent: str,
        producer_kind: str,
        producer_extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self._current()
        if _snapshot_sha(current) != expected_parent:
            raise HarnessError(
                "WRITE_CONFLICT",
                "Current snapshot changed before provider generation",
                details={
                    "expected_parent": expected_parent,
                    "actual_parent": _snapshot_sha(current),
                },
                exit_code=3,
            )
        self.verify(expected_parent)
        context = self._agent_context_for_operation(current, "recommendation")
        draft = provider.generate(context)
        draft.validate_against(context)
        return self.record_recommendation(
            draft.as_payload(),
            expected_parent=expected_parent,
            producer_kind=producer_kind,
            producer_extra=producer_extra,
        )

    @staticmethod
    def _normalized_usage(value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise HarnessError(
                "PROVIDER_USAGE_INVALID",
                "A successful provider trace must include token usage",
                exit_code=4,
            )
        counts: dict[str, int] = {}
        for name in ("input_tokens", "output_tokens"):
            item = value.get(name)
            if not isinstance(item, int) or isinstance(item, bool) or item < 0:
                raise HarnessError(
                    "PROVIDER_USAGE_INVALID",
                    "Provider token usage is missing or invalid",
                    details={"field": name},
                    exit_code=4,
                )
            counts[name] = item
        input_tokens = counts["input_tokens"]
        output_tokens = counts["output_tokens"]
        total_tokens = input_tokens + output_tokens
        reported_total = value.get("total_tokens", total_tokens)
        if (
            not isinstance(reported_total, int)
            or isinstance(reported_total, bool)
            or reported_total != total_tokens
        ):
            raise HarnessError(
                "PROVIDER_USAGE_INVALID",
                "Provider total token usage does not match input plus output tokens",
                exit_code=4,
            )
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

    @staticmethod
    def _provider_configuration(provider: Any) -> dict[str, Any]:
        return {
            "model": getattr(provider, "model", None),
            "reasoning_effort": getattr(provider, "reasoning_effort", None),
            "max_tool_rounds": getattr(provider, "max_tool_calls", None),
            "max_output_tokens": getattr(provider, "max_output_tokens", None),
            "timeout_seconds": getattr(provider, "timeout_seconds", None),
            "max_retries": getattr(provider, "max_retries", None),
            "max_context_bytes": getattr(provider, "max_context_bytes", None),
            "max_lookup_bytes": getattr(provider, "max_lookup_bytes", None),
        }

    @_serialized_mutation
    def _commit_openai_agent_result(
        self,
        *,
        operation: str,
        expected_parent: str,
        consent_sha: str,
        manifest_sha: str,
        run: Any,
        result_ref: str,
        result_type: str,
        result_parents: Mapping[str, str],
        result_payload: Mapping[str, Any],
        transcript_events: list[dict[str, Any]],
        started_at: str,
    ) -> dict[str, Any]:
        from ai_work_harness.decision.models import ArtifactEnvelope

        current = self._current()
        refs = _snapshot_refs(current)
        if refs.get("agent_consent") != consent_sha:
            raise HarnessError(
                "OUTBOUND_CONSENT_STALE",
                "The active outbound consent changed before the provider result was committed",
                exit_code=3,
            )
        consent_payload = self.store.read_artifact(consent_sha).payload
        consent_manifest = consent_payload["outbound_manifest"]
        usage = self._normalized_usage(run.usage)
        result_artifact = ArtifactEnvelope.create(
            artifact_type=result_type,
            session_id=self.session_id,
            producer={"kind": "openai"},
            parents=dict(result_parents),
            payload=dict(result_payload),
        )
        result_sha = digest_json(result_artifact.to_document())
        transcript_document = {"events": transcript_events}
        transcript_bytes = canonical_json_bytes(transcript_document)
        transcript_sha = sha256_bytes(transcript_bytes)
        run_payload = {
            "run_id": self.store.new_event_id(),
            "provider": run.provider,
            "model": run.model,
            "operation": operation,
            "prompt_id": run.prompt_id,
            "prompt_sha256": run.prompt_sha256,
            "input_sha256": consent_manifest["input_sha256"],
            "input_snapshot_sha256": consent_manifest["input_snapshot_sha256"],
            "outbound_manifest_sha256": manifest_sha,
            "transcript_sha256": transcript_sha,
            "tool_name": run.tool_name,
            "response_id": run.response_id,
            "usage": usage,
            "result_sha256": result_sha,
            "status": "succeeded",
            "error_code": None,
            "started_at": started_at,
            "completed_at": _timestamp(self.clock()),
        }
        run_artifact = ArtifactEnvelope.create(
            artifact_type="agent-run",
            session_id=self.session_id,
            producer={"kind": "core"},
            parents={"agent_consent": consent_sha, result_ref: result_sha},
            payload=run_payload,
        )
        run_sha = digest_json(run_artifact.to_document())

        if self.store.put_artifact(result_artifact) != result_sha:
            raise AssertionError("result artifact digest changed during persistence")
        if self.store.put_blob(transcript_bytes) != transcript_sha:
            raise AssertionError("transcript digest changed during persistence")
        if self.store.put_artifact(run_artifact) != run_sha:
            raise AssertionError("agent-run artifact digest changed during persistence")
        updated_refs = dict(refs)
        for key in DOWNSTREAM[result_ref]:
            updated_refs.pop(key, None)
        updated_refs.pop("agent_consent", None)
        updated_refs[result_ref] = result_sha
        updated_refs["agent_run"] = run_sha
        snapshot = self.store.commit(
            expected_parent=expected_parent,
            refs=updated_refs,
            operation=f"agent.run.{operation}",
        )
        return self._result(
            snapshot,
            artifact=result_ref,
            artifact_sha256=result_sha,
            agent_run_sha256=run_sha,
            outbound_manifest_sha256=manifest_sha,
        )

    def run_openai_agent(
        self,
        *,
        operation: str,
        expected_parent: str,
        provider: Any | None = None,
    ) -> dict[str, Any]:
        from ai_work_harness.decision.openai_provider import (
            OpenAIProvider,
            validate_openai_endpoint_environment,
        )

        validate_openai_endpoint_environment()
        current = self._current()
        if _snapshot_sha(current) != expected_parent:
            raise HarnessError(
                "WRITE_CONFLICT",
                "Current snapshot changed before the provider run",
                details={
                    "expected_parent": expected_parent,
                    "actual_parent": _snapshot_sha(current),
                },
                exit_code=3,
            )
        self.verify(expected_parent)
        refs = _snapshot_refs(current)
        if "agent_consent" not in refs:
            raise HarnessError(
                "OUTBOUND_CONSENT_REQUIRED",
                "Run agent preview and bind explicit consent before OpenAI generation",
                exit_code=4,
            )
        consent_sha = refs["agent_consent"]
        consent = _artifact_payload(self._read(refs, "agent_consent"))
        manifest = consent.get("outbound_manifest")
        if not isinstance(manifest, dict) or manifest.get("operation") != operation:
            raise HarnessError(
                "OUTBOUND_CONSENT_REQUIRED",
                "The current outbound consent does not cover this agent operation",
                exit_code=4,
            )
        if digest_json(manifest) != consent["subject_sha256"]:
            raise HarnessError(
                "OUTBOUND_CONSENT_STALE",
                "The outbound consent manifest digest is invalid",
                exit_code=5,
            )
        input_snapshot = self.store.load_snapshot(manifest["input_snapshot_sha256"])
        expected_manifest = self._agent_manifest(
            operation=operation,
            provider=manifest["provider"],
            model=manifest["model"],
            snapshot=input_snapshot,
        )
        if manifest != expected_manifest:
            raise HarnessError(
                "OUTBOUND_CONSENT_STALE",
                "The outbound inputs or provider configuration changed after consent",
                exit_code=3,
            )
        expected_provider_configuration = {
            "model": manifest["model"],
            "reasoning_effort": manifest["reasoning_effort"],
            "max_tool_rounds": manifest["max_tool_rounds"],
            "max_output_tokens": manifest["max_output_tokens"],
            "timeout_seconds": manifest["timeout_seconds"],
            "max_retries": manifest["max_retries"],
            "max_context_bytes": manifest["max_context_bytes"],
            "max_lookup_bytes": manifest["max_lookup_bytes"],
        }
        if provider is None:
            provider = OpenAIProvider(
                model=manifest["model"],
                reasoning_effort=manifest["reasoning_effort"],
                max_tool_calls=manifest["max_tool_rounds"],
                max_output_tokens=manifest["max_output_tokens"],
                timeout_seconds=manifest["timeout_seconds"],
                max_retries=manifest["max_retries"],
                max_context_bytes=manifest["max_context_bytes"],
                max_lookup_bytes=manifest["max_lookup_bytes"],
            )
        actual_provider_configuration = self._provider_configuration(provider)
        if actual_provider_configuration != expected_provider_configuration:
            raise HarnessError(
                "OUTBOUND_CONSENT_STALE",
                "The provider configuration differs from the consented configuration",
                details={
                    "expected": expected_provider_configuration,
                    "actual": actual_provider_configuration,
                },
                exit_code=3,
            )

        context = self._agent_context_for_operation(input_snapshot, operation)
        started_at = _timestamp(self.clock())
        if operation == "evaluations":
            draft = provider.generate_evaluations(context)
            draft.validate_against(context)
            result_payload = draft.as_payload()
            candidates = _artifact_payload(self._read(refs, "candidate_set"))["candidates"]
            criteria = _artifact_payload(self._read(refs, "criteria_set"))["criteria"]
            evidence = _artifact_payload(self._read(refs, "evidence_set"))["evidence"]
            result_payload = validate_evaluations(
                result_payload,
                candidate_ids={item["candidate_id"] for item in candidates},
                criterion_ids={item["criterion_id"] for item in criteria},
                evidence_by_id={item["evidence_id"]: item for item in evidence},
            )
            result_ref = "evaluation_set"
            result_type = "evaluation-set"
            result_parents = {
                "candidate_set": refs["candidate_set"],
                "criteria_set": refs["criteria_set"],
                "evidence_set": refs["evidence_set"],
            }
        elif operation == "recommendation":
            draft = provider.generate_recommendation(context)
            draft.validate_against(context)
            result_payload = draft.as_payload()
            comparison = _artifact_payload(self._read(refs, "comparison"))
            if (
                result_payload["disposition"] == "select"
                and result_payload["candidate_id"] not in comparison["eligible_candidate_ids"]
            ):
                raise HarnessError(
                    "INELIGIBLE_RECOMMENDATION",
                    "Recommendation may select only a must-eligible candidate",
                )
            result_ref = "recommendation"
            result_type = "recommendation"
            result_parents = {"comparison": refs["comparison"]}
        else:
            raise HarnessError(
                "INVALID_AGENT_OPERATION",
                "Agent operation must be evaluations or recommendation",
            )

        run = getattr(provider, "last_run", None)
        if run is None:
            raise HarnessError(
                "PROVIDER_TRACE_MISSING",
                "A successful OpenAI provider run did not expose trace metadata",
                exit_code=4,
            )
        expected_tool = (
            "submit_evaluations" if operation == "evaluations" else "submit_recommendation"
        )
        expected_trace = {
            "provider": manifest["provider"],
            "model": manifest["model"],
            "prompt_id": manifest["prompt_id"],
            "prompt_sha256": manifest["prompt_sha256"],
            "tool_name": expected_tool,
        }
        actual_trace = {
            "provider": getattr(run, "provider", None),
            "model": getattr(run, "model", None),
            "prompt_id": getattr(run, "prompt_id", None),
            "prompt_sha256": getattr(run, "prompt_sha256", None),
            "tool_name": getattr(run, "tool_name", None),
        }
        if actual_trace != expected_trace:
            raise HarnessError(
                "PROVIDER_TRACE_MISMATCH",
                "The provider trace differs from the consented provider configuration",
                details={"expected": expected_trace, "actual": actual_trace},
                exit_code=4,
            )
        final_provider_configuration = self._provider_configuration(provider)
        if final_provider_configuration != expected_provider_configuration:
            raise HarnessError(
                "PROVIDER_TRACE_MISMATCH",
                "The provider configuration changed during the outbound run",
                details={
                    "expected": expected_provider_configuration,
                    "actual": final_provider_configuration,
                },
                exit_code=4,
            )
        transcript_events = [dict(event) for event in getattr(run, "transcript", ())]
        try:
            from ai_work_harness.decision.openai_provider import validate_tool_transcript

            validate_tool_transcript(
                transcript_events,
                context=context,
                expected_submission=expected_tool,
                result_payload=result_payload,
            )
        except (TypeError, ValueError) as exc:
            raise HarnessError(
                "PROVIDER_TRACE_MISMATCH",
                "The provider tool transcript is not bound to the frozen inputs and result",
                details={"reason": str(exc)},
                exit_code=4,
            ) from exc
        return self._commit_openai_agent_result(
            operation=operation,
            expected_parent=expected_parent,
            consent_sha=consent_sha,
            manifest_sha=consent["subject_sha256"],
            run=run,
            result_ref=result_ref,
            result_type=result_type,
            result_parents=result_parents,
            result_payload=result_payload,
            transcript_events=transcript_events,
            started_at=started_at,
        )

    def replay_agent_validate(self, *, agent_run_sha: str | None = None) -> dict[str, Any]:
        try:
            return self._replay_agent_validate(agent_run_sha=agent_run_sha)
        except HarnessError as exc:
            if exc.code == "AGENT_REPLAY_INVALID" and exc.exit_code == 5:
                raise
            raise HarnessError(
                "AGENT_REPLAY_INVALID",
                "Recorded agent run failed structural replay validation",
                details={"reason_code": exc.code},
                exit_code=5,
            ) from exc
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise HarnessError(
                "AGENT_REPLAY_INVALID",
                "Recorded agent run has an invalid structure",
                exit_code=5,
            ) from exc

    def _replay_agent_validate(self, *, agent_run_sha: str | None = None) -> dict[str, Any]:
        current = self._current()
        refs = _snapshot_refs(current)
        run_sha = agent_run_sha or self._require_ref(refs, "agent_run")
        run_artifact = self.store.read_artifact(run_sha)
        if run_artifact.artifact_type != "agent-run":
            raise HarnessError(
                "AGENT_REPLAY_INVALID",
                "The requested digest is not an agent-run artifact",
                exit_code=5,
            )
        run = _artifact_payload(run_artifact)
        parents = _artifact_parents(run_artifact)
        result_ref, result_type, expected_tool = self._agent_operation_contract(run["operation"])
        if set(parents) != {"agent_consent", result_ref}:
            raise HarnessError(
                "AGENT_REPLAY_INVALID",
                "The agent-run parent roles do not match its operation",
                exit_code=5,
            )
        consent_artifact = self.store.read_artifact(parents["agent_consent"])
        consent, manifest, input_snapshot = self._validate_outbound_consent_artifact(
            consent_artifact
        )
        if consent["subject_sha256"] != run["outbound_manifest_sha256"]:
            raise HarnessError(
                "AGENT_REPLAY_INVALID",
                "The agent-run does not bind its consent manifest digest",
                exit_code=5,
            )
        expected_manifest = self._agent_manifest(
            operation=run["operation"],
            provider=run["provider"],
            model=run["model"],
            snapshot=input_snapshot,
        )
        result_sha = parents.get(result_ref)
        if result_sha is None:
            raise HarnessError(
                "AGENT_REPLAY_INVALID",
                "The agent-run result binding is missing",
                exit_code=5,
            )
        result = self.store.read_artifact(result_sha)
        if result.artifact_type != result_type:
            raise HarnessError(
                "AGENT_REPLAY_INVALID",
                "The agent-run result artifact type is invalid",
                exit_code=5,
            )
        transcript_document = parse_json_bytes(
            self.store.read_object(run["transcript_sha256"]),
            label="agent tool transcript",
        )
        transcript_valid = False
        if isinstance(transcript_document, dict) and set(transcript_document) == {"events"}:
            try:
                from ai_work_harness.decision.openai_provider import validate_tool_transcript

                validate_tool_transcript(
                    transcript_document["events"],
                    context=self._agent_context_for_operation(
                        input_snapshot,
                        run["operation"],
                    ),
                    expected_submission=expected_tool,
                    result_payload=_artifact_payload(result),
                )
                transcript_valid = True
            except (TypeError, ValueError):
                transcript_valid = False
        checks = {
            "manifest": manifest == expected_manifest,
            "manifest_sha256": digest_json(manifest) == run["outbound_manifest_sha256"],
            "input_sha256": manifest["input_sha256"] == run["input_sha256"],
            "input_snapshot_sha256": (
                manifest["input_snapshot_sha256"] == run["input_snapshot_sha256"]
            ),
            "prompt_sha256": manifest["prompt_sha256"] == run["prompt_sha256"],
            "tool_name": run["tool_name"] == expected_tool,
            "result_sha256": result_sha == run["result_sha256"],
            "transcript_sha256": digest_json(transcript_document) == run["transcript_sha256"],
            "transcript": transcript_valid,
        }
        failed = sorted(key for key, valid in checks.items() if not valid)
        if failed:
            raise HarnessError(
                "AGENT_REPLAY_INVALID",
                "Recorded agent input/output bindings failed validation",
                details={"failed_checks": failed},
                exit_code=5,
            )
        return self._result(
            current,
            replay_valid=True,
            mode="validate",
            agent_run_sha256=run_sha,
            checks=checks,
        )

    @_serialized_mutation
    def import_final_decision(self, payload: Any, *, expected_parent: str) -> dict[str, Any]:
        current = self._current()
        refs = _snapshot_refs(current)
        comparison_sha = self._require_ref(refs, "comparison")
        comparison = _artifact_payload(self._read(refs, "comparison"))
        document = validate_final_decision(
            payload,
            comparison=comparison,
            producer_kind=_artifact_producer_kind(self._read(refs, "evaluation_set")),
        )
        if "final_decision" in refs:
            active_final = _artifact_payload(self._read(refs, "final_decision"))
            if _final_decision_semantics(active_final) == _final_decision_semantics(document):
                raise HarnessError(
                    "FINAL_DECISION_UNCHANGED",
                    "The final decision must change semantically before it is recorded again",
                    details={"final_decision_sha256": refs["final_decision"]},
                    exit_code=3,
                )
        parents = {"comparison": comparison_sha}
        recommendation = None
        if "recommendation" in refs:
            parents["recommendation"] = refs["recommendation"]
            recommendation = _artifact_payload(self._read(refs, "recommendation"))
        relation = recommendation_relation(document, recommendation)
        final_payload = {
            **document,
            "recommendation_relation": relation,
            "recorded_at": _timestamp(self.clock()),
        }
        _, digest = self._create_artifact(
            artifact_type="final-decision",
            producer_kind="local_operator",
            parents=parents,
            payload=final_payload,
        )
        snapshot = self._commit_ref(
            expected_parent=expected_parent,
            ref_name="final_decision",
            digest=digest,
            invalidate_from="final_decision",
            operation="final.import",
        )
        return self._result(
            snapshot,
            artifact="final_decision",
            artifact_sha256=digest,
            recommendation_relation=relation,
        )

    @_serialized_mutation
    def create_approval_challenge(
        self,
        *,
        disposition: str,
        expected_parent: str,
    ) -> dict[str, Any]:
        if disposition not in {"approved", "rejected", "changes_requested"}:
            raise HarnessError("INVALID_APPROVAL_DISPOSITION", "Unknown approval disposition")
        self.verify(expected_parent)
        current = self._current()
        if _snapshot_sha(current) != expected_parent:
            raise HarnessError(
                "WRITE_CONFLICT",
                "Expected parent is not current",
                details={"expected": expected_parent, "current": _snapshot_sha(current)},
                exit_code=3,
            )
        refs = _snapshot_refs(current)
        if "human_approval" in refs:
            approval = _artifact_payload(self._read(refs, "human_approval"))
            raise HarnessError(
                "ACTIVE_APPROVAL_EXISTS",
                "Change the final decision or an upstream artifact before creating a new challenge",
                details={
                    "approval_sha256": refs["human_approval"],
                    "disposition": approval["disposition"],
                },
                exit_code=3,
            )
        final_sha = self._require_ref(refs, "final_decision")
        final_decision = _artifact_payload(self._read(refs, "final_decision"))
        if disposition == "approved" and final_decision["disposition"] not in {
            "select",
            "reject_all",
        }:
            raise HarnessError(
                "DECISION_NOT_APPROVABLE",
                "defer and request_more_evidence cannot be approved as complete",
            )
        bundle_refs = {
            key: value
            for key, value in refs.items()
            if key not in {"decision_bundle", "approval_challenge", "human_approval"}
        }
        bundle_payload = {"refs": bundle_refs}
        _, bundle_sha = self._create_artifact(
            artifact_type="decision-bundle",
            producer_kind="core",
            parents=bundle_refs,
            payload=bundle_payload,
        )
        challenge_id = self.store.new_event_id()
        nonce = secrets.token_hex(16)
        issued_at = self.clock()
        expires_at = issued_at + timedelta(minutes=10)
        challenge_payload = {
            "challenge_id": challenge_id,
            "decision_bundle_sha256": bundle_sha,
            "parent_snapshot_sha256": expected_parent,
            "proposed_disposition": disposition,
            "nonce": nonce,
            "issued_at": _timestamp(issued_at),
            "expires_at": _timestamp(expires_at),
            "actor_label": "local_operator",
            "identity_verified": False,
        }
        _, challenge_sha = self._create_artifact(
            artifact_type="approval-challenge",
            producer_kind="core",
            parents={"decision_bundle": bundle_sha, "final_decision": final_sha},
            payload=challenge_payload,
        )
        updated_refs = dict(refs)
        updated_refs.pop("human_approval", None)
        updated_refs["decision_bundle"] = bundle_sha
        updated_refs["approval_challenge"] = challenge_sha
        snapshot = self.store.commit(
            expected_parent=expected_parent,
            refs=updated_refs,
            operation="approval.challenge",
        )
        return self._result(
            snapshot,
            artifact="approval_challenge",
            challenge_id=challenge_id,
            nonce=nonce,
            decision_bundle_sha256=bundle_sha,
            expires_at=challenge_payload["expires_at"],
            identity_verified=False,
        )

    @_serialized_mutation
    def commit_approval(
        self,
        *,
        challenge_id: str,
        nonce: str,
        expected_bundle_sha: str,
        reason: str,
        expected_parent: str,
    ) -> dict[str, Any]:
        reason_value = require_string(reason, "reason")
        assert reason_value is not None
        self.verify(expected_parent)
        current = self._current()
        if _snapshot_sha(current) != expected_parent:
            raise HarnessError(
                "CHALLENGE_STALE",
                "Challenge snapshot is no longer current",
                exit_code=3,
            )
        refs = _snapshot_refs(current)
        challenge_sha = refs.get("approval_challenge")
        if challenge_sha is None:
            raise HarnessError(
                "CHALLENGE_STALE",
                "The approval challenge is no longer active",
                exit_code=3,
            )
        challenge = _artifact_payload(self._read(refs, "approval_challenge"))
        if (
            getattr(current.snapshot, "parent_snapshot_sha256", None)
            != challenge["parent_snapshot_sha256"]
        ):
            raise HarnessError(
                "CHALLENGE_STALE",
                "An intervening mutation invalidated the approval challenge",
                exit_code=3,
            )
        mismatches = {
            key: {"expected": expected, "actual": challenge.get(key)}
            for key, expected in {
                "challenge_id": challenge_id,
                "nonce": nonce,
                "decision_bundle_sha256": expected_bundle_sha,
            }.items()
            if challenge.get(key) != expected
        }
        if mismatches:
            raise HarnessError(
                "CHALLENGE_MISMATCH",
                "Approval challenge response does not match",
                details={"mismatches": mismatches},
                exit_code=3,
            )
        approval_time = self.clock()
        if approval_time < _parse_timestamp(challenge["issued_at"]):
            raise HarnessError(
                "CHALLENGE_CLOCK_REGRESSION",
                "Current time precedes challenge issuance",
                exit_code=3,
            )
        if approval_time >= _parse_timestamp(challenge["expires_at"]):
            raise HarnessError("CHALLENGE_EXPIRED", "Approval challenge has expired", exit_code=3)
        bundle_sha = self._require_ref(refs, "decision_bundle")
        if bundle_sha != expected_bundle_sha:
            raise HarnessError(
                "CHALLENGE_STALE",
                "Decision bundle is no longer current",
                exit_code=3,
            )
        final_decision = _artifact_payload(self._read(refs, "final_decision"))
        disposition = challenge["proposed_disposition"]
        if disposition == "approved" and final_decision["disposition"] not in {
            "select",
            "reject_all",
        }:
            raise HarnessError("DECISION_NOT_APPROVABLE", "Current final decision is provisional")
        approval_payload = {
            "approval_id": self.store.new_event_id(),
            "challenge_id": challenge_id,
            "challenge_sha256": challenge_sha,
            "decision_bundle_sha256": bundle_sha,
            "disposition": disposition,
            "reason": reason_value,
            "actor_label": "local_operator",
            "identity_verified": False,
            "approved_at": _timestamp(approval_time),
        }
        _, approval_sha = self._create_artifact(
            artifact_type="human-approval",
            producer_kind="core",
            parents={
                "approval_challenge": challenge_sha,
                "decision_bundle": bundle_sha,
                "final_decision": refs["final_decision"],
            },
            payload=approval_payload,
        )
        updated_refs = dict(refs)
        updated_refs.pop("approval_challenge", None)
        updated_refs["human_approval"] = approval_sha
        snapshot = self.store.commit(
            expected_parent=expected_parent,
            refs=updated_refs,
            operation="approval.commit",
        )
        return self._result(
            snapshot,
            artifact="human_approval",
            artifact_sha256=approval_sha,
            approval_id=approval_payload["approval_id"],
            disposition=disposition,
            identity_verified=False,
        )

    def _validate_outbound_consent_artifact(
        self,
        artifact: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], Any]:
        try:
            if getattr(artifact, "artifact_type", None) != "human-confirmation":
                raise HarnessError("OUTBOUND_CONSENT_STALE", "Consent artifact type is invalid")
            payload = _artifact_payload(artifact)
            manifest = payload.get("outbound_manifest")
            if (
                payload.get("subject_type") != "outbound-manifest"
                or payload.get("method") not in {"digest_challenge", "guided_exact_phrase"}
                or not isinstance(manifest, dict)
                or digest_json(manifest) != payload.get("subject_sha256")
            ):
                raise HarnessError(
                    "OUTBOUND_CONSENT_STALE",
                    "Consent does not bind its outbound manifest",
                )
            operation = manifest.get("operation")
            if not isinstance(operation, str):
                raise HarnessError(
                    "OUTBOUND_CONSENT_STALE",
                    "Consent manifest operation is invalid",
                )
            self._agent_operation_contract(operation)
            input_snapshot = self.store.load_snapshot(manifest["input_snapshot_sha256"])
            input_refs = _snapshot_refs(input_snapshot)
            parent_names = [
                "source_manifest",
                "candidate_set",
                "criteria_set",
                "evidence_set",
            ]
            if operation == "recommendation":
                parent_names.append("comparison")
            expected_parents = {name: self._require_ref(input_refs, name) for name in parent_names}
            if _artifact_parents(artifact) != expected_parents:
                raise HarnessError(
                    "OUTBOUND_CONSENT_STALE",
                    "Consent parents do not match its pinned input snapshot",
                )
            expected_manifest = self._agent_manifest(
                operation=operation,
                provider=manifest["provider"],
                model=manifest["model"],
                snapshot=input_snapshot,
            )
            if manifest != expected_manifest:
                raise HarnessError(
                    "OUTBOUND_CONSENT_STALE",
                    "Consent manifest does not match its pinned decision context",
                )
            return payload, manifest, input_snapshot
        except HarnessError as exc:
            if exc.code == "OUTBOUND_CONSENT_STALE" and exc.exit_code == 5:
                raise
            raise HarnessError(
                "OUTBOUND_CONSENT_STALE",
                "Outbound consent binding failed integrity verification",
                details={"reason_code": exc.code},
                exit_code=5,
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise HarnessError(
                "OUTBOUND_CONSENT_STALE",
                "Outbound consent structure is invalid",
                exit_code=5,
            ) from exc

    def _verify_active_bindings(self, snapshot: Any) -> None:
        refs = _snapshot_refs(snapshot)
        for ref_name, artifact_sha in refs.items():
            artifact = self.store.read_artifact(artifact_sha)
            for parent_name, parent_sha in _artifact_parents(artifact).items():
                if ref_name == "human_approval" and parent_name == "approval_challenge":
                    continue
                if ref_name == "agent_run" and parent_name == "agent_consent":
                    continue
                if refs.get(parent_name) != parent_sha:
                    raise HarnessError(
                        "ARTIFACT_BINDING_STALE",
                        f"{ref_name} is bound to a non-current parent",
                        details={
                            "artifact": ref_name,
                            "parent": parent_name,
                            "expected": refs.get(parent_name),
                            "actual": parent_sha,
                        },
                        exit_code=5,
                    )
        for subject_type, subject_ref in SUBJECT_REFS.items():
            confirmation_ref = CONFIRMATION_REFS[subject_type]
            if confirmation_ref not in refs:
                continue
            confirmation_artifact = self._read(refs, confirmation_ref)
            confirmation = _artifact_payload(confirmation_artifact)
            expected_subject_sha = refs.get(subject_ref)
            if (
                confirmation["subject_type"] != subject_type
                or confirmation["subject_sha256"] != expected_subject_sha
                or _artifact_parents(confirmation_artifact) != {subject_ref: expected_subject_sha}
            ):
                raise HarnessError(
                    "CONFIRMATION_STALE",
                    f"{confirmation_ref} does not bind the current subject",
                    exit_code=5,
                )
        if "agent_consent" in refs:
            self._validate_outbound_consent_artifact(self._read(refs, "agent_consent"))
        if "agent_run" in refs:
            try:
                run_artifact = self._read(refs, "agent_run")
                run = _artifact_payload(run_artifact)
                run_parents = _artifact_parents(run_artifact)
                result_ref, result_type, expected_tool = self._agent_operation_contract(
                    run["operation"]
                )
                if set(run_parents) != {"agent_consent", result_ref}:
                    raise HarnessError(
                        "AGENT_BINDING_STALE",
                        "Agent run parents do not match its operation",
                    )
                consent_artifact = self.store.read_artifact(run_parents["agent_consent"])
                consent, manifest, _input_snapshot = self._validate_outbound_consent_artifact(
                    consent_artifact
                )
                result_artifact = self.store.read_artifact(run_parents[result_ref])
                if (
                    result_artifact.artifact_type != result_type
                    or run_parents[result_ref] != run["result_sha256"]
                    or run["tool_name"] != expected_tool
                    or run["outbound_manifest_sha256"] != consent["subject_sha256"]
                    or run["input_snapshot_sha256"] != manifest["input_snapshot_sha256"]
                    or run["input_sha256"] != manifest["input_sha256"]
                    or run["provider"] != manifest["provider"]
                    or run["model"] != manifest["model"]
                    or run["prompt_id"] != manifest["prompt_id"]
                    or run["prompt_sha256"] != manifest["prompt_sha256"]
                ):
                    raise HarnessError(
                        "AGENT_BINDING_STALE",
                        "Agent run trace does not match its consented result",
                    )
            except HarnessError as exc:
                if exc.code == "AGENT_BINDING_STALE" and exc.exit_code == 5:
                    raise
                raise HarnessError(
                    "AGENT_BINDING_STALE",
                    "Agent run binding failed integrity verification",
                    details={"reason_code": exc.code},
                    exit_code=5,
                ) from exc
            except (KeyError, TypeError, ValueError) as exc:
                raise HarnessError(
                    "AGENT_BINDING_STALE",
                    "Agent run binding structure is invalid",
                    exit_code=5,
                ) from exc
        if "human_approval" in refs:
            approval_artifact = self._read(refs, "human_approval")
            approval = _artifact_payload(approval_artifact)
            approval_parents = _artifact_parents(approval_artifact)
            if approval_parents.get("approval_challenge") != approval["challenge_sha256"]:
                raise HarnessError(
                    "APPROVAL_BINDING_STALE",
                    "Approval challenge binding is stale",
                    exit_code=5,
                )
            challenge_artifact = self.store.read_artifact(approval["challenge_sha256"])
            challenge = _artifact_payload(challenge_artifact)
            challenge_parents = _artifact_parents(challenge_artifact)
            if approval["decision_bundle_sha256"] != refs["decision_bundle"]:
                raise HarnessError(
                    "APPROVAL_BINDING_STALE",
                    "Approval bundle binding is stale",
                    exit_code=5,
                )
            if challenge["decision_bundle_sha256"] != approval["decision_bundle_sha256"]:
                raise HarnessError(
                    "APPROVAL_BINDING_STALE",
                    "Challenge bundle binding is stale",
                    exit_code=5,
                )
            if challenge_parents.get("decision_bundle") != approval["decision_bundle_sha256"]:
                raise HarnessError(
                    "APPROVAL_BINDING_STALE",
                    "Challenge parent bundle is stale",
                    exit_code=5,
                )
            if approval["challenge_id"] != challenge["challenge_id"]:
                raise HarnessError(
                    "APPROVAL_BINDING_STALE",
                    "Approval challenge ID is stale",
                    exit_code=5,
                )
            if approval["disposition"] != challenge["proposed_disposition"]:
                raise HarnessError(
                    "APPROVAL_BINDING_STALE",
                    "Approval disposition does not match the challenged disposition",
                    exit_code=5,
                )
            approval_time = _parse_timestamp(approval["approved_at"])
            if not (
                _parse_timestamp(challenge["issued_at"])
                <= approval_time
                < _parse_timestamp(challenge["expires_at"])
            ):
                raise HarnessError(
                    "APPROVAL_BINDING_STALE",
                    "Approval timestamp is outside the challenge validity window",
                    exit_code=5,
                )
            record = getattr(snapshot, "snapshot", snapshot)
            challenge_snapshot_sha = getattr(record, "parent_snapshot_sha256", None)
            if not isinstance(challenge_snapshot_sha, str):
                raise HarnessError(
                    "APPROVAL_BINDING_STALE",
                    "Approval snapshot does not directly follow its challenge",
                    exit_code=5,
                )
            challenge_snapshot = self.store.load_snapshot(challenge_snapshot_sha)
            challenge_refs = _snapshot_refs(challenge_snapshot)
            challenge_record = getattr(challenge_snapshot, "snapshot", challenge_snapshot)
            if (
                challenge_refs.get("approval_challenge") != approval["challenge_sha256"]
                or challenge_refs.get("decision_bundle") != approval["decision_bundle_sha256"]
                or getattr(challenge_record, "parent_snapshot_sha256", None)
                != challenge["parent_snapshot_sha256"]
            ):
                raise HarnessError(
                    "APPROVAL_BINDING_STALE",
                    "Approval does not directly bind the recorded challenge snapshot",
                    exit_code=5,
                )

    def _verify_workflow(self, snapshot: Any) -> None:
        from ai_work_harness.decision.verification import verify_workflow

        refs = _snapshot_refs(snapshot)
        try:
            artifacts = {
                key: _artifact_dict(self.store.read_artifact(digest))
                for key, digest in refs.items()
            }
            excerpt_hashes = {}
            if "source_manifest" in artifacts and "evidence_set" in artifacts:
                excerpt_hashes = self._source_excerpt_hashes(
                    artifacts["source_manifest"]["payload"], artifacts["evidence_set"]["payload"]
                )
            verify_workflow(refs=refs, artifacts=artifacts, excerpt_hashes=excerpt_hashes)
        except HarnessError as exc:
            raise HarnessError(
                "WORKFLOW_INTEGRITY_FAILED",
                "Stored workflow violates decision policy",
                details={"reason_code": exc.code, "reason": exc.message},
                exit_code=5,
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise HarnessError(
                "WORKFLOW_INTEGRITY_FAILED",
                "Stored workflow structure is invalid",
                details={"reason_code": "INVALID_WORKFLOW_STATE"},
                exit_code=5,
            ) from exc

    def verify(self, snapshot_sha256: str | None = None) -> dict[str, Any]:
        snapshot = (
            self.store.load_snapshot(snapshot_sha256)
            if snapshot_sha256 is not None
            else self._current()
        )
        report = self.store.verify(_snapshot_sha(snapshot))
        if getattr(report, "ok", True) is False:
            issues = getattr(report, "issues", ())
            raise HarnessError(
                "INTEGRITY_VERIFICATION_FAILED",
                "Snapshot integrity verification failed",
                details={
                    "issues": [
                        {
                            "code": issue.code,
                            "message": issue.message,
                            "path": issue.path,
                        }
                        for issue in issues
                    ]
                },
                exit_code=5,
            )
        self._verify_active_bindings(snapshot)
        self._verify_workflow(snapshot)
        return self._result(
            snapshot,
            verified=True,
            issues=[],
        )

    def _lifecycle_state(self, refs: Mapping[str, str]) -> str:
        order = (
            ("human_approval", "finalized"),
            ("approval_challenge", "challenge_pending"),
            ("final_decision", "decision_recorded"),
            ("recommendation", "recommended"),
            ("comparison", "compared"),
            ("evaluation_review_set", "reviews_ready"),
            ("evaluation_set", "evaluations_ready"),
            ("evidence_set", "evidence_ready"),
            ("criteria_confirmation", "criteria_confirmed"),
            ("criteria_set", "criteria_draft"),
            ("candidate_confirmation", "candidates_confirmed"),
            ("candidate_set", "candidates_draft"),
            ("frame_confirmation", "frame_confirmed"),
            ("decision_frame", "frame_draft"),
        )
        return next((state for key, state in order if key in refs), "initialized")

    def _historical_stale_reasons(self, snapshot: Any) -> list[str]:
        current_refs = _snapshot_refs(snapshot)
        record = getattr(snapshot, "snapshot", snapshot)
        cursor = getattr(record, "parent_snapshot_sha256", None)
        while isinstance(cursor, str):
            historical = self.store.load_snapshot(cursor)
            old_refs = _snapshot_refs(historical)
            if "human_approval" in old_refs and "decision_bundle" in old_refs:
                bundle = _artifact_payload(self._read(old_refs, "decision_bundle"))["refs"]
                labels = {
                    "source_manifest": "source_changed",
                    "decision_frame": "frame_changed",
                    "frame_confirmation": "frame_confirmation_changed",
                    "candidate_set": "candidate_set_changed",
                    "candidate_confirmation": "candidate_confirmation_changed",
                    "criteria_set": "criteria_set_changed",
                    "criteria_confirmation": "criteria_confirmation_changed",
                    "evidence_set": "evidence_changed",
                    "evaluation_set": "evaluations_changed",
                    "evaluation_review_set": "reviews_changed",
                    "comparison": "comparison_changed",
                    "recommendation": "recommendation_changed",
                    "final_decision": "final_decision_changed",
                }
                for key, label in labels.items():
                    if bundle.get(key) != current_refs.get(key):
                        return [label]
                return ["approval_superseded"]
            historical_record = getattr(historical, "snapshot", historical)
            cursor = getattr(historical_record, "parent_snapshot_sha256", None)
        return []

    def status(self) -> dict[str, Any]:
        return self._status_for_snapshot(self._current())

    def read_operator_state(
        self,
        snapshot_sha256: str | None = None,
    ) -> ValidatedOperatorState:
        """Return one immutable, fail-closed model for current or an explicit snapshot."""

        from ai_work_harness.decision.guidance import (
            ApprovalChallengeBinding,
            ValidatedOperatorState,
        )

        snapshot = (
            self.store.load_snapshot(snapshot_sha256)
            if snapshot_sha256 is not None
            else self._current()
        )
        snapshot_sha = _snapshot_sha(snapshot)
        # Verify by the pinned digest.  A concurrent pointer move does not change
        # which graph this query plans from, and integrity failures are not
        # converted into a best-effort status response.
        self.verify(snapshot_sha)
        refs = _snapshot_refs(snapshot)
        artifacts: dict[str, dict[str, Any]] = {}
        for ref_name, digest in refs.items():
            artifact = self.store.read_artifact(digest)
            artifacts[ref_name] = {
                "artifact_type": getattr(artifact, "artifact_type", ref_name.replace("_", "-")),
                "producer_kind": _artifact_producer_kind(artifact),
                "producer": _thaw(getattr(artifact, "producer", {})),
                "payload": _artifact_payload(artifact),
            }

        approval = artifacts["human_approval"]["payload"] if "human_approval" in artifacts else None
        final_decision = (
            artifacts["final_decision"]["payload"] if "final_decision" in artifacts else None
        )
        decision_complete = bool(
            approval
            and approval["disposition"] == "approved"
            and final_decision
            and final_decision["disposition"] in {"select", "reject_all"}
        )
        ready = bool(
            decision_complete
            and final_decision is not None
            and final_decision["disposition"] == "select"
        )
        stale_reasons = (
            () if "human_approval" in refs else tuple(self._historical_stale_reasons(snapshot))
        )
        active_challenge = None
        if "approval_challenge" in refs:
            active_challenge = ApprovalChallengeBinding.from_mapping(
                artifacts["approval_challenge"]["payload"],
                challenge_sha256=refs["approval_challenge"],
                challenge_snapshot_sha256=snapshot_sha,
            )
        outbound_consent_operation = None
        if "agent_consent" in refs:
            manifest = artifacts["agent_consent"]["payload"].get("outbound_manifest")
            if isinstance(manifest, dict) and manifest.get("operation") in {
                "evaluations",
                "recommendation",
            }:
                outbound_consent_operation = manifest["operation"]
        record = getattr(snapshot, "snapshot", snapshot)
        return ValidatedOperatorState(
            session_id=self.session_id,
            generation=record.generation,
            pinned_snapshot_sha256=snapshot_sha,
            lifecycle_state=self._lifecycle_state(refs),
            refs=refs,
            artifacts=artifacts,
            decision_complete=decision_complete,
            ready=ready,
            stale_reasons=stale_reasons,
            active_challenge=active_challenge,
            outbound_consent_operation=outbound_consent_operation,
        )

    def operator_plan(self) -> dict[str, Any]:
        """Return the sanitized read-only operator-plan.v1 value."""

        from ai_work_harness.decision.guidance import plan_operator_next

        state = self.read_operator_state()
        plan = plan_operator_next(state, observed_at=self.clock())
        return {
            "ok": True,
            "session_id": self.session_id,
            "generation": state.generation,
            "snapshot_sha256": state.pinned_snapshot_sha256,
            **plan.to_public_dict(),
        }

    def read_source_excerpt(
        self,
        *,
        snapshot_sha256: str,
        source_id: str,
        start_line: int,
        end_line: int,
    ) -> dict[str, Any]:
        """Read and hash one line-bounded excerpt from a verified pinned snapshot."""

        if (
            not isinstance(start_line, int)
            or isinstance(start_line, bool)
            or not isinstance(end_line, int)
            or isinstance(end_line, bool)
            or start_line < 1
            or end_line < start_line
        ):
            raise HarnessError(
                "INVALID_SOURCE_LOCATOR",
                "Source line range must be 1-based, inclusive, and ordered",
            )
        snapshot = self.store.load_snapshot(snapshot_sha256)
        self.verify(snapshot_sha256)
        refs = _snapshot_refs(snapshot)
        manifest = _artifact_payload(self._read(refs, "source_manifest"))
        source = next(
            (item for item in manifest["sources"] if item["source_id"] == source_id),
            None,
        )
        if source is None:
            raise HarnessError(
                "UNKNOWN_REFERENCE",
                "Source excerpt references an unknown source",
                details={"source_id": source_id},
            )
        raw = self.store.read_object(source["blob_sha256"])
        try:
            lines = raw.decode("utf-8", errors="strict").splitlines(keepends=True)
        except UnicodeDecodeError as exc:  # pragma: no cover - capture validates this
            raise HarnessError(
                "CORRUPT_SOURCE",
                "Captured source is no longer UTF-8",
                exit_code=5,
            ) from exc
        if end_line > len(lines):
            raise HarnessError(
                "INVALID_SOURCE_LOCATOR",
                "Source line range exceeds the captured source",
                details={"line_count": len(lines), "end_line": end_line},
            )
        excerpt_bytes = "".join(lines[start_line - 1 : end_line]).encode("utf-8")
        return self._result(
            snapshot,
            source_id=source_id,
            start_line=start_line,
            end_line=end_line,
            excerpt=excerpt_bytes.decode("utf-8"),
            excerpt_sha256=sha256_bytes(excerpt_bytes),
        )

    def preview_invalidation(
        self,
        *,
        snapshot_sha256: str,
        ref_name: str,
    ) -> tuple[str, ...]:
        """List active refs that a replacement would make stale, without writing."""

        if ref_name not in DOWNSTREAM:
            raise HarnessError(
                "UNKNOWN_ARTIFACT_REF",
                "Artifact does not define a replacement boundary",
                details={"ref_name": ref_name},
            )
        snapshot = self.store.load_snapshot(snapshot_sha256)
        self.verify(snapshot_sha256)
        refs = _snapshot_refs(snapshot)
        return tuple(sorted(set(refs).intersection(DOWNSTREAM[ref_name])))

    def _status_for_snapshot(self, snapshot: Any) -> dict[str, Any]:
        refs = _snapshot_refs(snapshot)
        verified = True
        verification_error: dict[str, Any] | None = None
        try:
            self.verify(_snapshot_sha(snapshot))
        except HarnessError as exc:
            verified = False
            verification_error = exc.as_dict()["error"]
        approval: dict[str, Any] | None = None
        final_decision: dict[str, Any] | None = None
        if "human_approval" in refs:
            approval = _artifact_payload(self._read(refs, "human_approval"))
        if "final_decision" in refs:
            final_decision = _artifact_payload(self._read(refs, "final_decision"))
        decision_complete = bool(
            verified
            and approval
            and approval["disposition"] == "approved"
            and final_decision
            and final_decision["disposition"] in {"select", "reject_all"}
        )
        ready = bool(
            decision_complete
            and final_decision is not None
            and final_decision["disposition"] == "select"
        )
        stale_reasons = [] if "human_approval" in refs else self._historical_stale_reasons(snapshot)
        return self._result(
            snapshot,
            lifecycle_state=self._lifecycle_state(refs),
            verified=verified,
            verification_error=verification_error,
            decision_complete=decision_complete,
            ready=ready,
            stale_reasons=stale_reasons,
            refs=refs,
        )

    def doctor(self) -> dict[str, Any]:
        report = self.store.doctor()
        if hasattr(report, "as_dict"):
            details = report.as_dict()
        else:
            details = {key: value for key, value in vars(report).items() if not key.startswith("_")}
        if getattr(report, "ok", False) is not True:
            lock = details.get("writer_lock")
            if (
                details.get("integrity_ok") is True
                and isinstance(lock, dict)
                and lock.get("state") != "absent"
            ):
                raise HarnessError(
                    "WRITE_LOCK_PRESENT",
                    "Storage integrity passed, but the writer lock requires operator attention",
                    details={"doctor": details},
                    exit_code=3,
                )
            raise HarnessError(
                "INTEGRITY_VERIFICATION_FAILED",
                "Decision doctor found integrity damage",
                details={"doctor": details},
                exit_code=5,
            )
        return {"ok": True, "session_id": self.session_id, "doctor": details}

    def export_view(
        self,
        output: Path,
        *,
        snapshot_sha256: str | None = None,
        include_cited_excerpts: bool = False,
    ) -> dict[str, Any]:
        output = output.resolve()
        managed_root = (self.project_root / ".ai-work-harness" / "v2").resolve()
        try:
            output.relative_to(managed_root)
        except ValueError:
            pass
        else:
            raise HarnessError(
                "EXPORT_PATH_MANAGED",
                "Viewer exports must be written outside managed v2 storage",
            )
        snapshot = (
            self.store.load_snapshot(snapshot_sha256)
            if snapshot_sha256 is not None
            else self._current()
        )
        self.verify(_snapshot_sha(snapshot))
        refs = _snapshot_refs(snapshot)
        allowed = (
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
            "human_approval",
        )
        artifacts: dict[str, Any] = {}
        digest_map: dict[str, str] = {}
        for key in allowed:
            if key not in refs:
                continue
            artifact = self._read(refs, key)
            document = _artifact_dict(artifact)
            artifacts[key] = document
            digest_map[key] = refs[key]
        cited_excerpts: dict[str, str] = {}
        if include_cited_excerpts and "evidence_set" in refs:
            for evidence in self._agent_context(snapshot).as_dict()["evidence"]:
                excerpt = evidence.get("cited_excerpt")
                if isinstance(excerpt, str):
                    cited_excerpts[evidence["evidence_id"]] = excerpt
        status = self._status_for_snapshot(snapshot)
        bundle = {
            "schema_version": "decision-view.v1",
            "snapshot_sha256": _snapshot_sha(snapshot),
            "generated_at": _timestamp(self.clock()),
            "include_cited_excerpts": include_cited_excerpts,
            "status": {
                key: status[key]
                for key in (
                    "lifecycle_state",
                    "verified",
                    "decision_complete",
                    "ready",
                    "stale_reasons",
                )
            },
            "artifacts": artifacts,
            "digest_map": digest_map,
            "cited_excerpts": cited_excerpts,
        }
        bundle["integrity_sha256"] = digest_json(bundle)
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        temporary = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_json_bytes(bundle))
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return {
            "ok": True,
            "session_id": self.session_id,
            "snapshot_sha256": _snapshot_sha(snapshot),
            "output": str(output),
            "integrity_sha256": bundle["integrity_sha256"],
        }


def load_payload(path: Path) -> Any:
    return read_json_file(path, label=path.name)
