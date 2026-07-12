"""Tests for the smarter file read/write/find/edit tool layer and the agent
loop's failed-read recovery. See shamsu/tools/agent_tools.py and
shamsu/agents/chat_loop.py."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.tools.agent_tools import AgentToolRegistry


def _registry(tmp_path: Path) -> AgentToolRegistry:
    return AgentToolRegistry(tmp_path, approval_func=lambda _request: True)


def _write(tmp_path: Path, rel: str, content: str = "x\n") -> Path:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def _write_docx(tmp_path: Path, rel: str, paragraphs: list[str]) -> Path:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(
        f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("word/document.xml", document)
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


def test_read_file_extracts_docx_text(tmp_path: Path):
    _write_docx(
        tmp_path,
        "input_files/Practice01_FA.docx",
        ["Question 1: What is 2 + 2?", "Question 2: Name one primary color."],
    )
    registry = _registry(tmp_path)

    result = registry.read_file("input_files/Practice01_FA.docx")

    assert result.ok is True
    assert result.message == "Read DOCX file."
    assert "Question 1: What is 2 + 2?" in result.data["content"]
    assert "Question 2: Name one primary color." in result.data["content"]


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
    # File is untouched on an ambiguous edit.
    assert (tmp_path / "nums.py").read_text(encoding="utf-8") == "a = 1\nb = 1\nc = 1\n"


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
