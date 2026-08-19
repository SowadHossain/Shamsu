"""The whole conversation stays reachable, across forks and past the window.

`messages.jsonl` was always lossless and always out of reach: older turns
survive as a few summary lines, so a decision from turn three was gone the
moment the window moved past it. Nothing was lost - nothing could get it back.
"""
from __future__ import annotations

from shamsu.session.history import ancestry, render_hits, search_history
from shamsu.session.manager import SessionManager


def _buried(manager, title: str, decision: str, filler: int = 120):
    session = manager.create_session(title)
    session.append_message("user", decision)
    session.append_message("assistant", "Understood.")
    for i in range(filler):
        session.append_message("user", f"unrelated turn {i}")
        session.append_message("assistant", f"done {i}")
    return session


def test_a_decision_buried_past_the_window_is_still_findable(tmp_path):
    manager = SessionManager(tmp_path)
    session = _buried(manager, "build", "we settled on port 8080 for the dev server")

    hits = search_history(manager, session.session_id, "which port does the dev server use?")

    assert hits
    assert "8080" in hits[0].text


def test_history_search_crosses_a_fork(tmp_path):
    """The point of recording the parent: forking must cost nothing in recall."""
    manager = SessionManager(tmp_path)
    parent = _buried(manager, "phase one", "the window is 900x700, decided today")
    child = manager.fork(parent.session_id, "phase two")
    child.append_message("user", "add a scoreboard")

    hits = search_history(manager, child.session_id, "how big is the game window?")

    assert hits, "the parent conversation became unreachable after forking"
    assert "900x700" in hits[0].text
    assert hits[0].session_title == "phase one"


def test_a_fork_records_its_parent_and_carries_the_summary(tmp_path):
    manager = SessionManager(tmp_path)
    parent = manager.create_session("original")
    parent.append_message("user", "hello")
    parent.save_summary("- we chose sqlite", 2)

    child = manager.fork(parent.session_id)

    assert child.metadata.parent_session_id == parent.session_id
    carried, _upto = child.load_summary()
    assert "sqlite" in carried
    assert [m.title for m in ancestry(manager, child.session_id)] == [
        child.metadata.title,
        "original",
    ]


def test_a_chain_of_forks_is_walked_to_the_root(tmp_path):
    manager = SessionManager(tmp_path)
    first = _buried(manager, "one", "the API key lives in .env", filler=5)
    second = manager.fork(first.session_id, "two")
    third = manager.fork(second.session_id, "three")
    third.append_message("user", "deploy it")

    assert len(ancestry(manager, third.session_id)) == 3
    hits = search_history(manager, third.session_id, "where is the API key kept?")
    assert hits and ".env" in hits[0].text


def test_a_cycle_in_the_parent_links_does_not_hang(tmp_path):
    """A hand-edited or corrupted parent pointer must not spin forever."""
    manager = SessionManager(tmp_path)
    first = manager.create_session("a")
    second = manager.fork(first.session_id, "b")
    # Point the root back at its own descendant.
    first.metadata.parent_session_id = second.session_id
    manager._write_metadata(first.metadata)

    chain = ancestry(manager, second.session_id)

    assert len(chain) == 2, "the ancestry walk did not stop at the cycle"


def test_tool_payloads_are_not_dredged_up_by_a_history_search(tmp_path):
    """They are the bulk of a transcript and never what anyone means."""
    manager = SessionManager(tmp_path)
    session = manager.create_session("noisy")
    session.append_message("user", "write the parser")
    session.append_message("tool", "def parse(): pass\n" * 400, name="write_file")
    session.append_message("assistant", "The parser is written.")

    hits = search_history(manager, session.session_id, "parser")

    assert hits
    assert all(hit.role in {"user", "assistant"} for hit in hits)


def test_no_match_says_so_rather_than_inventing_one(tmp_path):
    manager = SessionManager(tmp_path)
    session = manager.create_session("empty-ish")
    session.append_message("user", "hello there")

    hits = search_history(manager, session.session_id, "kubernetes ingress annotations")

    assert render_hits(hits, "kubernetes ingress annotations").startswith("Nothing in")
