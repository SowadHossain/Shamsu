from __future__ import annotations

import json
from pathlib import Path

from shamsu.verify.gate import build_verification_plan, verify_and_repair
from shamsu.verify.wiring import WIRING_COMMAND, verify_wiring


class _Runner:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def run(self, command: str, cwd: Path) -> tuple[int, str, str]:
        self.commands.append(command)
        return 0, "", ""


def _write_full_stack_project(root: Path, frontend_route: str) -> list[str]:
    files = {
        "package.json": json.dumps({"scripts": {"build": "vite build"}}),
        "src/api.ts": (
            "export async function loadIncidents() {\n"
            f"  return fetch('{frontend_route}').then((response) => response.json())\n"
            "}\n"
        ),
        "server.ts": (
            "import express from 'express'\n"
            "const app = express()\n"
            "app.get('/api/incidents', (_request, response) => response.json([]))\n"
        ),
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return list(files)


def test_wiring_verifier_flags_frontend_route_missing_from_backend(tmp_path: Path):
    _write_full_stack_project(tmp_path, "/api/incidentz")

    result = verify_wiring(tmp_path)

    assert result.ok is False
    assert result.frontend_calls == 1
    assert result.backend_routes == 1
    assert result.diagnostics[0].file == "src/api.ts"
    assert result.diagnostics[0].line == 2
    assert result.diagnostics[0].kind == "frontend_backend_route"
    assert "/api/incidentz" in result.stderr()


def test_wiring_verifier_accepts_matching_dynamic_routes(tmp_path: Path):
    files = {
        "src/api.ts": "export const load = (id: string) => fetch(`/api/incidents/${id}`)\n",
        "server.ts": "app.get('/api/incidents/:id', handler)\n",
    }
    for relative, content in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    result = verify_wiring(tmp_path)

    assert result.ok is True
    assert result.frontend_calls == 1
    assert result.backend_routes == 1


def test_wiring_verifier_flags_query_table_missing_from_schema(tmp_path: Path):
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    (tmp_path / "server.py").write_text(
        'rows = db.execute("SELECT * FROM audit_logs").fetchall()\n',
        encoding="utf-8",
    )

    result = verify_wiring(tmp_path)

    assert result.ok is False
    assert result.schema_tables == 1
    assert result.query_tables == 1
    assert result.diagnostics[0].kind == "backend_schema_table"
    assert result.diagnostics[0].file == "server.py"
    assert "audit_logs" in result.diagnostics[0].message


def test_wiring_verifier_ignores_imports_and_sql_words_in_prose(tmp_path: Path):
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE users (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )
    (tmp_path / "service.py").write_text(
        '"""Select skills from the catalog and update settings when needed."""\n'
        "from pathlib import Path\n",
        encoding="utf-8",
    )

    result = verify_wiring(tmp_path)

    assert result.ok is True
    assert result.query_tables == 0


def test_verification_plan_adds_required_wiring_stage(tmp_path: Path):
    changed = _write_full_stack_project(tmp_path, "/api/incidents")

    plan = build_verification_plan(
        tmp_path,
        changed,
        stack="React Vite Express",
        lightweight=False,
    )

    assert plan.steps[0].stage == "wiring"
    assert plan.steps[0].command == WIRING_COMMAND
    assert plan.steps[0].required is True


def test_wiring_failure_enters_existing_repair_loop_and_is_reverified(tmp_path: Path):
    changed = _write_full_stack_project(tmp_path, "/api/incidentz")
    runner = _Runner()
    model_calls: list[str] = []

    def generate(system: str, user: str, schema: dict) -> str:
        model_calls.append(system)
        assert "src/api.ts" in user
        assert "/api/incidentz" in user
        return json.dumps(
            {
                "root_cause": "The frontend route is misspelled.",
                "target_file": "src/api.ts",
                "search": "/api/incidentz",
                "replace": "/api/incidents",
                "full_content": "",
                "inspected_files": ["src/api.ts"],
            }
        )

    outcome = verify_and_repair(
        tmp_path,
        changed,
        generate=generate,
        command_runner=runner,
        max_attempts=1,
        stack="React Vite Express",
    )

    assert outcome.verified is True
    assert outcome.repair_result is not None
    assert outcome.repair_result.success is True
    assert "/api/incidents" in (tmp_path / "src" / "api.ts").read_text(encoding="utf-8")
    assert model_calls
    assert runner.commands == ["npm install", "npm run build"]
