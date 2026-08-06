from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

from rich.console import Console

from shamsu.cli import repl


def _console() -> Console:
    return Console(file=StringIO(), force_terminal=False, width=100)


def _package(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text('{"compilerOptions":{}}', encoding="utf-8")


def _patch_agent(monkeypatch) -> list[str]:
    """Record every repair pass, without running a real agent loop."""
    calls: list[str] = []

    async def fake_agent(user_input, workspace, console, **kwargs):
        calls.append(user_input)

    monkeypatch.setattr(repl, "_run_agent_chat", fake_agent)
    monkeypatch.setattr(repl, "_ensure_node_modules", lambda *_a, **_k: True)
    return calls


def test_verify_passes_first_try_without_calling_the_agent(monkeypatch, tmp_path):
    _package(tmp_path)
    calls = _patch_agent(monkeypatch)
    monkeypatch.setattr(repl, "_run_frontend_typecheck", lambda _ws: (True, ""))

    ok = asyncio.run(repl._verify_and_repair_frontend(tmp_path, Path("prd.md"), _console()))

    assert ok is True
    assert calls == []  # nothing to repair


def test_verify_repairs_then_passes(monkeypatch, tmp_path):
    _package(tmp_path)
    calls = _patch_agent(monkeypatch)
    results = iter([(False, "rules.ts has no exported member createInputState"), (True, "")])
    monkeypatch.setattr(repl, "_run_frontend_typecheck", lambda _ws: next(results))

    ok = asyncio.run(repl._verify_and_repair_frontend(tmp_path, Path("prd.md"), _console()))

    assert ok is True
    assert len(calls) == 1  # one repair pass, then it compiled
    assert "createInputState" in calls[0]  # the real error was fed back


def test_verify_gives_up_after_max_attempts(monkeypatch, tmp_path):
    _package(tmp_path)
    calls = _patch_agent(monkeypatch)
    monkeypatch.setattr(repl, "_run_frontend_typecheck", lambda _ws: (False, "still broken"))

    ok = asyncio.run(
        repl._verify_and_repair_frontend(tmp_path, Path("prd.md"), _console(), max_attempts=2)
    )

    assert ok is False
    # attempt 1 fails -> repair; attempt 2 fails -> give up (no repair after the last try).
    assert len(calls) == 1


def test_verify_skips_non_node_project(monkeypatch, tmp_path):
    calls = _patch_agent(monkeypatch)
    # No package.json in tmp_path — nothing to compile.
    monkeypatch.setattr(
        repl, "_run_frontend_typecheck",
        lambda _ws: (_ for _ in ()).throw(AssertionError("should not typecheck a non-node project")),
    )

    ok = asyncio.run(repl._verify_and_repair_frontend(tmp_path, Path("prd.md"), _console()))

    assert ok is True
    assert calls == []


def test_verify_skips_project_without_tsconfig(monkeypatch, tmp_path):
    # A package.json without a tsconfig.json (e.g. a vanilla JS project) has no
    # tsc gate — don't run tsc and don't fail.
    (tmp_path / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    calls = _patch_agent(monkeypatch)
    monkeypatch.setattr(
        repl, "_run_frontend_typecheck",
        lambda _ws: (_ for _ in ()).throw(AssertionError("should not typecheck without tsconfig")),
    )

    ok = asyncio.run(repl._verify_and_repair_frontend(tmp_path, Path("prd.md"), _console()))

    assert ok is True
    assert calls == []
