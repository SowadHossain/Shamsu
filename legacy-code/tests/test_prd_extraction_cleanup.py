from __future__ import annotations

from shamsu.prd.contract import extract_contract
from shamsu.prd.parser import parse_prd_text


def test_markdown_table_delimiter_rows_do_not_become_requirements():
    contract = extract_contract(
        parse_prd_text(
            "# Demo\n\n"
            "## Features\n"
            "| Capability | Priority |\n"
            "| --- | --- |\n"
            "| Search tasks | Must |\n\n"
            "## Acceptance Criteria\n"
            "- Search returns matching tasks.\n",
            markdown=True,
        )
    )

    joined = "\n".join(contract.features)

    assert "---" not in joined
    assert any("Search tasks" in item for item in contract.features)


def test_box_drawing_flowcharts_do_not_become_feature_requirements():
    contract = extract_contract(
        parse_prd_text(
            "# Demo\n\n"
            "## Features\n"
            "┌──────────────┐\n"
            "│ Login Screen │\n"
            "└──────────────┘\n"
            "- Users can log in.\n\n"
            "## Acceptance Criteria\n"
            "- A user can log in.\n",
            markdown=True,
        )
    )

    assert contract.features == ["Users can log in."]


def test_fenced_sql_schema_is_extracted_without_becoming_a_persistence_requirement():
    contract = extract_contract(
        parse_prd_text(
            "# Ledger\n\n"
            "## Database Schema\n"
            "```sql\n"
            "CREATE TABLE accounts (\n"
            "  id integer primary key,\n"
            "  email varchar(255) not null\n"
            ");\n"
            "```\n\n"
            "## Acceptance Criteria\n"
            "- An account can be created.\n",
            markdown=True,
        )
    )

    assert {entity["name"] for entity in contract.entities} == {"Account"}
    assert contract.persistence_requirements == []
