from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_work_harness.errors import HarnessError
from ai_work_harness.io_utils import digest_json as digest_v1_json
from ai_work_harness.io_utils import (
    is_canonical_json_file,
    json_document_bytes,
    managed_input_path,
    read_json,
)
from ai_work_harness.io_utils import sha256_bytes as sha256_v1_bytes
from ai_work_harness.project import ProjectPaths, inspect_inputs, project_status
from ai_work_harness.routing import select_route
from ai_work_harness.validation import validate_document

from .canonical import digest_json, sha256_bytes
from .models import ArtifactEnvelope
from .store import DecisionStore

MAX_MIGRATION_SOURCE_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class V1MigrationSource:
    paths: ProjectPaths
    fingerprint: str
    artifacts: tuple[dict[str, Any], ...]
    inputs: tuple[dict[str, Any], ...]
    frame: dict[str, Any] | None


def _migration_media_type(logical_id: str) -> str:
    detected = mimetypes.guess_type(logical_id)[0]
    if detected == "text/markdown":
        return detected
    return "text/plain"


def _invalid(message: str, **details: Any) -> HarnessError:
    return HarnessError(
        "V1_MIGRATION_SOURCE_INVALID",
        message,
        details=details,
    )


def _load_v1_artifact(path: Path, kind: str) -> dict[str, Any]:
    try:
        value = read_json(path, label=path.name)
        validate_document(kind, value)
    except HarnessError as exc:
        raise _invalid(
            f"The v1 {path.name} artifact is invalid",
            artifact=path.name,
            reason_code=exc.code,
        ) from exc
    if not isinstance(value, dict) or not is_canonical_json_file(path, value):
        raise _invalid(
            f"The v1 {path.name} artifact is not canonical JSON",
            artifact=path.name,
        )
    return value


def _artifact_record(
    paths: ProjectPaths,
    path: Path,
    *,
    raw: bytes | None = None,
    document: Any | None = None,
) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise _invalid("The v1 tree contains a missing or symbolic-link artifact", path=str(path))
    captured = path.read_bytes() if raw is None else raw
    if document is not None and captured != json_document_bytes(document):
        raise _invalid(
            "The v1 artifact changed while the migration source was inspected",
            path=str(path),
        )
    return {
        "path": path.relative_to(paths.state).as_posix(),
        "sha256": sha256_v1_bytes(captured),
        "bytes": len(captured),
    }


def _capture_managed_input(path: Path, *, logical_id: str) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise _invalid(
            "The v1 tree contains a missing or symbolic-link managed input",
            logical_id=logical_id,
            path=str(path),
        )
    try:
        before_bytes = path.stat().st_size
    except OSError as exc:
        raise _invalid(
            "A v1 managed input cannot be inspected",
            logical_id=logical_id,
            path=str(path),
        ) from exc
    if before_bytes > MAX_MIGRATION_SOURCE_BYTES:
        raise _invalid(
            "A v1 managed input exceeds the v2 Phase 1 source limit",
            logical_id=logical_id,
            bytes=before_bytes,
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _invalid(
            "A v1 managed input cannot be read",
            logical_id=logical_id,
            path=str(path),
        ) from exc
    if len(raw) > MAX_MIGRATION_SOURCE_BYTES:
        raise _invalid(
            "A v1 managed input exceeds the v2 Phase 1 source limit",
            logical_id=logical_id,
            bytes=len(raw),
        )
    try:
        after_bytes = path.stat().st_size
    except OSError as exc:
        raise _invalid(
            "A v1 managed input changed while the migration source was inspected",
            logical_id=logical_id,
            path=str(path),
        ) from exc
    if after_bytes > MAX_MIGRATION_SOURCE_BYTES:
        raise _invalid(
            "A v1 managed input exceeds the v2 Phase 1 source limit",
            logical_id=logical_id,
            bytes=after_bytes,
        )
    if before_bytes != after_bytes or after_bytes != len(raw):
        raise _invalid(
            "A v1 managed input changed while the migration source was inspected",
            logical_id=logical_id,
            before_bytes=before_bytes,
            after_bytes=after_bytes,
            captured_bytes=len(raw),
        )
    return raw


def inspect_v1_migration_source(root: Path) -> V1MigrationSource:
    paths = ProjectPaths.from_root(root)
    try:
        paths.require_initialized()
        input_report = inspect_inputs(paths)
    except HarnessError as exc:
        raise _invalid("The v1 input manifest cannot be inspected", reason_code=exc.code) from exc
    if not input_report["ok"] or not input_report["canonical"]:
        raise _invalid(
            "The v1 managed inputs are incomplete or changed",
            errors=input_report["errors"],
            canonical=input_report["canonical"],
        )

    manifest = _load_v1_artifact(paths.manifest, "input-manifest")
    actual_input_sha = input_report["actual_manifest_sha256"]
    artifacts = [_artifact_record(paths, paths.manifest, document=manifest)]
    inputs: list[dict[str, Any]] = []
    captured_entries: list[dict[str, Any]] = []
    for entry in manifest["inputs"]:
        path = managed_input_path(paths.inputs, entry["logical_id"])
        raw = _capture_managed_input(path, logical_id=entry["logical_id"])
        record = _artifact_record(paths, path, raw=raw)
        artifacts.append(record)
        if record["bytes"] != entry["bytes"] or record["sha256"] != entry["sha256"]:
            raise _invalid(
                "A v1 managed input changed while the migration source was inspected",
                logical_id=entry["logical_id"],
            )
        try:
            raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise _invalid(
                "A v1 managed input is not supported UTF-8 text",
                logical_id=entry["logical_id"],
            ) from exc
        inputs.append({**entry, "raw": raw})
        captured_entries.append(
            {
                "logical_id": entry["logical_id"],
                "bytes": len(raw),
                "sha256": record["sha256"],
            }
        )
    captured_input_sha = digest_v1_json(
        {
            "schema_version": 1,
            "inputs": sorted(captured_entries, key=lambda item: item["logical_id"]),
        }
    )
    if captured_input_sha != actual_input_sha:
        raise _invalid("The v1 input tree changed while it was inspected")

    frame: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    route: dict[str, Any] | None = None
    if paths.frame.exists():
        frame = _load_v1_artifact(paths.frame, "frame")
        frame_record = _artifact_record(paths, paths.frame, document=frame)
        artifacts.append(frame_record)
        if frame["input_manifest_sha256"] != actual_input_sha:
            raise _invalid("The v1 frame input binding is stale")
    if paths.profile.exists():
        if frame is None:
            raise _invalid("The v1 task profile exists without a frame")
        profile = _load_v1_artifact(paths.profile, "task-profile")
        profile_record = _artifact_record(paths, paths.profile, document=profile)
        artifacts.append(profile_record)
        if frame["user_confirmed"] is not True:
            raise _invalid("The v1 task profile depends on an unconfirmed frame")
        if profile["input_manifest_sha256"] != actual_input_sha:
            raise _invalid("The v1 task profile input binding is stale")
        if profile["frame_sha256"] != frame_record["sha256"]:
            raise _invalid("The v1 task profile frame binding is stale")
    if paths.route.exists():
        if profile is None:
            raise _invalid("The v1 route exists without a task profile")
        route = _load_v1_artifact(paths.route, "route")
        artifacts.append(_artifact_record(paths, paths.route, document=route))
        expected = {
            "input_manifest_sha256": actual_input_sha,
            "frame_sha256": frame_record["sha256"],
            "profile_sha256": profile_record["sha256"],
        }
        if any(route[field] != value for field, value in expected.items()):
            raise _invalid("The v1 route binding is stale")
        if route["route"] != select_route(profile):
            raise _invalid("The v1 route is not the deterministic route for its profile")

    approval_paths = sorted(paths.approvals.glob("*.json"))
    if approval_paths and route is None:
        raise _invalid("The v1 tree contains approvals without a complete route")
    for approval_path in approval_paths:
        approval = _load_v1_artifact(approval_path, "approval")
        artifacts.append(_artifact_record(paths, approval_path, document=approval))
    if approval_paths:
        approval_status = project_status(root)["approvals"]
        invalid = [item for item in approval_status if not item["valid"]]
        if invalid:
            raise _invalid("The v1 tree contains a stale or invalid approval", approvals=invalid)

    artifacts.sort(key=lambda item: item["path"])
    fingerprint = digest_json({"v1_artifacts": artifacts})
    return V1MigrationSource(
        paths=paths,
        fingerprint=fingerprint,
        artifacts=tuple(artifacts),
        inputs=tuple(inputs),
        frame=frame,
    )


def _existing_migration(store: DecisionStore, fingerprint: str) -> dict[str, Any] | None:
    try:
        current = store.get_current()
    except HarnessError as exc:
        if exc.code == "SESSION_NOT_FOUND":
            return None
        raise
    refs = dict(current.refs)
    if not refs and current.generation == 0:
        return {"resume_parent": current.snapshot_sha256}
    allowed = {"source_manifest", "decision_frame", "migration_report"}
    if set(refs) - allowed or "migration_report" not in refs or current.generation != 1:
        raise HarnessError(
            "V1_MIGRATION_TARGET_CONFLICT",
            "The target v2 session has progressed beyond its migration result",
            details={"generation": current.generation, "refs": sorted(refs)},
            exit_code=3,
        )
    report = store.read_artifact(refs["migration_report"])
    if report.payload["v1_fingerprint"] != fingerprint:
        raise HarnessError(
            "V1_MIGRATION_FINGERPRINT_CHANGED",
            "The v1 tree changed after this target session was migrated",
            exit_code=3,
        )
    return {
        "ok": True,
        "session_id": store.session_id,
        "generation": current.generation,
        "snapshot_sha256": current.snapshot_sha256,
        "already_migrated": True,
        "v1_fingerprint": fingerprint,
    }


def _apply_migration(
    source: V1MigrationSource,
    target: DecisionStore,
    session_id: str,
    *,
    parent: str,
) -> dict[str, Any]:
    with target.mutation(parent):
        migrated_sources: list[dict[str, Any]] = []
        source_manifest_items: list[dict[str, Any]] = []
        for index, item in enumerate(source.inputs, start=1):
            raw = item["raw"]
            blob_sha = sha256_bytes(raw)
            source_id = f"source-{index:03d}"
            migrated_sources.append(
                {
                    "source_id": source_id,
                    "v1_logical_id": item["logical_id"],
                    "blob_sha256": blob_sha,
                    "bytes": len(raw),
                }
            )
            source_manifest_items.append(
                {
                    "source_id": source_id,
                    "media_type": _migration_media_type(item["logical_id"]),
                    "bytes": len(raw),
                    "blob_sha256": blob_sha,
                }
            )

        refs: dict[str, str] = {}
        manifest_artifact: ArtifactEnvelope | None = None
        if source_manifest_items:
            manifest_artifact = ArtifactEnvelope.create(
                artifact_type="source-manifest",
                session_id=session_id,
                producer={"kind": "migration"},
                payload={"sources": source_manifest_items},
            )
            refs["source_manifest"] = digest_json(manifest_artifact.to_document())

        frame_migrated = bool(source_manifest_items and source.frame is not None)
        frame_artifact: ArtifactEnvelope | None = None
        if frame_migrated:
            assert source.frame is not None
            frame_artifact = ArtifactEnvelope.create(
                artifact_type="decision-frame",
                session_id=session_id,
                producer={"kind": "migration"},
                parents={"source_manifest": refs["source_manifest"]},
                payload={
                    "user_statement_verbatim": source.frame["user_statement"],
                    "ai_initial_interpretation": source.frame["ai_interpretation"],
                    "business_user": None,
                    "blocked_decision": None,
                    "problem_statement": None,
                    "scope_in": [],
                    "scope_out": [],
                    "assumptions": [],
                    "open_questions": ["Reconfirm the migrated v1 frame under the v2 contract."],
                },
            )
            refs["decision_frame"] = digest_json(frame_artifact.to_document())

        report_parents = dict(refs)
        report_artifact = ArtifactEnvelope.create(
            artifact_type="migration-report",
            session_id=session_id,
            producer={"kind": "core"},
            parents=report_parents,
            payload={
                "v1_fingerprint": source.fingerprint,
                "v1_artifacts": list(source.artifacts),
                "migrated_sources": migrated_sources,
                "frame_migrated": frame_migrated,
                "confirmations_promoted": False,
                "approvals_promoted": False,
            },
        )
        refs["migration_report"] = digest_json(report_artifact.to_document())

        for item in source.inputs:
            raw = item["raw"]
            if target.put_blob(raw) != sha256_bytes(raw):
                raise AssertionError("migrated source digest changed during persistence")
        if (
            manifest_artifact is not None
            and target.put_artifact(manifest_artifact) != refs["source_manifest"]
        ):
            raise AssertionError("source manifest digest changed during persistence")
        if (
            frame_artifact is not None
            and target.put_artifact(frame_artifact) != refs["decision_frame"]
        ):
            raise AssertionError("migrated frame digest changed during persistence")
        if target.put_artifact(report_artifact) != refs["migration_report"]:
            raise AssertionError("migration report digest changed during persistence")
        snapshot = target.commit(
            expected_parent=parent,
            refs=refs,
            operation="migrate-v1",
        )
    return {
        "ok": True,
        "session_id": session_id,
        "generation": snapshot.generation,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "already_migrated": False,
        "v1_fingerprint": source.fingerprint,
        "sources_migrated": len(migrated_sources),
        "frame_migrated": frame_migrated,
        "confirmations_promoted": False,
        "approvals_promoted": False,
    }


def migrate_v1(
    root: Path,
    session_id: str,
    *,
    store: DecisionStore | None = None,
) -> dict[str, Any]:
    source = inspect_v1_migration_source(root)
    target = store or DecisionStore(root, session_id)
    existing = _existing_migration(target, source.fingerprint)
    if existing is not None and "already_migrated" in existing:
        return existing
    if existing is None:
        try:
            parent = target.initialize().snapshot_sha256
        except HarnessError as exc:
            if exc.code != "SESSION_EXISTS":
                raise
            raced = _existing_migration(target, source.fingerprint)
            if raced is None:
                raise
            if "already_migrated" in raced:
                return raced
            parent = raced["resume_parent"]
    else:
        parent = existing["resume_parent"]

    try:
        return _apply_migration(source, target, session_id, parent=parent)
    except HarnessError as exc:
        if exc.code != "WRITE_CONFLICT":
            raise
        converged = _existing_migration(target, source.fingerprint)
        if converged is not None and "already_migrated" in converged:
            return converged
        raise
