from __future__ import annotations

from pathlib import Path

from shamsu.cli import repl
from shamsu.tools.agent_tools import AgentToolRegistry


def test_agent_read_file_tool_extracts_pdf_text(tmp_path: Path):
    # The model's read_file tool used to hard-reject PDFs ("Not a supported
    # text file"), leaving the agent blind to a PDF PRD. It should extract text
    # the same way @mention resolution already does for PDFs.
    from shamsu.prd.input import parse_prd_file

    pdf_path = tmp_path / "Product Requirements Document.pdf"
    _write_minimal_pdf(pdf_path, "Build a multiplayer racing game.")
    expected = parse_prd_file(pdf_path).raw_text

    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    result = tools.read_file("Product Requirements Document.pdf")

    assert result.ok is True
    assert expected.strip() in result.data["content"]


def test_frontend_repair_request_injects_code_around_tsc_error_lines(tmp_path: Path):
    game_dir = tmp_path / "src" / "game"
    game_dir.mkdir(parents=True)
    lines = [f"const line{i} = {i};" for i in range(1, 90)]
    lines[70] = "function broken(a, b {"  # line 71 (1-indexed), missing ')'
    (game_dir / "rules.ts").write_text("\n".join(lines) + "\n", encoding="utf-8")

    errors = (
        "src/game/rules.ts(71,17): error TS1005: ')' expected.\n"
        "src/game/rules.ts(71,32): error TS1005: ',' expected.\n"
    )

    request = repl._build_frontend_repair_request(errors, tmp_path, Path("."))

    assert "function broken(a, b {" in request
    assert "src/game/rules.ts around line 71" in request


def test_frontend_repair_request_omits_snippet_section_when_no_locations_found():
    request = repl._build_frontend_repair_request("some vague build failure", Path("."), Path("."))

    assert "Actual code around" not in request


def _write_minimal_pdf(path: Path, text: str) -> None:
    # Minimal valid single-page PDF containing the given text, built by hand so
    # the test has no external PDF-generation dependency.
    content_stream = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET"
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        "/MediaBox [0 0 612 792] /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content_stream)} >>\nstream\n{content_stream}\nendstream",
    ]
    offsets = [0]
    body = "%PDF-1.4\n"
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{index} 0 obj\n{obj}\nendobj\n"
    xref_offset = len(body)
    body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for offset in offsets[1:]:
        body += f"{offset:010d} 00000 n \n"
    body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF"
    path.write_bytes(body.encode("latin-1"))
