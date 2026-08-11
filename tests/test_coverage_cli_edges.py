from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_work_harness import cli
from ai_work_harness.errors import HarnessError


class _Store:
    def __init__(self) -> None:
        self.current = SimpleNamespace(generation=7, snapshot_sha256="a" * 64)

    def get_current(self) -> Any:
        return self.current

    def load_snapshot(self, sha: str) -> Any:
        return SimpleNamespace(generation=6, snapshot_sha256=sha)


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.store = _Store()

    def __getattr__(self, name: str):
        def invoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
            self.calls.append((name, args, kwargs))
            return {
                "ok": True,
                "session_id": "demo",
                "generation": 7,
                "snapshot_sha256": "a" * 64,
                "called": name,
            }

        return invoke


def _args(operation: str, tmp_path: Path, **overrides: Any) -> argparse.Namespace:
    payload = tmp_path / "payload.json"
    payload.write_text('{"value":"payload"}', encoding="utf-8")
    reason = tmp_path / "reason.txt"
    reason.write_text("Because it is reviewed.\n", encoding="utf-8")
    values: dict[str, Any] = {
        "decision_operation": operation,
        "root": tmp_path,
        "session_id": "demo",
        "expected_parent": "a" * 64,
        "source_id": "source",
        "source": tmp_path / "source.md",
        "media_type": "text/markdown",
        "input_file": payload,
        "confirmation_subject": "decision-frame",
        "expected_artifact_sha": "b" * 64,
        "producer": "agent_import",
        "provider": "fixture",
        "disposition": "approved",
        "challenge_id": "00000000-0000-4000-8000-000000000001",
        "nonce": "c" * 32,
        "expected_bundle_sha": "d" * 64,
        "reason_file": reason,
        "snapshot": "e" * 64,
        "operation": "evaluations",
        "model": None,
        "expected_manifest_sha": "f" * 64,
        "agent_run_sha": None,
        "output": tmp_path / "view.json",
        "include_cited_excerpts": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.parametrize(
    ("operation", "method"),
    [
        ("decision init", "initialize"),
        ("decision source capture", "capture_source"),
        ("decision frame import", "import_frame"),
        ("decision candidates import", "import_candidates"),
        ("decision criteria import", "import_criteria"),
        ("decision frame confirm", "confirm"),
        ("decision candidates confirm", "confirm"),
        ("decision criteria confirm", "confirm"),
        ("decision evidence import", "import_evidence"),
        ("decision evaluations import", "import_evaluations"),
        ("decision evaluations generate", "generate_evaluations"),
        ("decision evaluations review", "import_reviews"),
        ("decision compare", "compare"),
        ("decision recommend", "generate_recommendation"),
        ("decision final import", "import_final_decision"),
        ("decision approval challenge", "create_approval_challenge"),
        ("decision approval commit", "commit_approval"),
        ("decision status", "status"),
        ("decision verify", "verify"),
        ("decision doctor", "doctor"),
        ("decision agent preview", "preview_agent"),
        ("decision agent consent", "consent_agent"),
        ("decision agent replay", "replay_agent_validate"),
        ("decision export-view", "export_view"),
    ],
)
def test_all_v2_cli_dispatch_leaves_call_one_service_method(
    operation: str,
    method: str,
    tmp_path: Path,
) -> None:
    service = _Service()
    overrides = {"input_file": None} if operation == "decision recommend" else {}
    result = cli._execute_decision(_args(operation, tmp_path, **overrides), service)
    assert result["called"] == method
    assert [call[0] for call in service.calls] == [method]


def test_cli_dispatches_file_recommendation_and_openai_agent_branches(tmp_path: Path) -> None:
    service = _Service()
    result = cli._execute_decision(
        _args("decision recommend", tmp_path, input_file=tmp_path / "payload.json"),
        service,
    )
    assert result["called"] == "record_recommendation"

    for operation, purpose in (
        ("decision evaluations generate", "evaluations"),
        ("decision recommend", "recommendation"),
    ):
        service = _Service()
        result = cli._execute_decision(
            _args(operation, tmp_path, provider="openai", input_file=None),
            service,
        )
        assert result["called"] == "run_openai_agent"
        assert service.calls[0][2]["operation"] == purpose


def test_cli_migration_leaf_and_unknown_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called: list[tuple[Path, str]] = []

    def fake_migrate(root: Path, session_id: str) -> dict[str, Any]:
        called.append((root, session_id))
        return {"migrated": True}

    monkeypatch.setattr("ai_work_harness.decision.migration.migrate_v1", fake_migrate)
    assert cli._execute_decision(_args("decision migrate-v1", tmp_path), _Service()) == {
        "migrated": True
    }
    assert called == [(tmp_path, "demo")]

    with pytest.raises(HarnessError) as caught:
        cli._execute_decision(_args("decision unknown", tmp_path), _Service())
    assert caught.value.code == "UNKNOWN_COMMAND"


def test_provider_selector_is_explicit_and_never_silently_falls_back() -> None:
    evaluation, producer = cli._provider_for("fixture", purpose="evaluations")
    recommendation, producer2 = cli._provider_for("fixture", purpose="recommendation")
    assert producer == producer2 == "fixture"
    assert hasattr(evaluation, "generate")
    assert hasattr(recommendation, "generate")

    with pytest.raises(HarnessError) as consent:
        cli._provider_for("openai", purpose="evaluations")
    assert consent.value.code == "OUTBOUND_CONSENT_REQUIRED"
    assert consent.value.exit_code == 4
    with pytest.raises(HarnessError) as unsupported:
        cli._provider_for("other", purpose="evaluations")
    assert unsupported.value.code == "UNSUPPORTED_PROVIDER"


def test_reason_file_is_regular_utf8_and_strips_only_line_endings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.txt"
    with pytest.raises(HarnessError) as caught:
        cli._load_reason(missing)
    assert caught.value.code == "ARTIFACT_MISSING"

    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"\xff")
    with pytest.raises(HarnessError) as caught:
        cli._load_reason(invalid)
    assert caught.value.code == "INVALID_REASON_FILE"

    valid = tmp_path / "valid.txt"
    valid.write_text("  keep spaces  \r\n", encoding="utf-8")
    assert cli._load_reason(valid) == "  keep spaces  "

    monkeypatch.setattr(
        Path,
        "read_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no")),
    )
    with pytest.raises(HarnessError) as caught:
        cli._load_reason(valid)
    assert caught.value.code == "ARTIFACT_READ_FAILED"


def test_success_and_error_envelopes_pin_snapshot_position() -> None:
    service = _Service()
    success = cli._decision_success(
        operation="decision status",
        session_id="demo",
        service=service,
        value={"ok": True, "session_id": "demo", "detail": "value"},
    )
    assert success == {
        "ok": True,
        "command": "decision status",
        "session_id": "demo",
        "generation": 7,
        "snapshot_sha256": "a" * 64,
        "result": {"detail": "value"},
    }
    generation, digest = cli._decision_position(service, "b" * 64)
    assert (generation, digest) == (6, "b" * 64)

    service.store.get_current = lambda: (_ for _ in ()).throw(OSError("broken"))
    assert cli._decision_position(service) == (None, None)
    error = cli._decision_error(
        operation="decision verify",
        session_id="demo",
        service=None,
        error=HarnessError("BROKEN", "broken", exit_code=5),
    )
    assert error["generation"] is None
    assert error["error"]["code"] == "BROKEN"


def test_decision_main_emits_structured_success_domain_and_internal_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    class SuccessService:
        def __init__(self, _root: Path, _session_id: str) -> None:
            self.store = _Store()

        def initialize(self) -> dict[str, Any]:
            return {
                "ok": True,
                "session_id": "demo",
                "generation": 0,
                "snapshot_sha256": "a" * 64,
            }

    monkeypatch.setattr("ai_work_harness.decision.service.DecisionService", SuccessService)
    assert cli.main(["--root", str(tmp_path), "decision", "init", "--session-id", "demo"]) == 0
    success = json.loads(capsys.readouterr().out)
    assert success["ok"] is True
    assert success["command"] == "decision init"

    class DomainService(SuccessService):
        def initialize(self) -> dict[str, Any]:
            raise HarnessError("WRITE_CONFLICT", "stale", exit_code=3)

    monkeypatch.setattr("ai_work_harness.decision.service.DecisionService", DomainService)
    assert cli.main(["--root", str(tmp_path), "decision", "init", "--session-id", "demo"]) == 3
    domain = json.loads(capsys.readouterr().err)
    assert domain["ok"] is False
    assert domain["error"]["code"] == "WRITE_CONFLICT"

    class InternalService(SuccessService):
        def initialize(self) -> dict[str, Any]:
            raise RuntimeError("must not leak")

    monkeypatch.setattr("ai_work_harness.decision.service.DecisionService", InternalService)
    assert cli.main(["--root", str(tmp_path), "decision", "init", "--session-id", "demo"]) == 1
    internal = json.loads(capsys.readouterr().err)
    assert internal["error"]["code"] == "INTERNAL_ERROR"
    assert "must not leak" not in json.dumps(internal)


@pytest.mark.parametrize("error", [OSError("disk failed"), PermissionError("access denied")])
def test_v2_main_maps_filesystem_errors_before_the_internal_error_boundary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    error: OSError,
) -> None:
    class FilesystemService:
        def __init__(self, _root: Path, _session_id: str) -> None:
            self.store = _Store()

        def initialize(self) -> dict[str, Any]:
            raise error

    monkeypatch.setattr("ai_work_harness.decision.service.DecisionService", FilesystemService)

    assert cli.main(["--root", str(tmp_path), "decision", "init", "--session-id", "demo"]) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["ok"] is False
    assert payload["command"] == "decision init"
    assert payload["generation"] == 7
    assert payload["snapshot_sha256"] == "a" * 64
    assert payload["error"] == {
        "code": "FILESYSTEM_ERROR",
        "details": {"reason": str(error)},
        "message": "A filesystem operation failed",
    }


def test_v1_main_maps_domain_and_filesystem_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "_run_v1",
        lambda _args: (_ for _ in ()).throw(HarnessError("BAD", "bad")),
    )
    assert cli.main(["status"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "BAD"

    monkeypatch.setattr(cli, "_run_v1", lambda _args: (_ for _ in ()).throw(OSError("disk")))
    assert cli.main(["status"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "FILESYSTEM_ERROR"


def test_v1_dispatch_unknown_command_is_structured() -> None:
    with pytest.raises(HarnessError) as caught:
        cli._run_v1(argparse.Namespace(command="unknown", root=Path.cwd()))
    assert caught.value.code == "UNKNOWN_COMMAND"


@pytest.mark.parametrize(
    ("command", "target", "extras"),
    [
        ("capture", "capture_input", {"logical_id": "source", "source": Path("source")}),
        (
            "frame",
            "write_frame",
            {
                "user_statement": "user",
                "ai_interpretation": "ai",
                "user_confirmed": False,
                "agreed_frame": "frame",
            },
        ),
        (
            "approve",
            "create_approval",
            {"decision": "choose", "reason": "because"},
        ),
        ("status", "project_status", {}),
        ("verify", "verify_project", {}),
    ],
)
def test_v1_dispatch_leaves_remain_explicit(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    target: str,
    extras: dict[str, Any],
) -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        return {"called": target}

    monkeypatch.setattr(cli, target, fake)
    args = argparse.Namespace(command=command, root=Path("project"), **extras)
    assert cli._run_v1(args) == {"called": target}
    assert len(calls) == 1
