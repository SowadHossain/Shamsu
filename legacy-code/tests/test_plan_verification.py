"""Tests for the end-of-plan integration verify gate (rest of G4): the chat loop
reports which files it changed, and _execute_plan verifies the whole set once at
the end with an honest verdict."""
from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

import shamsu.cli.repl as repl
from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.verify.gate import VerifyOutcome


class _NoPlanLLM:
    async def run_specialist(self, specialist, pack):  # noqa: ANN001
        raise RuntimeError("no planner in tests")


class _ScriptedClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)

    async def chat(self, model, messages, tools, stream, options):  # noqa: ANN001
        return self._responses.pop(0)


def _tool_response(name: str, arguments: dict) -> dict:
    return {"message": {"content": "", "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": arguments}}]}}


def _text_response(content: str) -> dict:
    return {"message": {"content": content, "tool_calls": []}}


def _console() -> tuple[Console, StringIO]:
    buffer = StringIO()
    return Console(file=buffer, force_terminal=False, width=100), buffer


# ---------------------------------------------------------------------------
# The loop reports changed files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_loop_reports_changed_files(tmp_path: Path):
    client = _ScriptedClient(
        [
            _tool_response("write_file", {"filepath": "hello.py", "content": "print('hi')\n"}),
            _text_response("Done, created hello.py."),
        ]
    )
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=_NoPlanLLM(),
    )
    result = await loop.run("create hello.py that prints hi")
    assert "hello.py" in result.changed_files
    assert (tmp_path / "hello.py").is_file()


@pytest.mark.asyncio
async def test_mutation_request_cannot_finish_on_model_done_without_tool(tmp_path: Path):
    client = _ScriptedClient(
        [
            # One response per missing-mutation recovery attempt, plus the
            # first: prose can never finish a mutation request, however many
            # times the model insists it is done.
            _text_response("Done, fixed app.py."),
            _text_response("The fix should now work."),
            _text_response("app.py is already correct."),
        ]
    )
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=_NoPlanLLM(),
    )

    result = await loop.run("fix app.py")

    assert result.stopped is True
    assert "no file mutation succeeded" in result.final
    assert "No file was changed" in result.final


@pytest.mark.asyncio
async def test_prose_after_read_gets_one_bounded_mutation_tool_recovery(tmp_path: Path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    client = _ScriptedClient(
        [
            _tool_response("read_file", {"filepath": "app.py"}),
            _text_response("The value needs to be changed to 2."),
            _tool_response(
                "edit_file",
                {
                    "filepath": "app.py",
                    "old_string": "VALUE = 1",
                    "new_string": "VALUE = 2",
                },
            ),
            _text_response("Done."),
        ]
    )
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=_NoPlanLLM(),
    )

    result = await loop.run("fix app.py so VALUE is 2")

    assert result.stopped is False
    assert result.changed_files == ("app.py",)
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"


@pytest.mark.asyncio
async def test_successful_tool_result_overrides_false_model_failure(tmp_path: Path):
    client = _ScriptedClient(
        [
            _tool_response("write_file", {"filepath": "hello.py", "content": "print('hi')\n"}),
            _text_response("I could not create hello.py."),
        ]
    )
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=_NoPlanLLM(),
    )

    result = await loop.run("create hello.py")

    assert "mutation succeeded on disk" in result.final
    assert "[verified]" in result.final
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hi')\n"


@pytest.mark.asyncio
async def test_successful_write_is_not_reparsed_as_markdown_fallback(tmp_path: Path):
    client = _ScriptedClient(
        [
            _tool_response("write_file", {"filepath": "hello.py", "content": "print('hi')\n"}),
            _text_response("Done.\n```bash\npython hello.py\n```"),
        ]
    )
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=_NoPlanLLM(),
    )

    result = await loop.run("create hello.py")

    assert result.awaiting_user is False
    assert result.changed_files == ("hello.py",)


@pytest.mark.asyncio
async def test_markdown_fallback_write_counts_as_successful_mutation(tmp_path: Path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    client = _ScriptedClient(
        [
            _text_response("```python\nvalue = 2\n```"),
            _text_response("I could not edit app.py."),
        ]
    )
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=_NoPlanLLM(),
    )

    result = await loop.run("fix the bug in app.py")

    assert "mutation succeeded on disk" in result.final
    assert result.changed_files == ("app.py",)
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"


# ---------------------------------------------------------------------------
# _verify_completed_plan honest verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_completed_plan_reports_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        repl,
        "verify_only",
        lambda *a, **k: VerifyOutcome(
            verified=False, exit_code=1, command="python -m py_compile a.py",
            summary="Verification FAILED: `python -m py_compile a.py` (exit 1).",
        ),
    )
    console, buffer = _console()
    await repl._verify_completed_plan(["a.py"], tmp_path, console, None)
    out = buffer.getvalue()
    assert "UNVERIFIED" in out
    assert "did NOT pass" in out


@pytest.mark.asyncio
async def test_verify_completed_plan_reports_success(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        repl,
        "verify_only",
        lambda *a, **k: VerifyOutcome(
            verified=True, exit_code=0, command="python -m py_compile a.py",
            summary="Verification passed: `python -m py_compile a.py` (exit 0).",
        ),
    )
    console, buffer = _console()
    await repl._verify_completed_plan(["a.py"], tmp_path, console, None)
    assert "verified" in buffer.getvalue().lower()


@pytest.mark.asyncio
async def test_verify_completed_plan_unverifiable_is_quiet_but_honest(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        repl,
        "verify_only",
        lambda *a, **k: VerifyOutcome(verified=False, unverifiable=True, summary="UNVERIFIED"),
    )
    console, buffer = _console()
    await repl._verify_completed_plan(["notes.txt"], tmp_path, console, None)
    assert "UNVERIFIED" in buffer.getvalue()


@pytest.mark.asyncio
async def test_verify_completed_plan_skips_when_no_changes(tmp_path: Path, monkeypatch):
    def _boom(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("verify must not run with no changed files")

    monkeypatch.setattr(repl, "verify_only", _boom)
    console, buffer = _console()
    await repl._verify_completed_plan([], tmp_path, console, None)
    assert buffer.getvalue() == ""


# ---------------------------------------------------------------------------
# Gap E2: "unverifiable" used to be permanent for node stacks - lightweight
# mode drops installs, so JS/TS users never got a verdict at all. Now the
# heavy verifier is OFFERED (one approval, at the end-of-plan pause), never
# run silently.
# ---------------------------------------------------------------------------


class _RecordingApproval:
    def __init__(self, answer: bool) -> None:
        self.answer = answer
        self.requests = []

    def ask(self, request):  # noqa: ANN001
        self.requests.append(request)
        return self.answer


@pytest.mark.asyncio
async def test_unverifiable_node_changes_offer_the_heavy_verifier(tmp_path, monkeypatch):
    from shamsu.verify.gate import VerifyOutcome

    approval = _RecordingApproval(answer=True)
    monkeypatch.setattr(repl, "_make_approval_manager", lambda *a, **k: approval)

    heavy_calls: list[dict] = []

    def _fake_verify(workspace, files, lightweight=True, **kwargs):
        if lightweight:
            return VerifyOutcome(verified=False, unverifiable=True, summary="node build too heavy")
        heavy_calls.append({"files": list(files)})
        return VerifyOutcome(verified=True, exit_code=0, command="npm run build", summary="build passed")

    monkeypatch.setattr(repl, "verify_only", _fake_verify)
    console = Console(record=True, width=100)

    await repl._verify_completed_plan(["package.json", "src/app.js"], tmp_path, console, None)

    assert approval.requests, "the heavy verifier must be OFFERED"
    assert "install" in approval.requests[0].reason.lower()
    assert heavy_calls, "approval should trigger the full verify"
    assert "Plan verified (full build)" in console.export_text()


@pytest.mark.asyncio
async def test_denying_the_heavy_verifier_leaves_it_unverified(tmp_path, monkeypatch):
    from shamsu.verify.gate import VerifyOutcome

    approval = _RecordingApproval(answer=False)
    monkeypatch.setattr(repl, "_make_approval_manager", lambda *a, **k: approval)

    def _fake_verify(workspace, files, lightweight=True, **kwargs):
        assert lightweight, "a denied offer must never run the heavy verify"
        return VerifyOutcome(verified=False, unverifiable=True, summary="node build too heavy")

    monkeypatch.setattr(repl, "verify_only", _fake_verify)
    console = Console(record=True, width=100)

    await repl._verify_completed_plan(["package.json"], tmp_path, console, None)

    out = console.export_text()
    assert "Skipped the full verifier" in out
    assert "UNVERIFIED" in out


@pytest.mark.asyncio
async def test_truly_unverifiable_changes_get_no_offer(tmp_path, monkeypatch):
    """No heavy command exists either (e.g. a lone .md change): don't ask the
    user to approve something that cannot run."""
    from shamsu.verify.gate import VerifyOutcome

    approval = _RecordingApproval(answer=True)
    monkeypatch.setattr(repl, "_make_approval_manager", lambda *a, **k: approval)
    monkeypatch.setattr(
        repl,
        "verify_only",
        lambda *a, **k: VerifyOutcome(verified=False, unverifiable=True, summary="nothing to run"),
    )
    console = Console(record=True, width=100)

    await repl._verify_completed_plan(["README.md"], tmp_path, console, None)

    assert not approval.requests
    assert "left UNVERIFIED" in console.export_text()
