from __future__ import annotations

from shamsu.agents.clarification import (
    ClarificationNeed,
    build_pending_question,
    classify_reply,
    format_question,
    need_for_file_candidates,
    resolve_answer,
)


def _pending(options):
    return build_pending_question(
        "Which file should I use?",
        options,
        created_from_prompt="read the file src/App.tsx",
    )


def test_build_pending_question_normalizes_options():
    pending = build_pending_question("Q?", [{"label": " a ", "description": " x "}, "b"])
    assert pending["question"] == "Q?"
    assert pending["options"] == [
        {"label": "a", "description": "x"},
        {"label": "b", "description": ""},
    ]
    assert pending["allow_free_text"] is True
    assert pending["awaiting"] == "user_input"


def test_numbered_option_resolves_to_first_choice():
    pending = _pending([{"label": "client/src/App.tsx"}, {"label": "admin/src/App.tsx"}])

    answer = resolve_answer(pending, "1")

    assert answer.resolved is True
    assert answer.kind == "option"
    assert answer.option["label"] == "client/src/App.tsx"
    assert answer.value == "client/src/App.tsx"


def test_numbered_option_out_of_range_falls_back_to_free_text():
    pending = _pending([{"label": "a"}, {"label": "b"}])

    answer = resolve_answer(pending, "9")

    # "9" is not a valid choice, but free text is allowed, so it is kept as text.
    assert answer.kind == "free_text"
    assert answer.value == "9"


def test_label_and_unique_substring_match():
    pending = _pending([{"label": "client/src/App.tsx"}, {"label": "admin/src/App.tsx"}])

    exact = resolve_answer(pending, "admin/src/App.tsx")
    partial = resolve_answer(pending, "client")

    assert exact.option["label"] == "admin/src/App.tsx"
    assert partial.option["label"] == "client/src/App.tsx"


def test_bare_yes_on_single_option_selects_it():
    pending = _pending([{"label": "client/src/App.tsx"}])

    answer = resolve_answer(pending, "yes")

    assert answer.kind == "option"
    assert answer.option["label"] == "client/src/App.tsx"


def test_yes_no_cancel_continue_classification():
    pending = _pending([{"label": "a"}, {"label": "b"}])

    assert resolve_answer(pending, "cancel").kind == "cancel"
    assert resolve_answer(pending, "no").kind == "negative"
    assert resolve_answer(pending, "continue").kind == "continue"
    # Bare "yes" with several options is affirmative, not a choice.
    assert resolve_answer(pending, "yes").kind == "affirmative"


def test_unresolved_when_free_text_disabled_and_no_match():
    pending = build_pending_question("Pick one", [{"label": "a"}], allow_free_text=False)

    answer = resolve_answer(pending, "something else")

    assert answer.resolved is False
    assert answer.kind == "unresolved"


def test_classify_reply_standalone():
    assert classify_reply("yes") == "affirmative"
    assert classify_reply("Continue") == "continue"
    assert classify_reply("cancel") == "cancel"
    assert classify_reply("no") == "negative"
    assert classify_reply("read app.py") == "other"


def test_need_for_file_candidates_builds_options():
    need = need_for_file_candidates("src/App.tsx", ["client/src/App.tsx", "admin/src/App.tsx"])

    assert isinstance(need, ClarificationNeed)
    assert need.needed is True
    assert [option["label"] for option in need.options] == ["client/src/App.tsx", "admin/src/App.tsx"]
    pending = need.to_pending(created_from_prompt="open App.tsx")
    assert pending["created_from_prompt"] == "open App.tsx"
    assert len(pending["options"]) == 2


def test_format_question_numbers_options():
    pending = _pending([{"label": "client/src/App.tsx", "description": "frontend"}, {"label": "admin/src/App.tsx"}])

    text = format_question(pending)

    assert "Which file should I use?" in text
    assert "1. client/src/App.tsx - frontend" in text
    assert "2. admin/src/App.tsx" in text
