from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pytest

from ai_work_harness.guided_io import (
    MESSAGES,
    ConsolePort,
    MessageCatalog,
    PromptChoice,
    QuitRequested,
    TerminalConsole,
    catalog_text,
    get_catalog,
    prompt_choice,
    prompt_int,
    prompt_list,
    prompt_nonblank,
    prompt_optional,
    prompt_path,
    prompt_yes_no,
)


class ScriptedConsole:
    def __init__(self, responses: list[str | BaseException], *, interactive: bool = True) -> None:
        self._responses: Iterator[str | BaseException] = iter(responses)
        self._interactive = interactive
        self.writes: list[str] = []
        self.prompts: list[str] = []

    def is_interactive(self) -> bool:
        return self._interactive

    def write(self, text: str) -> None:
        self.writes.append(text)

    def read(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        try:
            response = next(self._responses)
        except StopIteration as exc:
            raise EOFError("script exhausted") from exc
        if isinstance(response, BaseException):
            raise response
        return response


def test_catalogs_have_identical_public_keys_and_stable_named_interpolation() -> None:
    assert set(MESSAGES) == {"ko", "en"}
    assert set(MESSAGES["ko"]) == set(MESSAGES["en"])
    assert len(MESSAGES["ko"]) >= 100

    english = get_catalog("EN")
    korean = MessageCatalog("ko")
    values = {
        "root": "/workspace",
        "session_id": "triage",
        "generation": 4,
        "snapshot": "0123456789ab",
        "state": "frame_draft",
        "verified": "yes",
        "next_action": "confirm frame",
    }
    assert english.language == "en"
    assert "Root: /workspace" in english.text("header", **values)
    assert "루트: /workspace" in korean.text("header", **values)
    assert catalog_text("en", "error", code="WRITE_CONFLICT", message="changed") == (
        "WRITE_CONFLICT: changed"
    )
    assert english.messages["stage.approval"] == "Approval"

    with pytest.raises(ValueError, match="unsupported guided language"):
        MessageCatalog("fr")
    with pytest.raises(KeyError, match="unknown guided message key"):
        english.text("missing.key")
    with pytest.raises(ValueError, match=r"missing=.*session_id"):
        english.text("session.resume")
    with pytest.raises(ValueError, match=r"extra=.*unused"):
        english.text("continue", unused=True)


class _InterruptingInput(io.StringIO):
    def readline(self, *args: object, **kwargs: object) -> str:
        raise KeyboardInterrupt


def test_terminal_console_reads_writes_and_propagates_eof_and_interrupt() -> None:
    output = io.StringIO()
    terminal = TerminalConsole(io.StringIO("answer\r\n"), output)

    assert isinstance(terminal, ConsolePort)
    assert terminal.is_interactive() is False
    terminal.write("status")
    assert terminal.read("Choice: ") == "answer"
    assert output.getvalue() == "status\nChoice: "

    with pytest.raises(EOFError, match="interactive input closed"):
        TerminalConsole(io.StringIO(""), io.StringIO()).read("> ")
    with pytest.raises(KeyboardInterrupt):
        TerminalConsole(_InterruptingInput(), io.StringIO()).read("> ")


def test_choice_is_scriptable_by_number_value_label_or_alias() -> None:
    choices = (
        PromptChoice("confirm", "Confirm", ("c",)),
        PromptChoice("edit", "Edit", ("e",)),
    )
    console = ScriptedConsole(["unknown", "2"])

    assert prompt_choice(console, "Choice: ", choices) == "edit"
    assert console.writes == [
        "1. Confirm",
        "2. Edit",
        "Choose one of the listed options.",
    ]
    assert console.prompts == ["Choice: ", "Choice: "]

    assert prompt_choice(ScriptedConsole(["CONFIRM"]), "> ", choices) == "confirm"
    assert prompt_choice(ScriptedConsole(["c"]), "> ", choices) == "confirm"
    assert (
        prompt_choice(
            ScriptedConsole([""]),
            "> ",
            {"confirm": "Confirm", "edit": "Edit"},
            default="confirm",
        )
        == "confirm"
    )


@pytest.mark.parametrize("quit_token", ["q", "QUIT", "종료"])
def test_explicit_quit_is_a_distinct_signal(quit_token: str) -> None:
    with pytest.raises(QuitRequested):
        prompt_nonblank(ScriptedConsole([quit_token]), "> ")

    assert (
        prompt_nonblank(
            ScriptedConsole([quit_token]),
            "> ",
            allow_quit=False,
        )
        == quit_token
    )


def test_eof_and_ctrl_c_propagate_from_prompt_helpers() -> None:
    with pytest.raises(EOFError, match="script exhausted"):
        prompt_nonblank(ScriptedConsole([]), "> ")
    with pytest.raises(KeyboardInterrupt):
        prompt_yes_no(ScriptedConsole([KeyboardInterrupt()]), "Continue? ")


def test_nonblank_and_yes_no_retry_without_hiding_localized_input() -> None:
    nonblank = ScriptedConsole(["   ", "  이유  "])
    assert (
        prompt_nonblank(
            nonblank,
            "이유: ",
            invalid_message="내용을 입력하세요.",
        )
        == "이유"
    )
    assert nonblank.writes == ["내용을 입력하세요."]


def test_optional_value_preserves_blank_and_global_quit() -> None:
    assert prompt_optional(ScriptedConsole(["   "]), "> ") is None
    assert prompt_optional(ScriptedConsole(["  note  "]), "> ") == "note"
    with pytest.raises(QuitRequested):
        prompt_optional(ScriptedConsole(["quit"]), "> ")

    assert prompt_yes_no(ScriptedConsole(["네"]), "계속? ") is True
    assert prompt_yes_no(ScriptedConsole(["아니요"]), "계속? ") is False
    retried = ScriptedConsole(["maybe", "yes"])
    assert prompt_yes_no(retried, "Continue? ") is True
    assert retried.writes == ["Answer yes or no."]
    assert prompt_yes_no(ScriptedConsole([""]), "Continue? ", default=False) is False


def test_list_and_integer_prompts_apply_explicit_bounds() -> None:
    listed = ScriptedConsole(["alpha, alpha", "alpha, beta"])
    assert prompt_list(listed, "IDs: ", min_items=2, max_items=2) == ("alpha", "beta")
    assert listed.writes == ["Enter a valid separated list."]
    assert prompt_list(ScriptedConsole([""]), "Optional: ", min_items=0) == ()

    integer = ScriptedConsole(["3.5", "0", "2"])
    assert prompt_int(integer, "Count: ", minimum=1, maximum=3) == 2
    assert integer.writes == ["Enter a whole number.", "Enter a number in the allowed range."]
    assert prompt_int(ScriptedConsole([""]), "Count: ", default=3, maximum=4) == 3

    with pytest.raises(ValueError, match="list bounds"):
        prompt_list(ScriptedConsole([]), "IDs: ", min_items=2, max_items=1)
    with pytest.raises(ValueError, match="outside"):
        prompt_int(ScriptedConsole([]), "Count: ", default=4, maximum=3)


def test_path_prompt_can_validate_files_and_directories(tmp_path: Path) -> None:
    file_path = tmp_path / "payload.json"
    file_path.write_text("{}", encoding="utf-8")
    missing = tmp_path / "missing.json"
    console = ScriptedConsole([str(missing), str(file_path)])

    assert prompt_path(console, "Path: ", must_exist=True, file_only=True) == file_path
    assert console.writes == ["Enter a valid path."]
    assert (
        prompt_path(
            ScriptedConsole([str(tmp_path)]),
            "Directory: ",
            directory_only=True,
        )
        == tmp_path
    )

    with pytest.raises(ValueError, match="mutually exclusive"):
        prompt_path(ScriptedConsole([]), "Path: ", file_only=True, directory_only=True)


def test_prompt_choice_rejects_ambiguous_or_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="default"):
        prompt_choice(
            ScriptedConsole([]),
            "> ",
            (PromptChoice("one", "One"),),
            default="two",
        )
    with pytest.raises(ValueError, match="ambiguous"):
        prompt_choice(
            ScriptedConsole([]),
            "> ",
            (
                PromptChoice("one", "One", ("same",)),
                PromptChoice("two", "Two", ("same",)),
            ),
        )
