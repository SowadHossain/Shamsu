"""`@file` references, and the documents a text decoder cannot read.

Both were live failures on the same request. Asked to review
`@OpenBazaar_Marketplace_PRD.docx`, SHAMSU answered *"the repository contains a
single file named @OpenBazaar_Marketplace_PRD.docx"* — it had looked for a path
starting with `@`, and even with the right path a `.docx` decodes to kilobytes
of replacement characters that a model will summarise regardless.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from shamsu.agent.mentions import resolve
from shamsu.tools.documents import (
    ExtractionFailed,
    describe_unreadable,
    extract,
    is_extractable,
)

DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>OpenBazaar Marketplace</w:t></w:r></w:p>
    <w:p><w:r><w:t></w:t></w:r></w:p>
    <w:p><w:r><w:t>A decentralised </w:t></w:r><w:r><w:t>marketplace.</w:t></w:r></w:p>
    <w:p><w:r><w:t>Sellers list items.</w:t></w:r></w:p>
  </w:body>
</w:document>
"""


def _docx(path: Path, xml: str = DOCUMENT_XML) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return path


class TestReadingAWordDocument:
    def test_the_text_comes_out(self, tmp_path: Path) -> None:
        document = extract(_docx(tmp_path / "prd.docx"))
        assert "OpenBazaar Marketplace" in document.text
        assert "Sellers list items." in document.text

    def test_runs_are_joined_without_inserting_spaces(self, tmp_path: Path) -> None:
        """Word splits a sentence across runs whenever formatting changes."""
        document = extract(_docx(tmp_path / "prd.docx"))
        assert "A decentralised marketplace." in document.text

    def test_paragraphs_become_lines(self, tmp_path: Path) -> None:
        document = extract(_docx(tmp_path / "prd.docx"))
        assert document.text.splitlines()[0] == "OpenBazaar Marketplace"
        assert document.paragraphs == 3

    def test_the_rendering_says_what_it_is(self, tmp_path: Path) -> None:
        rendered = extract(_docx(tmp_path / "prd.docx")).render("prd.docx")
        assert "Word document" in rendered
        assert "extracted text" in rendered

    def test_a_file_that_is_not_really_a_docx_fails_honestly(self, tmp_path: Path) -> None:
        fake = tmp_path / "prd.docx"
        fake.write_text("this is not a zip", encoding="utf-8")
        with pytest.raises(ExtractionFailed, match="not a readable Word document"):
            extract(fake)

    def test_a_docx_without_document_xml_fails_honestly(self, tmp_path: Path) -> None:
        path = tmp_path / "prd.docx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/other.xml", "<a/>")
        with pytest.raises(ExtractionFailed):
            extract(path)


def _pdf(path: Path, *lines: str) -> Path:
    """A genuine minimal PDF with a Flate-compressed text stream."""
    import zlib

    drawn = " 0 -18 Td ".join(f"({line}) Tj" for line in lines)
    stream = zlib.compress(f"BT /F1 12 Tf 72 720 Td {drawn} ET".encode())
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" /Filter /FlateDecode >>stream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    start = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n"
    ).encode()

    path.write_bytes(bytes(out))
    return path


class TestReadingAPdf:
    def test_the_text_comes_out(self, tmp_path: Path) -> None:
        document = extract(_pdf(tmp_path / "spec.pdf", "Payment Gateway", "Version 2 draft"))
        assert "Payment Gateway" in document.text
        assert "Version 2 draft" in document.text

    def test_it_reports_the_page_count(self, tmp_path: Path) -> None:
        document = extract(_pdf(tmp_path / "spec.pdf", "One page"))
        assert "1 page(s)" in document.kind

    def test_a_pdf_is_extractable_not_merely_named(self) -> None:
        """It used to be listed as unreadable, which stopped being true."""
        assert is_extractable(Path("spec.pdf")) is True
        assert describe_unreadable(Path("spec.pdf")) is None

    def test_a_file_that_is_not_a_pdf_fails_honestly(self, tmp_path: Path) -> None:
        fake = tmp_path / "spec.pdf"
        fake.write_text("not a pdf at all", encoding="utf-8")
        with pytest.raises(ExtractionFailed, match="could not be read as a PDF"):
            extract(fake)

    def test_a_scanned_pdf_says_so_rather_than_returning_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty string invites the model to describe what it never read."""
        import pypdf

        class _Blank:
            pages = [object(), object()]

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

        monkeypatch.setattr(pypdf, "PdfReader", lambda *a, **k: _Blank())
        monkeypatch.setattr(
            _Blank, "pages", [type("P", (), {"extract_text": lambda self: ""})() for _ in range(2)]
        )

        with pytest.raises(ExtractionFailed, match="scanned"):
            extract(_pdf(tmp_path / "scan.pdf", "ignored"))

    def test_a_missing_extra_names_the_install_command(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The honest report is "not installed", not "PDFs cannot be read"."""
        import builtins

        real = builtins.__import__

        def blocked(name: str, *args: object, **kwargs: object) -> object:
            if name == "pypdf":
                raise ImportError("no module named pypdf")
            return real(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", blocked)
        with pytest.raises(ExtractionFailed, match=r"shamsu\[documents\]"):
            extract(_pdf(tmp_path / "spec.pdf", "text"))


SHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DRAW_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _xlsx(path: Path, rows: list[list[str]]) -> Path:
    """A workbook built the way Excel builds one: strings held once, by index."""
    shared = list(dict.fromkeys(cell for row in rows for cell in row))
    sst = "".join(f"<si><t>{value}</t></si>" for value in shared)
    body = "".join(
        "<row>" + "".join(f'<c t="s"><v>{shared.index(c)}</v></c>' for c in row) + "</row>"
        for row in rows
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{SHEET_NS}">{sst}</sst>')
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            f'<worksheet xmlns="{SHEET_NS}"><sheetData>{body}</sheetData></worksheet>',
        )
    return path


def _pptx(path: Path, slides: dict[int, list[str]]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for number, lines in slides.items():
            text = "".join(f"<a:t>{line}</a:t>" for line in lines)
            archive.writestr(
                f"ppt/slides/slide{number}.xml", f'<sld xmlns:a="{DRAW_NS}">{text}</sld>'
            )
    return path


class TestReadingASpreadsheet:
    def test_shared_strings_are_resolved(self, tmp_path: Path) -> None:
        """The whole job. `<v>3</v>` with t="s" is the fourth string, not three."""
        document = extract(_xlsx(tmp_path / "reqs.xlsx", [["Requirement", "Priority"]]))
        assert "Requirement\tPriority" in document.text

    def test_rows_stay_rows(self, tmp_path: Path) -> None:
        """A requirements matrix read column-first says something else entirely."""
        rows = [["Requirement", "Priority"], ["Login page", "High"]]
        assert extract(_xlsx(tmp_path / "r.xlsx", rows)).text.splitlines() == [
            "Requirement\tPriority",
            "Login page\tHigh",
        ]

    def test_a_workbook_with_no_sheets_fails_honestly(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.xlsx"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/workbook.xml", "<workbook/>")
        with pytest.raises(ExtractionFailed, match="no worksheets"):
            extract(path)

    def test_something_renamed_to_xlsx_fails_honestly(self, tmp_path: Path) -> None:
        path = tmp_path / "reqs.xlsx"
        path.write_text("id,name\n1,a\n", encoding="utf-8")
        with pytest.raises(ExtractionFailed, match="not a readable Excel file"):
            extract(path)


class TestReadingADeck:
    def test_slides_come_out_in_numeric_order(self, tmp_path: Path) -> None:
        """`slide10.xml` sorts before `slide2.xml` as text, which reorders a deck."""
        deck = _pptx(tmp_path / "d.pptx", {1: ["one"], 2: ["two"], 10: ["ten"], 11: ["eleven"]})
        assert [line for line in extract(deck).text.splitlines() if not line.startswith("---")] == [
            "one",
            "two",
            "ten",
            "eleven",
        ]

    def test_each_slide_is_labelled(self, tmp_path: Path) -> None:
        text = extract(_pptx(tmp_path / "d.pptx", {1: ["hello"]})).text
        assert "--- slide 1 ---" in text

    def test_an_image_only_deck_says_so(self, tmp_path: Path) -> None:
        """Better than an empty string, which invites describing an unread deck."""
        with pytest.raises(ExtractionFailed, match="images rather than text"):
            extract(_pptx(tmp_path / "d.pptx", {1: [], 2: []}))


class TestFormatsItCannotRead:
    @pytest.mark.parametrize("name", ["logo.png", "app.exe", "data.sqlite", "font.woff2"])
    def test_binary_formats_are_recognised(self, name: str) -> None:
        assert describe_unreadable(Path(name)) is not None

    @pytest.mark.parametrize("name", ["old.doc", "sheet.xls"])
    def test_the_pre_2007_binaries_are_still_out_of_reach(self, name: str) -> None:
        """Their `x` successors are zips of XML; these are neither."""
        assert describe_unreadable(Path(name)) is not None
        assert is_extractable(Path(name)) is False

    @pytest.mark.parametrize("name", ["prd.docx", "reqs.xlsx", "deck.pptx", "spec.pdf"])
    def test_the_document_formats_are_claimed(self, name: str) -> None:
        assert is_extractable(Path(name)) is True
        assert describe_unreadable(Path(name)) is None

    @pytest.mark.parametrize(
        "name",
        ["calc.py", "main.rs", "app.go", "Main.java", "q.sql", "Dockerfile", "Makefile", "x.zig"],
    )
    def test_source_in_any_language_is_left_to_the_text_decoder(self, name: str) -> None:
        """There is no language allowlist — that is the point of this being small."""
        assert describe_unreadable(Path(name)) is None
        assert is_extractable(Path(name)) is False


class TestResolvingMentions:
    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "auth.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "OpenBazaar_PRD.docx").write_bytes(b"PK\x03\x04")
        return tmp_path

    def test_the_marker_is_removed(self, workspace: Path) -> None:
        """The live bug: the agent searched for a file named `@…`."""
        found = resolve("review @OpenBazaar_PRD.docx please", workspace)
        assert "@" not in found.text
        assert "OpenBazaar_PRD.docx" in found.text

    def test_a_bare_filename_resolves_to_its_path(self, workspace: Path) -> None:
        found = resolve("what does @auth.py do", workspace)
        assert found.resolved == ("src/auth.py",)
        assert "src/auth.py" in found.text

    def test_resolved_files_are_named_for_the_agent(self, workspace: Path) -> None:
        found = resolve("summarise @auth.py", workspace)
        assert "referred to these files: src/auth.py" in found.text

    def test_trailing_punctuation_is_not_part_of_the_name(self, workspace: Path) -> None:
        found = resolve("look at @auth.py, then stop", workspace)
        assert found.resolved == ("src/auth.py",)
        assert "src/auth.py, then stop" in found.text

    def test_an_unknown_reference_is_reported_not_dropped(self, workspace: Path) -> None:
        """Deleting the only concrete noun changes what was asked."""
        found = resolve("read @nope.md", workspace)
        assert found.unresolved == ("nope.md",)
        assert "nope.md" in found.text
        assert not found.resolved

    def test_an_ambiguous_name_resolves_to_nothing(self, tmp_path: Path) -> None:
        """Picking one of three `__init__.py` would be wrong twice."""
        for package in ("a", "b"):
            (tmp_path / package).mkdir()
            (tmp_path / package / "__init__.py").write_text("", encoding="utf-8")

        found = resolve("open @__init__.py", tmp_path)
        assert not found.resolved
        assert found.unresolved == ("__init__.py",)

    def test_an_email_address_is_not_a_mention(self, workspace: Path) -> None:
        found = resolve("mail someone@example.com about it", workspace)
        assert found.text == "mail someone@example.com about it"
        assert not found.resolved and not found.unresolved

    def test_a_request_without_mentions_is_untouched(self, workspace: Path) -> None:
        assert resolve("fix the login bug", workspace).text == "fix the login bug"

    def test_several_mentions_all_resolve(self, workspace: Path) -> None:
        found = resolve("compare @auth.py and @OpenBazaar_PRD.docx", workspace)
        assert set(found.resolved) == {"src/auth.py", "OpenBazaar_PRD.docx"}


def _para(text: str, *, style: str = "", bold: bool = False, numbered: bool = False) -> str:
    parts = []
    if style:
        parts.append(f'<w:pStyle w:val="{style}"/>')
    if numbered:
        parts.append('<w:numPr><w:ilvl w:val="0"/></w:numPr>')
    properties = f"<w:pPr>{''.join(parts)}</w:pPr>" if parts else ""
    run = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f"<w:p>{properties}<w:r>{run}<w:t>{text}</w:t></w:r></w:p>"


def _worddoc(path: Path, *paragraphs: str) -> Path:
    ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(paragraphs)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f'<?xml version="1.0"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>',
        )
    return path


class TestAWordDocumentBecomesMarkdown:
    """Structure is the most valuable thing in a specification.

    Extraction used to emit bare text, so a `.docx` PRD arrived as an
    undifferentiated wall of lines — "Features" indistinguishable from the
    feature beneath it. The same document saved as `.md` planned a real project
    precisely because `## Features` was visible as a heading.
    """

    def test_a_styled_heading_keeps_its_level(self, tmp_path: Path) -> None:
        document = _worddoc(
            tmp_path / "prd.docx",
            _para("Overview", style="Heading1"),
            _para("Details", style="Heading2"),
        )
        assert extract(document).text.splitlines() == ["# Overview", "## Details"]

    def test_a_title_outranks_a_subtitle(self, tmp_path: Path) -> None:
        document = _worddoc(
            tmp_path / "prd.docx", _para("PRD", style="Title"), _para("v2", style="Subtitle")
        )
        assert extract(document).text.splitlines() == ["# PRD", "## v2"]

    def test_a_bold_line_is_treated_as_a_heading(self, tmp_path: Path) -> None:
        """The case that matters most: authors bold a line far more often than
        they apply a heading style, and v1's extractor missed exactly this."""
        document = _worddoc(tmp_path / "prd.docx", _para("Features", bold=True))
        assert extract(document).text == "## Features"

    def test_a_long_bold_sentence_is_not_a_heading(self, tmp_path: Path) -> None:
        """Bold prose is emphasis; promoting it would invent structure."""
        sentence = "This whole sentence is bold for emphasis " * 4
        document = _worddoc(tmp_path / "prd.docx", _para(sentence.strip(), bold=True))
        assert not extract(document).text.startswith("#")

    def test_list_items_become_bullets(self, tmp_path: Path) -> None:
        document = _worddoc(
            tmp_path / "prd.docx",
            _para("Add a bookmark", numbered=True),
            _para("List all bookmarks", numbered=True),
        )
        assert extract(document).text.splitlines() == ["- Add a bookmark", "- List all bookmarks"]

    def test_ordinary_paragraphs_are_left_alone(self, tmp_path: Path) -> None:
        document = _worddoc(tmp_path / "prd.docx", _para("A command-line bookmark manager."))
        assert extract(document).text == "A command-line bookmark manager."

    def test_the_rendering_says_it_converted(self, tmp_path: Path) -> None:
        """The model should know it is reading markdown, not a Word file."""
        document = _worddoc(tmp_path / "prd.docx", _para("Overview", style="Heading1"))
        assert "markdown" in extract(document).render("prd.docx")

    def test_a_heading_deeper_than_six_is_clamped(self, tmp_path: Path) -> None:
        """Markdown has no `#######`."""
        document = _worddoc(tmp_path / "prd.docx", _para("Deep", style="Heading9"))
        assert extract(document).text == "###### Deep"
