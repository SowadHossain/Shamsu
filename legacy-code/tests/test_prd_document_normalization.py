from __future__ import annotations

from shamsu.prd.document import normalize_pdf_pages
from shamsu.prd.parser import parse_prd_text


def test_pdf_normalization_removes_layout_noise_and_preserves_pages():
    document = normalize_pdf_pages(
        [
            "TaskFlow PRD\n1 Product Overview\nA local-first todo applica-\ntion for small teams.\n1",
            "TaskFlow PRD\n2 Product Goals\n2.1 Primary Goals\nKeep each user's data private.\n2",
            "TaskFlow PRD\n3 Target Users\nBusy professionals\n3",
        ]
    )

    assert "TaskFlow PRD" not in document.text
    assert "\n1\n" not in f"\n{document.text}\n"
    assert "A local-first todo application for small teams." in document.text
    assert document.line_pages[0] == 1
    assert document.confidence == 0.6
    assert document.warnings == ["Very little text was extracted for the document length."]


def test_numbered_list_items_do_not_become_sections():
    parsed = parse_prd_text(
        "\n".join(
            [
                "1 Product Overview",
                "TaskFlow is a full-stack web application.",
                "7 User Registration",
                "4 The visitor enters:",
                "5 Full name",
                "6 Email address",
                "7 Password",
                "7.1 Registration Form",
                "The form validates user input.",
                "32 Frontend Pages",
                "4 Dashboard",
            ]
        )
    )

    assert "1 Product Overview" in parsed.sections
    assert "7 User Registration" in parsed.sections
    assert "7.1 Registration Form" in parsed.sections
    assert "4 The visitor enters:" not in parsed.sections
    assert "4 Dashboard" not in parsed.sections
    assert parsed.sections["7 User Registration"] == [
        "4 The visitor enters:",
        "5 Full name",
        "6 Email address",
        "7 Password",
    ]


def test_parser_records_section_page_provenance():
    parsed = parse_prd_text(
        "1 Product Overview\nTaskFlow is a todo app.\n2 Product Goals\nKeep data private.",
        line_pages=[1, 1, 2, 2],
    )

    assert parsed.source_refs["1 Product Overview"] == [
        {"page": 1, "kind": "heading"},
        {"page": 1, "kind": "content"},
    ]
    assert parsed.source_refs["2 Product Goals"] == [
        {"page": 2, "kind": "heading"},
        {"page": 2, "kind": "content"},
    ]


def test_common_numbered_prd_headings_do_not_need_product_specific_allowlist():
    parsed = parse_prd_text(
        "\n".join(
            [
                "PRD: Orbit Desk",
                "1. Overview",
                "A collaborative workspace.",
                "2. Goals",
                "Support role-aware work.",
                "3. Non-Goals (out of scope)",
                "Real-time video.",
                "4. Users & Roles",
                "Admin and member.",
                "5. Tech Stack",
                "Framework chosen by the implementer.",
                "6. Data Model",
                "Workspace and Membership.",
            ]
        )
    )

    assert list(parsed.sections) == [
        "1 Overview",
        "2 Goals",
        "3 Non-Goals (out of scope)",
        "4 Users & Roles",
        "5 Tech Stack",
        "6 Data Model",
    ]
