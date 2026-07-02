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
