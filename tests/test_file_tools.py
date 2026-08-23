"""Tests for the smarter file read/write/find/edit tool layer and the agent
loop's failed-read recovery. See shamsu/tools/agent_tools.py and
shamsu/agents/chat_loop.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.audit.trail import SessionAuditLog
from shamsu.tools.agent_tools import AgentToolRegistry


def _registry(tmp_path: Path) -> AgentToolRegistry:
    return AgentToolRegistry(tmp_path, approval_func=lambda _request: True)


def _write(tmp_path: Path, rel: str, content: str = "x\n") -> Path:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


def test_read_file_exact_path_works(tmp_path: Path):
    _write(tmp_path, "app.py", "print('hi')\n")
    registry = _registry(tmp_path)

    result = registry.read_file("app.py")

    assert result.ok is True
    assert result.data["content"] == "print('hi')\n"
    assert result.data["resolved_filepath"] == "app.py"


@pytest.mark.parametrize(
    "path",
    [
        ".env.example",
        ".npmrc",
        "Dockerfile.dev",
        "frontend/src/styles.scss",
        "frontend/src/theme.less",
        "backend/schema.prisma",
        "backend/schema.graphql",
        "backend/src/schema.sql",
        "assets/logo.svg",
        "config/app.properties",
    ],
)
def test_read_file_accepts_common_project_text_files(tmp_path: Path, path: str):
    _write(tmp_path, path, "VALUE=true\n")
    registry = _registry(tmp_path)

    result = registry.read_file(path)

    assert result.ok is True
    assert result.data["content"] == "VALUE=true\n"


def test_read_file_missing_path_returns_candidates(tmp_path: Path):
    _write(tmp_path, "a/notes.md", "one\n")
    _write(tmp_path, "b/notes.md", "two\n")
    registry = _registry(tmp_path)

    result = registry.read_file("notes.md")

    assert result.ok is False
    assert set(result.data["candidates"]) == {"a/notes.md", "b/notes.md"}
    assert "Candidates" in result.message


def test_read_file_resolves_single_strong_candidate(tmp_path: Path):
    _write(tmp_path, "client/src/App.tsx", "export const App = () => null\n")
    registry = _registry(tmp_path)

    result = registry.read_file("src/App.tsx")

    assert result.ok is True
    assert result.data["resolved_filepath"] == "client/src/App.tsx"
    assert result.data["filepath"] == "src/App.tsx"
    assert "export const App" in result.data["content"]


def test_write_reuses_path_resolved_by_prior_scoped_read(tmp_path: Path):
    existing = _write(tmp_path, "demo/backend/core/tests/test_canvas.py", "OLD = True\n")
    registry = _registry(tmp_path)
    registry.set_allowed_read_paths(("demo",))
    registry.set_allowed_write_paths(("demo",))

    read = registry.read_file("test_canvas.py")
    written = registry.write_file("test_canvas.py", "OLD = False\n", overwrite=True)

    assert read.ok is True
    assert read.data["resolved_filepath"] == "demo/backend/core/tests/test_canvas.py"
    assert written.ok is True
    assert written.data["resolved_filepath"] == "demo/backend/core/tests/test_canvas.py"
    assert existing.read_text(encoding="utf-8") == "OLD = False\n"
    assert not (tmp_path / "demo/test_canvas.py").exists()


def test_read_file_does_not_auto_pick_with_multiple_candidates(tmp_path: Path):
    _write(tmp_path, "client/src/App.tsx", "client\n")
    _write(tmp_path, "web/src/App.tsx", "web\n")
    registry = _registry(tmp_path)

    result = registry.read_file("src/App.tsx")

    assert result.ok is False
    assert "content" not in result.data
    assert set(result.data["candidates"]) == {"client/src/App.tsx", "web/src/App.tsx"}


def test_read_file_directory_reports_listing(tmp_path: Path):
    _write(tmp_path, "src/app.py", "x\n")
    registry = _registry(tmp_path)

    result = registry.read_file("src")

    assert result.ok is False
    assert result.data["kind"] == "directory"


def test_read_file_line_range(tmp_path: Path):
    _write(tmp_path, "big.py", "\n".join(f"line{i}" for i in range(1, 21)) + "\n")
    registry = _registry(tmp_path)

    result = registry.read_file("big.py", start_line="5", end_line="7")

    assert result.ok is True
    assert result.data["content"] == "line5\nline6\nline7"
    assert result.data["start_line"] == 5
    assert result.data["end_line"] == 7
    assert result.data["total_lines"] == 20


def test_read_file_normalizes_quoted_and_backslash_paths(tmp_path: Path):
    _write(tmp_path, "src/app.py", "x = 1\n")
    registry = _registry(tmp_path)

    result = registry.read_file("`src\\app.py`")

    assert result.ok is True
    assert result.data["resolved_filepath"] == "src/app.py"


# ---------------------------------------------------------------------------
# file_info
# ---------------------------------------------------------------------------


def test_file_info_reports_file_directory_missing(tmp_path: Path):
    _write(tmp_path, "src/app.py", "x\n")
    registry = _registry(tmp_path)

    file_result = registry.file_info("src/app.py")
    dir_result = registry.file_info("src")
    missing_result = registry.file_info("does/not/exist.py")

    assert file_result.data["kind"] == "file"
    assert file_result.data["exists"] is True
    assert file_result.data["extension"] == ".py"
    assert dir_result.data["kind"] == "directory"
    assert missing_result.data["kind"] == "missing"
    assert missing_result.data["exists"] is False


# ---------------------------------------------------------------------------
# find_file
# ---------------------------------------------------------------------------


def test_find_file_finds_basename_case_insensitively(tmp_path: Path):
    _write(tmp_path, "src/Widget.tsx", "x\n")
    registry = _registry(tmp_path)

    result = registry.find_file("widget.tsx")

    assert result.ok is True
    assert "src/Widget.tsx" in result.data["matches"]


# ---------------------------------------------------------------------------
# grep_files
# ---------------------------------------------------------------------------


def test_grep_files_finds_symbol_with_line_numbers(tmp_path: Path):
    _write(tmp_path, "src/game.ts", "import x\nexport class GameCanvas {}\n")
    registry = _registry(tmp_path)

    result = registry.grep_files("GameCanvas")

    assert result.ok is True
    hits = result.data["matches"]
    assert len(hits) == 1
    assert hits[0]["filepath"] == "src/game.ts"
    assert hits[0]["line"] == 2
    assert "GameCanvas" in hits[0]["text"]


def test_grep_files_ignores_node_modules_and_git(tmp_path: Path):
    _write(tmp_path, "src/game.ts", "class GameCanvas {}\n")
    _write(tmp_path, "node_modules/pkg/index.js", "class GameCanvas {}\n")
    _write(tmp_path, ".git/config", "GameCanvas\n")
    registry = _registry(tmp_path)

    result = registry.grep_files("GameCanvas")

    paths = {hit["filepath"] for hit in result.data["matches"]}
    assert paths == {"src/game.ts"}


def test_grep_files_extension_filter(tmp_path: Path):
    _write(tmp_path, "a.ts", "TARGET\n")
    _write(tmp_path, "b.py", "TARGET\n")
    registry = _registry(tmp_path)

    result = registry.grep_files("TARGET", extensions=".ts")

    paths = {hit["filepath"] for hit in result.data["matches"]}
    assert paths == {"a.ts"}


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


def test_edit_file_replaces_one_exact_match(tmp_path: Path):
    _write(tmp_path, "greet.py", "print('hello world')\n")
    registry = _registry(tmp_path)

    result = registry.edit_file("greet.py", "hello world", "hi there")

    assert result.ok is True
    assert result.data["replacements"] == 1
    assert (tmp_path / "greet.py").read_text(encoding="utf-8") == "print('hi there')\n"


def test_edit_file_fails_when_old_string_absent(tmp_path: Path):
    _write(tmp_path, "greet.py", "print('hello')\n")
    registry = _registry(tmp_path)

    result = registry.edit_file("greet.py", "goodbye", "hi")

    assert result.ok is False
    assert result.data["matches"] == 0
    assert result.data["current_excerpt"] == "print('hello')\n"
    assert (tmp_path / "greet.py").read_text(encoding="utf-8") == "print('hello')\n"


def test_edit_file_tolerates_trailing_whitespace_drift(tmp_path: Path):
    """Regression: a local model reproducing the block without the file's
    trailing whitespace used to fail with 'old_string not found'. It must now
    succeed, and the untouched context line must keep its real bytes."""
    _write(tmp_path, "app.py", "def greet(name):\n    msg = 'hi'   \n    return msg\n")
    registry = _registry(tmp_path)

    result = registry.edit_file(
        "app.py",
        "def greet(name):\n    msg = 'hi'\n    return msg",   # no trailing spaces
        "def greet(name):\n    msg = 'hello'\n    return msg",
    )

    assert result.ok is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == (
        "def greet(name):\n    msg = 'hello'\n    return msg\n"
    )


def test_edit_file_indents_multiline_replacement_at_inline_anchor(tmp_path: Path):
    _write(
        tmp_path,
        "models.py",
        "class User:\n    role = field()\n    created_at = field()\n",
    )
    registry = _registry(tmp_path)

    result = registry.edit_file(
        "models.py",
        "role = field()",
        "name = field()\nrole = field()",
    )

    assert result.ok is True
    assert (tmp_path / "models.py").read_text(encoding="utf-8") == (
        "class User:\n    name = field()\n    role = field()\n    created_at = field()\n"
    )


def test_edit_file_does_not_fuzzy_match_on_leading_indent_change(tmp_path: Path):
    """Safety: leading-indentation differences are NOT auto-fixed (that could
    silently re-indent code), so a dedented old_string still fails cleanly."""
    _write(tmp_path, "app.py", "class C:\n    def m(self):\n        return 1\n")
    registry = _registry(tmp_path)

    result = registry.edit_file(
        "app.py",
        "def m(self):\n    return 1",   # dedented vs the file's real indentation
        "def m(self):\n    return 2",
    )

    assert result.ok is False
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == (
        "class C:\n    def m(self):\n        return 1\n"
    )


def test_edit_file_fails_on_multiple_matches_without_replace_all(tmp_path: Path):
    _write(tmp_path, "nums.py", "a = 1\nb = 1\nc = 1\n")
    registry = _registry(tmp_path)

    result = registry.edit_file("nums.py", "= 1", "= 2")

    assert result.ok is False
    assert result.data["matches"] == 3
    assert len(result.data["candidate_contexts"]) == 3
    assert result.data["candidate_contexts"][0]["text"].startswith("a = 1")
    # File is untouched on an ambiguous edit.
    assert (tmp_path / "nums.py").read_text(encoding="utf-8") == "a = 1\nb = 1\nc = 1\n"


def test_edit_file_uses_uniquely_named_request_context_to_disambiguate(tmp_path: Path):
    source = (
        "def add(a, b):\n"
        "    return a + b\n\n"
        "def subtract(a, b):\n"
        "    return a + b\n"
    )
    _write(tmp_path, "calc.py", source)
    registry = _registry(tmp_path)
    registry.set_user_request("Fix the subtract function in calc.py so it returns a - b")

    result = registry.edit_file("calc.py", "return a + b", "return a - b")

    assert result.ok is True
    assert result.data["auto_disambiguated"] is True
    assert (tmp_path / "calc.py").read_text(encoding="utf-8") == (
        "def add(a, b):\n"
        "    return a + b\n\n"
        "def subtract(a, b):\n"
        "    return a - b\n"
    )


def test_edit_file_replace_all(tmp_path: Path):
    _write(tmp_path, "nums.py", "a = 1\nb = 1\n")
    registry = _registry(tmp_path)

    result = registry.edit_file("nums.py", "= 1", "= 2", replace_all=True)

    assert result.ok is True
    assert result.data["replacements"] == 2
    assert (tmp_path / "nums.py").read_text(encoding="utf-8") == "a = 2\nb = 2\n"


def test_edit_file_missing_file_returns_candidates(tmp_path: Path):
    _write(tmp_path, "client/src/App.tsx", "old\n")
    registry = _registry(tmp_path)

    result = registry.edit_file("src/App.tsx", "old", "new")

    assert result.ok is False
    assert "client/src/App.tsx" in result.data["candidates"]
    # No wrong-path file gets created by a failed edit.
    assert not (tmp_path / "src" / "App.tsx").exists()


def test_scoped_basename_edit_allows_unique_resolved_candidate(tmp_path: Path):
    _write(tmp_path, "project/frontend/src/App.jsx", "old\n")
    registry = _registry(tmp_path)
    registry.set_allowed_write_paths(["App.jsx"])

    result = registry.edit_file("project/frontend/src/App.jsx", "old", "new")

    assert result.ok is True
    assert (tmp_path / "project" / "frontend" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    ) == "new\n"


def test_scoped_basename_edit_refuses_ambiguous_resolved_candidate(tmp_path: Path):
    _write(tmp_path, "project/frontend/src/App.jsx", "old\n")
    _write(tmp_path, "project/admin/src/App.jsx", "old\n")
    registry = _registry(tmp_path)
    registry.set_allowed_write_paths(["App.jsx"])

    result = registry.edit_file("project/frontend/src/App.jsx", "old", "new")

    assert result.ok is False
    assert "allowed changes only to app.jsx" in result.message
    assert (tmp_path / "project" / "frontend" / "src" / "App.jsx").read_text(
        encoding="utf-8"
    ) == "old\n"


def test_edit_file_uses_transaction_backup(tmp_path: Path):
    original = "print('before')\n"
    _write(tmp_path, "mod.py", original)
    registry = _registry(tmp_path)

    result = registry.edit_file("mod.py", "before", "after")

    assert result.ok is True
    transaction_id = result.data["transaction_id"]
    manifest = registry.transactions.load_manifest(transaction_id)
    assert manifest is not None
    assert "mod.py" in manifest["backups"]
    backup_path = tmp_path / ".shamsu" / "mutations" / transaction_id / manifest["backups"]["mod.py"]
    assert backup_path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


def test_write_file_refuses_wrong_path_duplicate(tmp_path: Path):
    _write(tmp_path, "client/src/App.tsx", "real\n")
    registry = _registry(tmp_path)

    result = registry.write_file("src/App.tsx", "duplicate\n", overwrite=True)

    assert result.ok is False
    assert "client/src/App.tsx" in result.data["candidates"]
    assert not (tmp_path / "src" / "App.tsx").exists()


def test_write_file_reports_created_and_overwrote(tmp_path: Path):
    registry = _registry(tmp_path)

    created = registry.write_file("fresh.py", "x = 1\n", overwrite=True)
    overwritten = registry.write_file("fresh.py", "x = 2\n", overwrite=True)

    assert created.data["created"] is True
    assert created.data["overwrote"] is False
    assert overwritten.data["created"] is False
    assert overwritten.data["overwrote"] is True


def test_write_file_schema_exposes_overwrite_parameter(tmp_path: Path):
    registry = _registry(tmp_path)

    schema = next(
        item for item in registry.tool_schemas()
        if (item.get("function") or {}).get("name") == "write_file"
    )

    properties = schema["function"]["parameters"]["properties"]
    assert "overwrite" in properties
    assert properties["overwrite"]["default"] == "true"


# ---------------------------------------------------------------------------
# append_file
# ---------------------------------------------------------------------------


def test_append_file_adds_separator_and_transaction_backup(tmp_path: Path):
    _write(tmp_path, "calculator.py", "def add(a, b):\n    return a + b")
    approvals = []
    registry = AgentToolRegistry(
        tmp_path,
        approval_func=lambda request: approvals.append(request) or True,
    )

    result = registry.append_file(
        "calculator.py",
        "def subtract(a, b):\n    return a - b\n",
    )

    assert result.ok is True
    assert result.data["separator_added"] is True
    assert result.data["resolved_filepath"] == "calculator.py"
    assert result.data["transaction_id"]
    assert approvals[0].description == "Append to file: calculator.py"
    assert approvals[0].target_paths == ["calculator.py"]
    assert (tmp_path / "calculator.py").read_text(encoding="utf-8") == (
        "def add(a, b):\n    return a + b\n"
        "def subtract(a, b):\n    return a - b\n"
    )
    manifest = registry.transactions.load_manifest(result.data["transaction_id"])
    assert manifest is not None
    assert manifest["status"] == "applied"
    assert manifest["backups"]["calculator.py"]


def test_append_file_rejects_missing_file_and_read_only_request(tmp_path: Path):
    registry = _registry(tmp_path)

    missing = registry.append_file("missing.py", "value = 1\n")
    registry.set_read_only(True)
    blocked = registry.append_file("missing.py", "value = 1\n")

    assert missing.ok is False
    assert "does not exist" in missing.message
    assert blocked.ok is False
    assert blocked.data["read_only"] is True
    assert not (tmp_path / "missing.py").exists()


# ---------------------------------------------------------------------------
# Agent loop failed-read recovery
# ---------------------------------------------------------------------------


class _NoPlanLLM:
    """Planner stub that raises, so AgentChatLoop._append_plan is a no-op and no
    real backend is required."""

    async def run_specialist(self, specialist, pack):  # noqa: ANN001, D401
        raise RuntimeError("no planner in tests")


class _ScriptedClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, model, messages, tools, stream, options):  # noqa: ANN001
        self.calls += 1
        if not self._responses:
            raise AssertionError("ScriptedClient ran out of responses")
        return self._responses.pop(0)


def _tool_response(name: str, arguments: dict, call_id: str = "c1") -> dict:
    return {"message": {"content": "", "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}]}}


def _text_response(content: str) -> dict:
    return {"message": {"content": content, "tool_calls": []}}


@pytest.mark.asyncio
async def test_loop_injects_read_failure_correction(tmp_path: Path):
    _write(tmp_path, "client/src/App.tsx", "client\n")
    _write(tmp_path, "web/src/App.tsx", "web\n")
    registry = _registry(tmp_path)
    client = _ScriptedClient(
        [
            _tool_response("read_file", {"filepath": "src/App.tsx"}),
            _text_response("I inspected the candidates and reported back."),
        ]
    )
    loop = AgentChatLoop(tmp_path, client=client, tools=registry, llm=_NoPlanLLM())

    result = await loop.run("open the app component")

    user_contents = [m.content for m in loop.state.all_messages if m.role == "user"]
    assert any("NOT read" in c and "Candidates" in c for c in user_contents)
    assert result.final == "I inspected the candidates and reported back."


@pytest.mark.asyncio
async def test_loop_does_not_stop_on_prose_only_read_promise(tmp_path: Path):
    _write(tmp_path, "client/src/App.tsx", "client\n")
    _write(tmp_path, "web/src/App.tsx", "web\n")
    registry = _registry(tmp_path)
    client = _ScriptedClient(
        [
            # Round 1: read wrong path -> fails with two candidates.
            _tool_response("read_file", {"filepath": "src/App.tsx"}),
            # Round 2: prose-only stall promise, NO tool call.
            _text_response("I will read client/src/App.tsx next."),
            # Round 3: after the injected correction, actually read a candidate.
            _tool_response("read_file", {"filepath": "client/src/App.tsx"}),
            # Round 4: real final answer.
            _text_response("Done. I read the file."),
        ]
    )
    loop = AgentChatLoop(tmp_path, client=client, tools=registry, llm=_NoPlanLLM())

    result = await loop.run("open the app component")

    # The loop did NOT end on the hollow "I will read..." promise.
    assert result.final == "Done. I read the file."
    assert result.stopped is False
    assert client.calls == 4


@pytest.mark.asyncio
async def test_loop_salvages_explicit_quoted_read_promise(tmp_path: Path):
    _write(tmp_path, "seed_data.py", "SEED = True\n")
    registry = _registry(tmp_path)
    client = _ScriptedClient(
        [
            _text_response("Understood. Let's proceed with reading `seed_data.py` next."),
            _text_response("The seed data is present."),
        ]
    )
    loop = AgentChatLoop(tmp_path, client=client, tools=registry, llm=_NoPlanLLM())

    result = await loop.run("inspect the seed data")

    assert result.final == "The seed data is present."
    assert client.calls == 2
    assert any(message.role == "tool" for message in loop.state.all_messages)
    assistant_with_call = next(
        message
        for message in loop.state.all_messages
        if message.role == "assistant" and message.tool_calls
    )
    assert assistant_with_call.tool_calls[0]["function"]["arguments"] == {
        "filepath": "seed_data.py"
    }


def test_no_candidate_read_failure_names_the_files_that_do_exist(tmp_path: Path):
    """Light-tier failure observed live: the model read the DESTINATION of a
    rename first, got the old "go use find_file" coaching, and echoed the
    coaching as its final answer. The no-candidate correction must instead name
    the real files so the productive next call is the easiest continuation."""
    _write(tmp_path, "old_name.py", "GREETING = 'hi'\n")
    registry = _registry(tmp_path)
    loop = AgentChatLoop(tmp_path, client=_ScriptedClient([]), tools=registry, llm=_NoPlanLLM())

    message = loop._read_failure_correction("new_name.py", "not found")

    assert "old_name.py" in message
    assert "NOT read" in message
    assert "find_file" not in message


@pytest.mark.asyncio
async def test_requested_new_file_recovers_from_failed_read_by_writing(tmp_path: Path):
    registry = _registry(tmp_path)
    client = _ScriptedClient(
        [
            _tool_response("read_file", {"filepath": "converter.py"}),
            _tool_response(
                "write_file",
                {"filepath": "converter.py", "content": "print('212.0')\n"},
            ),
            _text_response("Created converter.py."),
        ]
    )
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=registry,
        llm=_NoPlanLLM(),
        use_long_term_memory=False,
        use_planner=False,
    )

    result = await loop.run("Create a new file named converter.py with the implementation.")

    assert result.awaiting_user is False
    assert result.changed_files == ("converter.py",)
    assert (tmp_path / "converter.py").read_text(encoding="utf-8") == "print('212.0')\n"
    user_contents = [message.content for message in loop.state.all_messages if message.role == "user"]
    assert any("call write_file" in content and "Do not ask" in content for content in user_contents)


@pytest.mark.asyncio
async def test_failed_edit_prose_is_forced_into_bounded_mutation_retry(tmp_path: Path):
    _write(tmp_path, "converter.py", "print('broken')\n")
    registry = _registry(tmp_path)
    client = _ScriptedClient(
        [
            _tool_response(
                "edit_file",
                {
                    "filepath": "converter.py",
                    "old_string": "missing",
                    "new_string": "fixed",
                },
            ),
            _text_response("The edit is fixed now."),
            _tool_response(
                "write_file",
                {"filepath": "converter.py", "content": "print('fixed')\n"},
            ),
            _text_response("Fixed converter.py."),
        ]
    )
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=registry,
        llm=_NoPlanLLM(),
        use_long_term_memory=False,
        use_planner=False,
    )

    result = await loop.run("Fix converter.py.")

    assert result.stopped is False
    assert result.changed_files == ("converter.py",)
    assert (tmp_path / "converter.py").read_text(encoding="utf-8") == "print('fixed')\n"
    assert client.calls == 4


@pytest.mark.asyncio
async def test_empty_anchor_edit_recovers_with_append_file(tmp_path: Path):
    _write(tmp_path, "calculator.py", "def add(a, b):\n    return a + b\n")
    registry = _registry(tmp_path)
    audit = SessionAuditLog(tmp_path, "append-recovery")
    client = _ScriptedClient(
        [
            _tool_response(
                "edit_file",
                {
                    "filepath": "calculator.py",
                    "old_string": "",
                    "new_string": "def subtract(a, b):\n    return a - b\n",
                },
            ),
            _tool_response(
                "append_file",
                {
                    "filepath": "calculator.py",
                    "content": "def subtract(a, b):\n    return a - b\n",
                },
            ),
            _text_response("Added subtract to calculator.py."),
        ]
    )
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=registry,
        llm=_NoPlanLLM(),
        audit=audit,
        use_long_term_memory=False,
        use_planner=False,
    )

    result = await loop.run("Add a subtract(a, b) function to calculator.py.")

    assert result.stopped is False
    assert result.changed_files == ("calculator.py",)
    assert "def subtract" in (tmp_path / "calculator.py").read_text(encoding="utf-8")
    user_contents = [message.content for message in loop.state.all_messages if message.role == "user"]
    # The correction still refuses to ASSUME which was meant, but now spells out
    # both branches: a 7B model given only the ambiguity repeated the identical
    # empty-anchor call until the run timed out (live 2026-08-01).
    assert any(
        "does not reveal whether you intended to add or replace" in content
        for content in user_contents
    )
    assert any("to ADD it as new content, call append_file" in content for content in user_contents)
    assert any("to REPLACE existing content, call read_file" in content for content in user_contents)
    records = [
        json.loads(line)
        for line in audit.session_path.read_text(encoding="utf-8").splitlines()
    ]
    change = next(record for record in records if record["event_type"] == "file.change")
    assert change["action"] == "append"
    assert change["filepath"] == "calculator.py"
    assert change["transaction_id"]


# ---------------------------------------------------------------------------
# R6 - an impossible line range must REFUSE, not quietly read something else
#
# `end = min(total_lines, max(end, start))` made every malformed range legal.
# Live 2026-08-23 in F:\Work\demo2\test2, `read_file(player.js, 145, 20)` came
# back ok:true / "Read file." / one line, with end_line silently rewritten to
# 145 - and the model sent that identical payload five times. 8 of 47 ranged
# reads in that session were impossible; all 8 were answered with a success.
# ---------------------------------------------------------------------------


def _numbered(tmp_path: Path, rel: str, lines: int) -> Path:
    return _write(tmp_path, rel, "\n".join(f"line {i}" for i in range(1, lines + 1)))


def test_end_line_before_start_line_is_refused(tmp_path: Path) -> None:
    _numbered(tmp_path, "player.js", 151)

    result = _registry(tmp_path).read_file("player.js", start_line=145, end_line=20)

    assert not result.ok, "an empty range must not report success"
    assert result.data.get("impossible_range") is True
    assert "NOT read" in result.message
    # It must not hand back a clamped range pretending to be what was asked for.
    assert "content" not in result.data


def test_the_refusal_names_the_call_that_was_probably_meant(tmp_path: Path) -> None:
    """A refusal a model cannot act on costs the same as a silent success."""
    _numbered(tmp_path, "player.js", 151)

    result = _registry(tmp_path).read_file("player.js", start_line=145, end_line=20)

    assert "end_line is a line NUMBER, not a count" in result.message
    assert "start_line=145" in result.message and "end_line=164" in result.message


def test_start_line_past_the_end_is_refused_rather_than_read_as_empty(tmp_path: Path) -> None:
    """`lines[start-1:end]` past EOF is [], which used to come back as a
    successful read with no content at all."""
    _numbered(tmp_path, "player.js", 151)

    result = _registry(tmp_path).read_file("player.js", start_line=200, end_line=210)

    assert not result.ok
    assert result.data.get("past_end") is True
    assert "151" in result.message


def test_a_range_running_past_the_end_still_reads(tmp_path: Path) -> None:
    """Only the IMPOSSIBLE cases are refused. Asking for more than is left is a
    perfectly ordinary way to say 'from here to the end'."""
    _numbered(tmp_path, "player.js", 151)

    result = _registry(tmp_path).read_file("player.js", start_line=145, end_line=400)

    assert result.ok
    assert result.data["start_line"] == 145
    assert result.data["end_line"] == 151
    assert "line 151" in result.data["content"]


def test_ordinary_ranges_are_untouched(tmp_path: Path) -> None:
    _numbered(tmp_path, "player.js", 151)
    registry = _registry(tmp_path)

    result = registry.read_file("player.js", start_line=148, end_line=153)
    assert result.ok and result.data["start_line"] == 148

    whole = registry.read_file("player.js")
    assert whole.ok and whole.data["total_lines"] == 151
