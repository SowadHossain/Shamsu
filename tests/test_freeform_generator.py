"""Phase 2b: template-free generation (build anything from the PRD)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import start_run
from shamsu.agents.freeform_generator import (
    FreeformGenerator,
    PlannedFile,
    _default_verify,
    _normalize_planned_files,
    _sanitize_generated_content,
)
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

VITE_PRD = """# Ops Console

## Overview
A local-first React and Vite operations dashboard with a small terminal CLI.

## Recommended Technical Stack
- TypeScript
- React
- Vite
- Node.js
- Zod
- Vitest

### Entity: Incident

Fields:
- id: string, required
- title: string, required
- severity: enum, values: low, medium, high
- status: enum, values: new, in_progress, completed
"""


class FakeRunner:
    def __init__(self, exit_code: int) -> None:
        self._exit_code = exit_code
        self.commands: list[str] = []

    def run(self, command: str, cwd) -> tuple[int, str, str]:
        self.commands.append(command)
        return (self._exit_code, "", "" if self._exit_code == 0 else "syntax error")


class CompileRunner:
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.commands: list[str] = []

    def run(self, command: str, cwd) -> tuple[int, str, str]:
        self.commands.append(command)
        source = (Path(cwd) / self.filename).read_text(encoding="utf-8", errors="replace")
        try:
            compile(source, self.filename, "exec")
        except SyntaxError as exc:
            return (1, "", f"  File \"{self.filename}\", line {exc.lineno}\nSyntaxError: {exc.msg}\n")
        return (0, "", "")


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


def _vite_project():
    return build_project_spec(parse_prd_text(VITE_PRD, markdown=True))


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


def test_freeform_skips_timed_out_file_generation(tmp_path: Path):
    project = _project()
    files = [
        {"path": "slow.py", "purpose": "times out"},
        {"path": "app.py", "purpose": "entry point"},
    ]

    def generate(system: str, user: str, schema: dict) -> str:
        if "planning a small project" in system:
            return json.dumps({"stack": "python", "files": files})
        if "slow.py" in user.split("## File to write now", 1)[-1][:80]:
            raise TimeoutError("too slow")
        return json.dumps({"content": "print('ok')\n"})

    gen = FreeformGenerator(tmp_path, generate, command_runner=FakeRunner(0))
    result = gen.run(project, tmp_path / "cms")

    assert result.success is True
    assert result.written_files == ["app.py"]
    assert not (tmp_path / "cms" / "slow.py").exists()


def test_freeform_regenerates_source_when_strict_repair_has_no_edit(tmp_path: Path):
    project = _project()
    calls: list[str] = []

    def generate(system: str, user: str, schema: dict) -> str:
        calls.append(system)
        if "planning a small project" in system:
            return json.dumps({"stack": "python", "files": [{"path": "app.py", "purpose": "entry"}]})
        if "writing ONE file" in system:
            return json.dumps({"content": "def main():\n    print(f'{value[\n"})
        if "STRICT DEBUG MODE" in system:
            return json.dumps({"root_cause": "truncated file", "target_file": "app.py"})
        if "repairing ONE generated source file" in system:
            return json.dumps({"content": "def main():\n    print('ok')\n\nmain()\n"})
        return ""

    runner = CompileRunner("app.py")
    result = FreeformGenerator(tmp_path, generate, command_runner=runner).run(
        project,
        tmp_path / "cms",
    )

    assert result.success is True
    assert (tmp_path / "cms" / "app.py").read_text(encoding="utf-8") == (
        "def main():\n    print('ok')\n\nmain()\n"
    )
    assert any("repairing ONE generated source file" in system for system in calls)


def test_freeform_hardens_explicit_python_cli_prd(tmp_path: Path):
    prd_text = Path("evals/fixtures/prds/ledgerlite_medium.md").read_text(encoding="utf-8")
    project = build_project_spec(parse_prd_text(prd_text, markdown=True))

    def generate(system: str, user: str, schema: dict) -> str:
        if "planning a small project" in system:
            return json.dumps(
                {
                    "stack": "python",
                    "files": [
                        {"path": "ledgerlite.py", "purpose": "expense CLI"},
                        {"path": "ledgerlite.json", "purpose": "default JSON database"},
                    ],
                }
            )
        if "ledgerlite.json" in user.split("## File to write now", 1)[-1][:80]:
            return json.dumps({"content": "[]\n"})
        if "writing ONE file" in system:
            return json.dumps({"content": "print(f'{value[\n"})
        return ""

    target = tmp_path / "ledgerlite-medium-build"
    result = FreeformGenerator(
        tmp_path,
        generate,
        command_runner=CompileRunner("ledgerlite.py"),
    ).run(project, target)

    assert result.success is True
    assert "freeform" not in (target / "ledgerlite.py").read_text(encoding="utf-8").lower()

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "ledgerlite.py", *args],
            cwd=target,
            text=True,
            capture_output=True,
            check=False,
        )

    assert run("seed", "--db", "data.json").stdout.strip() == "seeded 4 expenses"
    added = run("add", "--db", "data.json", "--category", "travel", "--amount", "42.50", "--note", "taxi")
    assert added.returncode == 0
    assert added.stdout.strip() == "added expense travel 42.50"
    assert run("summary", "--db", "data.json").stdout.strip() == "total 192.50"
    assert run("export", "--db", "data.json", "--out", "report.csv").stdout.strip() == "exported report.csv"
    listed = run("list", "--db", "data.json").stdout
    assert "exp-001 supplies 18.25 notebooks" in listed
    assert "exp-005 travel 42.50 taxi" in listed
    assert (target / "report.csv").read_text(encoding="utf-8").startswith("id,category,amount,note")


def test_freeform_sanitizes_markdown_file_wrappers():
    assert _sanitize_generated_content("## package.json\n{\"scripts\": {}}\n", "package.json") == (
        '{"scripts": {}}\n'
    )
    assert _sanitize_generated_content("```json\n{\"ok\": true}\n```", "package.json") == (
        '{"ok": true}\n'
    )
    assert _sanitize_generated_content(
        "Here is package.json:\n```json\n{\"scripts\": {}}\n```",
        "package.json",
    ) == (
        '{"scripts": {}}\n'
    )
    assert _sanitize_generated_content("#!/usr/bin/env node\nconsole.log(1)\n", "bin/atlas") == (
        "#!/usr/bin/env node\nconsole.log(1)\n"
    )


def test_freeform_escapes_python_newlines_inside_quoted_strings():
    broken = (
        "with open('report.csv', 'w') as f:\n"
        "    f.write('id,category,amount,note\n"
        "')\n"
        "    f.write(\"exp-001,supplies,18.25,notebooks\n"
        "\")\n"
    )

    fixed = _sanitize_generated_content(broken, "ledgerlite.py")

    assert "f.write('id,category,amount,note\\n')" in fixed
    assert 'f.write("exp-001,supplies,18.25,notebooks\\n")' in fixed
    compile(fixed, "ledgerlite.py", "exec")


def test_freeform_normalizes_extensionless_plan_paths():
    normalized = _normalize_planned_files(
        [
            PlannedFile("src/cli"),
            PlannedFile("src/cli/index.ts"),
            PlannedFile("src/components"),
            PlannedFile("src/styles"),
            PlannedFile("src/assets"),
        ]
    )

    assert [file.path for file in normalized] == [
        "src/cli/index.ts",
        "src/components/index.tsx",
        "src/styles.css",
    ]


def test_freeform_plan_collision_does_not_crash_generation(tmp_path: Path):
    project = _project()
    files = [
        {"path": "src/cli", "purpose": "cli placeholder"},
        {"path": "src/cli/index.ts", "purpose": "cli entry"},
    ]

    def generate(system: str, user: str, schema: dict) -> str:
        if "planning a small project" in system:
            return json.dumps({"stack": "unknown", "files": files})
        return json.dumps({"content": "export const ok = true;\n"})

    result = FreeformGenerator(tmp_path, generate, command_runner=FakeRunner(0)).run(
        project,
        tmp_path / "cms",
    )

    assert result.written_files == ["src/cli/index.ts"]
    assert (tmp_path / "cms" / "src" / "cli").is_dir()
    assert (tmp_path / "cms" / "src" / "cli" / "index.ts").read_text() == (
        "export const ok = true;\n"
    )


def test_freeform_hardens_vite_react_project_before_verify(tmp_path: Path):
    project = _vite_project()

    def generate(system: str, user: str, schema: dict) -> str:
        if "planning a small project" in system:
            return json.dumps(
                {
                    "stack": "TypeScript React Vite",
                    "files": [
                        {"path": "package.json", "purpose": "manifest"},
                        {"path": "src/App.tsx", "purpose": "broken app shell"},
                    ],
                }
            )
        if "package.json" in user.split("## File to write now", 1)[-1][:80]:
            return json.dumps(
                {
                    "content": json.dumps(
                        {
                            "scripts": {"build": "vite build"},
                            "dependencies": {"@types/zod": "^2.0.2"},
                        }
                    )
                }
            )
        return json.dumps({"content": "export default function App() { return null }\n"})

    runner = FakeRunner(0)
    ledger = start_run(tmp_path, "build React Vite dashboard")
    set_current_run(ledger)
    try:
        result = FreeformGenerator(tmp_path, generate, command_runner=runner).run(
            project,
            tmp_path / "ops",
        )
    finally:
        clear_current_run()

    package = json.loads((tmp_path / "ops" / "package.json").read_text())
    assert result.success is True
    assert "index.html" in result.written_files
    assert "src/data.ts" in result.written_files
    assert "@types/zod" not in package["dependencies"]
    assert "@types/zod" not in package["devDependencies"]
    assert "@vitejs/plugin-react" in package["devDependencies"]
    assert "demo-admin" in (tmp_path / "ops" / "src" / "data.ts").read_text()
    assert runner.commands == ["npm install && npm run build"]
    events = [
        json.loads(line)
        for line in ledger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    hardened = next(event for event in events if event["type"] == "freeform_vite_react_hardened")
    assert hardened["skill"] == "react-vite"
    assert hardened["hook"] == "vite-react-contract-foundation"
    mutation_log = (ledger.mutations_dir / "mutations.jsonl").read_text(encoding="utf-8")
    assert "react-vite skill deterministic" in mutation_log


def test_freeform_hardens_atlasdesk_root_scripts_and_incident_filters(tmp_path: Path):
    prd_text = Path("evals/fixtures/prds/atlasdesk_long.md").read_text(encoding="utf-8")
    project = build_project_spec(parse_prd_text(prd_text, markdown=True))

    def generate(system: str, user: str, schema: dict) -> str:
        if "planning a small project" in system:
            return json.dumps(
                {
                    "stack": "TypeScript React Vite",
                    "files": [
                        {"path": "package.json", "purpose": "manifest"},
                        {"path": "src/App.tsx", "purpose": "broken app shell"},
                    ],
                }
            )
        if "package.json" in user.split("## File to write now", 1)[-1][:80]:
            return json.dumps({"content": json.dumps({"scripts": {"build": "vite build"}})})
        return json.dumps({"content": "export default function App() { return null }\n"})

    target = tmp_path / "atlasdesk-long-build"
    result = FreeformGenerator(tmp_path, generate, command_runner=FakeRunner(0)).run(
        project,
        target,
    )

    package = json.loads((target / "package.json").read_text(encoding="utf-8"))
    assert result.success is True
    assert package["scripts"]["test"] == "vitest run src/app.test.ts"
    assert package["scripts"]["seed"] == "node scripts/seed.mjs"
    assert package["scripts"]["status"] == "node scripts/status.mjs"
    assert (target / "scripts" / "seed.mjs").is_file()
    assert (target / "scripts" / "status.mjs").is_file()
    assert "filterIncidents" in (target / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "computeStatusCounts" in (target / "src" / "app.test.ts").read_text(encoding="utf-8")

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    def run_node(script: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [node, script],
            cwd=target,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )

    seed = run_node("scripts/seed.mjs")
    assert seed.returncode == 0
    assert seed.stdout.strip() == "seeded 6 records"
    status = run_node("scripts/status.mjs")
    assert status.returncode == 0
    assert status.stdout.strip() == "open 3 high 2 overdue 1"


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
