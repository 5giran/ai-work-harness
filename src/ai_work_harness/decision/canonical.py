from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ai_work_harness.errors import HarnessError

MAX_SAFE_INTEGER = (1 << 53) - 1

try:  # pragma: no cover - exercised when the optional runtime dependency is installed.
    _rfc8785: Any = importlib.import_module("rfc8785")
except ImportError:  # The built-in encoder covers the deliberately restricted I-JSON subset.
    _rfc8785 = None

CanonicalEncoder = Callable[[Any], bytes]


def _json_path(parts: Sequence[str | int]) -> str:
    result = "$"
    for part in parts:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


def _invalid_ijson(message: str, *, path: Sequence[str | int] = ()) -> HarnessError:
    return HarnessError(
        "INVALID_IJSON",
        message,
        details={"path": _json_path(path)},
    )


def _validate_string(value: str, path: Sequence[str | int]) -> None:
    for character in value:
        if 0xD800 <= ord(character) <= 0xDFFF:
            raise _invalid_ijson("I-JSON strings must contain Unicode scalar values", path=path)


def validate_ijson(value: Any) -> None:
    """Validate the deterministic I-JSON subset used by v2 artifacts.

    Floats are intentionally rejected. Decision artifacts do not need them, and omitting
    IEEE-754 formatting from the contract prevents cross-runtime digest drift.
    """

    active_containers: set[int] = set()

    def visit(item: Any, path: tuple[str | int, ...]) -> None:
        if item is None or isinstance(item, bool):
            return
        if isinstance(item, int):
            if abs(item) > MAX_SAFE_INTEGER:
                raise _invalid_ijson(
                    f"I-JSON integers must be within ±{MAX_SAFE_INTEGER}",
                    path=path,
                )
            return
        if isinstance(item, float):
            raise _invalid_ijson("Floating-point numbers are not supported", path=path)
        if isinstance(item, str):
            _validate_string(item, path)
            return

        container_id = id(item)
        if container_id in active_containers:
            raise _invalid_ijson("Cyclic values cannot be encoded as JSON", path=path)

        if isinstance(item, Mapping):
            active_containers.add(container_id)
            try:
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise _invalid_ijson("JSON object keys must be strings", path=path)
                    _validate_string(key, (*path, key))
                    visit(child, (*path, key))
            finally:
                active_containers.remove(container_id)
            return

        if isinstance(item, Sequence) and not isinstance(item, (bytes, bytearray, memoryview)):
            active_containers.add(container_id)
            try:
                for index, child in enumerate(item):
                    visit(child, (*path, index))
            finally:
                active_containers.remove(container_id)
            return

        raise _invalid_ijson(
            f"Unsupported JSON value type: {type(item).__name__}",
            path=path,
        )

    visit(value, ())


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise HarnessError(
                "DUPLICATE_JSON_KEY",
                "JSON objects must not contain duplicate keys",
                details={"key": key},
            )
        result[key] = value
    return result


def _parse_integer(value: str) -> int:
    parsed = int(value)
    if abs(parsed) > MAX_SAFE_INTEGER:
        raise _invalid_ijson(f"I-JSON integers must be within ±{MAX_SAFE_INTEGER}")
    return parsed


def _reject_float(value: str) -> float:
    raise _invalid_ijson("Floating-point numbers are not supported")


def _reject_constant(value: str) -> None:
    raise _invalid_ijson(f"Non-finite JSON number is not supported: {value}")


def parse_json_bytes(value: bytes | bytearray | memoryview, *, label: str = "JSON") -> Any:
    try:
        text = bytes(value).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HarnessError(
            "INVALID_JSON",
            f"{label} is not valid UTF-8 JSON",
            details={"label": label, "reason": str(exc)},
        ) from exc
    return parse_json_text(text, label=label)


def parse_json_text(value: str, *, label: str = "JSON") -> Any:
    if value.startswith("\ufeff"):
        raise HarnessError(
            "INVALID_JSON",
            f"{label} must not start with a UTF-8 BOM",
            details={"label": label},
        )
    try:
        result = json.loads(
            value,
            object_pairs_hook=_pairs_without_duplicates,
            parse_int=_parse_integer,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except HarnessError:
        raise
    except json.JSONDecodeError as exc:
        raise HarnessError(
            "INVALID_JSON",
            f"{label} is not valid JSON",
            details={
                "label": label,
                "line": exc.lineno,
                "column": exc.colno,
                "reason": exc.msg,
            },
        ) from exc
    validate_ijson(result)
    return result


def read_json_file(path: Path, *, label: str | None = None) -> Any:
    display = label or path.name
    if not path.is_file() or path.is_symlink():
        raise HarnessError(
            "ARTIFACT_MISSING",
            f"Required regular file is missing: {display}",
            details={"artifact": display},
        )
    try:
        value = path.read_bytes()
    except OSError as exc:
        raise HarnessError(
            "ARTIFACT_READ_FAILED",
            f"Could not read artifact: {display}",
            details={"artifact": display, "reason": str(exc)},
        ) from exc
    return parse_json_bytes(value, label=display)


def _utf16_sort_key(value: str) -> bytes:
    # RFC 8785 sorts property names by their UTF-16 code units, not Unicode code points.
    return value.encode("utf-16-be")


def _fallback_canonical_json(value: Any) -> bytes:
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if isinstance(value, Mapping):
        items = []
        for key in sorted(value, key=_utf16_sort_key):
            encoded_key = _fallback_canonical_json(key)
            encoded_value = _fallback_canonical_json(value[key])
            items.append(encoded_key + b":" + encoded_value)
        return b"{" + b",".join(items) + b"}"
    if isinstance(value, Sequence):
        return b"[" + b",".join(_fallback_canonical_json(item) for item in value) + b"]"
    raise AssertionError(f"validate_ijson accepted unsupported type: {type(value).__name__}")


def canonical_json_bytes(value: Any, *, encoder: CanonicalEncoder | None = None) -> bytes:
    """Return RFC 8785 bytes for the supported I-JSON subset.

    ``encoder`` is an explicit dependency seam for conformance tests and embedders. When the
    optional ``rfc8785`` dependency is installed it is preferred; otherwise the local encoder
    implements the same result for the intentionally float-free contract.
    """

    validate_ijson(value)
    selected = encoder
    if selected is None and _rfc8785 is not None:
        selected = _rfc8785.dumps
    encoded = selected(value) if selected is not None else _fallback_canonical_json(value)
    if not isinstance(encoded, bytes):
        raise TypeError("Canonical encoder must return bytes")
    return encoded


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Any, *, encoder: CanonicalEncoder | None = None) -> str:
    return sha256_bytes(canonical_json_bytes(value, encoder=encoder))
