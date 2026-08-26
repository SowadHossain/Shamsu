from __future__ import annotations

import pytest
from prompt_toolkit.document import Document

from shamsu.cli.arguments import parse_args
from shamsu.cli.app import SMALL_COMMANDS, SmallSlashCompleter


def test_default_command_opens_tui_contract() -> None:
    args = parse_args([])

    assert args.command is None
    assert "/tui" in SMALL_COMMANDS


def test_run_requires_prompt() -> None:
    with pytest.raises(SystemExit):
        parse_args(["run"])

    args = parse_args(["run", "--prompt", "hello"])

    assert args.command == "run"
    assert args.prompt == "hello"


def test_web_command_keeps_web_flags() -> None:
    args = parse_args(["web", "--port", "0", "--scan", "."])

    assert args.command == "web"
    assert args.port == 0
    assert args.scan == ["."]


def test_web_flag_alias() -> None:
    assert parse_args(["--web"]).command == "web"


def test_web_rejects_run_only_flags() -> None:
    with pytest.raises(SystemExit):
        parse_args(["web", "--prompt", "do it"])


def test_repl_is_only_a_compatibility_shim() -> None:
    from shamsu.cli import app, repl

    assert repl.main is app.main
    assert repl.SmallHarnessApp is app.SmallHarnessApp


def test_slash_completer_suggests_small_commands(tmp_path) -> None:
    completions = list(
        SmallSlashCompleter(tmp_path).get_completions(Document("/t"), None)
    )

    assert [item.text for item in completions] == ["/tui"]
