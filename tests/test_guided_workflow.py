from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ai_work_harness.decision.canonical import sha256_bytes
from ai_work_harness.decision.openai_provider import OpenAIProvider
from ai_work_harness.decision.operator import GuidedOperator
from ai_work_harness.decision.providers import FixtureProvider
from ai_work_harness.decision.service import DecisionService
from ai_work_harness.decision.store import DecisionStore
from ai_work_harness.errors import HarnessError
from ai_work_harness.guided_cli import (
    GuidedDecisionController,
    run_guided_cli,
    run_guided_session,
)
from ai_work_harness.guided_io import get_catalog

Response = str | BaseException | Callable[[str], str]


class ScriptedConsole:
    def __init__(self, responses: list[Response], *, interactive: bool = True) -> None:
        self.responses = deque(responses)
        self.interactive = interactive
        self.writes: list[str] = []
        self.prompts: list[str] = []

    def is_interactive(self) -> bool:
        return self.interactive

    def write(self, text: str) -> None:
        self.writes.append(text)

    def read(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise EOFError("script exhausted")
        response = self.responses.popleft()
        if isinstance(response, BaseException):
            raise response
        return response(prompt) if callable(response) else response

    @property
    def transcript(self) -> str:
        return "\n".join([*self.writes, *self.prompts])


def _session_root(root: Path, session_id: str) -> Path:
    return root / ".ai-work-harness" / "v2" / "sessions" / session_id


def _write_payload(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain(child) for child in value]
    return value


def _frame() -> dict:
    return {
        "user_statement_verbatim": "Choose a local triage option.",
        "ai_initial_interpretation": "Compare two local triage options.",
        "business_user": "support operator",
        "blocked_decision": "which local option to use",
        "problem_statement": "Choose one local operating option.",
        "scope_in": ["captured local evidence"],
        "scope_out": [],
        "assumptions": [],
        "open_questions": [],
    }


def _candidates() -> dict:
    return {
        "candidates": [
            {
                "candidate_id": candidate_id,
                "title": title,
                "summary": f"Use {title}.",
                "proposed_by": "local_operator",
                "benefits": ["local processing"],
                "drawbacks": ["requires maintenance"],
                "risks": ["operational drift"],
                "uncertainties": ["future volume"],
            }
            for candidate_id, title in (
                ("rules", "Rules"),
                ("classical-ml", "Classical ML"),
            )
        ]
    }


def _criteria() -> dict:
    return {
        "criteria": [
            {
                "criterion_id": "privacy",
                "title": "Privacy",
                "definition": "Keep captured data local.",
                "priority": "must",
            }
        ]
    }


def _evidence(source_bytes: bytes) -> dict:
    excerpt_sha256 = sha256_bytes(source_bytes)
    return {
        "evidence": [
            {
                "evidence_id": f"ev-{candidate_id}-privacy",
                "claim": f"{candidate_id} keeps captured data local.",
                "provenance": "source_observation",
                "source": {
                    "source_id": "brief",
                    "start_line": 1,
                    "end_line": 1,
                    "excerpt_sha256": excerpt_sha256,
                },
            }
            for candidate_id in ("rules", "classical-ml")
        ]
    }


def _advance(result: Mapping[str, object]) -> str:
    snapshot_sha256 = result["snapshot_sha256"]
    assert isinstance(snapshot_sha256, str)
    return snapshot_sha256


def _build_evidence_ready(service: DecisionService, root: Path) -> str:
    source_bytes = b"Rules and classical ML both keep captured data local.\n"
    source = root / f"{service.session_id}-review-source.md"
    source.write_bytes(source_bytes)
    parent = _advance(service.initialize())
    parent = _advance(
        service.capture_source(
            source_id="brief",
            source=source,
            expected_parent=parent,
            media_type="text/markdown",
        )
    )
    frame = service.import_frame(_frame(), expected_parent=parent)
    parent = _advance(frame)
    parent = _advance(
        service.confirm(
            "decision-frame",
            expected_artifact_sha=str(frame["artifact_sha256"]),
            expected_parent=parent,
        )
    )
    candidates = service.import_candidates(_candidates(), expected_parent=parent)
    parent = _advance(candidates)
    parent = _advance(
        service.confirm(
            "candidate-set",
            expected_artifact_sha=str(candidates["artifact_sha256"]),
            expected_parent=parent,
        )
    )
    criteria = service.import_criteria(_criteria(), expected_parent=parent)
    parent = _advance(criteria)
    parent = _advance(
        service.confirm(
            "criteria-set",
            expected_artifact_sha=str(criteria["artifact_sha256"]),
            expected_parent=parent,
        )
    )
    return _advance(service.import_evidence(_evidence(source_bytes), expected_parent=parent))


def _build_evaluation_ready(service: DecisionService, root: Path) -> str:
    parent = _build_evidence_ready(service, root)
    return _advance(
        service.generate_evaluations(
            FixtureProvider().evaluation_provider(),
            expected_parent=parent,
            producer_kind="fixture",
        )
    )


def _build_comparison_ready(service: DecisionService, root: Path) -> str:
    parent = _build_evaluation_ready(service, root)
    parent = _advance(
        service.import_reviews(
            {
                "reviews": [
                    {
                        "candidate_id": candidate_id,
                        "criterion_id": "privacy",
                        "outcome": "concur",
                        "reason": "The displayed source observation supports this assessment.",
                    }
                    for candidate_id in ("rules", "classical-ml")
                ]
            },
            expected_parent=parent,
        )
    )
    return _advance(service.compare(expected_parent=parent))


class FakeResponses:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def create(self, **request: object) -> dict[str, object]:
        self.requests.append(request)
        return self.response


class FakeOpenAIClient:
    def __init__(self, response: dict[str, object]) -> None:
        self.responses = FakeResponses(response)


def _tool_response(tool_name: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "id": "resp_guided_synthetic",
        "output": [
            {
                "type": "function_call",
                "name": tool_name,
                "arguments": json.dumps(payload),
            }
        ],
        "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
    }


def _provider_for(
    manifest: Mapping[str, object],
    response: dict[str, object],
) -> OpenAIProvider:
    return OpenAIProvider(
        client=FakeOpenAIClient(response),
        model=str(manifest["model"]),
        reasoning_effort=str(manifest["reasoning_effort"]),
        max_tool_calls=int(manifest["max_tool_rounds"]),
        max_output_tokens=int(manifest["max_output_tokens"]),
        timeout_seconds=float(manifest["timeout_seconds"]),
        max_retries=int(manifest["max_retries"]),
        max_context_bytes=int(manifest["max_context_bytes"]),
        max_lookup_bytes=int(manifest["max_lookup_bytes"]),
        sleeper=lambda _seconds: None,
    )


def _build_approval_challenge(service: DecisionService, root: Path) -> dict[str, object]:
    parent = _build_evaluation_ready(service, root)
    parent = _advance(
        service.import_reviews(
            {
                "reviews": [
                    {
                        "candidate_id": candidate_id,
                        "criterion_id": "privacy",
                        "outcome": "concur",
                        "reason": "The cited local evidence supports this assessment.",
                    }
                    for candidate_id in ("rules", "classical-ml")
                ]
            },
            expected_parent=parent,
        )
    )
    parent = _advance(service.compare(expected_parent=parent))
    parent = _advance(
        service.import_final_decision(
            {
                "disposition": "select",
                "candidate_id": "classical-ml",
                "reason": "Classical ML meets the reviewed must criterion.",
                "risk_acknowledgements": [],
            },
            expected_parent=parent,
        )
    )
    return service.create_approval_challenge(
        disposition="approved",
        expected_parent=parent,
    )


def _build_high_risk_comparison(service: DecisionService, root: Path) -> str:
    source_bytes = b"Synthetic privacy and quality evidence.\n"
    source = root / "high-risk-source.md"
    source.write_bytes(source_bytes)
    criteria = {
        "criteria": [
            *_criteria()["criteria"],
            {
                "criterion_id": "classification-quality",
                "title": "Classification quality",
                "definition": "Route the synthetic requests correctly.",
                "priority": "high",
            },
        ]
    }
    evidence = {
        "evidence": [
            {
                "evidence_id": f"ev-{candidate_id}-{criterion_id}",
                "claim": f"Synthetic observation for {candidate_id} and {criterion_id}.",
                "provenance": "source_observation",
                "source": {
                    "source_id": "brief",
                    "start_line": 1,
                    "end_line": 1,
                    "excerpt_sha256": sha256_bytes(source_bytes),
                },
            }
            for candidate_id in ("rules", "classical-ml")
            for criterion_id in ("privacy", "classification-quality")
        ]
    }
    parent = _advance(service.initialize())
    parent = _advance(
        service.capture_source(
            source_id="brief",
            source=source,
            expected_parent=parent,
            media_type="text/markdown",
        )
    )
    for subject, importer, payload in (
        ("decision-frame", service.import_frame, _frame()),
        ("candidate-set", service.import_candidates, _candidates()),
        ("criteria-set", service.import_criteria, criteria),
    ):
        imported = importer(payload, expected_parent=parent)
        parent = _advance(imported)
        parent = _advance(
            service.confirm(
                subject,
                expected_artifact_sha=str(imported["artifact_sha256"]),
                expected_parent=parent,
            )
        )
    parent = _advance(service.import_evidence(evidence, expected_parent=parent))
    parent = _advance(
        service.generate_evaluations(
            FixtureProvider().evaluation_provider(),
            expected_parent=parent,
            producer_kind="fixture",
        )
    )
    parent = _advance(
        service.import_reviews(
            {
                "reviews": [
                    {
                        "candidate_id": candidate_id,
                        "criterion_id": criterion_id,
                        "outcome": "concur",
                        "reason": "Reviewed the displayed synthetic evidence.",
                    }
                    for candidate_id in ("rules", "classical-ml")
                    for criterion_id in ("privacy", "classification-quality")
                ]
            },
            expected_parent=parent,
        )
    )
    parent = _advance(service.compare(expected_parent=parent))
    return _advance(
        service.generate_recommendation(
            FixtureProvider().recommendation_provider(),
            expected_parent=parent,
            producer_kind="fixture",
        )
    )


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


def test_non_tty_fails_before_session_creation(tmp_path: Path) -> None:
    console = ScriptedConsole([], interactive=False)

    exit_code = run_guided_cli(tmp_path, "not-created", language="en", console=console)

    assert exit_code == 2
    assert "INTERACTIVE_TERMINAL_REQUIRED" in console.transcript
    assert not _session_root(tmp_path, "not-created").exists()
    assert console.prompts == []


def test_invalid_session_id_is_contained_by_cli_boundary_before_prompting(
    tmp_path: Path,
) -> None:
    console = ScriptedConsole([])

    exit_code = run_guided_cli(tmp_path, "../escape", language="en", console=console)

    assert exit_code == 2
    assert "UNSAFE_ID" in console.transcript
    assert "session_id must be a lowercase, path-safe identifier" in console.transcript
    assert console.prompts == []
    assert not (tmp_path / ".ai-work-harness").exists()


@pytest.mark.parametrize(
    ("language", "decline"),
    [
        ("en", "no"),
        ("ko", "아니요"),
    ],
)
def test_missing_session_decline_is_localized_and_does_not_create(
    tmp_path: Path,
    language: str,
    decline: str,
) -> None:
    session_id = f"decline-{language}"
    console = ScriptedConsole([decline])

    exit_code = run_guided_cli(
        tmp_path,
        session_id,
        language=language,
        console=console,
    )

    assert exit_code == 0
    catalog = get_catalog(language)
    assert catalog.text("session.create", session_id=session_id) in console.transcript
    assert catalog.text("quit.done", session_id=session_id) in console.transcript
    assert not _session_root(tmp_path, session_id).exists()
    assert not console.responses


def test_missing_session_can_be_created_then_quit_as_resumable(tmp_path: Path) -> None:
    console = ScriptedConsole(["yes", "quit"])

    exit_code = run_guided_cli(
        tmp_path,
        "created-by-guide",
        language="en",
        console=console,
    )

    state = DecisionService(tmp_path, "created-by-guide").read_operator_state()
    assert exit_code == 0
    assert state.generation == 0
    assert state.refs == {}
    assert "Created decision session 'created-by-guide'." in console.transcript
    assert "Resume state: generation 0" in console.transcript
    assert not console.responses


def test_openai_exact_phrase_mismatch_creates_no_consent_or_network_call(
    tmp_path: Path,
) -> None:
    service = DecisionService(tmp_path, "openai-mismatch")
    parent = _build_evidence_ready(service, tmp_path)
    operator = GuidedOperator(service)
    called = False

    def forbidden_factory(_manifest: Mapping[str, object]) -> object:
        nonlocal called
        called = True
        raise AssertionError("provider must not be created before exact consent")

    console = ScriptedConsole(["SEND OPENAI wrong"])
    controller = GuidedDecisionController(
        operator,
        console,
        root=tmp_path,
        language="en",
        openai_provider_factory=forbidden_factory,
    )

    controller._fresh_openai("evaluations")

    assert called is False
    assert service.status()["snapshot_sha256"] == parent
    assert "agent_consent" not in service.status()["refs"]
    assert "nothing was sent" in console.transcript


def test_guided_openai_evaluation_uses_separate_consent_and_result_snapshots(
    tmp_path: Path,
) -> None:
    service = DecisionService(tmp_path, "openai-guided-evaluation")
    _build_evidence_ready(service, tmp_path)
    operator = GuidedOperator(service)
    input_context = service._decision_context()
    payload = FixtureProvider().generate_evaluations(input_context).as_payload()
    preview = operator.preview_agent(operation="evaluations")
    manifest_sha = str(preview["outbound_manifest_sha256"])
    console = ScriptedConsole([f"SEND OPENAI {manifest_sha[:12]}"])
    providers: list[OpenAIProvider] = []

    def provider_factory(manifest: Mapping[str, object]) -> OpenAIProvider:
        provider = _provider_for(
            manifest,
            _tool_response("submit_evaluations", payload),
        )
        providers.append(provider)
        return provider

    controller = GuidedDecisionController(
        operator,
        console,
        root=tmp_path,
        language="en",
        openai_provider_factory=provider_factory,
    )
    initial = operator.cursor

    controller._fresh_openai("evaluations")

    assert operator.cursor.generation == initial.generation + 2
    assert operator.plan.stage.value == "review"
    assert operator.plan.summary["producers"]["evaluation_set"] == "openai"
    assert "agent_consent" not in operator.state.refs
    assert "agent_run" in operator.state.refs
    run_artifact = service.store.read_artifact(operator.state.refs["agent_run"])
    consent_sha = run_artifact.parents["agent_consent"]
    consent = service.store.read_artifact(consent_sha)
    assert consent.payload["method"] == "guided_exact_phrase"
    assert consent.payload["subject_sha256"] == manifest_sha
    assert len(providers[0]._client.responses.requests) == 1
    assert manifest_sha not in console.transcript
    assert str(preview["outbound_manifest"]["source_sha256s"]) not in console.transcript


def test_failed_guided_openai_run_reuses_active_consent_without_new_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = DecisionService(tmp_path, "openai-guided-retry")
    _build_evidence_ready(service, tmp_path)
    operator = GuidedOperator(service)
    payload = FixtureProvider().generate_evaluations(service._decision_context()).as_payload()
    preview = operator.preview_agent(operation="evaluations")
    manifest_sha = str(preview["outbound_manifest_sha256"])
    refusal = {
        "id": "resp_guided_refusal",
        "output": [
            {
                "type": "message",
                "content": [{"type": "refusal", "refusal": "Synthetic refusal."}],
            }
        ],
    }
    failing = GuidedDecisionController(
        operator,
        ScriptedConsole([f"SEND OPENAI {manifest_sha[:12]}"]),
        root=tmp_path,
        language="en",
        openai_provider_factory=lambda manifest: _provider_for(manifest, refusal),
    )

    with pytest.raises(HarnessError) as caught:
        failing._fresh_openai("evaluations")

    assert caught.value.code == "MODEL_REFUSAL"
    consent_cursor = operator.cursor
    consent_sha = operator.state.refs["agent_consent"]
    assert operator.plan.recommended_action.value == "retry_evaluations"

    def preview_must_not_run(**_kwargs: object) -> dict[str, object]:
        raise AssertionError("active consent retry must not recalculate preview")

    monkeypatch.setattr(service, "preview_agent", preview_must_not_run)
    retry = GuidedDecisionController(
        operator,
        ScriptedConsole(["retry"]),
        root=tmp_path,
        language="en",
        openai_provider_factory=lambda manifest: _provider_for(
            manifest,
            _tool_response("submit_evaluations", payload),
        ),
    )

    retry._edit_evaluations()

    assert operator.cursor.generation == consent_cursor.generation + 1
    run = service.store.read_artifact(operator.state.refs["agent_run"])
    assert run.parents["agent_consent"] == consent_sha
    assert "agent_consent" not in operator.state.refs


def test_guided_openai_recommendation_returns_to_human_final_decision(
    tmp_path: Path,
) -> None:
    service = DecisionService(tmp_path, "openai-guided-recommendation")
    _build_comparison_ready(service, tmp_path)
    operator = GuidedOperator(service)
    payload = FixtureProvider().generate_recommendation(service._decision_context()).as_payload()
    preview = operator.preview_agent(operation="recommendation")
    fingerprint = str(preview["outbound_manifest_sha256"])[:12]
    controller = GuidedDecisionController(
        operator,
        ScriptedConsole([f"SEND OPENAI {fingerprint}"]),
        root=tmp_path,
        language="en",
        openai_provider_factory=lambda manifest: _provider_for(
            manifest,
            _tool_response("submit_recommendation", payload),
        ),
    )

    controller._fresh_openai("recommendation")

    assert operator.plan.stage.value == "final_decision"
    assert operator.plan.summary["producers"]["recommendation"] == "openai"


@pytest.mark.parametrize(
    ("signal", "expected_exit", "expected_text"),
    [
        (EOFError("closed"), 2, "INTERACTIVE_INPUT_CLOSED"),
        (KeyboardInterrupt(), 130, "interrupted"),
    ],
)
def test_input_close_and_interrupt_leave_missing_session_untouched(
    tmp_path: Path,
    signal: BaseException,
    expected_exit: int,
    expected_text: str,
) -> None:
    console = ScriptedConsole([signal])

    exit_code = run_guided_cli(tmp_path, "signal", language="en", console=console)

    assert exit_code == expected_exit
    assert expected_text.casefold() in console.transcript.casefold()
    assert not _session_root(tmp_path, "signal").exists()


def test_write_conflict_decline_does_not_reload_or_retry_guided_mutation(
    tmp_path: Path,
) -> None:
    session_id = "conflict-decline"
    service = DecisionService(tmp_path, session_id)
    service.initialize()
    guided_source = tmp_path / "guided-source.md"
    guided_source.write_text("guided mutation\n", encoding="utf-8")
    peer_source = tmp_path / "peer-source.md"
    peer_source.write_text("peer mutation wins\n", encoding="utf-8")
    peer = DecisionService(tmp_path, session_id)

    def advance_from_peer(prompt: str) -> str:
        assert "Media type" in prompt
        current = peer.status()["snapshot_sha256"]
        peer.capture_source(
            source_id="peer",
            source=peer_source,
            expected_parent=str(current),
            media_type="text/markdown",
        )
        return ""

    console = ScriptedConsole(
        [
            "",  # navigate: capture source
            "guided",
            str(guided_source),
            advance_from_peer,
            "no",  # do not reload after detecting the conflict
        ]
    )

    exit_code = run_guided_cli(
        tmp_path,
        session_id,
        language="en",
        console=console,
    )

    state = peer.read_operator_state()
    manifest = _plain(state.artifacts["source_manifest"]["payload"])
    assert exit_code == 3
    assert [source["source_id"] for source in manifest["sources"]] == ["peer"]
    assert "Another writer changed this session" in console.transcript
    assert "The attempted action was not retried" in console.transcript
    assert "Reloaded the current state" not in console.transcript
    assert sum("Source ID" in prompt for prompt in console.prompts) == 1
    assert not console.responses


def test_write_conflict_reload_is_explicit_and_does_not_retry_mutation(
    tmp_path: Path,
) -> None:
    session_id = "conflict-reload"
    service = DecisionService(tmp_path, session_id)
    service.initialize()
    guided_source = tmp_path / "guided-reload-source.md"
    guided_source.write_text("guided mutation\n", encoding="utf-8")
    peer_source = tmp_path / "peer-reload-source.md"
    peer_source.write_text("peer mutation wins\n", encoding="utf-8")
    peer = DecisionService(tmp_path, session_id)

    def advance_from_peer(prompt: str) -> str:
        assert "Media type" in prompt
        peer.capture_source(
            source_id="peer",
            source=peer_source,
            expected_parent=str(peer.status()["snapshot_sha256"]),
            media_type="text/markdown",
        )
        return ""

    console = ScriptedConsole(
        [
            "",
            "guided",
            str(guided_source),
            advance_from_peer,
            "yes",  # explicitly adopt the peer snapshot
            "quit",
        ]
    )

    exit_code = run_guided_cli(tmp_path, session_id, language="en", console=console)

    manifest = _plain(peer.read_operator_state().artifacts["source_manifest"]["payload"])
    assert exit_code == 0
    assert [source["source_id"] for source in manifest["sources"]] == ["peer"]
    assert "Reloaded the current state" in console.transcript
    assert sum("Source ID" in prompt for prompt in console.prompts) == 1
    assert not console.responses


@pytest.mark.parametrize("language", ["ko", "en"])
def test_json_draft_survives_confirmation_quit_without_confirmation_snapshot(
    tmp_path: Path,
    language: str,
) -> None:
    session_id = f"draft-{language}"
    service = DecisionService(tmp_path, session_id)
    initialized = service.initialize()
    source = tmp_path / f"source-{language}.md"
    source.write_text("Both options keep captured data local.\n", encoding="utf-8")
    captured = service.capture_source(
        source_id="brief",
        source=source,
        expected_parent=str(initialized["snapshot_sha256"]),
        media_type="text/markdown",
    )
    frame_path = _write_payload(tmp_path / f"frame-{language}.json", _frame())
    console = ScriptedConsole(
        [
            "",  # continue from source to frame
            "json",
            str(frame_path),
            "",  # do not save a second draft copy
            "",  # continue to semantic confirmation
            "quit",
        ]
    )

    result = run_guided_session(
        service,
        console,
        root=tmp_path,
        language=language,
    )

    state = service.read_operator_state()
    assert result == "quit"
    assert state.generation == int(captured["generation"]) + 1
    assert _plain(state.artifacts["decision_frame"]["payload"]) == _frame()
    assert "decision_frame" in state.refs
    assert "frame_confirmation" not in state.refs
    assert any("Artifact fingerprint:" in line for line in console.writes)
    assert not console.responses


def test_malformed_json_is_reported_and_reprompted_without_losing_session(
    tmp_path: Path,
) -> None:
    service = DecisionService(tmp_path, "json-retry")
    initialized = service.initialize()
    source = tmp_path / "json-retry-source.md"
    source.write_text("A local source remains captured.\n", encoding="utf-8")
    captured = service.capture_source(
        source_id="brief",
        source=source,
        expected_parent=str(initialized["snapshot_sha256"]),
        media_type="text/markdown",
    )
    malformed_path = tmp_path / "malformed-frame.json"
    malformed_path.write_text('{"problem_statement": ', encoding="utf-8")
    valid_path = _write_payload(tmp_path / "valid-frame.json", _frame())
    console = ScriptedConsole(
        [
            "",  # navigate: frame
            "json",
            str(malformed_path),
            str(valid_path),
            "",  # do not save another draft copy
            "quit",  # imported successfully; stop before confirmation
        ]
    )

    result = run_guided_session(service, console, root=tmp_path, language="en")

    state = service.read_operator_state()
    assert result == "quit"
    assert state.generation == int(captured["generation"]) + 1
    assert _plain(state.artifacts["decision_frame"]["payload"]) == _frame()
    assert "INVALID_JSON" in console.transcript
    assert console.prompts.count("JSON payload path: ") == 2
    assert not console.responses


def test_explicit_draft_save_rejects_managed_path_and_confirms_external_overwrite(
    tmp_path: Path,
) -> None:
    service = DecisionService(tmp_path, "safe-draft-save")
    initialized = service.initialize()
    source = tmp_path / "safe-draft-source.md"
    source.write_text("A captured source remains immutable.\n", encoding="utf-8")
    service.capture_source(
        source_id="brief",
        source=source,
        expected_parent=str(initialized["snapshot_sha256"]),
        media_type="text/markdown",
    )
    imported_path = _write_payload(tmp_path / "imported-frame.json", _frame())
    managed_target = tmp_path / ".ai-work-harness" / "v2" / "forbidden-draft.json"
    external_target = tmp_path / "existing-external-draft.json"
    external_target.write_text('{"sentinel": true}\n', encoding="utf-8")
    console = ScriptedConsole(
        [
            "",  # navigate: frame
            "json",
            str(imported_path),
            "yes",  # explicitly request a second draft copy
            str(managed_target),
            str(external_target),
            "yes",  # explicitly allow overwriting this existing external file
            "quit",  # imported successfully; stop before confirmation
        ]
    )

    result = run_guided_session(service, console, root=tmp_path, language="en")

    assert result == "quit"
    assert not managed_target.exists()
    assert json.loads(external_target.read_text(encoding="utf-8")) == _frame()
    assert "UNSAFE_DRAFT_PATH" in console.transcript
    assert "Overwrite the existing file?" in console.transcript
    assert "Saved frame." in console.transcript
    assert not console.responses


def test_form_workflow_builds_all_human_drafts_and_shows_cited_excerpt(
    tmp_path: Path,
) -> None:
    service = DecisionService(tmp_path, "form-workflow")
    service.initialize()
    source = tmp_path / "form-source.md"
    source.write_text(
        "Rules keep captured data local.\nClassical ML keeps captured data local.\n",
        encoding="utf-8",
    )
    console = ScriptedConsole(
        [
            "",  # navigate: capture source
            "brief",
            str(source),
            "",  # auto-detect media type
            "",  # navigate: frame
            "form",
            "Choose a local triage option.",
            "Compare two local triage options.",
            "support operator",
            "which local option to use",
            "Choose one local operating option.",
            "captured local evidence, operator review",
            "production rollout",
            "the source is synthetic",
            "future volume",
            "",  # do not save another draft copy
            "",  # navigate: confirm frame
            "confirm",
            "",  # navigate: candidates
            "form",
            "rules",
            "Rules",
            "Use deterministic rules.",
            "local_operator",
            "predictable behavior",
            "manual tuning",
            "operational drift",
            "future volume",
            "classical-ml",
            "Classical ML",
            "Use a supervised local classifier.",
            "local_operator",
            "learned routing",
            "model maintenance",
            "model drift",
            "future labels",
            "",  # no third candidate
            "",  # do not save another draft copy
            "",  # navigate: confirm candidates
            "confirm",
            "",  # navigate: criteria
            "form",
            "high",
            "speed",
            "Speed",
            "Respond within the operating target.",
            "",  # no second ordinary criterion
            "privacy",
            "Privacy",
            "Keep captured data local.",
            "",  # do not save another draft copy
            "",  # navigate: confirm criteria
            "confirm",
            "",  # navigate: evidence
            "form",
            "source_observation",
            "ev-local-privacy",
            "Both candidates keep captured data local.",
            "brief",
            "1",
            "2",
            "The two-line excerpt supports the claim.",
            "",  # no second evidence item
            "",  # do not save another draft copy
            "quit",  # stop before evaluation drafting
        ]
    )

    result = run_guided_session(service, console, root=tmp_path, language="en")

    state = service.read_operator_state()
    frame = _plain(state.artifacts["decision_frame"]["payload"])
    candidates = _plain(state.artifacts["candidate_set"]["payload"])
    criteria = _plain(state.artifacts["criteria_set"]["payload"])
    evidence = _plain(state.artifacts["evidence_set"]["payload"])
    assert result == "quit"
    assert frame["scope_in"] == ["captured local evidence", "operator review"]
    assert [item["candidate_id"] for item in candidates["candidates"]] == [
        "classical-ml",
        "rules",
    ]
    assert {item["priority"] for item in criteria["criteria"]} == {"high", "must"}
    assert evidence["evidence"][0]["source"] == {
        "source_id": "brief",
        "start_line": 1,
        "end_line": 2,
        "excerpt_sha256": sha256_bytes(source.read_bytes()),
    }
    assert "[Cited excerpt]" in console.writes
    assert "   1: Rules keep captured data local." in console.writes
    assert "   2: Classical ML keeps captured data local." in console.writes
    assert not console.responses


def test_request_revision_commits_only_the_reviewed_prefix_and_returns_to_drafting(
    tmp_path: Path,
) -> None:
    service = DecisionService(tmp_path, "partial-review")
    _build_evaluation_ready(service, tmp_path)
    console = ScriptedConsole(
        [
            "",  # navigate: review evaluations
            "request_revision",
            "The first rationale needs a more specific cited explanation.",
            "yes",  # commit this partial review batch
            "quit",  # stop when the planner returns to evaluation drafting
        ]
    )

    result = run_guided_session(service, console, root=tmp_path, language="en")

    state = service.read_operator_state()
    review_payload = _plain(state.artifacts["evaluation_review_set"]["payload"])
    assert result == "quit"
    assert review_payload == {
        "reviews": [
            {
                "candidate_id": "classical-ml",
                "criterion_id": "privacy",
                "outcome": "request_revision",
                "reason": "The first rationale needs a more specific cited explanation.",
            }
        ]
    }
    assert "comparison" not in state.refs
    assert "Import a revised evaluation" in console.transcript
    assert not console.responses


def test_review_override_uses_displayed_source_evidence_before_revision_request(
    tmp_path: Path,
) -> None:
    service = DecisionService(tmp_path, "override-review")
    _build_evaluation_ready(service, tmp_path)
    console = ScriptedConsole(
        [
            "",  # navigate: review evaluations
            "override",
            "The cited local observation supports an explicit override.",
            "meets",
            "ev-classical-ml-privacy",
            "request_revision",
            "The remaining Must cell needs a revised rationale.",
            "yes",  # request_revision makes this partial review storable
            "quit",
        ]
    )

    result = run_guided_session(service, console, root=tmp_path, language="en")

    reviews = _plain(service.read_operator_state().artifacts["evaluation_review_set"]["payload"])[
        "reviews"
    ]
    assert result == "quit"
    assert reviews[0] == {
        "candidate_id": "classical-ml",
        "criterion_id": "privacy",
        "outcome": "override",
        "reason": "The cited local observation supports an explicit override.",
        "replacement_assessment": "meets",
        "replacement_evidence_ids": ["ev-classical-ml-privacy"],
    }
    assert reviews[1]["outcome"] == "request_revision"
    assert "[Cited evidence]" in console.transcript
    assert "[Available factual evidence]" in console.transcript
    assert "Confidence: high" in console.transcript
    assert not console.responses


def test_final_decision_renders_and_acknowledges_required_high_risk(
    tmp_path: Path,
) -> None:
    service = DecisionService(tmp_path, "guided-high-risk")
    _build_high_risk_comparison(service, tmp_path)
    console = ScriptedConsole(
        [
            "",  # navigate: final decision
            "select",
            "rules",  # deliberately differ from the fixture recommendation
            "Rules are simpler for the current operating volume.",
            "yes",  # acknowledge rules/classification-quality = partial
            "yes",  # record final decision
            "quit",  # stop before creating an approval challenge
        ]
    )

    result = run_guided_session(service, console, root=tmp_path, language="en")

    final = _plain(service.read_operator_state().artifacts["final_decision"]["payload"])
    assert result == "quit"
    assert final["candidate_id"] == "rules"
    assert final["recommendation_relation"] == "different"
    assert final["risk_acknowledgements"] == ["rules/classification-quality"]
    assert "[Qualitative comparison]" in console.transcript
    assert "[Risk to acknowledge]" in console.transcript
    assert "Effective assessment: partial" in console.transcript
    assert "Recommendation relation: different" in console.transcript
    assert not console.responses


def _approval_phrase(prompt: str) -> str:
    first_line = prompt.splitlines()[0]
    marker = "Type exactly: "
    assert marker in first_line
    return first_line.split(marker, 1)[1]


def test_expired_approval_challenge_is_reissued_before_phrase_entry(
    tmp_path: Path,
) -> None:
    clock = MutableClock(datetime(2026, 8, 12, 3, 0, tzinfo=UTC))
    store = DecisionStore(tmp_path, "expired-challenge", clock=clock)
    service = DecisionService(
        tmp_path,
        "expired-challenge",
        store=store,
        clock=clock,
    )
    original = _build_approval_challenge(service, tmp_path)
    original_ref = service.status()["refs"]["approval_challenge"]
    original_generation = int(original["generation"])
    clock.advance(timedelta(minutes=10))
    console = ScriptedConsole(
        [
            "",  # navigate: handle expired challenge
            "yes",  # issue a fresh challenge for the same disposition
            "quit",  # inspect the fresh phrase in a later invocation
        ]
    )

    result = run_guided_session(service, console, root=tmp_path, language="en")

    state = service.read_operator_state()
    replacement_ref = state.refs["approval_challenge"]
    replacement = service.store.read_artifact(replacement_ref).payload
    assert result == "quit"
    assert state.generation == original_generation + 1
    assert replacement_ref != original_ref
    assert replacement["challenge_id"] != original["challenge_id"]
    assert replacement["proposed_disposition"] == "approved"
    assert "The approval challenge has expired" in console.transcript
    assert "Type exactly:" not in console.transcript
    assert not console.responses


def test_fixture_workflow_reaches_approved_ready_in_one_controller_invocation(
    tmp_path: Path,
) -> None:
    service = DecisionService(tmp_path, "fixture-e2e")
    service.initialize()
    source_bytes = b"Rules and classical ML both keep captured data local.\n"
    source = tmp_path / "brief.md"
    source.write_bytes(source_bytes)
    frame_path = _write_payload(tmp_path / "frame.json", _frame())
    candidates_path = _write_payload(tmp_path / "candidates.json", _candidates())
    criteria_path = _write_payload(tmp_path / "criteria.json", _criteria())
    evidence_path = _write_payload(tmp_path / "evidence.json", _evidence(source_bytes))
    export_path = tmp_path / "approved-view.json"
    console = ScriptedConsole(
        [
            "",  # navigate: capture source
            "brief",
            str(source),
            "",  # auto-detect media type
            "",  # navigate: frame
            "json",
            str(frame_path),
            "",  # do not save another draft
            "",  # navigate: confirm frame
            "confirm",
            "",  # navigate: candidates
            "json",
            str(candidates_path),
            "",  # do not save another draft
            "",  # navigate: confirm candidates
            "confirm",
            "",  # navigate: criteria
            "json",
            str(criteria_path),
            "",  # do not save another draft
            "",  # navigate: confirm criteria
            "confirm",
            "",  # navigate: evidence
            "json",
            str(evidence_path),
            "",  # do not save another draft
            "",  # navigate: evaluations
            "fixture",
            "",  # navigate: review
            "concur",
            "Reviewed classical ML privacy evidence.",
            "concur",
            "Reviewed rules privacy evidence.",
            "yes",  # commit completed reviews
            "",  # navigate: recommendation
            "fixture",
            "",  # navigate: final decision
            "select",
            "classical-ml",
            "Classical ML meets the reviewed must criterion.",
            "yes",  # record final decision
            "",  # navigate: create approval challenge
            "approved",
            "",  # navigate: commit active challenge
            _approval_phrase,
            "Reviewed the complete local decision bundle.",
            "change",  # terminal: inspect a possible post-approval change
            "final_decision",
            "no",  # preserve the approved bundle
            "export",
            str(export_path),
            "finish",  # export returns to the terminal menu
        ]
    )

    result = run_guided_session(service, console, root=tmp_path, language="en")

    status = service.status()
    assert result == "complete"
    assert status["lifecycle_state"] == "finalized"
    assert status["decision_complete"] is True
    assert status["ready"] is True
    assert "human_approval" in status["refs"]
    assert "approval_challenge" not in status["refs"]
    assert "Result: approval=approved, final=select, ready=true" in console.transcript
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported["snapshot_sha256"] == status["snapshot_sha256"]
    assert exported["status"]["ready"] is True
    assert "human_approval" in exported["artifacts"]
    assert "Active artifacts made stale" in console.transcript
    assert "Viewer export complete" in console.transcript
    assert not console.responses
