"""A repeated edit needs a way to say so.

`replace_text` refused any anchor matching more than once — correct, because
silently editing the first of several is how the wrong line gets changed. But
there was no way to express the other intention, and a real task wanted it:

    db/schema.sql has three columns reading `DEFAULT now` ... change every
    occurrence to `DEFAULT now()`

    ✗ file.patch  the 'find' text appears 3 times in db/schema.sql. Include
                  more surrounding context so it matches exactly once.

The model could not build three unique anchors, retried, and burned the step
on an edit it had described perfectly. This is the repository's oldest
recurring failure — a correct intention refused on a technicality — so the
answer is the same as it was for the missing append mode: give the intention a
spelling, and name it in the refusal.
"""

from __future__ import annotations

from shamsu.tools.editing import FilePatchInput, FilePatchTool

SCHEMA = (
    "CREATE TABLE bids (\n"
    "  created_at timestamptz DEFAULT now\n"
    ");\n"
    "CREATE TABLE orders (\n"
    "  created_at timestamptz DEFAULT now,\n"
    "  updated_at timestamptz DEFAULT now\n"
    ");\n"
)


def _apply(**arguments: object) -> tuple[str, str | None]:
    """The pure content step, which is the whole of what changed here."""
    return FilePatchTool._compute(  # noqa: SLF001 - deliberately the unit under test
        FilePatchInput.model_validate({"path": "schema.sql", **arguments}),
        SCHEMA,
        True,
    )


class TestAmbiguityIsStillRefusedByDefault:
    def test_several_matches_without_the_flag_is_an_error(self) -> None:
        _updated, problem = _apply(find="DEFAULT now", replace="DEFAULT now()")
        assert problem is not None
        assert "appears 3 times" in problem

    def test_the_refusal_names_the_flag(self) -> None:
        """Without this the model has no way to learn the capability exists."""
        _updated, problem = _apply(find="DEFAULT now", replace="DEFAULT now()")
        assert problem is not None
        assert "replace_all=true" in problem

    def test_a_unique_anchor_is_unaffected(self) -> None:
        updated, problem = _apply(find="CREATE TABLE bids", replace="CREATE TABLE b2")
        assert problem is None
        assert "CREATE TABLE b2" in updated


class TestReplaceAllDoesWhatItSays:
    def test_every_occurrence_changes(self) -> None:
        updated, problem = _apply(find="DEFAULT now", replace="DEFAULT now()", replace_all=True)
        assert problem is None
        assert updated.count("DEFAULT now()") == 3

    def test_nothing_else_changes(self) -> None:
        updated, problem = _apply(find="DEFAULT now", replace="DEFAULT now()", replace_all=True)
        assert problem is None
        assert updated.count("CREATE TABLE") == 2
        assert "bids" in updated and "orders" in updated

    def test_a_missing_anchor_is_still_an_error(self) -> None:
        """The flag relaxes uniqueness, not existence."""
        _updated, problem = _apply(find="NOT PRESENT", replace="x", replace_all=True)
        assert problem is not None
        assert "does not appear" in problem

    def test_it_is_harmless_on_a_single_match(self) -> None:
        updated, problem = _apply(
            find="CREATE TABLE bids", replace="CREATE TABLE b2", replace_all=True
        )
        assert problem is None
        assert updated.count("CREATE TABLE b2") == 1

    def test_the_default_is_still_single(self) -> None:
        """A caller that says nothing must keep the old, careful behaviour."""
        assert FilePatchInput(path="a.sql", find="x", replace="y").replace_all is False
