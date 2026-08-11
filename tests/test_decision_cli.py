from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_work_harness.cli import build_parser, main
from ai_work_harness.decision.service import DecisionService
from ai_work_harness.project import init_project


def _invoke(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    *arguments: str,
) -> tuple[int, dict[str, object], str, str]:
    exit_code = main(["--root", str(root), *arguments])
    captured = capsys.readouterr()
    serialized = captured.out if exit_code == 0 else captured.err
    return exit_code, json.loads(serialized), captured.out, captured.err


def _initialize(
    root: Path,
    capsys: pytest.CaptureFixture[str],
    session_id: str = "cli-session",
) -> dict[str, object]:
    exit_code, payload, stdout, stderr = _invoke(
        root,
        capsys,
        "decision",
        "init",
        "--session-id",
        session_id,
    )
    assert exit_code == 0
    assert stdout
    assert stderr == ""
    return payload


def test_decision_init_and_status_use_v2_json_envelope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    initialized = _initialize(tmp_path, capsys)

    assert set(initialized) == {
        "ok",
        "command",
        "session_id",
        "generation",
        "snapshot_sha256",
        "result",
    }
    assert initialized["ok"] is True
    assert initialized["command"] == "decision init"
    assert initialized["session_id"] == "cli-session"
    assert initialized["generation"] == 0
    assert len(str(initialized["snapshot_sha256"])) == 64
    assert initialized["result"] == {"lifecycle_state": "initialized"}

    exit_code, status, stdout, stderr = _invoke(
        tmp_path,
        capsys,
        "decision",
        "status",
        "--session-id",
        "cli-session",
    )

    assert exit_code == 0
    assert stdout
    assert stderr == ""
    assert status["command"] == "decision status"
    assert status["snapshot_sha256"] == initialized["snapshot_sha256"]
    assert status["result"]["ready"] is False  # type: ignore[index]
    assert status["result"]["verified"] is True  # type: ignore[index]


def test_decision_guide_cli_dispatches_before_any_non_tty_write(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "decision",
            "guide",
            "guided-smoke",
            "--lang",
            "en",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "INTERACTIVE_TERMINAL_REQUIRED" in captured.out
    assert captured.err == ""
    assert not (tmp_path / ".ai-work-harness").exists()


def test_v1_cli_output_is_not_reinterpreted_as_v2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code, payload, stdout, stderr = _invoke(tmp_path, capsys, "init")

    assert exit_code == 0
    assert stdout
    assert stderr == ""
    assert payload["ok"] is True
    assert "command" not in payload
    assert "session_id" not in payload
    assert "result" not in payload


def test_v1_init_can_follow_explicit_v2_init_without_reinterpreting_v2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    initialized = _initialize(tmp_path, capsys, "v2-first")

    exit_code, payload, stdout, stderr = _invoke(tmp_path, capsys, "init")

    assert exit_code == 0
    assert stdout
    assert stderr == ""
    assert payload["already_initialized"] is False
    assert (tmp_path / ".ai-work-harness" / "v2").is_dir()
    service = DecisionService(tmp_path, "v2-first")
    assert service.status()["snapshot_sha256"] == initialized["snapshot_sha256"]


def test_migrate_v1_uses_the_explicit_decision_namespace(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_project(tmp_path)

    exit_code, payload, stdout, stderr = _invoke(
        tmp_path,
        capsys,
        "decision",
        "migrate-v1",
        "--session-id",
        "migrated-session",
    )

    assert exit_code == 0
    assert stdout
    assert stderr == ""
    assert payload["command"] == "decision migrate-v1"
    assert payload["session_id"] == "migrated-session"
    assert payload["result"]["already_migrated"] is False  # type: ignore[index]


def test_source_capture_advances_exact_expected_parent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    initialized = _initialize(tmp_path, capsys)
    source = tmp_path / "facts.md"
    source.write_text("verified synthetic fact\n", encoding="utf-8")

    exit_code, payload, stdout, stderr = _invoke(
        tmp_path,
        capsys,
        "decision",
        "source",
        "capture",
        "--session-id",
        "cli-session",
        "--expected-parent",
        str(initialized["snapshot_sha256"]),
        "--source-id",
        "facts",
        "--file",
        str(source),
        "--media-type",
        "text/markdown",
    )

    assert exit_code == 0
    assert stdout
    assert stderr == ""
    assert payload["command"] == "decision source capture"
    assert payload["generation"] == 1
    assert payload["snapshot_sha256"] != initialized["snapshot_sha256"]
    assert payload["result"]["source"] == {  # type: ignore[index]
        "source_id": "facts",
        "media_type": "text/markdown",
        "bytes": len(b"verified synthetic fact\n"),
        "blob_sha256": payload["result"]["source"]["blob_sha256"],  # type: ignore[index]
    }


@pytest.mark.parametrize(
    "arguments",
    [
        ("decision", "source", "capture", "--session-id", "s", "--source-id", "a", "--file", "x"),
        ("decision", "frame", "import", "--session-id", "s", "--from", "x"),
        (
            "decision",
            "approval",
            "commit",
            "--session-id",
            "s",
            "--challenge-id",
            "00000000-0000-4000-8000-000000000000",
            "--nonce",
            "00",
            "--expected-bundle-sha",
            "0" * 64,
            "--reason-file",
            "reason.txt",
        ),
    ],
)
def test_every_state_mutation_requires_expected_parent(arguments: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(list(arguments))

    assert caught.value.code == 2


@pytest.mark.parametrize(
    ("provider", "error_code"),
    [
        ("openai", "OUTBOUND_CONSENT_REQUIRED"),
        ("unknown-provider", "UNSUPPORTED_PROVIDER"),
    ],
)
def test_provider_failure_is_structured_and_does_not_advance_pointer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    provider: str,
    error_code: str,
) -> None:
    initialized = _initialize(tmp_path, capsys)
    parent = str(initialized["snapshot_sha256"])

    exit_code, payload, stdout, stderr = _invoke(
        tmp_path,
        capsys,
        "decision",
        "evaluations",
        "generate",
        "--session-id",
        "cli-session",
        "--expected-parent",
        parent,
        "--provider",
        provider,
    )

    assert exit_code == 4
    assert stdout == ""
    assert stderr
    assert payload["ok"] is False
    assert payload["command"] == "decision evaluations generate"
    assert payload["generation"] == 0
    assert payload["snapshot_sha256"] == parent
    assert payload["error"]["code"] == error_code  # type: ignore[index]


def test_import_error_uses_v2_envelope_and_pins_current_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    initialized = _initialize(tmp_path, capsys)
    parent = str(initialized["snapshot_sha256"])

    exit_code, payload, stdout, stderr = _invoke(
        tmp_path,
        capsys,
        "decision",
        "frame",
        "import",
        "--session-id",
        "cli-session",
        "--expected-parent",
        parent,
        "--from",
        str(tmp_path / "missing.json"),
    )

    assert exit_code == 2
    assert stdout == ""
    assert stderr
    assert payload["command"] == "decision frame import"
    assert payload["generation"] == 0
    assert payload["snapshot_sha256"] == parent
    assert payload["error"]["code"] == "ARTIFACT_MISSING"  # type: ignore[index]


def test_unexpected_v2_failure_is_structured_exit_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialized = _initialize(tmp_path, capsys)
    parent = initialized["snapshot_sha256"]

    def fail_status(self: DecisionService) -> dict[str, object]:
        raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr(DecisionService, "status", fail_status)
    exit_code, payload, stdout, stderr = _invoke(
        tmp_path,
        capsys,
        "decision",
        "status",
        "--session-id",
        "cli-session",
    )

    assert exit_code == 1
    assert stdout == ""
    assert stderr
    assert payload["snapshot_sha256"] == parent
    assert payload["error"] == {  # type: ignore[comparison-overlap]
        "code": "INTERNAL_ERROR",
        "details": {},
        "message": "An unexpected internal error occurred",
    }
    assert "sensitive" not in stderr
