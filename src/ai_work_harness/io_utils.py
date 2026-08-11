from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import HarnessError

LOGICAL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def json_document_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def digest_json(value: Any) -> str:
    return sha256_bytes(json_document_bytes(value))


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, json_document_bytes(value))


def read_json(path: Path, *, label: str | None = None) -> Any:
    display = label or path.name
    if not path.is_file():
        raise HarnessError(
            "ARTIFACT_MISSING",
            f"Required artifact is missing: {display}",
            details={"artifact": display},
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError(
            "INVALID_JSON",
            f"Artifact is not valid UTF-8 JSON: {display}",
            details={"artifact": display, "reason": str(exc)},
        ) from exc


def is_canonical_json_file(path: Path, value: Any) -> bool:
    return path.read_bytes() == json_document_bytes(value)


def validate_logical_id(logical_id: str) -> str:
    candidate = logical_id.strip()
    path = PurePosixPath(candidate)
    if (
        not candidate
        or "\\" in candidate
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or LOGICAL_ID_PATTERN.fullmatch(candidate) is None
    ):
        raise HarnessError(
            "UNSAFE_LOGICAL_ID",
            "logical_id must be a safe relative POSIX path",
            details={"logical_id": logical_id},
        )
    return candidate


def managed_input_path(storage_root: Path, logical_id: str) -> Path:
    safe_id = validate_logical_id(logical_id)
    target = (storage_root / PurePosixPath(safe_id)).resolve()
    try:
        target.relative_to(storage_root.resolve())
    except ValueError as exc:
        raise HarnessError(
            "UNSAFE_LOGICAL_ID",
            "logical_id escapes managed input storage",
            details={"logical_id": logical_id},
        ) from exc
    return target
