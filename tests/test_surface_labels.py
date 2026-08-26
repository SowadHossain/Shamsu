"""One prompt shape for three surfaces, and a desktop that stops narrating.

A live session on 2026-08-20 produced this for a single Telegram prompt:

    ╭─ Telegram sus ─╮  entered a pairing code.
    ╭─ SHAMSU -> Telegram ─╮  Connected to this SHAMSU installation.
    ╭─ SHAMSU -> Telegram ─╮  Task received. SHAMSU is starting now.
    ╭─ SHAMSU -> Telegram ─╮  Working: SHAMSU remote task started

Four panels, none of which the reader wanted, two of them halves of the same
sentence, and all of it around turn output the turn renderer was already
printing properly. These tests pin the rules that replaced it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.cli.prompt_label import (
    SURFACE_CLI,
    SURFACE_TELEGRAM,
    SURFACE_WEB,
    prompt_label,
    session_prompt_label,
)
from shamsu.integrations.telegram.models import OutboundMessage

# -- the prompt line -----------------------------------------------------


def test_every_surface_names_itself() -> None:
    """Including the local one. A single scrollback now interleaves turns from
    three places, and the word before `>` is the only discriminator."""
    assert prompt_label("asteroids", SURFACE_CLI) == "shamsu (asteroids) cli> "
    assert prompt_label("asteroids", SURFACE_WEB) == "shamsu (asteroids) web> "
    assert prompt_label("asteroids", SURFACE_TELEGRAM) == "shamsu (asteroids) telegram> "


def test_a_long_title_is_cut_on_a_word_boundary() -> None:
    """`shamsu (Review Project And See W...) cli>` was the live complaint. The
    title is context, not content."""
    label = prompt_label("Review Project And See What Is Left", SURFACE_CLI)
    assert label == "shamsu (Review Project) cli> "
    assert "..." not in label


def test_a_missing_title_costs_the_parentheses_not_the_prompt() -> None:
    assert prompt_label("", SURFACE_CLI) == "shamsu cli> "
    assert prompt_label("   ", SURFACE_WEB) == "shamsu web> "


def test_a_placeholder_title_is_treated_as_no_title() -> None:
    """"(Untitled Session)" costs width and tells you nothing."""
    for empty in ("Untitled Session", "untitled", "SHAMSU Session", "session"):
        assert prompt_label(empty, SURFACE_CLI) == "shamsu cli> "


def test_a_broken_session_object_still_yields_a_prompt() -> None:
    """A decorative label must never be the reason the REPL stops accepting
    input."""

    class Exploding:
        @property
        def metadata(self):
            raise RuntimeError("no metadata here")

    assert session_prompt_label(Exploding(), SURFACE_CLI) == "shamsu cli> "
    assert session_prompt_label(None, SURFACE_CLI) == "shamsu cli> "


def test_the_repl_and_the_telegram_echo_use_the_same_builder() -> None:
    """The two used to compete for one slot: the REPL printed the thread name,
    the mirror printed `remote-telegram`, so a remote turn LOST the thread name
    to gain a source name - and you never saw both."""
    from shamsu.cli.repl import _session_prompt_label

    class Session:
        class metadata:  # noqa: N801 - a stub shaped like the real one
            title = "asteroids"

    assert _session_prompt_label(Session()) == "shamsu (asteroids) cli> "


# -- what reaches the desktop -------------------------------------------


def test_outbound_messages_are_not_mirrored_by_default() -> None:
    """The default is the fix. Every outbound message used to be mirrored, so a
    message type added later cannot silently start spamming the terminal."""
    assert OutboundMessage(1, "Task received. SHAMSU is starting now.").mirror_to_cli is False
    assert OutboundMessage(1, "Working: something").mirror_to_cli is False


def workspace(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / ".shamsu").mkdir(parents=True, exist_ok=True)
    return project


class RecordingMirror:
    def __init__(self) -> None:
        self.panels: list[tuple[str, str]] = []
        self.prompts: list[tuple[str, str]] = []

    def __call__(self, title: str, text: str) -> None:
        self.panels.append((title, text))

    def prompt_echo(self, prompt: str, title: str = "") -> None:
        self.prompts.append((prompt, title))


def service_for(tmp_path: Path, mirror: RecordingMirror):
    from shamsu.integrations.telegram.service import TelegramService
    from shamsu.integrations.telegram.transport import FakeTelegramTransport

    return TelegramService(
        workspace(tmp_path),
        transport=FakeTelegramTransport(),
        token="123456:AAH-fake-token-for-tests",
        cli_mirror=mirror,
    )


def test_turn_output_produces_no_panel(tmp_path: Path) -> None:
    mirror = RecordingMirror()
    service = service_for(tmp_path, mirror)

    service._mirror_outbound(OutboundMessage(1, "Working: SHAMSU remote task started"))
    service._mirror_outbound(OutboundMessage(1, "Task received. SHAMSU is starting now."))

    assert mirror.panels == []


def test_a_pairing_is_reported_in_the_third_person(tmp_path: Path) -> None:
    """The phone reads "Connected to this SHAMSU installation." The desktop
    needs to know WHO, which is a different sentence."""
    mirror = RecordingMirror()
    service = service_for(tmp_path, mirror)

    service._mirror_outbound(
        OutboundMessage(
            1,
            "Connected to this SHAMSU installation.",
            mirror_to_cli=True,
            cli_text="Ada paired with this installation.",
        )
    )

    assert mirror.panels == [("Telegram", "Ada paired with this installation.")]


def test_a_refused_stranger_is_still_reported(tmp_path: Path) -> None:
    """The authorization gate used to swallow exactly this - which is the one
    event you most want to see."""
    mirror = RecordingMirror()
    service = service_for(tmp_path, mirror)

    service._mirror_outbound(
        OutboundMessage(
            999,  # a chat that was never paired
            "You are not authorized.",
            mirror_to_cli=True,
            cli_text="Someone was refused: You are not authorized.",
        )
    )

    assert len(mirror.panels) == 1


def test_the_panel_title_is_a_channel_not_a_name(tmp_path: Path) -> None:
    """"Telegram sus" / "entered a pairing code." was one sentence sawn in half
    across a panel border."""
    from shamsu.integrations.telegram.service import TELEGRAM_PANEL_TITLE

    assert TELEGRAM_PANEL_TITLE == "Telegram"


def test_a_display_name_cannot_smuggle_rich_markup_into_the_terminal() -> None:
    """A Telegram display name is whatever a stranger typed into their profile,
    and rich reads `[red]` in a panel body as markup."""
    from rich.console import Console

    from shamsu.integrations.telegram.local import ConsoleTelegramMirror

    console = Console(record=True, width=100)
    ConsoleTelegramMirror(console)("Telegram", "[red]evil[/red] was refused: nope")
    text = console.export_text()
    assert "[red]evil[/red]" in text


def test_a_remote_prompt_is_echoed_as_a_prompt_not_a_panel() -> None:
    from rich.console import Console

    from shamsu.integrations.telegram.local import ConsoleTelegramMirror

    console = Console(record=True, width=120)
    ConsoleTelegramMirror(console).prompt_echo("what is the status?", "Review Project And More")
    text = console.export_text()
    assert "shamsu (Review Project) telegram> what is the status?" in text
    assert "╭" not in text


# -- provenance in the transcript ---------------------------------------


def test_a_message_records_which_surface_asked(tmp_path: Path) -> None:
    from shamsu.session.manager import SessionManager

    logger = SessionManager(workspace(tmp_path)).create_session("thread")
    logger.append_message("user", "from my phone", source="telegram")

    assert logger.read_messages()[-1]["source"] == "telegram"


def test_a_record_written_before_the_field_existed_still_reads(tmp_path: Path) -> None:
    """JSONL, so old records simply lack the key and every reader defaults it."""
    import json

    from shamsu.session.manager import SessionManager

    logger = SessionManager(workspace(tmp_path)).create_session("thread")
    with logger.messages_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": "user", "content": "old", "timestamp": ""}) + "\n")

    from shamsu.webui import api

    payload = api.session_messages(workspace(tmp_path), logger.session_id)
    assert payload["messages"][-1]["source"] == ""


def test_the_web_transcript_carries_the_source(tmp_path: Path) -> None:
    from shamsu.session.manager import SessionManager
    from shamsu.webui import api

    project = workspace(tmp_path)
    logger = SessionManager(project).create_session("thread")
    logger.append_message("user", "from my phone", source="telegram")

    payload = api.session_messages(project, logger.session_id)
    assert payload["messages"][-1]["source"] == "telegram"


@pytest.mark.parametrize("surface", ["cli", "web", "telegram"])
def test_the_chat_loop_stamps_its_surface_onto_the_transcript(
    tmp_path: Path, surface: str
) -> None:
    """The value the turn stream already carries, now also on the message."""
    from shamsu.agents.simple_state import ChatState
    from shamsu.session.manager import SessionManager

    project = workspace(tmp_path)
    logger = SessionManager(project).create_session("thread")
    state = ChatState("system", session_logger=logger, hydrate=False, source=surface)
    state.append_user("hello")

    assert logger.read_messages()[-1]["source"] == surface
