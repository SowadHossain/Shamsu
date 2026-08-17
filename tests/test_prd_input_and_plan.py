from __future__ import annotations

from io import StringIO
from pathlib import Path

from rich.console import Console

from shamsu.cli.repl import _handle_parse_prd, _handle_plan_prd
from shamsu.prd.input import PRDParseError, parse_prd_file
from shamsu.prd.state import state_path
from shamsu.types import ApprovalRequest


def test_txt_prd_parses_title_sections_and_raw_text(tmp_path):
    prd = tmp_path / "TODO.txt"
    prd.write_text(
        "Todo App\n\nEntities\nTask: title (text), done (boolean)\n\nPages\nDashboard: stats",
        encoding="utf-8",
    )

    parsed = parse_prd_file(prd)

    assert parsed.title == "Todo App"
    assert "Entities" in parsed.sections
    assert "Task: title (text), done (boolean)" in parsed.sections["Entities"]
    assert "Todo App" in parsed.raw_text


def test_pdf_prd_parses_text_with_mocked_pdfplumber(monkeypatch, tmp_path):
    prd = tmp_path / "TODO.pdf"
    prd.write_bytes(b"%PDF mocked")

    class Page:
        def extract_text(self):
            return "# Todo App\n\n## Entities\n- Task: title (text)"

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("shamsu.prd.input.pdfplumber.open", lambda _path: Pdf())

    parsed = parse_prd_file(prd)

    assert parsed.title == "Todo App"
    assert parsed.sections["Entities"] == ["Task: title (text)"]


def test_empty_pdf_gets_friendly_error(monkeypatch, tmp_path):
    prd = tmp_path / "empty.pdf"
    prd.write_bytes(b"%PDF mocked")

    class Page:
        def extract_text(self):
            return ""

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("shamsu.prd.input.pdfplumber.open", lambda _path: Pdf())

    try:
        parse_prd_file(prd)
    except PRDParseError as exc:
        assert "Could not extract text" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected PRDParseError")


def test_pdf_ocr_only_processes_pages_without_usable_native_text(monkeypatch, tmp_path):
    prd = tmp_path / "mixed.pdf"
    prd.write_bytes(b"%PDF mocked")

    class Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

        def extract_tables(self):
            return []

    class Pdf:
        pages = [Page("# Native Overview\nEnough native text for this page."), Page("")]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    requested: list[int] = []

    def fake_ocr(_path, pages):
        requested.extend(pages)
        return ["## Roles\nAdministrator manages accounts."]

    monkeypatch.setattr("shamsu.prd.input.pdfplumber.open", lambda _path: Pdf())
    monkeypatch.setattr("shamsu.prd.input._ocr_pdf_pages", fake_ocr)

    parsed = parse_prd_file(prd)

    assert requested == [2]
    assert "Native Overview" in parsed.raw_text
    assert "Administrator manages accounts" in parsed.raw_text
    assert parsed.extraction_confidence <= 0.82
    assert any("OCR fallback supplied text" in item for item in parsed.extraction_warnings)


def test_pdf_prefixed_prd_title_wins_over_incidental_plain_heading(monkeypatch, tmp_path):
    prd = tmp_path / "brief.pdf"
    prd.write_bytes(b"%PDF mocked")

    class Page:
        def extract_text(self):
            return "PRD: Orbit Desk\n1. Overview\nAdmin\nBuild the workspace."

        def extract_tables(self):
            return []

    class Pdf:
        pages = [Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr("shamsu.prd.input.pdfplumber.open", lambda _path: Pdf())

    parsed = parse_prd_file(prd)

    assert parsed.title == "Orbit Desk"


def test_handle_parse_prd_accepts_txt(tmp_path):
    prd = tmp_path / "PROJECT.txt"
    prd.write_text("Project\n\nEntities\nTask: title (text)", encoding="utf-8")
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    _handle_parse_prd("parse-prd PROJECT.txt", tmp_path, console)

    assert "Title: Project" in output.getvalue()


def test_plan_prd_denied_writes_no_state(tmp_path):
    prd = tmp_path / "PROJECT.md"
    prd.write_text("# Project\n\n## Entities\n- Task: title (text)\n", encoding="utf-8")
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    def deny(_request: ApprovalRequest) -> bool:
        return False

    _handle_plan_prd("plan-prd PROJECT.md", tmp_path, console, approval_func=deny)

    assert "Project Plan" in output.getvalue()
    assert "not approved" in output.getvalue()
    assert not state_path(tmp_path).exists()


def test_plan_prd_approved_saves_generation_state(tmp_path):
    prd = tmp_path / "PROJECT.md"
    prd.write_text("# Project\n\n## Entities\n- Task: title (text)\n", encoding="utf-8")
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)
    requests: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> bool:
        requests.append(request)
        return True

    _handle_plan_prd("plan-prd PROJECT.md", tmp_path, console, approval_func=approve)

    assert requests
    assert requests[0].action_type == "file_write"
    assert "approved and saved" in output.getvalue()
    assert Path(state_path(tmp_path)).exists()


# --- the approved development plan is project state, not conversation state ---


class _CountingPlanner:
    """Stands in for the planner role; counts how often it is actually asked."""

    def __init__(self) -> None:
        self.calls = 0

    async def generate_structured(self, role, system, prompt, schema, **kwargs):  # noqa: ANN001
        import json as _json

        self.calls += 1
        return _json.dumps(
            {
                "plan_summary": "Build the todo app.",
                "stack": ["python"],
                "milestones": [
                    {"id": "M-001", "title": "Data model", "goal": "Define the Task entity."}
                ],
                "risks": [],
                "first_actions": [],
            }
        )


async def _prepare(repl, parsed, project, workspace, console, cache_path, force_replan=False):
    return await repl._prepare_prd_development_plan(
        parsed,
        Path("TODO_PRD.md"),
        project,
        ["M-001: Data model"],
        workspace,
        console,
        None,
        cache_path=cache_path,
        force_replan=force_replan,
    )


def test_an_approved_development_plan_is_reused_instead_of_replanned(tmp_path, monkeypatch):
    """"okay lets start from the beginning" must not re-run the planner.

    Only the plan's `source` string was ever persisted, so every command that
    touched the PRD paid for a fresh planning pass and could get a different
    architecture back.
    """
    import asyncio

    import shamsu.cli.repl as repl
    from shamsu.prd.input import parse_prd_file
    from shamsu.prd.project import build_project_spec

    prd = tmp_path / "TODO_PRD.md"
    prd.write_text("# Todo App\n\n## Entities\n- Task: title (text)\n", encoding="utf-8")
    parsed = parse_prd_file(prd)
    project = build_project_spec(parsed, request_text="build the todo app")

    planner = _CountingPlanner()
    monkeypatch.setattr(repl, "_make_llm_manager", lambda *a, **k: planner)
    console = Console(file=StringIO(), force_terminal=False, width=100)
    cache_path = repl._development_plan_cache_path(tmp_path, project, "todo-app")
    assert cache_path is not None

    first = asyncio.run(_prepare(repl, parsed, project, tmp_path, console, cache_path))
    second = asyncio.run(_prepare(repl, parsed, project, tmp_path, console, cache_path))

    assert first["source"] == "model"
    assert second["milestones"] == first["milestones"]
    assert planner.calls == 1, "the planner ran again for an already-approved plan"
    assert cache_path.exists()


def test_an_explicit_replan_still_regenerates(tmp_path, monkeypatch):
    import asyncio

    import shamsu.cli.repl as repl
    from shamsu.prd.input import parse_prd_file
    from shamsu.prd.project import build_project_spec

    prd = tmp_path / "TODO_PRD.md"
    prd.write_text("# Todo App\n\n## Entities\n- Task: title (text)\n", encoding="utf-8")
    parsed = parse_prd_file(prd)
    project = build_project_spec(parsed, request_text="build the todo app")

    planner = _CountingPlanner()
    monkeypatch.setattr(repl, "_make_llm_manager", lambda *a, **k: planner)
    console = Console(file=StringIO(), force_terminal=False, width=100)
    cache_path = repl._development_plan_cache_path(tmp_path, project, "todo-app")

    asyncio.run(_prepare(repl, parsed, project, tmp_path, console, cache_path))
    asyncio.run(_prepare(repl, parsed, project, tmp_path, console, cache_path, force_replan=True))

    assert planner.calls == 2


def test_replan_phrasing_is_recognised():
    import shamsu.cli.repl as repl

    for phrase in ("replan it", "re-plan the build", "give me a fresh plan", "plan it again"):
        assert repl._REPLAN_RE.search(phrase), phrase
    assert not repl._REPLAN_RE.search("build the product from the prd")
