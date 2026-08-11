from __future__ import annotations

from typing import Any


def _default_exit_code(code: str) -> int:
    conflict_codes = {
        "WRITE_CONFLICT",
        "WRITE_LOCK_TIMEOUT",
        "IDEMPOTENCY_KEY_REUSE",
        "CONFIRMATION_DIGEST_MISMATCH",
        "CHALLENGE_STALE",
        "CHALLENGE_MISMATCH",
        "CHALLENGE_EXPIRED",
        "V1_MIGRATION_TARGET_CONFLICT",
        "V1_MIGRATION_FINGERPRINT_CHANGED",
        "OUTBOUND_MANIFEST_MISMATCH",
        "OUTBOUND_CONSENT_STALE",
    }
    if code in conflict_codes or code.endswith("_STALE"):
        return 3
    if code.startswith(("MODEL_", "PROVIDER_", "OUTBOUND_LIMIT_")) or code in {
        "UNSUPPORTED_PROVIDER",
        "MCP_SDK_UNAVAILABLE",
    }:
        return 4
    if (
        "INTEGRITY" in code
        or code.startswith("CORRUPT_")
        or code
        in {
            "OBJECT_MISSING",
            "SNAPSHOT_MISSING",
            "SNAPSHOT_CYCLE",
            "SNAPSHOT_GENERATION_MISMATCH",
            "SNAPSHOT_ROOT_INVALID",
            "CAS_COLLISION",
            "AGENT_REPLAY_INVALID",
        }
    ):
        return 5
    return 2


class HarnessError(RuntimeError):
    """Expected, user-actionable harness failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        exit_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.exit_code = _default_exit_code(code) if exit_code is None else exit_code

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            },
        }
