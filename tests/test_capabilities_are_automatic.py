"""Capabilities the harness reaches for, instead of hoping the model does.

One finding, four places. A capability that exists, is wired, is documented and
is reachable only if the model thinks to reach for it is a capability a 9B uses
and a 7B does not - and this project targets the 7B.

Memory settled the argument first: `run()` writes an evidence note at the end of
every turn because `memory_remember` "exists ONLY when the model volunteers a
tool call - and it does not", and `render_memory` puts it back without being
asked. These are the rest of them.

* **Skills.** `use_skill` was called ZERO times across every session logged to
  2026-08-28. The harness now matches one skill to the request and injects it.
* **The graph.** `_refresh_code_graph` refreshed an existing graph and never
  built one, and nothing else under `shamsu/agents/` did either - so the tools
  were withheld forever on a workspace nobody had indexed by hand.
* **The prompt.** Two probes for one capability: the prose named `graph_search`
  whenever the MCP binary was healthy, the schemas shipped only when the
  workspace was indexed. Advertised on nearly every turn, sent on nearly none.
* **The contract tools.** 515 tokens describing operations on a contract that
  did not exist yet, on every first turn of every build.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from shamsu.agents.simple_chat import (
    active_tool_schemas,
    available_tool_families,
)
from shamsu.agents.simple_prompt import simple_system_prompt
from shamsu.agents.simple_skills import MIN_SCORE, best_skill, render_skill
from shamsu.context.budget import count_tokens

#: Every tool name the prompt could plausibly mention.
_NAMED = re.compile(
    r"\b(memory_remember|memory_load|memory_list|memory_forget|history_search"
    r"|graph_search|explain_symbol|replace_symbol|patch_file|write_file"
    r"|append_file|read_symbol|ask_user|use_skill|contract_create"
    r"|contract_from_plan|contract_status|contract_assert_pass"
    r"|contract_assert_fail|contract_assert_skip)\b"
)


def _sent(workspace: Path, request: str) -> set[str]:
    families = available_tool_families(workspace)
    return {
        schema.get("function", schema)["name"]
        for schema in active_tool_schemas(32768, request=request, available=families)
    }


def _named(workspace: Path) -> set[str]:
    return set(_NAMED.findall(simple_system_prompt(workspace, has_history=False)))


# -- the prompt never names a tool it was not given --------------------------


#: The two tools the prompt names that a narrowed turn may knowingly lack.
#: `plan` withholds the write family on purpose (live 2026-08-18: a planning
#: request that could write wrote five files and produced no plan), and
#: `contract_create` ships only for a request `ask_for_a_plan` calls multi-part.
#: Both have their own tests; this one must not quietly reverse them.
DELIBERATELY_WITHHELD = {
    "write_file",
    "append_file",
    "patch_file",
    "replace_symbol",
    "contract_create",
    "contract_from_plan",
}


@pytest.mark.parametrize(
    "request_text",
    [
        "build me a snake game",
        "fix the bug in app.py",
        "what does this project do",
        "plan how you would add auth",
        "run the tests",
    ],
)
def test_a_fresh_workspace_is_promised_nothing_it_does_not_have(tmp_path, request_text):
    """smallcode issue #58: a small model trusts the prose over the array."""
    promised = _named(tmp_path) - DELIBERATELY_WITHHELD
    assert promised - _sent(tmp_path, request_text) == set()


@pytest.mark.parametrize(
    "request_text",
    [
        "fix the bug in app.py",
        "what does this project do",
        "plan how you would add auth",
        "run the tests",
    ],
)
def test_the_note_taking_tool_survives_every_narrowing(tmp_path, request_text):
    """It was dropped by every category but `write`, for no chosen reason,
    while the `recall` section named it on every single turn."""
    assert "memory_remember" in _sent(tmp_path, request_text)


def test_planning_still_cannot_write(tmp_path):
    """The floor must not reverse this - it has three tests of its own."""
    sent = _sent(tmp_path, "plan how you would add authentication")
    assert not sent & {"write_file", "patch_file", "replace_symbol", "append_file"}


def test_an_open_contract_unlocks_both_the_prose_and_the_schemas(tmp_path):
    (tmp_path / ".shamsu").mkdir(parents=True)
    (tmp_path / ".shamsu" / "contract.json").write_text("{}", encoding="utf-8")
    sent = _sent(tmp_path, "build me a snake game")
    assert "contract_assert_pass" in sent
    assert "contract_assert_pass" in _named(tmp_path)
    assert _named(tmp_path) - sent == set()


def test_the_graph_is_advertised_only_where_it_is_sent(tmp_path, monkeypatch):
    """The two probes used to ask different questions and disagree always."""
    assert "graph_search" not in _named(tmp_path)

    (tmp_path / ".shamsu" / "abstract").mkdir(parents=True)
    (tmp_path / ".shamsu" / "abstract" / "last-index.json").write_text(
        "{}", encoding="utf-8"
    )
    assert "graph_search" in _named(tmp_path)
    assert "graph_search" in _sent(tmp_path, "where is the render function")


def test_the_recall_prose_follows_the_memory_tools(tmp_path):
    assert "memory_remember" in _named(tmp_path)
    # No notes yet, so the tools that READ them are withheld - and unmentioned.
    assert "memory_load" not in _named(tmp_path)


# -- the contract tools are not shipped before there is a contract -----------


def _schema_tokens(workspace: Path) -> int:
    families = available_tool_families(workspace)
    return sum(
        count_tokens(json.dumps(schema))
        for schema in active_tool_schemas(
            32768, request="build me a snake game", available=families
        )
    )


def test_the_assert_tools_cost_nothing_until_there_is_something_to_assert(tmp_path):
    before = _schema_tokens(tmp_path)
    (tmp_path / ".shamsu").mkdir(parents=True)
    (tmp_path / ".shamsu" / "contract.json").write_text("{}", encoding="utf-8")
    assert _schema_tokens(tmp_path) - before > 300


def test_contract_create_is_never_withheld(tmp_path):
    """Gating the door you come in through is the bootstrap trap."""
    assert "contract_create" in _sent(tmp_path, "build me a snake game")
    assert "contract_from_plan" in _sent(tmp_path, "build me a snake game")


# -- one skill, matched by the harness --------------------------------------


class _Skill:
    def __init__(self, name, triggers=(), tags=(), instructions="body", budget=900):
        self.name = name
        self.triggers = triggers
        self.tags = tags
        self.instructions = instructions
        self.context_budget_tokens = budget


def test_a_specific_skill_beats_a_general_one():
    """`developer` answers to "build" and would otherwise win everything."""
    general = _Skill("developer", triggers=("build", "create", "fix"))
    specific = _Skill("react-vite", triggers=("react", "vite", "dashboard"))
    chosen = best_skill([general, specific], "build me a react dashboard with vite")
    assert chosen is specific


def test_a_multi_word_trigger_outscores_a_bare_verb():
    verb = _Skill("developer", triggers=("build",))
    phrase = _Skill("large-file-surgery", triggers=("part by part",))
    chosen = best_skill([verb, phrase], "build this file part by part")
    assert chosen is phrase


def test_a_bare_verb_alone_is_not_enough_to_inject_anything():
    """One weak match must not put 900 tokens in every window."""
    general = _Skill("developer", triggers=("fix",))
    assert best_skill([general], "fix it") is None
    assert MIN_SCORE > 2.0


def test_nothing_matches_nothing():
    assert best_skill([], "build me a snake game") is None
    assert best_skill([_Skill("react-vite", triggers=("react",))], "") is None


def test_a_matched_skill_is_rendered_within_its_budget(tmp_path, monkeypatch):
    long_body = "\n".join(f"line {n} of guidance" for n in range(400))
    skill = _Skill("react-vite", triggers=("react",), instructions=long_body)
    monkeypatch.setattr(
        "shamsu.agents.simple_chat._skills_worth_offering", lambda *_a: [skill]
    )
    monkeypatch.setattr(
        "shamsu.agents.simple_chat._skill_catalog",
        lambda *_a: type("C", (), {"sorted_skills": lambda self: [skill]})(),
    )
    # Names the skill outright, so it clears MIN_SCORE - one bare trigger word
    # deliberately does not, which `test_a_bare_verb_alone...` pins.
    rendered = render_skill(tmp_path, "set up react-vite for this project", 200)
    assert rendered
    assert count_tokens(rendered) <= 260  # budget plus the one-line header
    assert "use_skill" in rendered  # it says where the rest is


def test_a_zero_budget_injects_nothing(tmp_path):
    assert render_skill(tmp_path, "build a react app", 0) == ""


# -- the graph builds itself -------------------------------------------------


class _Adapter:
    def __init__(self) -> None:
        self.indexed: list[Path] = []
        self.refreshed: list[Path] = []
        self.available = True

    def is_available(self, workspace):
        return self.available

    def index_workspace(self, workspace, force=False):
        self.indexed.append(Path(workspace))
        return {"ok": True}

    def refresh_workspace(self, workspace):
        self.refreshed.append(Path(workspace))
        return {"ok": True}


@pytest.fixture
def adapter(monkeypatch):
    made = _Adapter()
    monkeypatch.setattr(
        "shamsu.tools.codebase_memory.CodebaseMemoryAdapter", lambda *_a, **_k: made
    )
    return made


def _loop(workspace: Path, files: list[str]):
    from shamsu.agents.simple_chat import SimpleChatLoop

    loop = SimpleChatLoop.__new__(SimpleChatLoop)
    loop.workspace = workspace
    loop._files = files
    loop._activity = lambda *_a, **_k: None
    loop._trace = lambda *_a, **_k: None
    return loop


def test_a_workspace_with_no_graph_gets_one_built(tmp_path, adapter):
    """The regression: this used to return early and index nothing, ever."""
    _loop(tmp_path, ["main.py"])._refresh_code_graph()
    assert adapter.indexed == [tmp_path]


def test_an_existing_graph_is_refreshed_not_rebuilt(tmp_path, adapter):
    (tmp_path / ".shamsu" / "abstract").mkdir(parents=True)
    (tmp_path / ".shamsu" / "abstract" / "last-index.json").write_text("{}")
    _loop(tmp_path, ["main.py"])._refresh_code_graph()
    assert adapter.refreshed == [tmp_path]
    assert adapter.indexed == []


def test_an_empty_workspace_is_not_indexed(tmp_path, adapter):
    _loop(tmp_path, [])._refresh_code_graph()
    assert adapter.indexed == []


def test_no_indexer_installed_costs_one_check_and_nothing_else(tmp_path, adapter):
    adapter.available = False
    _loop(tmp_path, ["main.py"])._refresh_code_graph()
    assert adapter.indexed == []


def test_an_indexer_that_raises_does_not_end_the_turn(tmp_path, monkeypatch):
    class _Angry:
        def is_available(self, _workspace):
            raise RuntimeError("no binary")

    monkeypatch.setattr(
        "shamsu.tools.codebase_memory.CodebaseMemoryAdapter", lambda *_a, **_k: _Angry()
    )
    _loop(tmp_path, ["main.py"])._refresh_code_graph()  # must not raise
