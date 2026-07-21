"""The 2026-07-20 dogfood failures, pinned so they cannot come back.

Seven everyday prompts were run headless against a real workspace; four failed.
Tracing the run artifacts collapsed those four reports into three causes, each
covered here with the VERBATIM prompt that triggered it:

1. a read-only clause read as intent to act - "Do not modify files" contains
   `modify` + `files`, so keyword detectors saw a write request. It picked the
   wrong route, produced a false failure verdict, and was never enforced;
2. the markdown fallback turning a correct answer into a destructive edit -
   the model replied ```\n5\n``` (the command output it was asked for) and the
   fallback wrote `5` over the user's script;
3. any prompt naming a .md/.txt/.pdf file hijacking into `prd.build`, because
   the named-but-nonexistent file silently fell back to "the one PRD here".

The dogfood log lives at test-shamsu/SHAMSU_FRESH_DOGFOOD_2026-07-20.md.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.chat_loop import _request_requires_workspace_change
from shamsu.agents.markdown_fallback import MarkdownWriteFallback
from shamsu.cli import repl
from shamsu.safety import read_only
from shamsu.tools.agent_tools import AgentToolRegistry

# The exact prompts from the failing runs.
WEB_PROMPT = (
    "Use web search to find the official Python 3.13 release date. "
    "Give the date and source URL. Do not modify files."
)
RUN_SCRIPT_PROMPT = "Run qa_probe.py and tell me the command output. Do not change files."
CREATE_FILE_PROMPT = (
    "Create a new file named shamsu_smoke_note.md in this workspace. Put one short "
    "sentence in it saying this file was created by a SHAMSU smoke test. "
    "Do not modify any other files."
)
DRY_RUN_PROMPT = (
    "Dry run only: create a file named dry_run_should_not_exist.txt with the text "
    "dry run probe. Do not modify any files."
)

QA_PROBE = 'def add(a, b):\n    return a + b\n\nif __name__ == "__main__":\n    print(add(2, 3))\n'


@pytest.fixture()
def prd_workspace(tmp_path: Path) -> Path:
    """A workspace holding exactly one PRD - the condition that armed the hijack."""
    (tmp_path / "prd.pdf").write_bytes(b"%PDF-1.4 fake")
    (tmp_path / "qa_probe.py").write_text(QA_PROBE, encoding="utf-8")
    return tmp_path


# --- 1. the read-only clause is a constraint, never an instruction to act ------


@pytest.mark.parametrize("prompt", [WEB_PROMPT, RUN_SCRIPT_PROMPT, DRY_RUN_PROMPT])
def test_blanket_read_only_clauses_are_detected(prompt: str):
    assert read_only.applies(prompt)


def test_do_not_modify_any_OTHER_files_is_a_carve_out_not_a_ban():
    """Found by re-running the dogfood after the first fix: reading this as
    blanket read-only made SHAMSU refuse to create the file the prompt asked
    for - failing the task from the opposite direction. "Other" means "this
    one is fine, leave the rest alone"."""
    assert read_only.is_scoped(CREATE_FILE_PROMPT)
    assert read_only.applies(CREATE_FILE_PROMPT) is False
    # The clause is still masked before intent detection either way: it must
    # never read as a request TO modify files.
    assert "modify" not in read_only.strip(CREATE_FILE_PROMPT).lower()


def test_do_not_modify_anything_else_is_also_a_scoped_carve_out():
    prompt = "Create notes.md with hello. Do not modify anything else."

    assert read_only.is_scoped(prompt) is True
    assert read_only.applies(prompt) is False


def test_agent_safety_uses_current_request_not_augmented_context():
    class FakeTools:
        def __init__(self):
            self.user_request = ""
            self.read_only = False
            self.allowed = None

        def set_user_request(self, value):
            self.user_request = value

        def set_read_only(self, value):
            self.read_only = value

        def set_allowed_write_paths(self, value):
            self.allowed = tuple(value)

    tools = FakeTools()
    current = "Create mcp-smoke.txt with hello."
    augmented = current + "\n\nEarlier context: Do not modify any files."

    repl._configure_agent_request_safety(tools, current)

    assert read_only.applies(augmented) is True
    assert tools.user_request == current
    assert tools.read_only is False


def test_agent_safety_scopes_anything_else_to_requested_file():
    class FakeTools:
        def __init__(self):
            self.read_only = False
            self.allowed = None

        def set_user_request(self, _value):
            pass

        def set_read_only(self, value):
            self.read_only = value

        def set_allowed_write_paths(self, value):
            self.allowed = tuple(value)

    tools = FakeTools()
    repl._configure_agent_request_safety(
        tools, "Create notes.md with hello. Do not modify anything else."
    )

    assert tools.read_only is False
    assert tools.allowed == ("notes.md",)


def test_read_only_clause_does_not_create_a_write_route(prd_workspace: Path):
    """Measured live: this prompt matched NO route without its final sentence
    and `file.write` with it. The clause was the entire cause of the route."""
    without = repl._matching_route_labels(
        "Use web search to find the official Python 3.13 release date. "
        "Give the date and source URL.",
        prd_workspace,
    )
    with_clause = repl._matching_route_labels(WEB_PROMPT, prd_workspace)

    assert "file.write" not in with_clause
    assert with_clause == without


def test_read_only_request_is_never_failed_for_not_writing():
    """The web answer was correct and complete, then reported as
    "I did not complete the requested workspace change" because
    `_WORKSPACE_CHANGE_RE` matched the `modify` in "Do not modify files"."""
    assert _request_requires_workspace_change(WEB_PROMPT) is False
    assert _request_requires_workspace_change(RUN_SCRIPT_PROMPT) is False
    # A genuine change request must still be gated.
    assert _request_requires_workspace_change("fix the bug in app.py") is True


def test_read_only_outranks_blanket_approval(prd_workspace: Path):
    """The run that ate qa_probe.py was under `--approval allow`. Approval mode
    answers "may I act without asking", not "may I ignore what you told me"."""
    tools = AgentToolRegistry(prd_workspace, approval_func=lambda _request: True)
    tools.set_read_only(True)

    result = tools.execute("write_file", {"filepath": "qa_probe.py", "content": "5\n"})

    assert result.ok is False
    assert "not to change files" in result.message
    assert (prd_workspace / "qa_probe.py").read_text(encoding="utf-8") == QA_PROBE


@pytest.mark.parametrize("tool,args", [
    ("edit_file", {"filepath": "qa_probe.py", "old_string": "a + b", "new_string": "a - b"}),
    ("delete_file", {"filepath": "qa_probe.py"}),
    ("move_file", {"source": "qa_probe.py", "destination": "moved.py"}),
])
def test_read_only_blocks_every_mutating_tool(prd_workspace: Path, tool: str, args: dict):
    tools = AgentToolRegistry(prd_workspace, approval_func=lambda _request: True)
    tools.set_read_only(True)

    assert tools.execute(tool, args).ok is False
    assert (prd_workspace / "qa_probe.py").read_text(encoding="utf-8") == QA_PROBE


# --- 2. a reported RESULT is not a file to write ------------------------------


def test_command_output_never_overwrites_the_script_that_produced_it(tmp_path: Path):
    """The whole failure, verbatim. The model did nothing wrong: it was asked
    for the command output and it gave the command output."""
    (tmp_path / "qa_probe.py").write_text(QA_PROBE, encoding="utf-8")
    fallback = MarkdownWriteFallback(
        AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    )

    result = fallback.maybe_write(RUN_SCRIPT_PROMPT, "Command output:\n```\n5\n```")

    assert result.handled is False
    assert (tmp_path / "qa_probe.py").read_text(encoding="utf-8") == QA_PROBE


def test_output_block_is_refused_even_without_the_read_only_clause(tmp_path: Path):
    """The content guards must stand alone - a user who does not think to say
    "do not change files" is owed the same protection."""
    (tmp_path / "qa_probe.py").write_text(QA_PROBE, encoding="utf-8")
    fallback = MarkdownWriteFallback(
        AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    )

    result = fallback.maybe_write(
        "Run qa_probe.py and tell me the command output.", "Command output:\n```\n5\n```"
    )

    assert result.handled is False
    assert (tmp_path / "qa_probe.py").read_text(encoding="utf-8") == QA_PROBE


@pytest.mark.parametrize("literal", ["5", "hello", "done", '{"a": 1}', "42"])
def test_a_bare_literal_is_not_python_source(literal: str):
    """`ast.parse` accepts all of these, which is why the old "must at least
    compile" gate let command output through as a .py replacement."""
    from shamsu.agents.markdown_fallback import _parses_as_python

    assert _parses_as_python(literal) is False
    assert _parses_as_python("x = 1\n") is True
    assert _parses_as_python("def f():\n    return 2\n") is True


def test_a_small_file_is_not_a_free_target(tmp_path: Path):
    """`_plausible_replacement` waived the ratio check entirely below 40 lines,
    leaving the files a single stray token can destroy outright unprotected."""
    from shamsu.agents.markdown_fallback import _plausible_replacement

    target = tmp_path / "small.py"
    target.write_text(QA_PROBE, encoding="utf-8")

    assert _plausible_replacement(target, "5\n") is False
    assert _plausible_replacement(target, QA_PROBE) is True


def test_read_only_stops_the_fallback_writing_at_all(tmp_path: Path):
    fallback = MarkdownWriteFallback(
        AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    )

    result = fallback.maybe_write(
        "create notes.py. Do not modify files.",
        "```python\nprint('hi')\n```",
        read_only=True,
    )

    assert result.handled is False
    assert not (tmp_path / "notes.py").exists()


# --- 3. naming a file must not hand the run to the PRD builder ----------------


def test_creating_a_named_file_does_not_hijack_into_a_prd_build(prd_workspace: Path):
    """`shamsu_smoke_note.md` was extracted as the "PRD path", failed to
    resolve, and silently fell back to the workspace's only PRD - so a
    one-sentence file request built a TaskFlow landing page instead."""
    assert repl._resolve_build_prd(CREATE_FILE_PROMPT, prd_workspace) is None
    assert repl._looks_like_prd_build_request(CREATE_FILE_PROMPT, prd_workspace) is False
    assert "prd.build" not in repl._matching_route_labels(CREATE_FILE_PROMPT, prd_workspace)


def test_dry_run_file_creation_does_not_hijack_either(prd_workspace: Path):
    assert repl._looks_like_prd_build_request(DRY_RUN_PROMPT, prd_workspace) is False


def test_a_real_prd_build_request_still_routes_to_prd_build(prd_workspace: Path):
    """The hijack fix must not cost the feature: an actual build request, with
    or without naming the PRD, still reaches the builder."""
    assert repl._looks_like_prd_build_request("build the app from the prd", prd_workspace)
    assert repl._resolve_build_prd("build the app from the prd", prd_workspace) is not None
    assert repl._looks_like_prd_build_request("build the product from prd.pdf", prd_workspace)


def test_product_nouns_match_on_word_boundaries(prd_workspace: Path):
    """The noun list contains "it", and membership was a raw substring test -
    so with/quit/write/edit all satisfied the "names a product" half."""
    assert repl._looks_like_prd_build_request(
        "create a wait list widget with a submit button", prd_workspace
    ) is False


# --- 4. an explicit tool instruction is obeyed --------------------------------


def test_use_web_search_actually_routes_to_web(prd_workspace: Path):
    """Exposed by re-running the dogfood: fixing the bogus `file.write` route
    revealed that nothing matched this prompt at all, so "Use web search to
    find the release date" fell through to the tool-less QA brain and answered
    from stale model memory - it returned the wrong YEAR. The phrase list had
    "search the web" but not "web search"."""
    assert repl._matching_route_labels(WEB_PROMPT, prd_workspace) == ["web"]


def test_explicit_mcp_request_reaches_the_tool_calling_agent(prd_workspace: Path):
    prompt = "Use the external MCP filesystem server to list the workspace root."

    assert repl._classify_route_label(prompt, prd_workspace) == "mcp"


@pytest.mark.parametrize("prompt", [
    "look up the django auth docs",
    "search online for the colyseus changelog",
    "check on the web for the latest release",
])
def test_other_explicit_web_phrasings_still_route_to_web(prompt: str, prd_workspace: Path):
    assert "web" in repl._matching_route_labels(prompt, prd_workspace)


@pytest.mark.parametrize("prompt", [
    "what does add() do in qa_probe.py",
    "search for the parse function",
])
def test_local_questions_do_not_route_to_web(prompt: str, prd_workspace: Path):
    assert "web" not in repl._matching_route_labels(prompt, prd_workspace)


# --- 5. a slash command is a command, not a prompt ----------------------------


def test_headless_dispatches_read_only_inspection_commands():
    from shamsu.cli.noninteractive import _HEADLESS_COMMAND_HANDLERS

    assert "run" in _HEADLESS_COMMAND_HANDLERS
    assert "runs" in _HEADLESS_COMMAND_HANDLERS
    assert "abstract" in _HEADLESS_COMMAND_HANDLERS
    assert "mcp" in _HEADLESS_COMMAND_HANDLERS


def test_headless_dispatches_abstract_status(tmp_path: Path):
    from rich.console import Console

    from shamsu.cli.noninteractive import _dispatch_slash_command

    console = Console(record=True)
    handled, refusal = _dispatch_slash_command("abstract status", tmp_path, console)

    assert handled is True
    assert refusal == ""
    assert "Index:" in console.export_text()


def test_headless_refuses_mutating_abstract_commands(tmp_path: Path):
    from rich.console import Console

    from shamsu.cli.noninteractive import _dispatch_slash_command

    handled, refusal = _dispatch_slash_command("abstract repair", tmp_path, Console())

    assert handled is False
    assert "not available in headless mode" in refusal


def test_headless_dispatches_mcp_status(tmp_path: Path):
    from rich.console import Console

    from shamsu.cli.noninteractive import _dispatch_slash_command

    console = Console(record=True)
    handled, refusal = _dispatch_slash_command("mcp status", tmp_path, console)

    assert handled is True
    assert refusal == ""
    assert "No MCP servers configured" in console.export_text()


def test_headless_refuses_mutating_mcp_admin_commands(tmp_path: Path):
    from rich.console import Console

    from shamsu.cli.noninteractive import _dispatch_slash_command

    handled, refusal = _dispatch_slash_command("mcp auth logout server", tmp_path, Console())

    assert handled is False
    assert "not available in headless mode" in refusal


def test_headless_refuses_unsupported_slash_commands_instead_of_guessing(tmp_path: Path):
    """`/run show <id>` used to reach the model as English, which sent the agent
    off to RUN the file it saw named in the prompt. Anything not dispatchable
    must say so rather than degrade into a model prompt."""
    from rich.console import Console

    from shamsu.cli.noninteractive import _dispatch_slash_command

    handled, refusal = _dispatch_slash_command(
        "memory status", tmp_path, Console(file=__import__("io").StringIO())
    )

    assert handled is False
    assert "not available in headless mode" in refusal
