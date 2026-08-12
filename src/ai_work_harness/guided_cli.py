"""Human-oriented, resumable line workflow for v2 decisions."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ai_work_harness.decision.guidance import OperatorAction
from ai_work_harness.decision.operator import GuidedOperator
from ai_work_harness.decision.providers import FixtureProvider
from ai_work_harness.decision.service import DecisionService, load_payload
from ai_work_harness.errors import HarnessError
from ai_work_harness.guided_io import (
    ConsolePort,
    MessageCatalog,
    PromptChoice,
    QuitRequested,
    TerminalConsole,
    get_catalog,
    prompt_choice,
    prompt_int,
    prompt_list,
    prompt_nonblank,
    prompt_optional,
    prompt_path,
    prompt_yes_no,
)
from ai_work_harness.io_utils import atomic_write_bytes

_SUBJECTS = {
    "decision_frame": ("decision-frame", "frame"),
    "candidate_set": ("candidate-set", "candidates"),
    "criteria_set": ("criteria-set", "criteria"),
}

_CHANGE_REFS = (
    "source_manifest",
    "decision_frame",
    "candidate_set",
    "criteria_set",
    "evidence_set",
    "evaluation_set",
    "final_decision",
)

OpenAIProviderFactory = Callable[[Mapping[str, Any]], Any]


def _display_value(value: Any) -> Any:
    """Convert immutable service views into ordinary JSON display values."""

    if isinstance(value, Mapping):
        return {str(key): _display_value(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_display_value(child) for child in value]
    return value


class GuidedDecisionController:
    """Render one pinned operator plan at a time and delegate all policy to core."""

    def __init__(
        self,
        operator: GuidedOperator,
        console: ConsolePort,
        *,
        root: Path,
        language: str = "ko",
        openai_provider_factory: OpenAIProviderFactory | None = None,
    ) -> None:
        self.operator = operator
        self.console = console
        self.root = root.resolve()
        self.catalog = get_catalog(language)
        self.language = self.catalog.language
        self.openai_provider_factory = openai_provider_factory

    def _t(self, key: str, **values: object) -> str:
        return self.catalog.text(key, **values)

    def _bi(self, korean: str, english: str) -> str:
        return korean if self.language == "ko" else english

    def _choice(
        self,
        prompt: str,
        choices: Sequence[PromptChoice],
        *,
        default: str | None = None,
        allow_quit: bool = True,
    ) -> str:
        return prompt_choice(
            self.console,
            prompt,
            choices,
            default=default,
            invalid_message=self._t("error.invalid_choice"),
            allow_quit=allow_quit,
        )

    def _yes_no(self, prompt: str, *, default: bool = False) -> bool:
        suffix = self._t("prompt.yes_no_default_yes" if default else "prompt.yes_no_default_no")
        return prompt_yes_no(
            self.console,
            f"{prompt} {suffix}",
            default=default,
            invalid_message=self._t("error.invalid_yes_no"),
        )

    def _nonblank(self, prompt: str) -> str:
        return prompt_nonblank(
            self.console,
            prompt,
            invalid_message=self._t("error.nonblank"),
        )

    def _list(self, prompt: str, *, minimum: int = 0) -> list[str]:
        return list(
            prompt_list(
                self.console,
                prompt,
                min_items=minimum,
                invalid_message=self._t("error.invalid_list"),
            )
        )

    def _optional(self, prompt: str) -> str | None:
        return prompt_optional(self.console, prompt)

    def _payload(self, ref_name: str) -> Mapping[str, Any]:
        payload = self.operator.semantic_payload(ref_name)
        return payload if isinstance(payload, Mapping) else {}

    def _write_json(self, value: Mapping[str, Any]) -> None:
        self.console.write(
            json.dumps(
                _display_value(value),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )

    def _header(self) -> None:
        plan = self.operator.plan
        stage = self._t(f"stage.{plan.stage.value}")
        next_action = self._t(f"action.{plan.recommended_action.value}")
        self.console.write("")
        self.console.write(self._t("app.title"))
        self.console.write(
            self._t(
                "header",
                root=str(self.root),
                session_id=self.operator.cursor.session_id,
                generation=self.operator.cursor.generation,
                snapshot=f"{self.operator.cursor.pinned_snapshot_sha256[:12]}…",
                state=stage,
                verified=self._bi("예", "yes"),
                next_action=next_action,
            )
        )
        if plan.stale_reasons:
            label = self._bi("Stale 사유", "Stale reasons")
            self.console.write(f"{label}: {', '.join(plan.stale_reasons)}")
        if len(plan.available_actions) > 1:
            label = self._bi("가능한 작업", "Available actions")
            actions = ", ".join(self._t(f"action.{item.value}") for item in plan.available_actions)
            self.console.write(f"{label}: {actions}")

    def _navigation(self) -> str:
        choices = [
            PromptChoice("continue", self._t("continue"), aliases=("c", "계속")),
        ]
        if any(ref in self.operator.state.refs for ref in _CHANGE_REFS):
            choices.append(PromptChoice("change", self._t("change"), aliases=("m", "변경")))
        choices.append(PromptChoice("quit", self._t("quit"), aliases=("exit",)))
        selected = self._choice(self._t("prompt.choice"), choices, default="continue")
        if selected == "quit":
            raise QuitRequested
        return selected

    def run(self) -> str:
        """Run until a terminal result or an explicit resumable quit."""

        try:
            while True:
                self._header()
                if self.operator.plan.stage.value == "complete":
                    if self._terminal():
                        return "complete"
                    continue
                if self.operator.plan.recommended_action is OperatorAction.DERIVE_COMPARISON:
                    self.console.write(self._t("comparison_auto"))
                    if not self._perform(self.operator.compare):
                        continue
                    self._show_comparison()
                    continue
                if self._navigation() == "change":
                    self._change_menu()
                    continue
                self._dispatch(self.operator.plan.recommended_action)
        except QuitRequested:
            self.console.write(self._t("quit.done", session_id=self.operator.cursor.session_id))
            self._resume_summary()
            return "quit"

    def _perform(self, operation: Any, /, *args: Any, **kwargs: Any) -> bool:
        try:
            before = self.operator.cursor
            operation(*args, **kwargs)
            if self.operator.cursor == before:
                self.console.write(
                    self._bi(
                        "내용이 기존 산출물과 같아 새 snapshot을 만들지 않았습니다.",
                        "The content matched the active artifact; no new snapshot was created.",
                    )
                )
            return True
        except HarnessError as exc:
            if exc.code in {"CHALLENGE_EXPIRED", "CHALLENGE_CLOCK_REGRESSION"}:
                self.console.write(self._t("error.domain", code=exc.code, message=exc.message))
                self.operator.reload()
                return False
            if exc.code == "WRITE_CONFLICT":
                self.console.write(self._t("conflict.detected"))
                expected = str(
                    exc.details.get("expected_parent")
                    or exc.details.get("expected")
                    or self.operator.cursor.pinned_snapshot_sha256
                )
                actual = str(
                    exc.details.get("actual_parent")
                    or exc.details.get("current")
                    or self._bi("알 수 없음", "unknown")
                )
                self.console.write(
                    self._t(
                        "conflict.current",
                        expected=f"{expected[:12]}…",
                        actual=f"{actual[:12]}…",
                    )
                )
                if self._yes_no(self._t("conflict.reload"), default=False):
                    self.operator.reload()
                    self.console.write(self._t("reload.done"))
                    return False
                raise
            if exc.exit_code == 2:
                self.console.write(self._t("error.domain", code=exc.code, message=exc.message))
                return False
            raise

    def _dispatch(self, action: OperatorAction) -> None:
        handlers = {
            OperatorAction.CAPTURE_SOURCE: self._capture_source,
            OperatorAction.IMPORT_FRAME: lambda: self._edit_subject("decision_frame"),
            OperatorAction.CONFIRM_FRAME: lambda: self._confirm_subject("decision_frame"),
            OperatorAction.IMPORT_CANDIDATES: lambda: self._edit_subject("candidate_set"),
            OperatorAction.CONFIRM_CANDIDATES: lambda: self._confirm_subject("candidate_set"),
            OperatorAction.IMPORT_CRITERIA: lambda: self._edit_subject("criteria_set"),
            OperatorAction.CONFIRM_CRITERIA: lambda: self._confirm_subject("criteria_set"),
            OperatorAction.IMPORT_EVIDENCE: self._edit_evidence,
            OperatorAction.IMPORT_EVALUATIONS: self._edit_evaluations,
            OperatorAction.GENERATE_EVALUATIONS: self._edit_evaluations,
            OperatorAction.RETRY_EVALUATIONS: self._edit_evaluations,
            OperatorAction.REVIEW_EVALUATIONS: self._review_evaluations,
            OperatorAction.REVISE_EVALUATIONS: self._edit_evaluations,
            OperatorAction.GENERATE_RECOMMENDATION: self._recommendation,
            OperatorAction.IMPORT_RECOMMENDATION: self._recommendation,
            OperatorAction.RETRY_RECOMMENDATION: self._recommendation,
            OperatorAction.RECORD_FINAL_DECISION: self._final_decision,
            OperatorAction.CREATE_APPROVAL_CHALLENGE: self._approval,
            OperatorAction.COMMIT_APPROVAL: self._approval,
            OperatorAction.REISSUE_APPROVAL_CHALLENGE: self._approval,
            OperatorAction.REFRESH_STATE: self._refresh_state,
            OperatorAction.EXPORT_DECISION: self._terminal,
        }
        handler = handlers.get(action)
        if handler is None:
            raise HarnessError("GUIDED_ACTION_UNSUPPORTED", f"Unsupported action: {action.value}")
        handler()

    def _refresh_state(self) -> None:
        self.console.write(
            self._bi(
                "현재 시각에는 challenge가 아직 유효하지 않습니다.",
                "The challenge is not valid at the current time.",
            )
        )
        if not self._yes_no(
            self._bi("현재 상태를 다시 읽을까요?", "Reload the current state?"),
            default=False,
        ):
            raise QuitRequested
        self.operator.reload()

    def _resume_summary(self) -> None:
        self.console.write(
            self._bi(
                (
                    f"재개 상태: generation {self.operator.cursor.generation}, "
                    f"snapshot {self.operator.cursor.pinned_snapshot_sha256[:12]}…"
                ),
                (
                    f"Resume state: generation {self.operator.cursor.generation}, "
                    f"snapshot {self.operator.cursor.pinned_snapshot_sha256[:12]}…"
                ),
            )
        )

    def _capture_source(self) -> None:
        source_id = self._nonblank(self._bi("Source ID: ", "Source ID: "))
        source = prompt_path(
            self.console,
            self._bi("UTF-8 text/Markdown 파일: ", "UTF-8 text/Markdown file: "),
            must_exist=True,
            file_only=True,
            invalid_message=self._t("error.invalid_path"),
        )
        media_type = self._optional(
            self._bi(
                "Media type (자동 감지는 Enter): ",
                "Media type (Enter to auto-detect): ",
            )
        )
        self._perform(
            self.operator.capture_source,
            source_id=source_id,
            source=source,
            media_type=media_type,
        )

    def _draft_mode(self, subject: str, *, allow_agent: bool = True) -> str:
        choices = [
            PromptChoice("form", self._t("draft.create")),
            PromptChoice("json", self._t("draft.import")),
        ]
        if allow_agent:
            choices.append(PromptChoice("agent", self._t("draft.agent")))
        return self._choice(
            self._t("draft_mode", subject=subject),
            tuple(choices),
        )

    def _load_json_payload(self) -> Any:
        while True:
            path = prompt_path(
                self.console,
                self._t("draft.path"),
                must_exist=True,
                file_only=True,
                invalid_message=self._t("error.invalid_path"),
            )
            try:
                return load_payload(path)
            except HarnessError as exc:
                if exc.exit_code != 2:
                    raise
                self.console.write(self._t("error.domain", code=exc.code, message=exc.message))

    def _maybe_save_draft(self, subject: str, payload: Mapping[str, Any]) -> None:
        if not self._yes_no(
            self._bi(
                "이 초안을 지정한 외부 JSON 파일에도 저장할까요?",
                "Also save this draft to an explicit external JSON file?",
            ),
            default=False,
        ):
            return
        while True:
            path = prompt_path(
                self.console,
                self._bi("저장 경로: ", "Save path: "),
                invalid_message=self._t("error.invalid_path"),
            )
            try:
                target = self._validated_external_output_path(path, kind="Draft")
                if target.exists() and not self._yes_no(
                    self._bi(
                        "기존 파일을 덮어쓸까요?",
                        "Overwrite the existing file?",
                    ),
                    default=False,
                ):
                    return
                document = (
                    f"{json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)}\n"
                ).encode()
                atomic_write_bytes(target, document)
            except HarnessError as exc:
                self.console.write(self._t("error.domain", code=exc.code, message=exc.message))
                continue
            except OSError as exc:
                self.console.write(
                    self._t("error.domain", code="FILESYSTEM_ERROR", message=str(exc))
                )
                continue
            self.console.write(self._t("saved", subject=subject))
            return

    def _validated_external_output_path(self, path: Path, *, kind: str) -> Path:
        unsafe_code = "UNSAFE_DRAFT_PATH" if kind == "Draft" else "UNSAFE_EXPORT_PATH"
        invalid_code = "INVALID_DRAFT_PATH" if kind == "Draft" else "INVALID_EXPORT_PATH"
        absolute = path.expanduser().absolute()
        managed = (self.root / ".ai-work-harness" / "v2").resolve()
        resolved = absolute.resolve(strict=False)
        if absolute.is_relative_to(managed) or resolved.is_relative_to(managed):
            raise HarnessError(
                unsafe_code,
                f"{kind} output must be outside managed v2 storage",
            )
        if not absolute.parent.is_dir():
            raise HarnessError(
                invalid_code,
                f"{kind} output parent does not exist",
            )
        existing_components = (absolute, *absolute.parents)
        if any(component.is_symlink() for component in existing_components):
            raise HarnessError(
                unsafe_code,
                f"{kind} output and its parent path must not use symlinks",
            )
        if absolute.exists() and not absolute.is_file():
            raise HarnessError(
                invalid_code,
                f"{kind} output must be a file path",
            )
        return absolute

    def _edit_subject(self, ref_name: str) -> None:
        _subject_type, subject = _SUBJECTS[ref_name]
        mode = self._draft_mode(subject)
        if mode == "agent":
            self.console.write(
                self._bi(
                    "Agent/MCP가 초안을 기록한 뒤 같은 guide 명령으로 재개하세요.",
                    "Have the agent/MCP record its draft, then resume the same guide command.",
                )
            )
            raise QuitRequested
        payload = self._load_json_payload() if mode == "json" else self._subject_form(ref_name)
        if isinstance(payload, Mapping):
            self._maybe_save_draft(subject, payload)
        operation = {
            "decision_frame": self.operator.import_frame,
            "candidate_set": self.operator.import_candidates,
            "criteria_set": self.operator.import_criteria,
        }[ref_name]
        self._perform(operation, payload)

    def _subject_form(self, ref_name: str) -> dict[str, Any]:
        if ref_name == "decision_frame":
            return self._frame_form()
        if ref_name == "candidate_set":
            return self._candidate_form()
        if ref_name == "criteria_set":
            return self._criteria_form()
        raise AssertionError(f"unknown guided subject: {ref_name}")

    def _frame_form(self) -> dict[str, Any]:
        return {
            "user_statement_verbatim": self._nonblank(
                self._bi("사용자 원문: ", "User statement verbatim: ")
            ),
            "ai_initial_interpretation": self._nonblank(
                self._bi("AI 초기 해석: ", "AI initial interpretation: ")
            ),
            "business_user": self._nonblank(self._bi("업무 사용자: ", "Business user: ")),
            "blocked_decision": self._nonblank(self._bi("막힌 결정: ", "Blocked decision: ")),
            "problem_statement": self._nonblank(self._bi("문제문: ", "Problem statement: ")),
            "scope_in": self._list(
                self._bi("포함 범위 (쉼표, 없으면 Enter): ", "Scope in (comma list or Enter): ")
            ),
            "scope_out": self._list(
                self._bi("제외 범위 (쉼표, 없으면 Enter): ", "Scope out (comma list or Enter): ")
            ),
            "assumptions": self._list(
                self._bi("가정 (쉼표, 없으면 Enter): ", "Assumptions (comma list or Enter): ")
            ),
            "open_questions": self._list(
                self._bi(
                    "미확정 질문 (쉼표, 없으면 Enter): ",
                    "Open questions (comma list or Enter): ",
                )
            ),
        }

    def _candidate_form(self) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        while len(candidates) < 2 or self._yes_no(
            self._bi("후보를 하나 더 추가할까요?", "Add another candidate?"),
            default=False,
        ):
            candidates.append(
                {
                    "candidate_id": self._nonblank("Candidate ID: "),
                    "title": self._nonblank(self._bi("후보 이름: ", "Candidate title: ")),
                    "summary": self._nonblank(self._bi("후보 설명: ", "Candidate summary: ")),
                    "proposed_by": self._nonblank(self._bi("제안 주체: ", "Proposed by: ")),
                    "benefits": self._list(
                        self._bi("장점 (쉼표): ", "Benefits (comma list): "), minimum=1
                    ),
                    "drawbacks": self._list(
                        self._bi("단점 (쉼표): ", "Drawbacks (comma list): "), minimum=1
                    ),
                    "risks": self._list(
                        self._bi("위험 (쉼표): ", "Risks (comma list): "), minimum=1
                    ),
                    "uncertainties": self._list(
                        self._bi("불확실성 (쉼표): ", "Uncertainties (comma list): "),
                        minimum=1,
                    ),
                }
            )
        return {"candidates": candidates}

    def _criteria_form(self) -> dict[str, Any]:
        criteria: list[dict[str, Any]] = []
        while not criteria or self._yes_no(
            self._bi("기준을 하나 더 추가할까요?", "Add another criterion?"),
            default=False,
        ):
            priority = self._choice(
                self._bi("우선순위: ", "Priority: "),
                tuple(PromptChoice(value, value) for value in ("must", "high", "medium", "low")),
            )
            criteria.append(
                {
                    "criterion_id": self._nonblank("Criterion ID: "),
                    "title": self._nonblank(self._bi("기준 이름: ", "Criterion title: ")),
                    "definition": self._nonblank(self._bi("기준 정의: ", "Criterion definition: ")),
                    "priority": priority,
                }
            )
        if not any(item["priority"] == "must" for item in criteria):
            self.console.write(
                self._bi(
                    "최소 한 개의 must 기준이 필요합니다. must 기준을 추가합니다.",
                    "At least one must criterion is required; add one now.",
                )
            )
            criteria.append(
                {
                    "criterion_id": self._nonblank("Must criterion ID: "),
                    "title": self._nonblank(self._bi("기준 이름: ", "Criterion title: ")),
                    "definition": self._nonblank(self._bi("기준 정의: ", "Criterion definition: ")),
                    "priority": "must",
                }
            )
        return {"criteria": criteria}

    def _confirm_subject(self, ref_name: str) -> None:
        subject_type, subject = _SUBJECTS[ref_name]
        payload = self._payload(ref_name)
        digest = self.operator.state.refs[ref_name]
        self.console.write(self._t("confirmation.heading", subject=subject))
        self._write_json(payload)
        self.console.write(self._t("confirmation.fingerprint", fingerprint=digest[:12]))
        selected = self._choice(
            self._t("prompt.choice"),
            (
                PromptChoice("confirm", self._t("confirmation.confirm", subject=subject)),
                PromptChoice("edit", self._t("confirmation.edit", subject=subject)),
                PromptChoice("quit", self._t("confirmation.quit")),
            ),
        )
        if selected == "quit":
            raise QuitRequested
        if selected == "edit":
            self._edit_subject(ref_name)
            return
        self._perform(self.operator.confirm, subject_type)

    def _edit_evidence(self) -> None:
        mode = self._draft_mode("evidence", allow_agent=False)
        payload = self._load_json_payload() if mode == "json" else self._evidence_form()
        if isinstance(payload, Mapping):
            self._maybe_save_draft("evidence", payload)
        self._perform(self.operator.import_evidence, payload)

    def _evidence_form(self) -> dict[str, Any]:
        evidence: list[dict[str, Any]] = []
        while not evidence or self._yes_no(
            self._bi("근거를 하나 더 추가할까요?", "Add another evidence item?"),
            default=False,
        ):
            provenance = self._choice(
                self._bi("근거 유형: ", "Evidence provenance: "),
                (
                    PromptChoice("source_observation", "source_observation"),
                    PromptChoice("user_assertion", "user_assertion"),
                    PromptChoice("agent_inference", "agent_inference"),
                ),
            )
            item: dict[str, Any] = {
                "evidence_id": self._nonblank("Evidence ID: "),
                "claim": self._nonblank(self._bi("Claim: ", "Claim: ")),
                "provenance": provenance,
                "source": None,
            }
            if provenance == "source_observation":
                sources = self.operator.plan.summary.get("sources", ())
                source_ids = [
                    source["source_id"]
                    for source in sources
                    if isinstance(source, Mapping) and isinstance(source.get("source_id"), str)
                ]
                if not source_ids:
                    raise HarnessError("SOURCE_REQUIRED", "No captured source is available")
                source_id = self._choice(
                    self._bi("Source 선택: ", "Choose source: "),
                    tuple(PromptChoice(value, value) for value in source_ids),
                )
                start, end, excerpt = self._prompt_source_excerpt(source_id)
                self._render_excerpt(excerpt, start_line=start)
                item["source"] = {
                    "source_id": source_id,
                    "start_line": start,
                    "end_line": end,
                    "excerpt_sha256": excerpt["excerpt_sha256"],
                }
            notes = self._optional(self._bi("메모 (선택): ", "Notes (optional): "))
            if notes is not None:
                item["notes"] = notes
            evidence.append(item)
        return {"evidence": evidence}

    def _prompt_source_excerpt(
        self,
        source_id: str,
    ) -> tuple[int, int, Mapping[str, Any]]:
        while True:
            start = prompt_int(
                self.console,
                self._bi("시작 행 (1-based): ", "Start line (1-based): "),
                minimum=1,
                invalid_message=self._t("error.invalid_integer"),
            )
            end = prompt_int(
                self.console,
                self._bi("끝 행 (inclusive): ", "End line (inclusive): "),
                minimum=start,
                invalid_message=self._t("error.invalid_integer"),
            )
            try:
                return (
                    start,
                    end,
                    self.operator.service.read_source_excerpt(
                        snapshot_sha256=self.operator.cursor.pinned_snapshot_sha256,
                        source_id=source_id,
                        start_line=start,
                        end_line=end,
                    ),
                )
            except HarnessError as exc:
                if exc.exit_code != 2:
                    raise
                self.console.write(self._t("error.domain", code=exc.code, message=exc.message))

    def _render_excerpt(
        self,
        excerpt: Mapping[str, Any],
        *,
        start_line: int,
    ) -> None:
        self.console.write(self._bi("[인용 범위]", "[Cited excerpt]"))
        for line_number, line in enumerate(
            str(excerpt["excerpt"]).splitlines(),
            start=start_line,
        ):
            self.console.write(f"{line_number:>4}: {line}")

    def _edit_evaluations(self) -> None:
        if self.operator.state.outbound_consent_operation == "evaluations":
            recovery = self._openai_recovery_choice("evaluations")
            if recovery == "retry":
                self._run_consented_openai("evaluations")
                return
            if recovery == "agent":
                self.console.write(
                    self._bi(
                        "MCP/agent가 evaluation draft를 기록한 뒤 재개하세요.",
                        "Have MCP/the agent record an evaluation draft, then resume.",
                    )
                )
                raise QuitRequested
            payload = self._load_json_payload()
            self._perform(self.operator.import_evaluations, payload)
            return
        choices = [PromptChoice("json", self._t("draft.import"))]
        if self.operator.plan.recommended_action is not OperatorAction.REVISE_EVALUATIONS:
            choices[:0] = (
                PromptChoice("fixture", "Fixture"),
                PromptChoice("openai", self._t("openai.option")),
            )
        choices.append(PromptChoice("agent", self._t("draft.agent")))
        selected = self._choice(
            self._bi("평가 초안 방식: ", "Evaluation draft mode: "),
            tuple(choices),
        )
        if selected == "agent":
            self.console.write(
                self._bi(
                    "MCP/agent가 evaluation draft를 기록한 뒤 재개하세요.",
                    "Have MCP/the agent record an evaluation draft, then resume.",
                )
            )
            raise QuitRequested
        if selected == "json":
            payload = self._load_json_payload()
            self._perform(self.operator.import_evaluations, payload)
            return
        if selected == "openai":
            self._fresh_openai("evaluations")
            return
        fixture = FixtureProvider().evaluation_provider()
        self._perform(
            self.operator.mutate,
            self.operator.service.generate_evaluations,
            fixture,
            producer_kind="fixture",
        )

    def _review_evaluations(self) -> None:
        reviews: list[dict[str, Any]] = []
        evidence = self._payload("evidence_set").get("evidence", ())
        evidence_by_id = {
            item["evidence_id"]: item
            for item in evidence
            if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)
        }
        source_evidence_ids = tuple(
            sorted(
                evidence_id
                for evidence_id, item in evidence_by_id.items()
                if item.get("provenance") == "source_observation"
            )
        )
        for pending in self.operator.plan.pending_reviews:
            self.console.write(
                self._t(
                    "review.heading",
                    candidate=pending.candidate_title,
                    criterion=pending.criterion_title,
                )
            )
            self.console.write(
                self._t(
                    "review.assessment",
                    assessment=pending.assessment,
                    priority=pending.priority,
                )
            )
            self.console.write(
                self._bi(
                    f"신뢰도: {pending.confidence}",
                    f"Confidence: {pending.confidence}",
                )
            )
            self.console.write(self._t("review.rationale", rationale=pending.rationale))
            if pending.uncertainties:
                self.console.write(
                    self._t(
                        "review.uncertainty",
                        uncertainty="; ".join(pending.uncertainties),
                    )
                )
            self.console.write(self._bi("[인용 근거]", "[Cited evidence]"))
            for evidence_id in pending.evidence_ids:
                item = evidence_by_id.get(evidence_id)
                if item is not None:
                    self._render_evidence_item(item)
            outcome = self._choice(
                self._t("prompt.choice"),
                (
                    PromptChoice("concur", self._t("review.concur")),
                    PromptChoice("override", self._t("review.override")),
                    PromptChoice("request_revision", self._t("review.request_revision")),
                ),
            )
            review: dict[str, Any] = {
                "candidate_id": pending.candidate_id,
                "criterion_id": pending.criterion_id,
                "outcome": outcome,
                "reason": self._nonblank(self._t("review.reason")),
            }
            if outcome == "override":
                replacement = self._choice(
                    self._t("review.replacement"),
                    tuple(
                        PromptChoice(value, value)
                        for value in (
                            "meets",
                            "partial",
                            "fails",
                            "insufficient_evidence",
                            "not_applicable",
                        )
                    ),
                )
                replacement_evidence = (
                    []
                    if replacement == "insufficient_evidence"
                    else self._prompt_evidence_ids(
                        source_evidence_ids,
                        evidence_by_id,
                    )
                )
                review.update(
                    {
                        "replacement_assessment": replacement,
                        "replacement_evidence_ids": replacement_evidence,
                    }
                )
            reviews.append(review)
            if outcome == "request_revision":
                break
        if not reviews:
            raise HarnessError("REVIEW_REQUIRED", "No pending review cells are available")
        self._write_json({"reviews": reviews})
        if self._yes_no(self._t("review.commit"), default=False):
            self._perform(self.operator.import_reviews, {"reviews": reviews})

    def _render_evidence_item(self, item: Mapping[str, Any]) -> None:
        evidence_id = str(item.get("evidence_id", "unknown"))
        self.console.write(f"- {evidence_id} [{item.get('provenance')}]: {item.get('claim')}")
        source = item.get("source")
        if not isinstance(source, Mapping):
            return
        excerpt = self.operator.service.read_source_excerpt(
            snapshot_sha256=self.operator.cursor.pinned_snapshot_sha256,
            source_id=str(source["source_id"]),
            start_line=int(source["start_line"]),
            end_line=int(source["end_line"]),
        )
        self._render_excerpt(excerpt, start_line=int(source["start_line"]))

    def _prompt_evidence_ids(
        self,
        allowed_ids: Sequence[str],
        evidence_by_id: Mapping[str, Mapping[str, Any]],
    ) -> list[str]:
        self.console.write(self._bi("[사용 가능한 사실 근거]", "[Available factual evidence]"))
        for evidence_id in allowed_ids:
            item = evidence_by_id[evidence_id]
            self.console.write(f"- {evidence_id}: {item.get('claim')}")
        while True:
            selected = self._list(
                self._bi(
                    "대체 evidence ID (쉼표): ",
                    "Replacement evidence IDs (comma list): ",
                ),
                minimum=1,
            )
            if set(selected).issubset(allowed_ids):
                return selected
            self.console.write(
                self._bi(
                    "표시된 source_observation evidence ID만 선택하세요.",
                    "Choose only the displayed source_observation evidence IDs.",
                )
            )

    def _show_comparison(self) -> None:
        comparison = self._payload("comparison")
        if not comparison:
            return
        candidates = {
            item["candidate_id"]: item.get("title", item["candidate_id"])
            for item in self._payload("candidate_set").get("candidates", ())
            if isinstance(item, Mapping) and isinstance(item.get("candidate_id"), str)
        }
        criteria = {
            item["criterion_id"]: item.get("title", item["criterion_id"])
            for item in self._payload("criteria_set").get("criteria", ())
            if isinstance(item, Mapping) and isinstance(item.get("criterion_id"), str)
        }
        label = self._bi("[정성 비교]", "[Qualitative comparison]")
        self.console.write(label)
        for cell in comparison.get("matrix", ()):
            if not isinstance(cell, Mapping):
                continue
            candidate_id = str(cell.get("candidate_id"))
            criterion_id = str(cell.get("criterion_id"))
            self.console.write(
                f"- {candidates.get(candidate_id, candidate_id)} [{candidate_id}] / "
                f"{criteria.get(criterion_id, criterion_id)} [{criterion_id}]: "
                f"{cell.get('effective_assessment')} · {cell.get('priority')} · "
                f"review={cell.get('review_status')}"
            )
        eligible = ", ".join(comparison.get("eligible_candidate_ids", ()))
        self.console.write(self._bi(f"적격 후보: {eligible}", f"Eligible candidates: {eligible}"))

    def _recommendation(self) -> None:
        if self.operator.state.outbound_consent_operation == "recommendation":
            recovery = self._openai_recovery_choice("recommendation")
            if recovery == "retry":
                self._run_consented_openai("recommendation")
                return
            if recovery == "agent":
                self.console.write(
                    self._bi(
                        "MCP/agent가 recommendation draft를 기록한 뒤 재개하세요.",
                        "Have MCP/the agent record a recommendation draft, then resume.",
                    )
                )
                raise QuitRequested
            payload = self._load_json_payload()
            self._perform(
                self.operator.record_recommendation,
                payload,
                producer_kind="local_operator",
            )
            return
        selected = self._choice(
            self._bi("추천 단계: ", "Recommendation step: "),
            (
                PromptChoice("fixture", "Fixture"),
                PromptChoice("openai", self._t("openai.option")),
                PromptChoice("json", self._t("draft.import")),
                PromptChoice(
                    "skip",
                    self._bi("추천 없이 최종 결정", "Continue to final without recommendation"),
                ),
            ),
        )
        if selected == "skip":
            self._final_decision()
            return
        if selected == "json":
            payload = self._load_json_payload()
            self._perform(
                self.operator.record_recommendation,
                payload,
                producer_kind="local_operator",
            )
            return
        if selected == "openai":
            self._fresh_openai("recommendation")
            return
        fixture = FixtureProvider().recommendation_provider()
        self._perform(
            self.operator.mutate,
            self.operator.service.generate_recommendation,
            fixture,
            producer_kind="fixture",
        )

    def _fresh_openai(self, operation: str) -> None:
        preview = self.operator.preview_agent(operation=operation)
        manifest = preview.get("outbound_manifest")
        manifest_sha = preview.get("outbound_manifest_sha256")
        if not isinstance(manifest, Mapping) or not isinstance(manifest_sha, str):
            raise HarnessError(
                "OUTBOUND_MANIFEST_INVALID",
                "OpenAI preview did not return a valid outbound manifest",
                exit_code=5,
            )
        self._show_openai_manifest(manifest, manifest_sha)
        fingerprint = manifest_sha[:12]
        phrase = f"SEND OPENAI {fingerprint}"
        entered = self.console.read(f"{self._t('openai.phrase', phrase=phrase)}\n> ")
        if entered != phrase:
            self.console.write(self._t("openai.mismatch"))
            return
        if not self._perform(
            self.operator.consent_agent,
            operation=operation,
            preview=preview,
        ):
            return
        self._run_consented_openai(operation)

    def _openai_recovery_choice(self, operation: str) -> str:
        consent = self._payload("agent_consent")
        manifest = consent.get("outbound_manifest")
        manifest_sha = consent.get("subject_sha256")
        if (
            not isinstance(manifest, Mapping)
            or not isinstance(manifest_sha, str)
            or manifest.get("operation") != operation
        ):
            raise HarnessError(
                "OUTBOUND_CONSENT_STALE",
                "Active outbound consent does not match the guided operation",
                exit_code=5,
            )
        self.console.write(self._t("openai.retry_intro"))
        self._show_openai_manifest(manifest, manifest_sha)
        return self._choice(
            self._t("prompt.choice"),
            (
                PromptChoice("retry", self._t("openai.retry")),
                PromptChoice("json", self._t("openai.local_import")),
                PromptChoice("agent", self._t("openai.agent_import")),
            ),
            default="retry",
        )

    def _show_openai_manifest(
        self,
        manifest: Mapping[str, Any],
        manifest_sha: str,
    ) -> None:
        self.console.write(self._t("openai.preview"))
        self.console.write(self._t("openai.operation", operation=str(manifest.get("operation"))))
        self.console.write(self._t("openai.provider", provider=str(manifest.get("provider"))))
        self.console.write(self._t("openai.model", model=str(manifest.get("model"))))
        self.console.write(self._t("openai.prompt_id", prompt_id=str(manifest.get("prompt_id"))))
        self.console.write(
            self._t(
                "openai.prompt_fingerprint",
                fingerprint=str(manifest.get("prompt_sha256", ""))[:12],
            )
        )
        self.console.write(
            self._t(
                "openai.input_fingerprint",
                fingerprint=str(manifest.get("input_sha256", ""))[:12],
            )
        )
        self.console.write(
            self._t("openai.source_bytes", bytes=str(manifest.get("source_excerpt_bytes")))
        )
        self.console.write(
            self._t("openai.evidence_count", count=str(manifest.get("evidence_count")))
        )
        self.console.write(self._t("openai.manifest_fingerprint", fingerprint=manifest_sha[:12]))

    def _run_consented_openai(self, operation: str) -> None:
        consent = self._payload("agent_consent")
        manifest = consent.get("outbound_manifest")
        if not isinstance(manifest, Mapping):
            raise HarnessError(
                "OUTBOUND_CONSENT_STALE",
                "Active outbound consent is missing its manifest",
                exit_code=5,
            )
        provider = (
            self.openai_provider_factory(manifest)
            if self.openai_provider_factory is not None
            else None
        )
        self._perform(
            self.operator.run_openai_agent,
            operation=operation,
            provider=provider,
        )

    def _final_decision(self) -> None:
        constraints = self.operator.plan.final_decision_constraints
        if constraints is None:
            raise HarnessError("WORKFLOW_GATE_REQUIRED", "Comparison is required")
        self.console.write(self._t("final.heading"))
        self._show_comparison()
        recommendation = self._payload("recommendation")
        if recommendation:
            self.console.write(
                self._bi(
                    (
                        f"AI 추천: {recommendation.get('disposition')} "
                        f"{recommendation.get('candidate_id') or ''}"
                    ),
                    (
                        f"AI recommendation: {recommendation.get('disposition')} "
                        f"{recommendation.get('candidate_id') or ''}"
                    ),
                )
            )
        dispositions = [
            PromptChoice("select", self._t("final.select")),
            PromptChoice("reject_all", self._t("final.reject_all")),
            PromptChoice("defer", self._t("final.defer")),
            PromptChoice(
                "request_more_evidence",
                self._t("final.request_more_evidence"),
            ),
        ]
        disposition = self._choice(self._t("prompt.choice"), dispositions)
        candidate_id: str | None = None
        if disposition == "select":
            if not constraints.eligible_candidate_ids:
                self.console.write(
                    self._bi(
                        "선택 가능한 적격 후보가 없습니다.",
                        "No eligible candidate can be selected.",
                    )
                )
                return
            candidate_id = self._choice(
                self._t("final.candidate"),
                tuple(PromptChoice(value, value) for value in constraints.eligible_candidate_ids),
            )
        reason = self._nonblank(self._t("final.reason"))
        acknowledgements: set[str] = set()
        if disposition == "select" and candidate_id is not None:
            required = constraints.required_by_candidate.get(candidate_id, ())
            if not self._acknowledge_risks(required):
                return
            acknowledgements.update(required)
        elif disposition == "reject_all":
            required = constraints.reject_all_required_acknowledgements
            if not self._acknowledge_risks(required):
                return
            acknowledgements.update(required)
            if constraints.reject_all_shared_risk_required:
                shared = constraints.reject_all_shared_risk_criterion_ids
                if not shared:
                    self.console.write(
                        self._bi(
                            "모든 적격 후보에 공통인 위험이 없어 reject-all을 기록할 수 없습니다.",
                            "Reject-all is unavailable because eligible candidates share no risk.",
                        )
                    )
                    return
                criterion = self._choice(
                    self._bi(
                        "모든 적격 후보의 공통 위험: ",
                        "Shared risk for every eligible candidate: ",
                    ),
                    tuple(PromptChoice(value, value) for value in shared),
                )
                acknowledgements.update(
                    f"{eligible_candidate}/{criterion}"
                    for eligible_candidate in constraints.eligible_candidate_ids
                )
        relation = self._predicted_recommendation_relation(disposition, candidate_id)
        self.console.write(self._t("final.recommendation_relation", relation=relation))
        payload = {
            "disposition": disposition,
            "candidate_id": candidate_id,
            "reason": reason,
            "risk_acknowledgements": sorted(acknowledgements),
        }
        self._write_json(payload)
        if self._yes_no(self._t("final.commit"), default=False):
            self._perform(self.operator.import_final_decision, payload)

    def _acknowledge_risks(self, cell_ids: Sequence[str]) -> bool:
        for cell_id in cell_ids:
            candidate, _, criterion = cell_id.partition("/")
            self._render_risk_cell(cell_id)
            if not self._yes_no(
                self._t("final.risk", candidate=candidate, criterion=criterion),
                default=False,
            ):
                self.console.write(
                    self._bi(
                        "필수 위험을 확인하지 않아 최종 결정을 기록하지 않았습니다.",
                        (
                            "The final decision was not recorded because a required "
                            "risk was not acknowledged."
                        ),
                    )
                )
                return False
        return True

    def _render_risk_cell(self, cell_id: str) -> None:
        candidate_id, _, criterion_id = cell_id.partition("/")
        candidate = next(
            (
                item
                for item in self._payload("candidate_set").get("candidates", ())
                if isinstance(item, Mapping) and item.get("candidate_id") == candidate_id
            ),
            {},
        )
        criterion = next(
            (
                item
                for item in self._payload("criteria_set").get("criteria", ())
                if isinstance(item, Mapping) and item.get("criterion_id") == criterion_id
            ),
            {},
        )
        evaluation = next(
            (
                item
                for item in self._payload("evaluation_set").get("cells", ())
                if isinstance(item, Mapping)
                and item.get("candidate_id") == candidate_id
                and item.get("criterion_id") == criterion_id
            ),
            {},
        )
        comparison = next(
            (
                item
                for item in self._payload("comparison").get("matrix", ())
                if isinstance(item, Mapping)
                and item.get("candidate_id") == candidate_id
                and item.get("criterion_id") == criterion_id
            ),
            {},
        )
        self.console.write(self._bi("[확인할 위험]", "[Risk to acknowledge]"))
        self.console.write(
            f"{candidate.get('title', candidate_id)} [{candidate_id}] / "
            f"{criterion.get('title', criterion_id)} [{criterion_id}]"
        )
        self.console.write(
            self._bi(
                (
                    f"유효 평가: {comparison.get('effective_assessment')} · "
                    f"우선순위: {comparison.get('priority')} · "
                    f"검토: {comparison.get('review_status')}"
                ),
                (
                    f"Effective assessment: {comparison.get('effective_assessment')} · "
                    f"priority: {comparison.get('priority')} · "
                    f"review: {comparison.get('review_status')}"
                ),
            )
        )
        self.console.write(
            self._t(
                "review.rationale",
                rationale=str(evaluation.get("rationale", "")),
            )
        )

    def _predicted_recommendation_relation(
        self,
        disposition: str,
        candidate_id: str | None,
    ) -> str:
        recommendation = self._payload("recommendation")
        if not recommendation:
            return "no_recommendation"
        if recommendation.get("disposition") != "select":
            return "no_recommendation"
        if disposition == "select" and recommendation.get("candidate_id") == candidate_id:
            return "same"
        return "different"

    def _approval(self) -> None:
        binding = self.operator.state.active_challenge
        final = self._payload("final_decision")
        self._show_decision_brief()
        if binding is None:
            final_disposition = final.get("disposition")
            choices: list[PromptChoice] = []
            if final_disposition in {"select", "reject_all"}:
                choices.append(PromptChoice("approved", self._t("approval.approved")))
            choices.extend(
                (
                    PromptChoice("rejected", self._t("approval.rejected")),
                    PromptChoice(
                        "changes_requested",
                        self._t("approval.changes_requested"),
                    ),
                )
            )
            disposition = self._choice(self._t("approval.heading"), tuple(choices))
            self._perform(
                self.operator.create_approval_challenge,
                disposition=disposition,
            )
            return

        challenge = self.operator.plan.public_challenge or {}
        status = challenge.get("status")
        if status == "expired":
            self.console.write(self._t("approval.expired"))
            if self._yes_no(self._t("approval.reissue"), default=False):
                self._perform(
                    self.operator.create_approval_challenge,
                    disposition=binding.proposed_disposition,
                )
            return
        if status != "active":
            self.console.write(
                self._bi(
                    "시계가 challenge 발급 시각보다 이전입니다. 상태를 새로 확인하세요.",
                    "The clock precedes challenge issuance; refresh the state.",
                )
            )
            return
        self.console.write(self._t("approval.heading"))
        self.console.write(
            self._t(
                "approval.expires",
                remaining=f"{challenge.get('remaining_seconds', 0)}s",
            )
        )
        fingerprint = binding.decision_bundle_sha256[:12]
        self.console.write(self._t("approval.bundle", fingerprint=fingerprint))
        target = self._approval_target(final)
        verb = {
            "approved": "APPROVE",
            "rejected": "REJECT",
            "changes_requested": "REQUEST-CHANGES",
        }[binding.proposed_disposition]
        phrase = f"{verb} {target} {fingerprint}"
        entered = self.console.read(f"{self._t('approval.phrase', phrase=phrase)}\n> ")
        if entered != phrase:
            self.console.write(
                self._bi(
                    "승인 문구가 일치하지 않아 기록하지 않았습니다.",
                    "The approval phrase did not match; nothing was recorded.",
                )
            )
            return
        reason = self._nonblank(self._t("approval.reason"))
        self._perform(self.operator.commit_approval, reason=reason)

    def _show_decision_brief(self) -> None:
        final = self._payload("final_decision")
        if not final:
            return
        candidate_id = final.get("candidate_id")
        candidate_title = candidate_id
        for item in self._payload("candidate_set").get("candidates", ()):
            if isinstance(item, Mapping) and item.get("candidate_id") == candidate_id:
                candidate_title = item.get("title", candidate_id)
                break
        self.console.write(self._bi("[승인할 결정 요약]", "[Decision approval brief]"))
        self.console.write(
            self._bi(
                (
                    f"처리: {final.get('disposition')} · 후보: "
                    f"{candidate_title or '-'} [{candidate_id or '-'}]"
                ),
                (
                    f"Disposition: {final.get('disposition')} · candidate: "
                    f"{candidate_title or '-'} [{candidate_id or '-'}]"
                ),
            )
        )
        self.console.write(
            self._bi(
                f"결정 이유: {final.get('reason')}",
                f"Decision reason: {final.get('reason')}",
            )
        )
        self.console.write(
            self._t(
                "final.recommendation_relation",
                relation=str(final.get("recommendation_relation")),
            )
        )
        recommendation = self._payload("recommendation")
        if recommendation:
            self.console.write(
                self._bi(
                    (
                        f"AI 추천: {recommendation.get('disposition')} "
                        f"{recommendation.get('candidate_id') or '-'} · "
                        f"{recommendation.get('rationale')}"
                    ),
                    (
                        f"AI recommendation: {recommendation.get('disposition')} "
                        f"{recommendation.get('candidate_id') or '-'} · "
                        f"{recommendation.get('rationale')}"
                    ),
                )
            )
        acknowledgements = final.get("risk_acknowledgements", ())
        if acknowledgements:
            self.console.write(self._bi("확인한 위험:", "Acknowledged risks:"))
            for cell_id in acknowledgements:
                if isinstance(cell_id, str):
                    self._render_risk_cell(cell_id)

    @staticmethod
    def _approval_target(final: Mapping[str, Any]) -> str:
        if final.get("disposition") == "select" and isinstance(final.get("candidate_id"), str):
            return str(final["candidate_id"])
        if final.get("disposition") == "reject_all":
            return "REJECT-ALL"
        return "DECISION"

    def _change_menu(self) -> None:
        active = [ref_name for ref_name in _CHANGE_REFS if ref_name in self.operator.state.refs]
        if not active:
            return
        selected = self._choice(
            self._bi("변경할 항목: ", "Artifact to change: "),
            tuple(PromptChoice(ref_name, ref_name.replace("_", " ")) for ref_name in active),
        )
        stale = self.operator.service.preview_invalidation(
            snapshot_sha256=self.operator.cursor.pinned_snapshot_sha256,
            ref_name=selected,
        )
        if stale:
            self.console.write(
                self._bi(
                    f"변경 시 stale 되는 활성 산출물: {', '.join(stale)}",
                    f"Active artifacts made stale: {', '.join(stale)}",
                )
            )
        if not self._yes_no(
            self._bi("이 변경을 계속할까요?", "Continue with this change?"),
            default=False,
        ):
            return
        if selected == "source_manifest":
            self._capture_source()
        elif selected in _SUBJECTS:
            self._edit_subject(selected)
        elif selected == "evidence_set":
            self._edit_evidence()
        elif selected == "evaluation_set":
            self._edit_evaluations()
        elif selected == "final_decision":
            self._final_decision()

    def _terminal(self) -> bool:
        approval = self._payload("human_approval")
        disposition = approval.get("disposition")
        final = self._payload("final_decision")
        self.console.write(
            self._bi(
                (
                    f"결과: approval={disposition}, final={final.get('disposition')}, "
                    f"ready={str(self.operator.plan.ready).lower()}"
                ),
                (
                    f"Result: approval={disposition}, final={final.get('disposition')}, "
                    f"ready={str(self.operator.plan.ready).lower()}"
                ),
            )
        )
        actions = [
            PromptChoice(
                "finish",
                self._bi("완료하고 종료", "Finish and exit"),
            ),
            PromptChoice(
                "export",
                self._bi("Viewer JSON 내보내기", "Export viewer JSON"),
            ),
            PromptChoice("change", self._t("change")),
        ]
        if disposition in {"rejected", "changes_requested"}:
            actions.append(
                PromptChoice(
                    "revise",
                    self._bi("최종 결정 수정", "Revise the final decision"),
                )
            )
        actions.append(PromptChoice("quit", self._t("quit")))
        selected = self._choice(
            self._t("prompt.choice"),
            tuple(actions),
            default="finish",
        )
        if selected == "finish":
            return True
        if selected == "quit":
            raise QuitRequested
        if selected == "change":
            self._change_menu()
            return False
        if selected == "revise":
            self._final_decision()
            return False
        while True:
            export_path = prompt_path(
                self.console,
                self._bi(
                    "Viewer JSON 경로: ",
                    "Viewer JSON path: ",
                ),
                invalid_message=self._t("error.invalid_path"),
            )
            try:
                export_path = self._validated_external_output_path(
                    export_path,
                    kind="Viewer",
                )
                if export_path.exists() and not self._yes_no(
                    self._bi(
                        "기존 Viewer JSON을 덮어쓸까요?",
                        "Overwrite the existing viewer JSON?",
                    ),
                    default=False,
                ):
                    continue
                self.operator.service.export_view(
                    Path(export_path),
                    snapshot_sha256=self.operator.cursor.pinned_snapshot_sha256,
                )
            except HarnessError as exc:
                if exc.exit_code != 2:
                    raise
                self.console.write(self._t("error.domain", code=exc.code, message=exc.message))
                continue
            except OSError as exc:
                self.console.write(
                    self._t("error.domain", code="FILESYSTEM_ERROR", message=str(exc))
                )
                continue
            self.console.write(self._bi("Viewer export 완료.", "Viewer export complete."))
            return False


def run_guided_session(
    service: DecisionService,
    console: ConsolePort,
    *,
    root: Path,
    language: str,
    openai_provider_factory: OpenAIProviderFactory | None = None,
) -> str:
    """Run an already-created session with injectable service and console ports."""

    operator = GuidedOperator(service)
    return GuidedDecisionController(
        operator,
        console,
        root=root,
        language=language,
        openai_provider_factory=openai_provider_factory,
    ).run()


def run_guided_cli(
    root: Path,
    session_id: str,
    *,
    language: str = "ko",
    console: ConsolePort | None = None,
    openai_provider_factory: OpenAIProviderFactory | None = None,
) -> int:
    """TTY entry point with stable guided exit semantics."""

    actual_console = TerminalConsole() if console is None else console
    catalog: MessageCatalog = get_catalog(language)
    if not actual_console.is_interactive():
        actual_console.write(
            catalog.text(
                "error.domain",
                code="INTERACTIVE_TERMINAL_REQUIRED",
                message=catalog.text("terminal"),
            )
        )
        return 2
    try:
        resolved_root = root.resolve()
        service = DecisionService(resolved_root, session_id)
        try:
            operator = GuidedOperator(service)
            actual_console.write(catalog.text("session.resume", session_id=session_id))
        except HarnessError as exc:
            if exc.code != "SESSION_NOT_FOUND":
                raise
            actual_console.write(catalog.text("root.label", root=str(resolved_root)))
            actual_console.write(catalog.text("session.label", session_id=session_id))
            create = prompt_yes_no(
                actual_console,
                f"{catalog.text('session.create', session_id=session_id)} "
                f"{catalog.text('prompt.yes_no_default_no')}",
                default=False,
                invalid_message=catalog.text("error.invalid_yes_no"),
            )
            if not create:
                actual_console.write(catalog.text("quit.done", session_id=session_id))
                return 0
            service.initialize()
            actual_console.write(catalog.text("session.created", session_id=session_id))
            operator = GuidedOperator(service)
        return (
            0
            if GuidedDecisionController(
                operator,
                actual_console,
                root=resolved_root,
                language=language,
                openai_provider_factory=openai_provider_factory,
            ).run()
            in {"quit", "complete"}
            else 1
        )
    except QuitRequested:
        actual_console.write(catalog.text("quit.done", session_id=session_id))
        return 0
    except KeyboardInterrupt:
        actual_console.write(catalog.text("interrupted"))
        return 130
    except EOFError:
        actual_console.write(
            catalog.text(
                "error.domain",
                code="INTERACTIVE_INPUT_CLOSED",
                message=catalog.text("input_closed"),
            )
        )
        return 2
    except HarnessError as exc:
        actual_console.write(catalog.text("error.domain", code=exc.code, message=exc.message))
        return exc.exit_code
    except OSError as exc:
        actual_console.write(
            catalog.text("error.domain", code="FILESYSTEM_ERROR", message=str(exc))
        )
        return 2
    except Exception:  # pragma: no cover - defensive CLI boundary
        actual_console.write(
            catalog.text(
                "error.domain",
                code="INTERNAL_ERROR",
                message="An unexpected internal error occurred",
            )
        )
        return 1
