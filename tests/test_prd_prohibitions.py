"""A PRD must be able to say "not that".

Every PRDContract field was a positive accumulator, so a negative constraint was
unrepresentable. "PostgreSQL 16 (NEVER SQLite, NEVER Django/Python)" was not
merely ignored - the words were read as stack MENTIONS, and because ("django", ...)
is entry 0 of _STACK_PATTERNS it won stack detection outright. The forbidden stack
landed in a field named `required_stack`, and render_brief then re-asserted it to
the model as a requirement on every turn.
"""
from __future__ import annotations

from shamsu.prd.contract import detect_prohibitions, extract_contract
from shamsu.prd.parser import parse_prd_text

STACK_LOCK = (
    "Build the OpenBazaar marketplace. Stack: PostgreSQL 16 "
    "(NEVER SQLite, NEVER Django/Python - reject any plan containing either). "
    "Use Node with Express for the backend and React with Vite for the frontend."
)

PRD = """# OpenBazaar

## Product Summary
A full-stack marketplace with authentication and persistence.

## Tech Stack
PostgreSQL 16. NEVER SQLite. NEVER Django or Python.
Node with Express for the API, React and Vite for the UI.

## Entities
User: fields email, phone_number

## Persistence Requirements
All data lives in PostgreSQL.
"""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_the_real_stack_lock_is_detected():
    assert detect_prohibitions(STACK_LOCK) == ["django", "python", "sqlite"]


def test_only_what_follows_the_negation_is_forbidden():
    """The dangerous failure mode: forbidding the stack that was asked for.

    A comma is not a clause boundary, so "Use PostgreSQL, never SQLite" is one
    clause. A clause-wide scan would forbid PostgreSQL as well and leave nothing
    to build with.
    """
    assert detect_prohibitions("Use PostgreSQL, never SQLite.") == ["sqlite"]
    assert detect_prohibitions("Never use PostgreSQL; use SQLite instead.") == ["postgres"]
    assert detect_prohibitions("Use PostgreSQL instead of SQLite.") == ["sqlite"]


def test_a_replacement_is_not_a_prohibition():
    """"replace X with Y" names Y after the marker, so Y must not be forbidden."""
    assert detect_prohibitions("Replace SQLite with PostgreSQL.") == []


def test_dotted_technology_names_survive_clause_splitting():
    """A bare `.` split loses everything after "Node.js"."""
    assert detect_prohibitions("Do not use Node.js or Express.") == ["express", "node"]
    assert detect_prohibitions("Avoid next.js entirely.") == ["next.js"]


def test_a_prd_without_prohibitions_forbids_nothing():
    assert detect_prohibitions("Build a Django app with SQLite and React.") == []


def test_spelling_variants_collapse():
    assert detect_prohibitions("Never use PostgreSQL.") == ["postgres"]
    assert detect_prohibitions("Never use golang.") == ["go"]


def test_several_negation_phrasings_are_recognised():
    for text, expected in (
        ("The app must not use jQuery.", ["jquery"]),
        ("Build it without Django.", ["django"]),
        ("Do not use SQLAlchemy.", ["sqlalchemy"]),
        ("MySQL is forbidden.", []),  # marker follows the name - not directional
        ("Avoid MySQL.", ["mysql"]),
    ):
        assert detect_prohibitions(text) == expected, text


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------


def _contract():
    return extract_contract(parse_prd_text(PRD, markdown=True), request_text=STACK_LOCK)


def test_prohibitions_reach_the_contract():
    contract = _contract()

    assert "django" in contract.prohibitions
    assert "sqlite" in contract.prohibitions
    assert "python" in contract.prohibitions


def test_a_forbidden_stack_never_becomes_the_stack_hint():
    """The inversion itself: django is entry 0, so it used to win detection."""
    assert _contract().stack_hint != "django"


def test_a_forbidden_technology_never_lands_in_required_stack():
    required = _contract().required_stack

    assert "django" not in required
    assert "sqlite" not in required
    assert "python" not in required


def test_the_brief_states_the_prohibition_instead_of_re_asserting_it():
    """render_brief is re-shown to the model every turn."""
    brief = _contract().render_brief()

    assert "FORBIDDEN" in brief
    assert "django" in brief.split("FORBIDDEN")[1].splitlines()[0]
    assert "required stack: django" not in brief
    assert "stack hint: django" not in brief


def test_an_unconstrained_prd_is_unchanged():
    """No prohibitions must mean no behaviour change at all."""
    prd = parse_prd_text(
        "# Notes\n\n## Product Summary\nA Django notes app.\n\n"
        "## Tech Stack\nDjango with SQLite.\n",
        markdown=True,
    )
    contract = extract_contract(prd)

    assert contract.prohibitions == []
    assert contract.stack_hint == "django"
    assert "FORBIDDEN" not in contract.render_brief()
