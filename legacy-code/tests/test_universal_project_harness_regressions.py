"""Cross-project regressions captured from the failed Canvas Lite build.

These are intentionally product-neutral. Each expected failure names a harness
contract that must hold for web apps, APIs, CLIs, libraries, and other project
types. Remove the xfail marker as the corresponding implementation lands.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from shamsu.agents.chat_state import ChatState
from shamsu.cli import repl
from shamsu.prd import input as prd_input
from shamsu.routing.operations import OperationStep
from shamsu.types import ParsedPRD


def test_explicit_backtick_document_path_with_spaces_resolves(tmp_path: Path):
    document = tmp_path / "Product Brief.pdf"
    document.write_bytes(b"%PDF-1.4 routing fixture")
    prompt = "Build the application described in `Product Brief.pdf`."

    assert repl._extract_prd_path_from_prompt(prompt) == "Product Brief.pdf"
    assert repl._resolve_build_prd(prompt, tmp_path) == document


def test_image_only_document_uses_ocr_fallback(monkeypatch, tmp_path: Path):
    class _Page:
        def extract_text(self):
            return ""

        def extract_tables(self):
            return []

    class _Pdf:
        pages = [_Page()]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    source = tmp_path / "Scanned Specification.pdf"
    source.write_bytes(b"%PDF-1.4 image-only fixture")
    monkeypatch.setattr(prd_input.pdfplumber, "open", lambda _path: _Pdf())
    monkeypatch.setattr(
        prd_input,
        "_ocr_pdf_pages",
        lambda _path, _pages: ["1 Product Overview\nBuild a role-aware application."],
        raising=False,
    )

    parsed = prd_input.parse_prd_file(source)

    assert "role-aware application" in parsed.raw_text
    assert any("OCR" in warning for warning in parsed.extraction_warnings)


def test_low_confidence_document_blocks_project_mutation():
    parsed = ParsedPRD(
        title="Unclear brief",
        sections={},
        raw_text="Unreadable fragments",
        extraction_confidence=0.24,
        extraction_warnings=["Most pages were unreadable."],
    )

    issue = repl._prd_grounding_issue(parsed)

    assert "24%" in issue
    assert "before I modify the project" in issue


def test_long_structured_project_build_bypasses_generic_composite_parser(tmp_path: Path):
    (tmp_path / "Product Brief.pdf").write_bytes(b"%PDF-1.4 routing fixture")
    prompt = """Build the complete application from `Product Brief.pdf`.

Requirements:
- Create a backend API and browser frontend.
- Implement authentication and role permissions.
- Run migrations, seed data, tests, and a production build.
- Do not claim completion until acceptance checks pass.
"""

    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.is_composite is False
    assert len(plan.steps) == 1
    assert plan.primary_route == "prd.build"
    assert plan.steps[0].instruction == prompt.strip()


def test_single_target_creation_keeps_all_content_requirements_atomic(tmp_path: Path):
    prompt = (
        "Create exactly one file: `src/schema.py`. "
        "Use the write tool immediately. "
        "Implement the complete domain model in this one file with validation and timestamps."
    )

    plan = repl._operation_plan(prompt, tmp_path)

    assert plan.is_composite is False
    assert len(plan.steps) == 1
    assert plan.primary_route == "file.write"
    assert plan.steps[0].instruction == prompt.rstrip(".")


def test_context_budget_counts_system_prompt_and_selected_history():
    state = ChatState("S" * 40, hydrate=False)
    state.append_user("O" * 20)
    state.append_user("U" * 20)

    tail, start = state.select_for_budget(65, len)
    messages = state.build_ollama_messages(tail, include_summary=start > 1)

    assert sum(len(str(message.get("content", ""))) for message in messages) <= 65


def test_failed_verification_overrides_successful_mutation_evidence():
    step = OperationStep(
        id=1,
        kind="mutation",
        route="file.write",
        instruction="Create src/schema.py",
    )
    result = SimpleNamespace(
        changed_files=("src/schema.py",),
        stopped=False,
        awaiting_user=False,
        final="Created the file.",
    )

    status, evidence = repl._composite_step_outcome(
        step,
        result,
        ["write_file"],
        {"mutation_finished", "verification_failed"},
    )

    assert status == "failed"
    assert "event:verification_failed" in evidence
