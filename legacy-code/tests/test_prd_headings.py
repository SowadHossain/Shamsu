"""Heading resolution: a PRD's own section names folded onto extractor terms."""
from __future__ import annotations

import pytest

from shamsu.prd.contract import extract_contract
from shamsu.prd.headings import (
    MODEL_ROLES,
    _parse_model_mappings,
    apply_heading_aliases,
    normalize_heading,
    resolve_headings,
)
from shamsu.types import ParsedPRD


def _parsed(sections: dict[str, list[str]]) -> ParsedPRD:
    raw = "\n".join(
        f"{heading}\n" + "\n".join(lines) for heading, lines in sections.items()
    )
    return ParsedPRD(title="Test PRD", sections=sections, raw_text=raw)


class TestDeterministicResolution:
    def test_reworded_heading_maps_to_canonical_term(self):
        resolution = resolve_headings(["4. User Types and Roles"])
        assert resolution.aliases["4. User Types and Roles"] == "users and roles"

    def test_headings_that_already_match_are_left_alone(self):
        resolution = resolve_headings(["Acceptance Criteria", "6 Data Model"])
        assert resolution.aliases == {}
        assert len(resolution.already_canonical) == 2

    def test_synonym_terms_are_not_treated_as_an_ambiguous_tie(self):
        # "users and roles" and "user roles" reduce to the same words and both
        # match here; that is one concept, not a contested match, so the
        # heading must still resolve rather than being deferred to the model.
        resolution = resolve_headings(["Types of User Roles"])
        assert resolution.aliases["Types of User Roles"] in {
            "users and roles",
            "user roles",
        }

    def test_generic_word_does_not_claim_an_unrelated_heading(self):
        # "controls" is game input; financial controls are not.
        resolution = resolve_headings(["18.3 Financial Controls"])
        assert "18.3 Financial Controls" not in resolution.aliases

    def test_one_shared_word_is_not_enough_for_a_long_heading(self):
        resolution = resolve_headings(["Detailed Notes On Feature Flag Rollout Order"])
        assert resolution.aliases == {}

    def test_more_specific_term_wins(self):
        # {out, scope} outranks {feature} on the same heading.
        resolution = resolve_headings(["Launch Scope Left Out"])
        assert resolution.aliases["Launch Scope Left Out"] == "out of scope"

    def test_unplaceable_heading_is_reported_not_guessed(self):
        resolution = resolve_headings(["12. Circulation Management"])
        assert resolution.unresolved == ["12. Circulation Management"]


class TestNormalization:
    @pytest.mark.parametrize(
        "heading,expected",
        [
            ("4. User Types and Roles", "user types and roles"),
            ("6 Data Model", "data model"),
            ("Non-Functional Requirements", "non functional requirements"),
            ("Users & Roles:", "users and roles"),
        ],
    )
    def test_normalize(self, heading, expected):
        assert normalize_heading(heading) == expected


class TestApplyAliases:
    def test_sections_are_renamed_and_merged(self):
        parsed = _parsed(
            {
                "Member Management": ["- register a member"],
                "Catalog Management": ["- add a book"],
            }
        )
        applied = apply_heading_aliases(
            parsed,
            {"Member Management": "features", "Catalog Management": "features"},
        )
        assert applied.sections["features"] == [
            "- register a member",
            "- add a book",
        ]

    def test_source_refs_follow_the_rename(self):
        parsed = ParsedPRD(
            title="T",
            sections={"Member Management": ["- x"]},
            source_refs={"Member Management": [{"page": 3, "kind": "heading"}]},
        )
        applied = apply_heading_aliases(parsed, {"Member Management": "features"})
        assert applied.source_refs["features"] == [{"page": 3, "kind": "heading"}]

    def test_no_aliases_returns_the_document_untouched(self):
        parsed = _parsed({"Features": ["- a"]})
        assert apply_heading_aliases(parsed, {}) is parsed


class TestExtractContractIntegration:
    def test_reworded_roles_section_is_extracted(self):
        parsed = _parsed(
            {
                "4. User Types and Roles": ["- Librarian: manages loans", "- Member: borrows"],
            }
        )
        contract_obj = extract_contract(parsed)
        assert "Librarian" in contract_obj.roles
        assert "Member" in contract_obj.roles

    def test_resolution_can_be_switched_off(self):
        parsed = _parsed({"4. User Types and Roles": ["- Librarian: manages loans"]})
        assert extract_contract(parsed, resolve_headings=False).roles == []

    def test_non_functional_section_reaches_nonfunctional_requirements(self):
        parsed = _parsed(
            {"Non-Functional Requirements": ["- responds within 200ms"]}
        )
        contract_obj = extract_contract(parsed)
        assert any(
            "200ms" in item for item in contract_obj.nonfunctional_requirements
        )


class TestModelMappingParsing:
    def test_roles_map_onto_canonical_terms(self):
        raw = (
            '{"mappings": [{"heading": "12. Circulation Management", '
            '"role": "features"}]}'
        )
        aliases = _parse_model_mappings(raw, ["12. Circulation Management"])
        assert aliases == {"12. Circulation Management": "features"}

    def test_heading_is_matched_even_if_the_model_reformats_it(self):
        raw = '{"mappings": [{"heading": "circulation management", "role": "features"}]}'
        aliases = _parse_model_mappings(raw, ["12. Circulation Management"])
        assert aliases == {"12. Circulation Management": "features"}

    def test_none_role_produces_no_alias(self):
        raw = '{"mappings": [{"heading": "48. Success Metrics", "role": "none"}]}'
        assert _parse_model_mappings(raw, ["48. Success Metrics"]) == {}

    def test_unknown_role_is_ignored(self):
        raw = '{"mappings": [{"heading": "X", "role": "nonsense"}]}'
        assert _parse_model_mappings(raw, ["X"]) == {}

    def test_malformed_output_yields_no_aliases(self):
        assert _parse_model_mappings("not json at all", ["X"]) == {}
        assert _parse_model_mappings("[1, 2, 3]", ["X"]) == {}

    def test_every_role_maps_to_a_known_term(self):
        from shamsu.prd.headings import CANONICAL_TERMS

        for role, term in MODEL_ROLES.items():
            assert term == "" or term in CANONICAL_TERMS, role
