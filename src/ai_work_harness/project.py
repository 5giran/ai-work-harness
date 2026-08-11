from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import HarnessError
from .io_utils import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    digest_json,
    is_canonical_json_file,
    managed_input_path,
    read_json,
    sha256_bytes,
    sha256_file,
    sha256_text,
    validate_logical_id,
)
from .routing import select_route
from .validation import schema_errors, validate_all_schemas, validate_document

STATE_DIRECTORY = ".ai-work-harness"


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    state: Path
    inputs: Path
    approvals: Path
    manifest: Path
    frame: Path
    profile: Path
    route: Path

    @classmethod
    def from_root(cls, root: Path) -> ProjectPaths:
        resolved = root.resolve()
        state = resolved / STATE_DIRECTORY
        return cls(
            root=resolved,
            state=state,
            inputs=state / "inputs",
            approvals=state / "approvals",
            manifest=state / "input-manifest.json",
            frame=state / "frame.json",
            profile=state / "task-profile.json",
            route=state / "route.json",
        )

    def require_initialized(self) -> None:
        if not self.manifest.is_file():
            raise HarnessError(
                "NOT_INITIALIZED",
                "Run init before using this project",
                details={"state_directory": STATE_DIRECTORY},
            )


def _require_nonblank(value: str, field: str) -> str:
    if not value.strip():
        raise HarnessError(
            "EMPTY_VALUE",
            f"{field} must not be blank",
            details={"field": field},
        )
    return value


def _load_validated(path: Path, kind: str) -> dict[str, Any]:
    value = read_json(path, label=path.name)
    validate_document(kind, value)
    if not isinstance(value, dict):
        raise HarnessError("INVALID_ARTIFACT", f"{path.name} must contain an object")
    return value


def init_project(root: Path) -> dict[str, Any]:
    paths = ProjectPaths.from_root(root)
    paths.root.mkdir(parents=True, exist_ok=True)
    if paths.state.exists() and not paths.manifest.is_file():
        raise HarnessError(
            "PARTIAL_INITIALIZATION",
            "State directory exists without an input manifest",
            details={"state_directory": STATE_DIRECTORY},
        )
    paths.inputs.mkdir(parents=True, exist_ok=True)
    paths.approvals.mkdir(parents=True, exist_ok=True)
    if paths.manifest.is_file():
        manifest = _load_validated(paths.manifest, "input-manifest")
        return {
            "ok": True,
            "already_initialized": True,
            "state_directory": STATE_DIRECTORY,
            "inputs": len(manifest["inputs"]),
        }
    manifest = {"schema_version": 1, "inputs": []}
    validate_document("input-manifest", manifest)
    atomic_write_json(paths.manifest, manifest)
    return {
        "ok": True,
        "already_initialized": False,
        "state_directory": STATE_DIRECTORY,
        "inputs": 0,
    }


def _load_manifest(paths: ProjectPaths) -> dict[str, Any]:
    paths.require_initialized()
    return _load_validated(paths.manifest, "input-manifest")


def inspect_inputs(paths: ProjectPaths) -> dict[str, Any]:
    manifest = _load_manifest(paths)
    errors: list[dict[str, str]] = []
    actual_entries: list[dict[str, Any]] = []
    expected_ids = {entry["logical_id"] for entry in manifest["inputs"]}

    for entry in sorted(manifest["inputs"], key=lambda item: item["logical_id"]):
        logical_id = entry["logical_id"]
        target = managed_input_path(paths.inputs, logical_id)
        if not target.is_file() or target.is_symlink():
            errors.append({"code": "INPUT_MISSING", "logical_id": logical_id})
            continue
        current = {
            "logical_id": logical_id,
            "bytes": target.stat().st_size,
            "sha256": sha256_file(target),
        }
        actual_entries.append(current)
        if current["bytes"] != entry["bytes"] or current["sha256"] != entry["sha256"]:
            errors.append({"code": "INPUT_CHANGED", "logical_id": logical_id})

    if paths.inputs.is_dir():
        for candidate in sorted(paths.inputs.rglob("*")):
            if not candidate.is_file() and not candidate.is_symlink():
                continue
            try:
                relative = candidate.relative_to(paths.inputs).as_posix()
            except ValueError:
                relative = "invalid"
            if relative not in expected_ids:
                errors.append({"code": "UNREGISTERED_INPUT", "logical_id": relative})

    actual_manifest = {
        "schema_version": 1,
        "inputs": sorted(actual_entries, key=lambda item: item["logical_id"]),
    }
    actual_sha256 = (
        digest_json(actual_manifest) if len(actual_entries) == len(expected_ids) else None
    )
    return {
        "ok": not errors,
        "errors": errors,
        "manifest_sha256": sha256_file(paths.manifest),
        "actual_manifest_sha256": actual_sha256,
        "canonical": is_canonical_json_file(paths.manifest, manifest),
        "entries": len(manifest["inputs"]),
    }


def _require_inputs_intact(paths: ProjectPaths) -> str:
    report = inspect_inputs(paths)
    if not report["ok"] or not report["canonical"]:
        raise HarnessError(
            "INPUT_INTEGRITY_FAILED",
            "Managed inputs do not match the canonical input manifest",
            details={"errors": report["errors"], "canonical": report["canonical"]},
        )
    actual = report["actual_manifest_sha256"]
    if not isinstance(actual, str):
        raise HarnessError("INPUT_INTEGRITY_FAILED", "Input manifest could not be recomputed")
    return actual


def capture_input(root: Path, *, logical_id: str, source: Path) -> dict[str, Any]:
    paths = ProjectPaths.from_root(root)
    manifest = _load_manifest(paths)
    safe_id = validate_logical_id(logical_id)
    source_path = source.resolve()
    if source.is_symlink() or not source_path.is_file():
        raise HarnessError(
            "INVALID_SOURCE",
            "Source must be a regular, non-symlink file",
            details={"logical_id": safe_id},
        )
    target = managed_input_path(paths.inputs, safe_id)
    try:
        source_path.relative_to(paths.inputs.resolve())
    except ValueError:
        pass
    else:
        raise HarnessError(
            "SOURCE_IN_MANAGED_STORAGE",
            "Capture requires a source outside managed input storage",
            details={"logical_id": safe_id},
        )
    content = source_path.read_bytes()
    entry = {
        "logical_id": safe_id,
        "bytes": len(content),
        "sha256": sha256_bytes(content),
    }
    atomic_write_bytes(target, content)
    by_id = {item["logical_id"]: item for item in manifest["inputs"]}
    by_id[safe_id] = entry
    updated = {
        "schema_version": 1,
        "inputs": [by_id[key] for key in sorted(by_id)],
    }
    validate_document("input-manifest", updated)
    atomic_write_json(paths.manifest, updated)
    return {"ok": True, "input": entry}


def write_frame(
    root: Path,
    *,
    user_statement: str,
    ai_interpretation: str,
    user_confirmed: bool,
    agreed_frame: str,
) -> dict[str, Any]:
    paths = ProjectPaths.from_root(root)
    input_sha256 = _require_inputs_intact(paths)
    frame = {
        "schema_version": 1,
        "input_manifest_sha256": input_sha256,
        "user_statement": _require_nonblank(user_statement, "user_statement"),
        "ai_interpretation": _require_nonblank(ai_interpretation, "ai_interpretation"),
        "user_confirmed": user_confirmed,
        "agreed_frame": _require_nonblank(agreed_frame, "agreed_frame"),
    }
    validate_document("frame", frame)
    atomic_write_json(paths.frame, frame)
    return {
        "ok": True,
        "artifact": "frame.json",
        "sha256": sha256_file(paths.frame),
        "user_confirmed": user_confirmed,
    }


def _load_current_frame(paths: ProjectPaths, input_sha256: str) -> dict[str, Any]:
    frame = _load_validated(paths.frame, "frame")
    if not is_canonical_json_file(paths.frame, frame):
        raise HarnessError("NONCANONICAL_ARTIFACT", "frame.json is not canonical JSON")
    if frame["input_manifest_sha256"] != input_sha256:
        raise HarnessError(
            "FRAME_INPUT_STALE",
            "Frame is bound to a different input manifest",
        )
    return frame


def create_route(
    root: Path,
    *,
    input_source: str,
    modalities: list[str],
    capabilities: list[str],
    objective: str,
) -> dict[str, Any]:
    paths = ProjectPaths.from_root(root)
    input_sha256 = _require_inputs_intact(paths)
    frame = _load_current_frame(paths, input_sha256)
    if frame["user_confirmed"] is not True:
        raise HarnessError(
            "FRAME_UNCONFIRMED",
            "The user must confirm the frame before routing",
        )
    frame_sha256 = sha256_file(paths.frame)
    profile = {
        "schema_version": 1,
        "input_manifest_sha256": input_sha256,
        "frame_sha256": frame_sha256,
        "input_source": _require_nonblank(input_source, "input_source"),
        "modalities": sorted(modalities),
        "capabilities": sorted(capabilities),
        "objective": _require_nonblank(objective, "objective"),
    }
    validate_document("task-profile", profile)
    selected = select_route(profile)
    profile_sha256 = digest_json(profile)
    route = {
        "schema_version": 1,
        "input_manifest_sha256": input_sha256,
        "frame_sha256": frame_sha256,
        "profile_sha256": profile_sha256,
        "route": selected,
    }
    validate_document("route", route)
    atomic_write_json(paths.profile, profile)
    atomic_write_json(paths.route, route)
    return {
        "ok": True,
        "artifact": "route.json",
        "route": selected,
        "sha256": sha256_file(paths.route),
    }


def _load_current_chain(paths: ProjectPaths) -> dict[str, Any]:
    input_sha256 = _require_inputs_intact(paths)
    frame = _load_current_frame(paths, input_sha256)
    if frame["user_confirmed"] is not True:
        raise HarnessError("FRAME_UNCONFIRMED", "The current frame is not user-confirmed")
    frame_sha256 = sha256_file(paths.frame)

    profile = _load_validated(paths.profile, "task-profile")
    if not is_canonical_json_file(paths.profile, profile):
        raise HarnessError("NONCANONICAL_ARTIFACT", "task-profile.json is not canonical JSON")
    if profile["input_manifest_sha256"] != input_sha256:
        raise HarnessError("PROFILE_INPUT_STALE", "Task profile input binding is stale")
    if profile["frame_sha256"] != frame_sha256:
        raise HarnessError("PROFILE_FRAME_STALE", "Task profile frame binding is stale")
    profile_sha256 = sha256_file(paths.profile)

    route = _load_validated(paths.route, "route")
    if not is_canonical_json_file(paths.route, route):
        raise HarnessError("NONCANONICAL_ARTIFACT", "route.json is not canonical JSON")
    expected_route = select_route(profile)
    if route["route"] != expected_route:
        raise HarnessError("ROUTE_MISMATCH", "Route does not match the deterministic router")
    expected_bindings = {
        "input_manifest_sha256": input_sha256,
        "frame_sha256": frame_sha256,
        "profile_sha256": profile_sha256,
    }
    mismatches = {
        field: {"expected": expected, "actual": route[field]}
        for field, expected in expected_bindings.items()
        if route[field] != expected
    }
    if mismatches:
        raise HarnessError(
            "ROUTE_BINDING_STALE",
            "Route bindings are stale",
            details={"mismatches": mismatches},
        )
    return {
        "input_manifest_sha256": input_sha256,
        "frame_sha256": frame_sha256,
        "profile_sha256": profile_sha256,
        "route_sha256": sha256_file(paths.route),
        "route": route["route"],
    }


def _binding_material(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": record["schema_version"],
        "input_manifest_sha256": record["input_manifest_sha256"],
        "frame_sha256": record["frame_sha256"],
        "profile_sha256": record["profile_sha256"],
        "route_sha256": record["route_sha256"],
        "decision": record["decision"],
        "reason": record["reason"],
    }


def _next_approval_id(paths: ProjectPaths) -> str:
    numbers: list[int] = []
    for path in paths.approvals.glob("approval-*.json"):
        suffix = path.stem.removeprefix("approval-")
        if suffix.isdigit():
            numbers.append(int(suffix))
    return f"approval-{max(numbers, default=0) + 1:06d}"


def create_approval(root: Path, *, decision: str, reason: str) -> dict[str, Any]:
    paths = ProjectPaths.from_root(root)
    chain = _load_current_chain(paths)
    decision_value = _require_nonblank(decision, "decision")
    reason_value = _require_nonblank(reason, "reason")
    record = {
        "schema_version": 1,
        "approval_id": _next_approval_id(paths),
        **{
            key: chain[key]
            for key in (
                "input_manifest_sha256",
                "frame_sha256",
                "profile_sha256",
                "route_sha256",
            )
        },
        "decision": decision_value,
        "decision_sha256": sha256_text(decision_value),
        "reason": reason_value,
        "reason_sha256": sha256_text(reason_value),
    }
    record["binding_sha256"] = sha256_bytes(canonical_json_bytes(_binding_material(record)))
    validate_document("approval", record)
    destination = paths.approvals / f"{record['approval_id']}.json"
    atomic_write_json(destination, record)
    return {
        "ok": True,
        "approval_id": record["approval_id"],
        "binding_sha256": record["binding_sha256"],
    }


def _current_component_hashes(
    paths: ProjectPaths,
    input_report: dict[str, Any],
) -> dict[str, str | None]:
    def existing_hash(path: Path) -> str | None:
        return sha256_file(path) if path.is_file() else None

    actual_input = input_report.get("actual_manifest_sha256") if input_report.get("ok") else None
    return {
        "input_manifest_sha256": actual_input if isinstance(actual_input, str) else None,
        "frame_sha256": existing_hash(paths.frame),
        "profile_sha256": existing_hash(paths.profile),
        "route_sha256": existing_hash(paths.route),
    }


def _approval_status(
    record: Any,
    *,
    components: dict[str, str | None],
    fallback_id: str,
) -> dict[str, Any]:
    validation_errors = schema_errors("approval", record)
    if validation_errors or not isinstance(record, dict):
        return {
            "approval_id": fallback_id,
            "valid": False,
            "stale_reasons": ["approval_schema_invalid"],
            "schema_errors": validation_errors,
        }

    reasons: list[str] = []
    component_reasons = {
        "input_manifest_sha256": "input_changed",
        "frame_sha256": "frame_changed",
        "profile_sha256": "profile_changed",
        "route_sha256": "route_changed",
    }
    for field, reason in component_reasons.items():
        if components[field] is None or record[field] != components[field]:
            reasons.append(reason)
    if sha256_text(record["decision"]) != record["decision_sha256"]:
        reasons.append("decision_changed")
    if sha256_text(record["reason"]) != record["reason_sha256"]:
        reasons.append("reason_changed")
    expected_binding = sha256_bytes(canonical_json_bytes(_binding_material(record)))
    if record["binding_sha256"] != expected_binding:
        reasons.append("approval_binding_changed")
    return {
        "approval_id": record["approval_id"],
        "valid": not reasons,
        "stale_reasons": reasons,
    }


def project_status(root: Path) -> dict[str, Any]:
    paths = ProjectPaths.from_root(root)
    paths.require_initialized()
    try:
        input_report = inspect_inputs(paths)
    except HarnessError as exc:
        input_report = {
            "ok": False,
            "canonical": False,
            "errors": [{"code": exc.code, "logical_id": "manifest"}],
            "actual_manifest_sha256": None,
            "entries": 0,
        }
    components = _current_component_hashes(paths, input_report)
    approvals: list[dict[str, Any]] = []
    for path in sorted(paths.approvals.glob("*.json")):
        try:
            record = read_json(path, label=path.name)
        except HarnessError as exc:
            approvals.append(
                {
                    "approval_id": path.stem,
                    "valid": False,
                    "stale_reasons": ["approval_schema_invalid"],
                    "schema_errors": [{"path": "$", "message": exc.message}],
                }
            )
            continue
        approvals.append(_approval_status(record, components=components, fallback_id=path.stem))
    return {
        "ok": True,
        "initialized": True,
        "inputs": {
            "valid": bool(input_report.get("ok") and input_report.get("canonical")),
            "entries": input_report.get("entries", 0),
            "errors": input_report.get("errors", []),
        },
        "artifacts": {
            "frame": paths.frame.is_file(),
            "task_profile": paths.profile.is_file(),
            "route": paths.route.is_file(),
        },
        "approvals": approvals,
        "ready": any(item["valid"] for item in approvals),
    }


def verify_project(root: Path) -> dict[str, Any]:
    paths = ProjectPaths.from_root(root)
    loaded_schemas = validate_all_schemas()
    chain = _load_current_chain(paths)
    for path, kind in (
        (paths.manifest, "input-manifest"),
        (paths.frame, "frame"),
        (paths.profile, "task-profile"),
        (paths.route, "route"),
    ):
        value = _load_validated(path, kind)
        if not is_canonical_json_file(path, value):
            raise HarnessError(
                "NONCANONICAL_ARTIFACT",
                f"{path.name} is not canonical JSON",
            )

    status = project_status(root)
    valid_approvals = [item["approval_id"] for item in status["approvals"] if item["valid"]]
    if not valid_approvals:
        raise HarnessError(
            "NO_VALID_APPROVAL",
            "No approval matches the current input and artifacts",
            details={"approvals": status["approvals"]},
        )
    for path in sorted(paths.approvals.glob("*.json")):
        approval = _load_validated(path, "approval")
        if not is_canonical_json_file(path, approval):
            raise HarnessError(
                "NONCANONICAL_ARTIFACT",
                f"{path.name} is not canonical JSON",
            )
    return {
        "ok": True,
        "route": chain["route"],
        "valid_approvals": valid_approvals,
        "schemas": loaded_schemas,
    }
