"""DOCX PRDs and SQL DDL data models.

Both exist because a real PRD (OpenBazaar, a Word document whose entire data
model is a PostgreSQL schema dump) extracted to one section, zero entities and
forty junk "features" - a document that had specified *more* than most produced
*less*.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from shamsu.prd.contract import extract_contract
from shamsu.prd.docx import DocxParseError, extract_docx_document
from shamsu.prd.extractor import extract_entities
from shamsu.prd.headings import resolve_headings
from shamsu.prd.input import PRDParseError, parse_prd_file
from shamsu.prd.parser import parse_prd_text
from shamsu.prd.sql_schema import entities_from_sql

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _paragraph(text: str, *, bold: bool = False, style: str = "", numbered: bool = False) -> str:
    properties = ""
    if style or numbered:
        inner = f'<w:pStyle w:val="{style}"/>' if style else ""
        inner += "<w:numPr><w:ilvl w:val=\"0\"/><w:numId w:val=\"1\"/></w:numPr>" if numbered else ""
        properties = f"<w:pPr>{inner}</w:pPr>"
    run_properties = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f"<w:p>{properties}<w:r>{run_properties}<w:t>{text}</w:t></w:r></w:p>"


def _table(rows: list[list[str]]) -> str:
    body = ""
    for row in rows:
        cells = "".join(
            f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row
        )
        body += f"<w:tr>{cells}</w:tr>"
    return f"<w:tbl>{body}</w:tbl>"


def _write_docx(path: Path, blocks: str, *, title: str = "") -> Path:
    document = f'<?xml version="1.0"?><w:document {_W}><w:body>{blocks}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)
        if title:
            archive.writestr(
                "docProps/core.xml",
                '<?xml version="1.0"?><cp:coreProperties '
                'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                f'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title>'
                "</cp:coreProperties>",
            )
    return path


# ── DOCX structure ────────────────────────────────────────────────────────


def test_bold_paragraphs_become_headings(tmp_path: Path):
    """The shape every real PRD uses: titles are bold body text, not styles."""
    path = _write_docx(
        tmp_path / "prd.docx",
        _paragraph("Marketplace PRD", bold=True)
        + _paragraph("1. Overview", bold=True)
        + _paragraph("A marketplace for second-hand goods.")
        + _paragraph("2. Data Model", bold=True)
        + _paragraph("Item: title, price"),
    )

    parsed = parse_prd_file(path)

    assert parsed.source_kind == "docx"
    assert parsed.title == "Marketplace PRD"
    assert "1. Overview" in parsed.sections
    assert "2. Data Model" in parsed.sections
    assert parsed.sections["1. Overview"] == ["A marketplace for second-hand goods."]


def test_numbering_sets_heading_depth(tmp_path: Path):
    path = _write_docx(
        tmp_path / "prd.docx",
        _paragraph("Doc", bold=True)
        + _paragraph("5. Technical Architecture", bold=True)
        + _paragraph("5.2 Database Schema", bold=True)
        + _paragraph("Tables live here."),
    )

    document = extract_docx_document(path)

    assert "## 5. Technical Architecture" in document.text
    assert "### 5.2 Database Schema" in document.text


def test_word_heading_styles_still_win(tmp_path: Path):
    path = _write_docx(
        tmp_path / "prd.docx",
        _paragraph("Doc", bold=True) + _paragraph("Features", style="Heading1"),
    )

    assert "## Features" in extract_docx_document(path).text


def test_long_bold_prose_is_not_a_heading(tmp_path: Path):
    sentence = (
        "This paragraph is emphasised for the reader but it is ordinary prose "
        "and it certainly does not name a section of the document."
    )
    path = _write_docx(
        tmp_path / "prd.docx", _paragraph("Doc", bold=True) + _paragraph(sentence, bold=True)
    )

    document = extract_docx_document(path)

    assert sentence in document.text
    assert f"## {sentence}" not in document.text


def test_bold_list_items_stay_list_items(tmp_path: Path):
    path = _write_docx(
        tmp_path / "prd.docx",
        _paragraph("Doc", bold=True) + _paragraph("Fast search", bold=True, numbered=True),
    )

    assert "- Fast search" in extract_docx_document(path).text


def test_tables_are_rendered_and_published(tmp_path: Path):
    path = _write_docx(
        tmp_path / "prd.docx",
        _paragraph("Doc", bold=True)
        + _table([["Role", "May bid"], ["Guest", "No"], ["Buyer", "Yes"]]),
    )

    parsed = parse_prd_file(path)

    assert parsed.tables[0]["rows"][0] == ["Role", "May bid"]
    assert "| Guest | No |" in parsed.raw_text


def test_ddl_and_diagrams_are_fenced_out_of_requirements(tmp_path: Path):
    ddl = "CREATE TABLE users ( user_id UUID PRIMARY KEY, email VARCHAR(255) NOT NULL );"
    path = _write_docx(
        tmp_path / "prd.docx",
        _paragraph("Doc", bold=True)
        + _paragraph("3. Features", bold=True)
        + _paragraph("Buyers can search listings.")
        + _paragraph("4. Schema", bold=True)
        + _paragraph(ddl),
    )

    parsed = parse_prd_file(path)

    # The schema is still in raw_text, where entity extraction reads it...
    assert "CREATE TABLE users" in parsed.raw_text
    # ...but it is not a requirement anybody should try to implement.
    assert all("CREATE TABLE" not in line for lines in parsed.sections.values() for line in lines)


def test_core_properties_title_is_preferred(tmp_path: Path):
    path = _write_docx(
        tmp_path / "prd.docx",
        _paragraph("Product Requirements Document (PRD)", bold=True) + _paragraph("Body."),
        title="OpenBazaar",
    )

    assert parse_prd_file(path).title == "OpenBazaar"


def test_generic_cover_label_is_not_taken_as_the_title(tmp_path: Path):
    path = _write_docx(
        tmp_path / "prd.docx",
        _paragraph("Product Requirement Document (PRD)", bold=True)
        + _paragraph("OpenBazaar: A Marketplace", bold=True)
        + _paragraph("Body."),
    )

    assert parse_prd_file(path).title == "OpenBazaar: A Marketplace"


def test_a_non_docx_file_reports_a_usable_error(tmp_path: Path):
    path = tmp_path / "prd.docx"
    path.write_text("this is not a zip", encoding="utf-8")

    with pytest.raises(PRDParseError):
        parse_prd_file(path)


def test_a_zip_without_a_document_part_is_rejected(tmp_path: Path):
    path = tmp_path / "prd.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("hello.txt", "nothing here")

    with pytest.raises(DocxParseError):
        extract_docx_document(path)


def test_read_file_opens_a_docx(tmp_path: Path):
    from shamsu.tools.workspace import WorkspaceTool

    _write_docx(
        tmp_path / "prd.docx",
        _paragraph("Doc", bold=True) + _paragraph("Buyers can search listings."),
    )

    assert "Buyers can search listings." in WorkspaceTool(tmp_path).read_file("prd.docx")


# ── entry points a real run actually touches ──────────────────────────────
#
# Supporting a format in the PRD parser is not the same as supporting it in the
# harness. Several gates were keyed to a literal ".pdf", so the model could be
# pointed at a PRD its own `read_file` then refused as "not a supported text
# file" - the failure that derailed the 2026-08-01 dogfood for PDFs.


def test_the_agents_own_read_file_tool_opens_a_docx(tmp_path: Path):
    from shamsu.tools.agent_tools import AgentToolRegistry

    _write_docx(
        tmp_path / "prd.docx",
        _paragraph("Doc", bold=True) + _paragraph("Buyers can search listings."),
    )

    result = AgentToolRegistry(tmp_path, approval_func=lambda _r: True).read_file("prd.docx")

    assert result.ok is True
    assert "Buyers can search listings." in str(result.data.get("content", ""))


def test_an_atmentioned_docx_resolves_with_its_text(tmp_path: Path):
    from shamsu.tools.workspace import MentionResolver

    _write_docx(
        tmp_path / "prd.docx",
        _paragraph("Doc", bold=True) + _paragraph("Buyers can search listings."),
    )

    context = MentionResolver(tmp_path).resolve("@prd.docx")

    assert context.kind == "file"
    assert "Buyers can search listings." in context.content


def test_a_docx_named_in_a_build_request_is_the_prd_whatever_it_is_called(tmp_path: Path):
    """A document SHAMSU reads is never code it writes, so an explicitly named
    one is the requirements document even when its name says nothing."""
    from shamsu.cli.repl import _resolved_prd_reference

    _write_docx(tmp_path / "unnamed spec.docx", _paragraph("Doc", bold=True) + _paragraph("Body."))

    found = _resolved_prd_reference("build the app from @'unnamed spec.docx'", tmp_path)

    assert found is not None and Path(found).name == "unnamed spec.docx"


def test_ingest_docs_reads_a_docx_instead_of_decoding_it_as_text(tmp_path: Path):
    """`.docx` is a zip: `read_text` on one raises UnicodeDecodeError."""
    from shamsu.retriever.documents import DOCUMENT_SOURCE_SUFFIXES
    from shamsu.tools.agent_tools import AgentToolRegistry

    assert ".docx" in DOCUMENT_SOURCE_SUFFIXES

    _write_docx(
        tmp_path / "spec.docx",
        _paragraph("Doc", bold=True) + _paragraph("Buyers can search listings."),
    )

    result = AgentToolRegistry(tmp_path, approval_func=lambda _r: True).ingest_docs(
        "spec.docx", name="spec"
    )

    assert "UnicodeDecodeError" not in result.message
    assert "codec can't decode" not in result.message


def test_a_docx_is_not_indexed_as_source_code(tmp_path: Path):
    """A .docx is a zip archive; the code index excluded .pdf but not this."""
    from shamsu.indexer.policy import is_indexable_file

    _write_docx(tmp_path / "prd.docx", _paragraph("Doc", bold=True))
    (tmp_path / "prd.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "app.py").write_text("x = 1", encoding="utf-8")

    assert is_indexable_file(tmp_path / "prd.docx", tmp_path) is False
    assert is_indexable_file(tmp_path / "prd.pdf", tmp_path) is False
    assert is_indexable_file(tmp_path / "app.py", tmp_path) is True


# ── SQL DDL → entities ────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE users (
    user_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) UNIQUE NOT NULL,
    cod_reliability_score DECIMAL(5,2) DEFAULT 100.00,
    is_verified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE items (
    item_id UUID PRIMARY KEY,
    seller_id UUID NOT NULL REFERENCES users(user_id) ON DELETE RESTRICT,
    description TEXT NOT NULL,
    sale_type VARCHAR(20) NOT NULL CHECK (sale_type IN ('FIXED', 'AUCTION', 'HYBRID')),
    reserve_price DECIMAL(12,2),
    PRIMARY KEY (item_id)
);
"""


def test_columns_become_fields_with_their_declared_precision():
    entities = {entity.name: entity for entity in entities_from_sql(SCHEMA)}

    user = {field.name: field for field in entities["User"].fields}
    assert user["email"].django_type == "CharField"
    assert user["email"].kwargs["max_length"] == 255
    assert user["email"].kwargs["unique"] is True
    assert user["cod_reliability_score"].kwargs == {
        "max_digits": 5,
        "decimal_places": 2,
        "null": True,
        "blank": True,
        "default": 100.0,
    }
    assert user["is_verified"].django_type == "BooleanField"


def test_primary_keys_and_bookkeeping_columns_are_dropped():
    user = next(entity for entity in entities_from_sql(SCHEMA) if entity.name == "User")
    names = {field.name for field in user.fields}

    assert "user_id" not in names
    assert "created_at" not in names


def test_references_become_foreign_keys_with_on_delete():
    item = next(entity for entity in entities_from_sql(SCHEMA) if entity.name == "Item")
    seller = next(field for field in item.fields if field.name == "seller")

    assert seller.django_type == "ForeignKey"
    assert seller.kwargs["to"] == "User"
    assert seller.kwargs["on_delete"] == "PROTECT"
    assert "belongs_to:User" in item.relationships


def test_check_in_constraints_become_choices():
    item = next(entity for entity in entities_from_sql(SCHEMA) if entity.name == "Item")
    sale_type = next(field for field in item.fields if field.name == "sale_type")

    assert [value for value, _ in sale_type.kwargs["choices"]] == ["FIXED", "AUCTION", "HYBRID"]


def test_table_level_constraints_are_not_columns():
    item = next(entity for entity in entities_from_sql(SCHEMA) if entity.name == "Item")

    assert all(not field.name.lower().startswith("primary") for field in item.fields)


def test_a_schema_flattened_onto_one_line_still_parses():
    """Word and PDF both destroy the newlines in a pasted schema."""
    flattened = " ".join(SCHEMA.split())

    assert {entity.name for entity in entities_from_sql(flattened)} == {"User", "Item"}


def test_prose_entity_definitions_outrank_the_ddl():
    prd = parse_prd_text(
        "## Data Model\n\n"
        "### User\n\nFields\n- nickname: string, required\n\n"
        "## Appendix\n\n"
        "```sql\nCREATE TABLE users (email VARCHAR(50) NOT NULL);\n```\n",
        markdown=True,
    )

    user = next(entity for entity in extract_entities(prd) if entity.name == "User")

    assert [field.name for field in user.fields] == ["nickname"]


def test_a_prd_whose_only_data_model_is_ddl_still_yields_entities():
    prd = parse_prd_text(f"## Schema\n\n```sql\n{SCHEMA}\n```\n", markdown=True)

    assert {entity.name for entity in extract_entities(prd)} == {"User", "Item"}


def test_text_without_ddl_is_unaffected():
    assert entities_from_sql("Create a table of contents for the document.") == []


# ── heading resolution ────────────────────────────────────────────────────


def test_padding_words_do_not_block_a_heading_match():
    resolution = resolve_headings(["3. Comprehensive Feature Specifications"])

    assert resolution.aliases["3. Comprehensive Feature Specifications"] == "features"


def test_a_genuinely_different_subject_is_still_rejected():
    heading = "Detailed Notes On Feature Flag Rollout Sequencing"

    assert heading in resolve_headings([heading]).unresolved


def test_subsections_inherit_their_parent_section():
    headings = [
        "3. Comprehensive Feature Specifications",
        "3.1 Item Listing Engine",
        "3.2 Auction Engine",
        "9. Appendix",
        "9.1 Glossary",
    ]

    resolution = resolve_headings(headings)

    assert resolution.aliases["3.1 Item Listing Engine"] == "features"
    assert resolution.aliases["3.2 Auction Engine"] == "features"
    # An unresolved parent hands nothing down.
    assert "9.1 Glossary" in resolution.unresolved


def test_a_subsection_that_resolves_itself_keeps_its_own_role():
    heading = "5.2 Database Schema (PostgreSQL DDL)"

    resolution = resolve_headings(["5. Technical Architecture & Database Design", heading])

    # It matches `database schema` by prefix, so it needs no alias - and the
    # inheritance pass must not hand it its parent's role instead.
    assert heading in resolution.already_canonical
    assert heading not in resolution.aliases


def test_children_of_an_already_canonical_parent_inherit_its_canonical_term():
    resolution = resolve_headings(
        ["4. Data Model For The Platform", "4.1 Ledger Records", "4.2 Audit Records"]
    )

    assert resolution.aliases["4.1 Ledger Records"] == "data model"
    assert resolution.aliases["4.2 Audit Records"] == "data model"


# ── the whole pipeline, on the document that motivated all of it ──────────


def test_a_word_prd_with_a_ddl_data_model_compiles_to_a_real_contract(tmp_path: Path):
    path = _write_docx(
        tmp_path / "OpenBazaar_PRD.docx",
        _paragraph("Product Requirement Document (PRD)", bold=True)
        + _paragraph("OpenBazaar Marketplace", bold=True)
        + _paragraph("3. Comprehensive Feature Specifications", bold=True)
        + _paragraph("3.1 Auction Engine", bold=True)
        + _paragraph("Buyers place bids above the current highest bid.", numbered=True)
        + _paragraph("Auctions extend by 3 minutes on a late bid.", numbered=True)
        + _paragraph("5.2 Database Schema (PostgreSQL DDL)", bold=True)
        + _paragraph(" ".join(SCHEMA.split())),
    )

    contract = extract_contract(parse_prd_file(path), request_text="build this as a Django project")

    assert {entity["name"] for entity in contract.entities} == {"User", "Item"}
    assert any("bids above the current highest bid" in feature for feature in contract.features)
    assert all("CREATE TABLE" not in feature for feature in contract.features)
