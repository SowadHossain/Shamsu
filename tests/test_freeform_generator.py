"""Phase 2b: template-free generation (build anything from the PRD)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shamsu.agents.freeform_generator import FreeformGenerator, _default_verify
from shamsu.prd.contract import PRDContract
from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.registry.suitability import GenerationStrategy

CMS_PRD = """# Markdown Knowledge Base

## Overview
A bespoke headless CMS for markdown docs with a Python REST API. Built in Python.

## Features
- Create and edit markdown documents
- Full-text search
"""


class FakeRunner:
    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code
        self.commands: list[str] = []

    def run(self, command: str, cwd) -> tuple[int, str, str]:
        self.commands.append(command)
        return (self._exit_code, "", "" if self._exit_code == 0 else "syntax error")


def _python_generate(stack: str = "python", files=None):
    files = files or [
        {"path": "app.py", "purpose": "entry point"},
        {"path": "requirements.txt", "purpose": "deps"},
    ]

    def generate(system: str, user: str, schema: dict) -> str:
        if "planning a small project" in system:
            return json.dumps({"stack": stack, "files": files})
        if "writing ONE file" in system:
            if "requirements.txt" in user.split("## File to write now", 1)[-1][:60]:
                return json.dumps({"content": "flask\n"})
            return json.dumps({"content": "print('hi')\n"})
        return ""

    return generate


def _project():
    return build_project_spec(parse_prd_text(CMS_PRD, markdown=True))


# --- deterministic verifier selection ----------------------------------------

def test_default_verify_python_uses_pycompile():
    contract = PRDContract(stack_hint="python")
    cmd = _default_verify("python", contract, ["app.py", "requirements.txt"])
    assert cmd.startswith("pip install -r requirements.txt")
    assert "py_compile app.py" in cmd


def test_default_verify_node_uses_npm_build():
    cmd = _default_verify("node", PRDContract(), ["package.json", "src/main.ts"])
    assert cmd == "npm install && npm run build"


def test_default_verify_unknown_stack_returns_empty():
    assert _default_verify("brainfuck", PRDContract(), ["notes.txt"]) == ""


# --- FreeformGenerator --------------------------------------------------------

def test_freeform_generates_and_verifies_python_project(tmp_path: Path):
    project = _project()
    assert project.suitability.strategy is GenerationStrategy.FREEFORM
    gen = FreeformGenerator(tmp_path, _python_generate(), command_runner=FakeRunner(0))
    result = gen.run(project, tmp_path / "cms")

    assert result.success is True
    assert result.verified is True
    assert set(result.written_files) == {"app.py", "requirements.txt"}
    assert (tmp_path / "cms" / "app.py").read_text() == "print('hi')\n"
    assert "py_compile" in result.verify_command
    assert "passed" in result.final_message.lower()


def test_freeform_unverified_when_no_verifier(tmp_path: Path):
    project = _project()
    gen = FreeformGenerator(
        tmp_path,
        _python_generate(stack="unknown", files=[{"path": "notes.txt", "purpose": "x"}]),
        command_runner=FakeRunner(0),
    )
    result = gen.run(project, tmp_path / "cms")
    assert result.written_files == ["notes.txt"]
    assert result.verified is False
    assert result.success is False
    assert "unverified" in result.final_message.lower()


def test_freeform_reports_failure_honestly_on_bad_build(tmp_path: Path):
    project = _project()
    gen = FreeformGenerator(
        tmp_path,
        _python_generate(),
        command_runner=FakeRunner(1),   # verifier always fails
    )
    result = gen.run(project, tmp_path / "cms")
    assert result.success is False
    lowered = result.final_message.lower()
    assert "passed" not in lowered
    assert "fixed" not in lowered


def test_freeform_no_plan_is_honest_failure(tmp_path: Path):
    project = _project()
    gen = FreeformGenerator(tmp_path, lambda s, u, sc: "", command_runner=FakeRunner(0))
    result = gen.run(project, tmp_path / "cms")
    assert result.success is False
    assert "plan" in result.final_message.lower()


# --- full pipeline routing ----------------------------------------------------

@pytest.mark.asyncio
async def test_full_pipeline_routes_cms_to_freeform(tmp_path: Path, monkeypatch):
    from shamsu.agents import freeform_generator as ff_mod
    from shamsu.agents.freeform_generator import FreeformRunResult
    from shamsu.agents.full_pipeline import FullDjangoPipeline

    prd = tmp_path / "cms.md"
    prd.write_text(CMS_PRD)
    captured: dict = {}

    def fake_run(self, project, target_dir):
        captured["strategy"] = project.suitability.strategy
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        return FreeformRunResult(
            target_dir=target, stack="python", written_files=["app.py"],
            verified=True, success=True, exit_code=0,
            final_message="Verifier passed (exit code 0).",
        )

    monkeypatch.setattr(ff_mod.FreeformGenerator, "run", fake_run)

    class _DummySearch:
        def search(self, *a, **k):
            return []

    result = await FullDjangoPipeline(
        tmp_path, search=_DummySearch(), approval_func=lambda _r: True,
        generate=lambda s, u, sc: "",
    ).run(prd, tmp_path / "cms")

    assert captured["strategy"] is GenerationStrategy.FREEFORM
    assert result.success is True
    assert result.written_files == ["app.py"]
