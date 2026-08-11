from __future__ import annotations

import json
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator

from .errors import HarnessError

SCHEMA_FILES = {
    "input-manifest": "input-manifest.schema.json",
    "frame": "frame.schema.json",
    "task-profile": "task-profile.schema.json",
    "route": "route.schema.json",
    "approval": "approval.schema.json",
}


def load_schema(kind: str) -> dict[str, Any]:
    try:
        filename = SCHEMA_FILES[kind]
    except KeyError as exc:
        raise HarnessError(
            "UNKNOWN_SCHEMA",
            f"Unknown schema kind: {kind}",
            details={"kind": kind},
        ) from exc
    resource = resources.files("ai_work_harness.schemas.v1").joinpath(filename)
    schema = json.loads(resource.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _json_path(parts: list[Any]) -> str:
    result = "$"
    for part in parts:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


def schema_errors(kind: str, value: Any) -> list[dict[str, str]]:
    validator = Draft202012Validator(load_schema(kind))
    errors = sorted(validator.iter_errors(value), key=lambda item: list(item.absolute_path))
    return [
        {
            "path": _json_path(list(error.absolute_path)),
            "message": error.message,
        }
        for error in errors
    ]


def validate_document(kind: str, value: Any) -> None:
    errors = schema_errors(kind, value)
    if errors:
        raise HarnessError(
            "SCHEMA_VALIDATION_FAILED",
            f"Artifact does not satisfy the {kind} v1 schema",
            details={"schema": kind, "errors": errors},
        )


def validate_all_schemas() -> list[str]:
    loaded: list[str] = []
    for kind in SCHEMA_FILES:
        load_schema(kind)
        loaded.append(kind)
    return loaded
