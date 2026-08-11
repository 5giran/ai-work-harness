from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from types import MappingProxyType
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from ai_work_harness.errors import HarnessError

from .canonical import parse_json_bytes, validate_ijson

SCHEMA_VERSION = "2.0"
SAFE_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
REF_KEY_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,61}[a-z0-9])?$")
OPERATION_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
EVENT_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

V2_SCHEMA_FILES = {
    "artifact-envelope": "artifact-envelope.schema.json",
    "snapshot": "snapshot.schema.json",
    "current-pointer": "current-pointer.schema.json",
    "decision-view-v1": "decision-view.v1.schema.json",
}

PUBLIC_ARTIFACT_TYPES = frozenset(
    {
        "agent-run",
        "approval-challenge",
        "candidate-set",
        "comparison",
        "criteria-set",
        "decision-bundle",
        "decision-frame",
        "evaluation-review-set",
        "evaluation-set",
        "evidence-set",
        "final-decision",
        "human-approval",
        "human-confirmation",
        "migration-report",
        "recommendation",
        "source-manifest",
    }
)
REF_ARTIFACT_TYPES = MappingProxyType(
    {
        "agent_consent": "human-confirmation",
        "agent_run": "agent-run",
        "approval_challenge": "approval-challenge",
        "candidate_confirmation": "human-confirmation",
        "candidate_set": "candidate-set",
        "comparison": "comparison",
        "criteria_confirmation": "human-confirmation",
        "criteria_set": "criteria-set",
        "decision_bundle": "decision-bundle",
        "decision_frame": "decision-frame",
        "evaluation_review_set": "evaluation-review-set",
        "evaluation_set": "evaluation-set",
        "evidence_set": "evidence-set",
        "final_decision": "final-decision",
        "frame_confirmation": "human-confirmation",
        "human_approval": "human-approval",
        "migration_report": "migration-report",
        "recommendation": "recommendation",
        "source_manifest": "source-manifest",
    }
)
PAYLOAD_SCHEMA_SUFFIX = ".payload.schema.json"


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdSource(Protocol):
    def new_id(self) -> str: ...


@dataclass(frozen=True)
class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class UUID4IdSource:
    def new_id(self) -> str:
        return str(uuid.uuid4())


def validate_safe_id(value: str, *, label: str = "id") -> str:
    if not isinstance(value, str) or SAFE_ID_PATTERN.fullmatch(value) is None:
        raise HarnessError(
            "UNSAFE_ID",
            f"{label} must be a lowercase, path-safe identifier",
            details={"field": label, "value": value},
        )
    return value


def validate_digest(value: str, *, label: str = "sha256") -> str:
    if not isinstance(value, str) or DIGEST_PATTERN.fullmatch(value) is None:
        raise HarnessError(
            "INVALID_DIGEST",
            f"{label} must be a lowercase SHA-256 digest",
            details={"field": label, "value": value},
        )
    return value


def validate_ref_key(value: str, *, label: str = "ref") -> str:
    if not isinstance(value, str) or REF_KEY_PATTERN.fullmatch(value) is None:
        raise HarnessError(
            "UNSAFE_REF_KEY",
            f"{label} must be a path-safe artifact reference key",
            details={"field": label, "value": value},
        )
    return value


def validate_event_id(value: str) -> str:
    if not isinstance(value, str) or EVENT_ID_PATTERN.fullmatch(value) is None:
        raise HarnessError(
            "INVALID_EVENT_ID",
            "event id must be a lowercase UUID4",
            details={"event_id": value},
        )
    return value


def format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HarnessError("INVALID_CLOCK", "Clock must return a timezone-aware datetime")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(child) for key, child in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


def discover_v2_payload_schemas() -> dict[str, str]:
    package = resources.files("ai_work_harness.schemas.v2")
    discovered = {
        item.name.removesuffix(PAYLOAD_SCHEMA_SUFFIX): item.name
        for item in package.iterdir()
        if item.is_file() and item.name.endswith(PAYLOAD_SCHEMA_SUFFIX)
    }
    missing = sorted(PUBLIC_ARTIFACT_TYPES - set(discovered))
    undeclared = sorted(set(discovered) - PUBLIC_ARTIFACT_TYPES)
    if missing or undeclared:
        raise HarnessError(
            "SCHEMA_REGISTRY_MISMATCH",
            "Packaged v2 payload schemas do not match the public artifact registry",
            details={"missing": missing, "undeclared": undeclared},
        )
    return dict(sorted(discovered.items()))


def load_v2_schema(kind: str) -> dict[str, Any]:
    if kind.startswith("payload:"):
        artifact_type = kind.removeprefix("payload:")
        try:
            filename = discover_v2_payload_schemas()[artifact_type]
        except KeyError as exc:
            raise HarnessError(
                "UNKNOWN_ARTIFACT_TYPE",
                f"Unknown v2 artifact type: {artifact_type}",
                details={"artifact_type": artifact_type},
            ) from exc
    else:
        try:
            filename = V2_SCHEMA_FILES[kind]
        except KeyError as exc:
            raise HarnessError(
                "UNKNOWN_SCHEMA",
                f"Unknown v2 schema kind: {kind}",
                details={"kind": kind},
            ) from exc
    resource = resources.files("ai_work_harness.schemas.v2").joinpath(filename)
    document = parse_json_bytes(resource.read_bytes(), label=filename)
    Draft202012Validator.check_schema(document)
    return document


def _json_path(parts: list[Any]) -> str:
    result = "$"
    for part in parts:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


def validate_v2_document(kind: str, value: Any) -> None:
    validate_ijson(value)
    validator = Draft202012Validator(load_v2_schema(kind))
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    if errors:
        raise HarnessError(
            "SCHEMA_VALIDATION_FAILED",
            f"Artifact does not satisfy the {kind} v2 schema",
            details={
                "schema": kind,
                "errors": [
                    {
                        "path": _json_path(list(error.absolute_path)),
                        "message": error.message,
                    }
                    for error in errors
                ],
            },
        )


def validate_all_v2_schemas() -> list[str]:
    kinds = [*V2_SCHEMA_FILES, *(f"payload:{item}" for item in discover_v2_payload_schemas())]
    for kind in kinds:
        load_v2_schema(kind)
    return kinds


def validate_artifact_payload(artifact_type: str, payload: Any) -> None:
    validate_v2_document(f"payload:{artifact_type}", payload)


@dataclass(frozen=True)
class ArtifactEnvelope:
    artifact_type: str
    session_id: str
    producer: Mapping[str, Any]
    parents: Mapping[str, str]
    payload: Mapping[str, Any]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        document = self.to_document()
        validate_v2_document("artifact-envelope", document)
        validate_artifact_payload(self.artifact_type, document["payload"])
        object.__setattr__(self, "parents", _freeze_json(document["parents"]))
        object.__setattr__(self, "producer", _freeze_json(document["producer"]))
        object.__setattr__(self, "payload", _freeze_json(document["payload"]))

    @classmethod
    def create(
        cls,
        *,
        artifact_type: str,
        session_id: str,
        producer: Mapping[str, Any],
        parents: Mapping[str, str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> ArtifactEnvelope:
        return cls(
            artifact_type=artifact_type,
            session_id=session_id,
            producer=producer,
            parents=parents or {},
            payload=payload or {},
        )

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> ArtifactEnvelope:
        document = _thaw_json(value)
        validate_v2_document("artifact-envelope", document)
        return cls(
            schema_version=document["schema_version"],
            artifact_type=document["artifact_type"],
            session_id=document["session_id"],
            producer=document["producer"],
            parents=document["parents"],
            payload=document["payload"],
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": self.artifact_type,
            "session_id": self.session_id,
            "producer": _thaw_json(self.producer),
            "parents": _thaw_json(self.parents),
            "payload": _thaw_json(self.payload),
        }


@dataclass(frozen=True)
class OperationRecord:
    name: str
    idempotency_key: str | None = None
    request_sha256: str | None = None

    def __post_init__(self) -> None:
        if OPERATION_NAME_PATTERN.fullmatch(self.name) is None:
            raise HarnessError(
                "INVALID_OPERATION_NAME",
                "operation.name contains unsupported characters",
                details={"operation": self.name},
            )
        paired = self.idempotency_key is not None and self.request_sha256 is not None
        if (self.idempotency_key is None) != (self.request_sha256 is None):
            raise HarnessError(
                "INVALID_IDEMPOTENCY_RECORD",
                "idempotency_key and request_sha256 must be provided together",
            )
        if paired:
            if IDEMPOTENCY_KEY_PATTERN.fullmatch(self.idempotency_key or "") is None:
                raise HarnessError(
                    "INVALID_IDEMPOTENCY_KEY",
                    "idempotency key contains unsupported characters",
                    details={"idempotency_key": self.idempotency_key},
                )
            validate_digest(self.request_sha256 or "", label="operation.request_sha256")

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> OperationRecord:
        return cls(
            name=value["name"],
            idempotency_key=value.get("idempotency_key"),
            request_sha256=value.get("request_sha256"),
        )

    def to_document(self) -> dict[str, Any]:
        result: dict[str, Any] = {"name": self.name}
        if self.idempotency_key is not None:
            result["idempotency_key"] = self.idempotency_key
            result["request_sha256"] = self.request_sha256
        return result


@dataclass(frozen=True)
class SnapshotRecord:
    session_id: str
    generation: int
    parent_snapshot_sha256: str | None
    created_at: str
    refs: Mapping[str, str]
    operation: OperationRecord | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        document = self.to_document()
        validate_v2_document("snapshot", document)
        object.__setattr__(self, "refs", _freeze_json(document["refs"]))

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> SnapshotRecord:
        document = _thaw_json(value)
        validate_v2_document("snapshot", document)
        operation = document.get("operation")
        return cls(
            schema_version=document["schema_version"],
            session_id=document["session_id"],
            generation=document["generation"],
            parent_snapshot_sha256=document["parent_snapshot_sha256"],
            created_at=document["created_at"],
            refs=document["refs"],
            operation=OperationRecord.from_document(operation) if operation else None,
        )

    def to_document(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "generation": self.generation,
            "parent_snapshot_sha256": self.parent_snapshot_sha256,
            "created_at": self.created_at,
            "refs": _thaw_json(self.refs),
        }
        if self.operation is not None:
            result["operation"] = self.operation.to_document()
        return result


@dataclass(frozen=True)
class CurrentPointer:
    session_id: str
    generation: int
    snapshot_sha256: str
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_v2_document("current-pointer", self.to_document())

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> CurrentPointer:
        document = _thaw_json(value)
        validate_v2_document("current-pointer", document)
        return cls(
            schema_version=document["schema_version"],
            session_id=document["session_id"],
            generation=document["generation"],
            snapshot_sha256=document["snapshot_sha256"],
        )

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "generation": self.generation,
            "snapshot_sha256": self.snapshot_sha256,
        }


@dataclass(frozen=True)
class StoredSnapshot:
    sha256: str
    snapshot: SnapshotRecord

    @property
    def snapshot_sha256(self) -> str:
        return self.sha256

    @property
    def generation(self) -> int:
        return self.snapshot.generation

    @property
    def refs(self) -> Mapping[str, str]:
        return self.snapshot.refs

    @property
    def parent_snapshot_sha256(self) -> str | None:
        return self.snapshot.parent_snapshot_sha256


@dataclass(frozen=True)
class IntegrityIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True)
class VerificationReport:
    ok: bool
    snapshot_sha256: str | None
    generation: int | None
    issues: tuple[IntegrityIssue, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "snapshot_sha256": self.snapshot_sha256,
            "generation": self.generation,
            "issues": [issue.__dict__ for issue in self.issues],
        }


@dataclass(frozen=True)
class DoctorReport:
    ok: bool
    current_snapshot_sha256: str | None
    reachable_snapshots: tuple[str, ...]
    orphan_snapshots: tuple[str, ...]
    referenced_objects: tuple[str, ...]
    orphan_objects: tuple[str, ...]
    issues: tuple[IntegrityIssue, ...]

    def as_dict(self) -> dict[str, Any]:
        return json.loads(
            json.dumps(
                {
                    "ok": self.ok,
                    "current_snapshot_sha256": self.current_snapshot_sha256,
                    "reachable_snapshots": self.reachable_snapshots,
                    "orphan_snapshots": self.orphan_snapshots,
                    "referenced_objects": self.referenced_objects,
                    "orphan_objects": self.orphan_objects,
                    "issues": [issue.__dict__ for issue in self.issues],
                }
            )
        )
