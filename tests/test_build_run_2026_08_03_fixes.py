"""Fixes found by actually driving SHAMSU through the OpenBazaar build.

Every one of these was invisible to the test suite and to any amount of reading
the code. They only appear when a real 7B writes a real Django app one file at a
time for forty-odd turns.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.routing.operations import _operation_kind, _route_for_kind
from shamsu.tools.agent_tools import AgentToolRegistry, _gutting_overwrite
from shamsu.verify import semantic

SOURCE = (
    "from django.shortcuts import render\n\n\n"
    "def home(request):\n    return render(request, 'a.html')\n\n\n"
    "def detail(request, pk):\n    return render(request, 'b.html')\n\n\n"
    "def sell(request):\n    return render(request, 'c.html')\n"
)


# ── a write that guts a file ──────────────────────────────────────────────
#
# Live: a working core/views.py holding four view functions was replaced by the
# three bytes "}". Not empty, so the empty-write guard passed it; nothing else
# looked at what was already there.


def test_replacing_a_source_file_with_a_fragment_is_refused(tmp_path: Path):
    target = tmp_path / "views.py"
    target.write_text(SOURCE, encoding="utf-8")

    reason = _gutting_overwrite(target, "}")

    assert "defines none" in reason


def test_the_write_tool_refuses_it_and_leaves_the_file_alone(tmp_path: Path):
    """For a .py file the syntax guard answers first - `}` does not parse - and
    either refusal keeps the working file, which is the property that matters."""
    (tmp_path / "views.py").write_text(SOURCE, encoding="utf-8")
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)

    result = tools.write_file("views.py", "}", overwrite=True)

    assert result.ok is False
    assert result.data.get("syntax_regression") or result.data.get("gutting_overwrite")
    assert (tmp_path / "views.py").read_text(encoding="utf-8") == SOURCE


def test_a_gutted_file_no_checker_understands_is_refused_by_the_gutting_guard(
    tmp_path: Path,
):
    """The gutting guard owns the languages the syntax guard cannot parse -
    a set that shrank sharply when the syntax guard stopped being Python-only.

    What is left to it is roughly `.rb` and `.php`: languages whose
    declarations `_DEFINITION_RE` recognises but for which `simple_verify` has
    no checker. Everything else it used to cover alone - .js, .ts, .jsx, .go,
    .rs, .java - is now bracket-checked as well, and the syntax gate reaches
    those first. This is the guard's own remaining case: caught on SHAPE,
    because nothing here can judge the grammar.
    """
    source = (
        "class User\n  def name\n    @name\n  end\nend\n\n"
        "class Order\n  def total\n    @total\n  end\nend\n\n"
        "class Item\n  def price\n    @price\n  end\nend\n" * 3
    )
    (tmp_path / "models.rb").write_text(source, encoding="utf-8")
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)

    result = tools.write_file("models.rb", "end", overwrite=True)

    assert result.ok is False
    assert result.data["gutting_overwrite"] is True
    assert (tmp_path / "models.rb").read_text(encoding="utf-8") == source


def test_a_gutted_javascript_file_is_refused_by_the_syntax_guard_now(tmp_path: Path):
    """Same file, same protection, a different guard reaching it first. `}`
    does not parse as JavaScript, so the syntax gate refuses before the
    gutting guard is consulted."""
    source = (
        "export function home() { return 1; }\n\n"
        "export function detail() { return 2; }\n\n"
        "export function sell() { return 3; }\n" * 3
    )
    (tmp_path / "views.js").write_text(source, encoding="utf-8")
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)

    result = tools.write_file("views.js", "}", overwrite=True)

    assert result.ok is False
    assert result.data["syntax_regression"] is True
    assert (tmp_path / "views.js").read_text(encoding="utf-8") == source


def test_a_genuine_rewrite_of_the_same_file_still_works(tmp_path: Path):
    (tmp_path / "views.py").write_text(SOURCE, encoding="utf-8")
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)

    assert tools.write_file("views.py", SOURCE.replace("sell", "listing"), overwrite=True).ok


def test_shrinking_to_one_real_function_is_allowed(tmp_path: Path):
    """The guard needs BOTH a drastic shrink and the loss of every declaration."""
    target = tmp_path / "views.py"
    target.write_text(SOURCE, encoding="utf-8")

    assert _gutting_overwrite(target, "def home(request):\n    return None\n") == ""


def test_a_new_file_is_never_gutting(tmp_path: Path):
    assert _gutting_overwrite(tmp_path / "absent.py", "}") == ""


def test_a_file_that_declared_nothing_is_not_protected(tmp_path: Path):
    target = tmp_path / "notes.txt"
    target.write_text("just prose, no declarations at all, " * 20, encoding="utf-8")

    assert _gutting_overwrite(target, "x") == ""


# ── routing: a write is never a web search ────────────────────────────────


def test_dictating_markup_does_not_read_as_a_web_search():
    """"browse" came from browse.html and "current" from "Current bid:", so a
    prompt whose whole job was to write a file went to web search."""
    kind = _operation_kind("rewrite templates/browse.html with a card showing current bid")

    assert kind == "mutation"


def test_a_real_web_search_still_routes_to_web():
    assert _operation_kind("search the web for the latest django release notes") == "web"


def test_rewrite_and_friends_count_as_mutations():
    for verb in ("rewrite", "overwrite", "replace", "append"):
        assert _operation_kind(f"{verb} core/views.py so it has three views") == "mutation"


def test_a_mutation_is_never_dispatched_to_web():
    """The invariant is the negative: a write must never become a web search.

    Which write route it takes depends on whether the clause names a file -
    a named target goes straight to file.write, an unnamed one to the agent
    loop that can find it. Both are writes; neither is web.
    """
    assert _route_for_kind("mutation", "web", "rewrite core/views.py") == "file.write"
    assert _route_for_kind("mutation", "web") == "agent-chat"


def test_a_question_can_still_be_dispatched_to_web():
    assert _route_for_kind("answer", "web") == "web"


# ── templates are code ────────────────────────────────────────────────────


def test_templates_are_probed_when_html_changes(tmp_path: Path):
    (tmp_path / "manage.py").write_text(
        "import os\nos.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n",
        encoding="utf-8",
    )

    assert semantic.should_probe_templates(["templates/browse.html"], tmp_path) is True
    assert semantic.should_probe_templates(["core/views.py"], tmp_path) is False


def test_templates_are_not_probed_outside_a_django_project(tmp_path: Path):
    assert semantic.should_probe_templates(["templates/browse.html"], tmp_path) is False


def test_the_template_probe_compiles_every_template(tmp_path: Path):
    written = semantic.write_template_probe(tmp_path)
    body = written.read_text(encoding="utf-8")

    assert "get_template" in body
    assert "TEMPLATE FAILURES" in body


# ── a write must not replace parsing Python with unparseable Python ───────
#
# The gutting guard needs the file to lose EVERY declaration, so a generation
# that stops mid-string slips past it: `return render(request, "item` keeps all
# seven `def`s and does not parse. It destroyed core/views.py twice on the same
# day, through write_file once and through append/edit the second time.

BROKEN_TAIL = 'def home(request):\n    return render(request, "item\n'


def test_a_truncated_write_is_refused(tmp_path: Path):
    from shamsu.tools.agent_tools import _breaks_a_working_file

    target = tmp_path / "views.py"
    target.write_text(SOURCE, encoding="utf-8")

    assert "does not parse" in _breaks_a_working_file(target, BROKEN_TAIL)


def test_an_already_broken_file_can_still_be_repaired(tmp_path: Path):
    """Only files that parsed BEFORE are protected."""
    from shamsu.tools.agent_tools import _breaks_a_working_file

    target = tmp_path / "views.py"
    target.write_text("def x(:\n", encoding="utf-8")

    assert _breaks_a_working_file(target, "def x(:\n    still broken\n") == ""


def test_a_file_type_with_no_checker_is_not_syntax_checked(tmp_path: Path):
    """Renamed from `test_non_python_files_are_not_syntax_checked`, because
    the guard is no longer Python-only.

    It was `if target.suffix.lower() != ".py": return ""`, while
    `simple_verify` has understood .js/.ts/.jsx/.css/.json all along - so the
    check that knows about braces was wired into `replace_symbol` and NOT
    into the two paths that write, and a JavaScript patch that ate a closing
    brace landed on disk. What survives of the old rule is narrower and still
    true: about a file nothing can parse, the honest answer is we cannot tell.
    """
    from shamsu.tools.agent_tools import _breaks_a_working_file

    target = tmp_path / "notes.txt"
    target.write_text("def a(): pass\n", encoding="utf-8")

    assert _breaks_a_working_file(target, "def x(:") == ""


def test_javascript_is_syntax_checked_now(tmp_path: Path):
    """The whole point of the widening, and the case that landed on disk."""
    from shamsu.tools.agent_tools import _breaks_a_working_file

    target = tmp_path / "app.js"
    target.write_text("function a() {\n  return 1;\n}\n", encoding="utf-8")

    assert _breaks_a_working_file(target, "function a() {\n  return 1;\n") != ""
    assert _breaks_a_working_file(target, "function a() {\n  return 2;\n}\n") == ""


def test_append_cannot_break_a_working_module(tmp_path: Path):
    (tmp_path / "views.py").write_text(SOURCE, encoding="utf-8")
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)

    result = tools.append_file("views.py", BROKEN_TAIL)

    assert result.ok is False
    assert result.data["syntax_regression"] is True
    assert (tmp_path / "views.py").read_text(encoding="utf-8") == SOURCE


def test_edit_cannot_break_a_working_module(tmp_path: Path):
    (tmp_path / "views.py").write_text(SOURCE, encoding="utf-8")
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)

    result = tools.edit_file("views.py", "return render(request, 'a.html')", 'return render(request, "item')

    assert result.ok is False
    assert (tmp_path / "views.py").read_text(encoding="utf-8") == SOURCE


def test_a_valid_append_still_works(tmp_path: Path):
    (tmp_path / "views.py").write_text(SOURCE, encoding="utf-8")
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)

    assert tools.append_file("views.py", "\n\ndef extra(request):\n    return None\n").ok


# ── a model that serializes its own tool call into the file ──────────────
#
# Observed live: templates/my_orders.html was written as
# `{"name": "write_file", "arguments": {"content": "...", "filepath": "..."}}`.
# The page then failed at render time with a baffling escaped-quote error.
# The inner content was correct, so salvaging beats refusing.


def test_a_serialized_tool_call_is_unwrapped(tmp_path: Path):
    import json

    from shamsu.tools.agent_tools import _unwrap_serialized_tool_call

    inner = '{% extends "base.html" %}\n<h1>Orders</h1>'
    envelope = json.dumps(
        {"name": "write_file", "arguments": {"content": inner, "filepath": "t.html"}}
    )

    assert _unwrap_serialized_tool_call(envelope, "write_file") == inner


def test_the_write_tool_stores_the_inner_content(tmp_path: Path):
    import json

    inner = '{% extends "base.html" %}\n<h1>Orders</h1>'
    envelope = json.dumps(
        {"name": "write_file", "arguments": {"content": inner, "filepath": "t.html"}}
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)

    assert tools.write_file("t.html", envelope).ok
    assert (tmp_path / "t.html").read_text(encoding="utf-8") == inner


def test_a_real_json_file_is_written_verbatim(tmp_path: Path):
    """The unwrap must not corrupt a legitimate .json deliverable."""
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    body = '{"name": "openbazaar", "version": "1.0.0"}'

    assert tools.write_file("package.json", body).ok
    assert (tmp_path / "package.json").read_text(encoding="utf-8") == body


def test_an_envelope_for_a_different_tool_is_left_alone(tmp_path: Path):
    import json

    from shamsu.tools.agent_tools import _unwrap_serialized_tool_call

    envelope = json.dumps({"name": "run_command", "arguments": {"content": "ls"}})

    assert _unwrap_serialized_tool_call(envelope, "write_file") is None
