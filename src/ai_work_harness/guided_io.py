"""Standard-library console and localization primitives for guided workflows.

This module knows how to read and validate lines.  It deliberately knows
nothing about repositories, snapshots, mutations, or decision policy so the
same helpers can be driven by a real terminal or a scripted test console.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Formatter
from types import MappingProxyType
from typing import Protocol, TextIO, runtime_checkable

SUPPORTED_LANGUAGES = ("ko", "en")
DEFAULT_QUIT_TOKENS = frozenset({"q", "quit", "종료"})


class QuitRequested(Exception):
    """The operator explicitly selected the resumable quit path."""


@runtime_checkable
class ConsolePort(Protocol):
    """Minimal line-oriented console boundary used by the guided runner."""

    def is_interactive(self) -> bool: ...

    def write(self, text: str) -> None: ...

    def read(self, prompt: str = "") -> str: ...


@dataclass(slots=True)
class TerminalConsole:
    """ConsolePort adapter over terminal-like text streams."""

    input_stream: TextIO
    output_stream: TextIO

    def __init__(
        self,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
    ) -> None:
        self.input_stream = sys.stdin if input_stream is None else input_stream
        self.output_stream = sys.stdout if output_stream is None else output_stream

    def is_interactive(self) -> bool:
        input_isatty = getattr(self.input_stream, "isatty", None)
        output_isatty = getattr(self.output_stream, "isatty", None)
        return bool(
            callable(input_isatty)
            and callable(output_isatty)
            and input_isatty()
            and output_isatty()
        )

    def write(self, text: str) -> None:
        self.output_stream.write(f"{text}\n")
        self.output_stream.flush()

    def read(self, prompt: str = "") -> str:
        self.output_stream.write(prompt)
        self.output_stream.flush()
        line = self.input_stream.readline()
        if line == "":
            raise EOFError("interactive input closed")
        return line.rstrip("\r\n")


_EN_MESSAGES = {
    "app.title": "AI Work Harness · Guided Decision",
    "continue": "Continue",
    "change": "Change",
    "quit": "Quit and resume later",
    "invalid": "Invalid input. Try again.",
    "create_session": "Create decision session '{session_id}'?",
    "session_created": "Created decision session '{session_id}'.",
    "draft_mode": "Choose how to prepare the {subject} draft.",
    "confirm": "Confirm",
    "edit": "Edit or replace",
    "saved": "Saved {subject}.",
    "comparison_auto": "Building the deterministic comparison.",
    "conflict": "Another writer changed this session. The action was not retried.",
    "reload": "Reload and review the new current state?",
    "terminal": "The guided command requires an interactive terminal.",
    "input_closed": "Interactive input closed before the prompt was completed.",
    "interrupted": "Guided input was interrupted. Current state is unchanged.",
    "error": "{code}: {message}",
    "root.label": "Root: {root}",
    "session.label": "Session: {session_id}",
    "header": (
        "Root: {root}\nSession: {session_id}\nGeneration: {generation}\n"
        "Snapshot: {snapshot}\nState: {state}\nVerified: {verified}\nNext: {next_action}"
    ),
    "header.generation": "Generation: {generation}",
    "header.snapshot": "Snapshot: {snapshot}",
    "header.state": "State: {state}",
    "header.verified": "Verified: {verified}",
    "header.next": "Next: {next_action}",
    "session.create": "Create decision session '{session_id}'?",
    "session.created": "Created decision session '{session_id}'.",
    "session.resume": "Resume decision session '{session_id}'.",
    "session.missing": "Session '{session_id}' does not exist.",
    "session.exists": "Session '{session_id}' already exists and can be resumed.",
    "quit.option": "Quit and resume later",
    "quit.prompt": "Quit the guide?",
    "quit.done": "Guide stopped. Resume later with session '{session_id}'.",
    "conflict.detected": (
        "Another writer changed this session. The attempted action was not retried."
    ),
    "conflict.current": "Pinned snapshot: {expected}; current snapshot: {actual}.",
    "conflict.reload": "Reload and review the new current state?",
    "reload.done": "Reloaded the current state. Review it before choosing another action.",
    "error.non_interactive": "The guided command requires an interactive terminal.",
    "error.input_closed": "Interactive input closed before the prompt was completed.",
    "error.invalid_choice": "Choose one of the listed options.",
    "error.nonblank": "Enter a nonblank value.",
    "error.invalid_yes_no": "Answer yes or no.",
    "error.invalid_list": "Enter a valid comma-separated list.",
    "error.invalid_integer": "Enter a whole number.",
    "error.integer_range": "Enter a number from {minimum} through {maximum}.",
    "error.invalid_path": "Enter a valid path.",
    "error.path_missing": "That path does not exist: {path}",
    "error.path_not_file": "That path is not a file: {path}",
    "error.path_not_directory": "That path is not a directory: {path}",
    "error.domain": "{code}: {message}",
    "error.unexpected": "Unexpected error: {message}",
    "prompt.choice": "Choice: ",
    "prompt.value": "> ",
    "prompt.yes_no": "[y/n] ",
    "prompt.yes_no_default_yes": "[Y/n] ",
    "prompt.yes_no_default_no": "[y/N] ",
    "stage.source": "Source capture",
    "stage.frame": "Decision frame",
    "stage.candidates": "Candidates",
    "stage.criteria": "Criteria",
    "stage.evidence": "Evidence",
    "stage.evaluation": "Evaluation",
    "stage.review": "Human review",
    "stage.comparison": "Deterministic comparison",
    "stage.recommendation": "Recommendation choice",
    "stage.final_decision": "Final decision",
    "stage.approval": "Approval",
    "stage.complete": "Complete",
    "action.capture_source": "Capture another source",
    "action.import_frame": "Create or import the decision frame",
    "action.confirm_frame": "Confirm the decision frame",
    "action.import_candidates": "Create or import candidates",
    "action.confirm_candidates": "Confirm candidates",
    "action.import_criteria": "Create or import criteria",
    "action.confirm_criteria": "Confirm criteria",
    "action.import_evidence": "Create or import evidence",
    "action.import_evaluations": "Import local evaluations",
    "action.generate_evaluations": "Generate Fixture evaluations",
    "action.retry_evaluations": "Retry evaluation generation",
    "action.review_evaluations": "Review pending evaluation cells",
    "action.revise_evaluations": "Import a revised evaluation",
    "action.derive_comparison": "Build the deterministic comparison",
    "action.import_recommendation": "Import a local recommendation",
    "action.generate_recommendation": "Generate a Fixture/OpenAI recommendation",
    "action.retry_recommendation": "Retry recommendation generation",
    "action.record_final_decision": "Record the human final decision",
    "action.create_approval_challenge": "Create an approval challenge",
    "action.commit_approval": "Commit the approval response",
    "action.reissue_approval_challenge": "Issue a new approval challenge",
    "action.refresh_state": "Reload the state",
    "action.export_decision": "Export the completed decision",
    "draft.create": "Create with the guided form",
    "draft.import": "Import a JSON payload",
    "draft.agent": "Quit and prepare an agent-authored draft",
    "draft.path": "JSON payload path: ",
    "confirmation.heading": "Validated {subject} summary",
    "confirmation.confirm": "Confirm this {subject}",
    "confirmation.edit": "Revise or replace this {subject}",
    "confirmation.quit": "Leave the draft unconfirmed and quit",
    "confirmation.fingerprint": "Artifact fingerprint: {fingerprint}",
    "review.heading": "Review required: {candidate} / {criterion}",
    "review.assessment": "Assessment: {assessment} · priority: {priority}",
    "review.rationale": "Rationale: {rationale}",
    "review.uncertainty": "Uncertainty: {uncertainty}",
    "review.concur": "Concur",
    "review.override": "Override",
    "review.request_revision": "Request revision",
    "review.reason": "Review reason: ",
    "review.replacement": "Replacement assessment: ",
    "review.commit": "Record these completed reviews?",
    "final.heading": "Human final decision",
    "final.select": "Select an eligible candidate",
    "final.reject_all": "Reject all eligible candidates",
    "final.defer": "Defer the decision",
    "final.request_more_evidence": "Request more evidence",
    "final.candidate": "Candidate: ",
    "final.reason": "Decision reason: ",
    "final.risk": "Acknowledge risk {candidate} / {criterion}?",
    "final.recommendation_relation": "Recommendation relation: {relation}",
    "final.commit": "Record this final decision?",
    "approval.heading": "Final approval",
    "approval.approved": "Approve",
    "approval.rejected": "Reject",
    "approval.changes_requested": "Request changes",
    "approval.expires": "Challenge expires in: {remaining}",
    "approval.bundle": "Decision bundle fingerprint: {fingerprint}",
    "approval.phrase": "Type exactly: {phrase}",
    "approval.reason": "Approval reason: ",
    "approval.commit": "Commit this approval response?",
    "approval.expired": "The approval challenge has expired.",
    "approval.reissue": "Issue a new challenge?",
    "openai.option": "Generate with OpenAI",
    "openai.preview": "Review outbound OpenAI request",
    "openai.provider": "Provider: {provider}",
    "openai.model": "Model: {model}",
    "openai.source_bytes": "Source excerpt bytes: {bytes}",
    "openai.prompt_fingerprint": "Prompt fingerprint: {fingerprint}",
    "openai.input_fingerprint": "Input fingerprint: {fingerprint}",
    "openai.consent": "Send this exact reviewed request to OpenAI?",
    "openai.retry": "Retry the consented OpenAI operation",
    "openai.local_import": "Import a local result instead",
}

_KO_MESSAGES = {
    "app.title": "AI Work Harness · 가이드 의사결정",
    "continue": "계속",
    "change": "변경",
    "quit": "종료하고 나중에 계속하기",
    "invalid": "올바르지 않은 입력입니다. 다시 시도하세요.",
    "create_session": "의사결정 세션 '{session_id}'을(를) 만들까요?",
    "session_created": "의사결정 세션 '{session_id}'을(를) 만들었습니다.",
    "draft_mode": "{subject} 초안을 준비할 방법을 선택하세요.",
    "confirm": "확인",
    "edit": "수정 또는 교체",
    "saved": "{subject}을(를) 저장했습니다.",
    "comparison_auto": "결정적 비교를 생성합니다.",
    "conflict": "다른 작성자가 이 세션을 변경했습니다. 작업을 자동 재시도하지 않았습니다.",
    "reload": "새 현재 상태를 불러와 다시 검토할까요?",
    "terminal": "가이드 명령은 대화형 터미널이 필요합니다.",
    "input_closed": "프롬프트가 끝나기 전에 대화형 입력이 닫혔습니다.",
    "interrupted": "가이드 입력이 중단되었습니다. 현재 상태는 바뀌지 않았습니다.",
    "error": "{code}: {message}",
    "root.label": "루트: {root}",
    "session.label": "세션: {session_id}",
    "header": (
        "루트: {root}\n세션: {session_id}\n세대: {generation}\n"
        "스냅샷: {snapshot}\n상태: {state}\n검증됨: {verified}\n다음: {next_action}"
    ),
    "header.generation": "세대: {generation}",
    "header.snapshot": "스냅샷: {snapshot}",
    "header.state": "상태: {state}",
    "header.verified": "검증됨: {verified}",
    "header.next": "다음: {next_action}",
    "session.create": "의사결정 세션 '{session_id}'을(를) 만들까요?",
    "session.created": "의사결정 세션 '{session_id}'을(를) 만들었습니다.",
    "session.resume": "의사결정 세션 '{session_id}'을(를) 이어서 진행합니다.",
    "session.missing": "세션 '{session_id}'이(가) 없습니다.",
    "session.exists": "세션 '{session_id}'이(가) 이미 있으며 이어서 진행할 수 있습니다.",
    "quit.option": "종료하고 나중에 계속하기",
    "quit.prompt": "가이드를 종료할까요?",
    "quit.done": "가이드를 중단했습니다. 세션 '{session_id}'으로 나중에 재개할 수 있습니다.",
    "conflict.detected": (
        "다른 작성자가 이 세션을 변경했습니다. 실패한 작업은 자동 재시도하지 않았습니다."
    ),
    "conflict.current": "고정 스냅샷: {expected}; 현재 스냅샷: {actual}.",
    "conflict.reload": "새 현재 상태를 불러와 다시 검토할까요?",
    "reload.done": "현재 상태를 다시 불러왔습니다. 다음 작업을 선택하기 전에 검토하세요.",
    "error.non_interactive": "가이드 명령은 대화형 터미널이 필요합니다.",
    "error.input_closed": "프롬프트가 끝나기 전에 대화형 입력이 닫혔습니다.",
    "error.invalid_choice": "표시된 선택지 중 하나를 고르세요.",
    "error.nonblank": "빈 값이 아닌 내용을 입력하세요.",
    "error.invalid_yes_no": "예 또는 아니요로 답하세요.",
    "error.invalid_list": "쉼표로 구분한 올바른 목록을 입력하세요.",
    "error.invalid_integer": "정수를 입력하세요.",
    "error.integer_range": "{minimum} 이상 {maximum} 이하의 수를 입력하세요.",
    "error.invalid_path": "올바른 경로를 입력하세요.",
    "error.path_missing": "경로가 존재하지 않습니다: {path}",
    "error.path_not_file": "파일 경로가 아닙니다: {path}",
    "error.path_not_directory": "디렉터리 경로가 아닙니다: {path}",
    "error.domain": "{code}: {message}",
    "error.unexpected": "예상하지 못한 오류: {message}",
    "prompt.choice": "선택: ",
    "prompt.value": "> ",
    "prompt.yes_no": "[예/아니요] ",
    "prompt.yes_no_default_yes": "[예/아니요, 기본 예] ",
    "prompt.yes_no_default_no": "[예/아니요, 기본 아니요] ",
    "stage.source": "원문 캡처",
    "stage.frame": "의사결정 프레임",
    "stage.candidates": "후보",
    "stage.criteria": "기준",
    "stage.evidence": "근거",
    "stage.evaluation": "평가",
    "stage.review": "사람 검토",
    "stage.comparison": "결정적 비교",
    "stage.recommendation": "추천 선택",
    "stage.final_decision": "최종 결정",
    "stage.approval": "승인",
    "stage.complete": "완료",
    "action.capture_source": "원문 추가 캡처",
    "action.import_frame": "의사결정 프레임 작성 또는 가져오기",
    "action.confirm_frame": "의사결정 프레임 확인",
    "action.import_candidates": "후보 작성 또는 가져오기",
    "action.confirm_candidates": "후보 확인",
    "action.import_criteria": "기준 작성 또는 가져오기",
    "action.confirm_criteria": "기준 확인",
    "action.import_evidence": "근거 작성 또는 가져오기",
    "action.import_evaluations": "로컬 평가 가져오기",
    "action.generate_evaluations": "Fixture 평가 생성",
    "action.retry_evaluations": "평가 생성 재시도",
    "action.review_evaluations": "대기 중인 평가 셀 검토",
    "action.revise_evaluations": "수정된 평가 가져오기",
    "action.derive_comparison": "결정적 비교 생성",
    "action.import_recommendation": "로컬 추천 가져오기",
    "action.generate_recommendation": "Fixture/OpenAI 추천 생성",
    "action.retry_recommendation": "추천 생성 재시도",
    "action.record_final_decision": "사람의 최종 결정 기록",
    "action.create_approval_challenge": "승인 challenge 생성",
    "action.commit_approval": "승인 응답 기록",
    "action.reissue_approval_challenge": "새 승인 challenge 발급",
    "action.refresh_state": "상태 다시 불러오기",
    "action.export_decision": "완료된 결정 내보내기",
    "draft.create": "가이드 양식으로 작성",
    "draft.import": "JSON payload 가져오기",
    "draft.agent": "종료 후 agent 초안 준비",
    "draft.path": "JSON payload 경로: ",
    "confirmation.heading": "검증된 {subject} 요약",
    "confirmation.confirm": "이 {subject} 확인",
    "confirmation.edit": "이 {subject} 수정 또는 교체",
    "confirmation.quit": "초안을 미확인 상태로 두고 종료",
    "confirmation.fingerprint": "Artifact fingerprint: {fingerprint}",
    "review.heading": "검토 필요: {candidate} / {criterion}",
    "review.assessment": "평가: {assessment} · 우선순위: {priority}",
    "review.rationale": "근거 설명: {rationale}",
    "review.uncertainty": "불확실성: {uncertainty}",
    "review.concur": "동의",
    "review.override": "재정의",
    "review.request_revision": "수정 요청",
    "review.reason": "검토 이유: ",
    "review.replacement": "대체 평가: ",
    "review.commit": "완료한 검토를 기록할까요?",
    "final.heading": "사람의 최종 결정",
    "final.select": "적격 후보 선택",
    "final.reject_all": "적격 후보 모두 거절",
    "final.defer": "결정 연기",
    "final.request_more_evidence": "추가 근거 요청",
    "final.candidate": "후보: ",
    "final.reason": "결정 이유: ",
    "final.risk": "위험 {candidate} / {criterion}을(를) 인지했습니까?",
    "final.recommendation_relation": "추천과의 관계: {relation}",
    "final.commit": "이 최종 결정을 기록할까요?",
    "approval.heading": "최종 승인",
    "approval.approved": "승인",
    "approval.rejected": "거절",
    "approval.changes_requested": "변경 요청",
    "approval.expires": "Challenge 만료까지: {remaining}",
    "approval.bundle": "Decision bundle fingerprint: {fingerprint}",
    "approval.phrase": "정확히 입력하세요: {phrase}",
    "approval.reason": "승인 이유: ",
    "approval.commit": "이 승인 응답을 기록할까요?",
    "approval.expired": "승인 challenge가 만료되었습니다.",
    "approval.reissue": "새 challenge를 발급할까요?",
    "openai.option": "OpenAI로 생성",
    "openai.preview": "OpenAI 외부 전송 요청 검토",
    "openai.provider": "Provider: {provider}",
    "openai.model": "Model: {model}",
    "openai.source_bytes": "원문 excerpt bytes: {bytes}",
    "openai.prompt_fingerprint": "Prompt fingerprint: {fingerprint}",
    "openai.input_fingerprint": "Input fingerprint: {fingerprint}",
    "openai.consent": "검토한 이 요청을 그대로 OpenAI에 전송할까요?",
    "openai.retry": "승인된 OpenAI 작업 재시도",
    "openai.local_import": "대신 로컬 결과 가져오기",
}


def _freeze_catalog(value: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(sorted(value.items())))


if set(_EN_MESSAGES) != set(_KO_MESSAGES):  # pragma: no cover - import-time invariant
    raise RuntimeError("ko/en guided message catalogs must expose identical keys")

_CATALOGS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {"en": _freeze_catalog(_EN_MESSAGES), "ko": _freeze_catalog(_KO_MESSAGES)}
)
# Public, immutable catalog data for callers that need to enumerate keys.
MESSAGES = _CATALOGS


def _placeholder_names(template: str) -> frozenset[str]:
    names: set[str] = set()
    for _literal, field_name, _format_spec, _conversion in Formatter().parse(template):
        if field_name is None:
            continue
        if not field_name or field_name.isdecimal():
            raise ValueError("catalog templates must use named placeholders")
        names.add(field_name.split(".", 1)[0].split("[", 1)[0])
    return frozenset(names)


@dataclass(frozen=True, slots=True)
class MessageCatalog:
    """Immutable ko/en lookup with strict, deterministic named interpolation."""

    language: str = "ko"

    def __post_init__(self) -> None:
        normalized = self.language.lower()
        if normalized not in _CATALOGS:
            raise ValueError(f"unsupported guided language: {self.language}")
        object.__setattr__(self, "language", normalized)

    @property
    def messages(self) -> Mapping[str, str]:
        return MESSAGES[self.language]

    def text(self, key: str, **values: object) -> str:
        try:
            template = _CATALOGS[self.language][key]
        except KeyError as exc:
            raise KeyError(f"unknown guided message key: {key}") from exc
        expected = _placeholder_names(template)
        actual = frozenset(values)
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        if missing or extra:
            raise ValueError(
                f"guided message '{key}' interpolation mismatch: missing={missing}, extra={extra}"
            )
        return template.format_map(values)


def get_catalog(language: str = "ko") -> MessageCatalog:
    return MessageCatalog(language)


def catalog_text(language: str, key: str, **values: object) -> str:
    return MessageCatalog(language).text(key, **values)


@dataclass(frozen=True, slots=True)
class PromptChoice:
    """A localized label mapped to a stable controller value."""

    value: str
    label: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.value.strip() or not self.label.strip():
            raise ValueError("choice value and label must be nonblank")
        aliases = tuple(self.aliases)
        if any(not alias.strip() for alias in aliases):
            raise ValueError("choice aliases must be nonblank")
        object.__setattr__(self, "aliases", aliases)


ChoiceCollection = Sequence[PromptChoice] | Mapping[str, str]


def _choices(value: ChoiceCollection) -> tuple[PromptChoice, ...]:
    if isinstance(value, Mapping):
        result = tuple(PromptChoice(key, label) for key, label in value.items())
    else:
        result = tuple(value)
    if not result:
        raise ValueError("at least one prompt choice is required")
    if any(not isinstance(item, PromptChoice) for item in result):
        raise TypeError("choices must contain PromptChoice values")
    if len({item.value for item in result}) != len(result):
        raise ValueError("prompt choice values must be unique")
    return result


def _read_response(
    console: ConsolePort,
    prompt: str,
    *,
    allow_quit: bool,
) -> str:
    response = console.read(prompt)
    if allow_quit and response.strip().casefold() in DEFAULT_QUIT_TOKENS:
        raise QuitRequested
    return response


def _invalid(console: ConsolePort, message: str | None, fallback: str) -> None:
    console.write(fallback if message is None else message)


def prompt_choice(
    console: ConsolePort,
    prompt: str,
    choices: ChoiceCollection,
    *,
    default: str | None = None,
    invalid_message: str | None = None,
    allow_quit: bool = True,
) -> str:
    """Display stable numbered choices and return the selected controller value."""

    normalized = _choices(choices)
    values = {item.value: item for item in normalized}
    if default is not None and default not in values:
        raise ValueError("default must name one of the prompt choice values")
    token_map: dict[str, str] = {}
    for index, item in enumerate(normalized, start=1):
        console.write(f"{index}. {item.label}")
        for token in (str(index), item.value, item.label, *item.aliases):
            normalized_token = token.strip().casefold()
            existing = token_map.get(normalized_token)
            if existing is not None and existing != item.value:
                raise ValueError(f"ambiguous prompt choice token: {token}")
            token_map[normalized_token] = item.value
    while True:
        response = _read_response(console, prompt, allow_quit=allow_quit).strip()
        if not response and default is not None:
            return default
        selected = token_map.get(response.casefold())
        if selected is not None:
            return selected
        _invalid(console, invalid_message, "Choose one of the listed options.")


def prompt_nonblank(
    console: ConsolePort,
    prompt: str,
    *,
    invalid_message: str | None = None,
    allow_quit: bool = True,
) -> str:
    while True:
        response = _read_response(console, prompt, allow_quit=allow_quit).strip()
        if response:
            return response
        _invalid(console, invalid_message, "Enter a nonblank value.")


def prompt_optional(
    console: ConsolePort,
    prompt: str,
    *,
    allow_quit: bool = True,
) -> str | None:
    """Read an optional trimmed value while preserving the global quit contract."""

    response = _read_response(console, prompt, allow_quit=allow_quit).strip()
    return response or None


def prompt_yes_no(
    console: ConsolePort,
    prompt: str,
    *,
    default: bool | None = None,
    invalid_message: str | None = None,
    allow_quit: bool = True,
) -> bool:
    yes_tokens = {"y", "yes", "예", "네"}
    no_tokens = {"n", "no", "아니요", "아니오"}
    while True:
        response = _read_response(console, prompt, allow_quit=allow_quit).strip().casefold()
        if not response and default is not None:
            return default
        if response in yes_tokens:
            return True
        if response in no_tokens:
            return False
        _invalid(console, invalid_message, "Answer yes or no.")


def prompt_list(
    console: ConsolePort,
    prompt: str,
    *,
    separator: str = ",",
    min_items: int = 1,
    max_items: int | None = None,
    unique: bool = True,
    invalid_message: str | None = None,
    allow_quit: bool = True,
) -> tuple[str, ...]:
    if not separator:
        raise ValueError("separator must not be empty")
    if min_items < 0 or (max_items is not None and max_items < min_items):
        raise ValueError("invalid prompt list bounds")
    while True:
        response = _read_response(console, prompt, allow_quit=allow_quit).strip()
        items = tuple(item.strip() for item in response.split(separator)) if response else ()
        valid = (
            all(items)
            and len(items) >= min_items
            and (max_items is None or len(items) <= max_items)
            and (not unique or len(set(items)) == len(items))
        )
        if valid or (not items and min_items == 0):
            return items
        _invalid(console, invalid_message, "Enter a valid separated list.")


def prompt_int(
    console: ConsolePort,
    prompt: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    default: int | None = None,
    invalid_message: str | None = None,
    allow_quit: bool = True,
) -> int:
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("minimum must not exceed maximum")
    if default is not None and (
        (minimum is not None and default < minimum) or (maximum is not None and default > maximum)
    ):
        raise ValueError("default is outside the allowed integer range")
    while True:
        response = _read_response(console, prompt, allow_quit=allow_quit).strip()
        if not response and default is not None:
            return default
        try:
            value = int(response, 10)
        except ValueError:
            _invalid(console, invalid_message, "Enter a whole number.")
            continue
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            _invalid(console, invalid_message, "Enter a number in the allowed range.")
            continue
        return value


def prompt_path(
    console: ConsolePort,
    prompt: str,
    *,
    must_exist: bool = False,
    file_only: bool = False,
    directory_only: bool = False,
    invalid_message: str | None = None,
    allow_quit: bool = True,
) -> Path:
    if file_only and directory_only:
        raise ValueError("file_only and directory_only are mutually exclusive")
    while True:
        raw = _read_response(console, prompt, allow_quit=allow_quit).strip()
        if not raw:
            _invalid(console, invalid_message, "Enter a valid path.")
            continue
        path = Path(raw).expanduser()
        try:
            valid = not must_exist or path.exists()
            valid = valid and (not file_only or path.is_file())
            valid = valid and (not directory_only or path.is_dir())
        except OSError:
            valid = False
        if valid:
            return path
        _invalid(console, invalid_message, "Enter a valid path.")
