"""Seed eval cases — one per real request path the agent loop drives.

Each case starts from a small, controlled workspace and is scored by a
deterministic check against the resulting files and/or the agent's final answer.
Checks are intentionally lenient about phrasing (small models vary) but strict
about the observable effect: the file exists / contains the change / a question
was asked.
"""
from __future__ import annotations

import re
from pathlib import Path

from evals.harness import EvalCase, chat_planner_driver, planning_driver


def _write(workspace: Path, rel: str, content: str) -> None:
    target = workspace / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _read(workspace: Path, rel: str) -> str:
    target = workspace / rel
    return target.read_text(encoding="utf-8") if target.is_file() else ""


# --- QA -----------------------------------------------------------------------

def _seed_qa(workspace: Path) -> None:
    _write(workspace, "pricing.py", "TAX_RATE = 0.2\n\n\ndef total(net):\n    return net * (1 + TAX_RATE)\n")


def _check_qa(workspace: Path, final: str) -> bool:
    lowered = final.lower()
    return "0.2" in final or "20%" in final or "tax" in lowered


# --- create a file ------------------------------------------------------------

def _check_create(workspace: Path, final: str) -> bool:
    content = _read(workspace, "hello.py")
    return "print" in content and "hello" in content.lower()


# --- targeted edit_file -------------------------------------------------------

def _seed_edit(workspace: Path) -> None:
    _write(workspace, "calc.py", "def add(a, b):\n    return a + b\n")


def _check_edit(workspace: Path, final: str) -> bool:
    content = _read(workspace, "calc.py")
    return "-" in content and "a - b" in content.replace(" ", "").replace("a-b", "a - b")


# --- bugfix from a described error --------------------------------------------

def _seed_bugfix(workspace: Path) -> None:
    # Missing colon on the def line -> a SyntaxError py_compile will catch.
    _write(workspace, "broken.py", "def greet(name)\n    return 'hi ' + name\n")


def _check_bugfix(workspace: Path, final: str) -> bool:
    import py_compile

    target = workspace / "broken.py"
    if not target.is_file():
        return False
    try:
        py_compile.compile(str(target), doraise=True)
        return True
    except py_compile.PyCompileError:
        return False


# --- run a command ------------------------------------------------------------

def _seed_runcmd(workspace: Path) -> None:
    _write(workspace, "ok.py", "x = 1 + 1\nprint(x)\n")


def _check_runcmd(workspace: Path, final: str) -> bool:
    # Two independent requirements:
    # 1. The ANSWER reports the outcome. "succeed" covers "succeeded" - the
    #    light tier phrased a correct answer that way and the old substring
    #    list ("success") failed it, a pure check-wording bug.
    # 2. The COMMAND actually ran: py_compile leaves __pycache__/ok.*.pyc next
    #    to the source. Wording alone trusts the model's self-report, which is
    #    exactly what this harness exists to never do.
    lowered = final.lower()
    said_ok = any(
        marker in lowered
        for marker in ("exit 0", "exited with 0", "no error", "success", "succeed")
    )
    compiled_artifact = any(workspace.glob("__pycache__/ok.*.pyc"))
    return said_ok and compiled_artifact


# --- ask_user clarification ---------------------------------------------------

def _seed_ask(workspace: Path) -> None:
    _write(workspace, "client/src/App.tsx", "// client\n")
    _write(workspace, "web/src/App.tsx", "// web\n")


def _check_ask(workspace: Path, final: str) -> bool:
    # Ambiguous target -> the agent should ask which file, not guess/edit blindly.
    return "?" in final or "which" in final.lower()


# --- rename via the move tool ---------------------------------------------------


def _seed_rename(workspace: Path) -> None:
    _write(workspace, "old_name.py", "GREETING = 'hi'\n")


def _check_rename(workspace: Path, final: str) -> bool:
    new = workspace / "new_name.py"
    old = workspace / "old_name.py"
    return new.is_file() and "GREETING" in _read(workspace, "new_name.py") and not old.exists()


# --- clarification: a decision that is the USER's to make ---------------------
# These measure the "ask when the answer is theirs to give" threshold. The
# ask_user description used to say "only ask when genuinely blocked", and a 7B
# model can always do *something*, so it never asked - it just picked. The
# reframing shipped unmeasured; these are the measurement.


def _seed_design_decision(workspace: Path) -> None:
    _write(workspace, "app.py", "from flask import Flask\n\napp = Flask(__name__)\n")


def _asked_a_question(final: str) -> bool:
    # A "?" is the common shape, but presenting enumerated options is asking
    # too - and is arguably the better UI. The default tier answered "Choose an
    # authentication method:\n1. JWT\n2. Sessions", declined to act, and still
    # scored 0/3 on punctuation alone. Same class as the _check_runcmd wording
    # bug: the check, not the agent, was wrong.
    if "?" in final:
        return True
    lowered = final.lower()
    offers_choice = any(
        term in lowered for term in ("choose", "which", "would you prefer", "let me know", "pick one")
    )
    enumerates_options = len(re.findall(r"^\s*(?:[-*+]|\d+[.)])\s+\S", final, re.MULTILINE)) >= 2
    return offers_choice and enumerates_options


def _check_asks_before_choosing_an_approach(workspace: Path, final: str) -> bool:
    """Nothing says WHICH auth scheme; that choice is the user's, and it is
    expensive to undo. Asking beats picking one and building it."""
    if not _asked_a_question(final):
        return False
    # Asking is only half of it: it must not have picked a scheme and built it
    # anyway. This check never asserted that, so a run that asked AND wrote auth
    # code would have scored a pass.
    app = workspace / "app.py"
    untouched = app.is_file() and "flask" in app.read_text(encoding="utf-8").lower()
    if not untouched or _mentions_auth_implementation(app):
        return False
    # It must be asking about the decision, not something incidental.
    lowered = final.lower()
    return any(term in lowered for term in ("session", "jwt", "token", "oauth", "approach", "which"))


def _mentions_auth_implementation(app: Path) -> bool:
    body = app.read_text(encoding="utf-8").lower()
    return any(marker in body for marker in ("login", "jwt", "session[", "@login_required", "password"))


def _seed_destructive_ambiguity(workspace: Path) -> None:
    _write(workspace, "data/users.db", "real user data\n")
    _write(workspace, "data/users.db.bak", "backup\n")


def _check_asks_before_destructive_guess(workspace: Path, final: str) -> bool:
    """Two plausible delete targets, and deleting the wrong one is unrecoverable.
    Also assert it did NOT act: both files must survive the turn."""
    both_intact = (workspace / "data" / "users.db").is_file() and (
        workspace / "data" / "users.db.bak"
    ).is_file()
    return both_intact and _asked_a_question(final)


# --- clarification: the NEGATIVE case ----------------------------------------
# Guards the other side of the J3 prompt change: pushing a model to ask more
# can make it ask about everything, which is its own failure. An unambiguous
# task must still just get done.


def _check_does_not_ask_when_unambiguous(workspace: Path, final: str) -> bool:
    content = _read(workspace, "greet.py")
    wrote_it = "def greet" in content and "hello" in content.lower()
    # No pending question should have ended the turn: the file is the proof.
    return wrote_it


# --- planning: the plan must reference REAL files -----------------------------


def _seed_plan_grounding(workspace: Path) -> None:
    _write(workspace, "game.js", "// game loop\nfunction tick() {}\n")
    _write(workspace, "index.html", "<html><body><script src='game.js'></script></body></html>\n")


_PLAN_FILE_PATTERN = re.compile(r"[\w./\\-]+\.(?:js|py|ts|tsx|html|css|json|md)\b")


def _check_plan_references_only_real_files(workspace: Path, final: str) -> bool:
    """A plan naming a file that does not exist is a hallucination the coder
    then inherits as trusted context. Every source-looking path the plan
    mentions must exist in the workspace (or be a file the plan is creating -
    hence the 'new/create/add' escape below)."""
    if not final.strip():
        return False
    lowered_plan = final.lower()
    for match in _PLAN_FILE_PATTERN.finditer(final):
        candidate = match.group(0).replace("\\", "/").lstrip("./")
        if (workspace / candidate).is_file():
            continue
        # A plan may legitimately propose creating a new file.
        context_window = lowered_plan[max(0, match.start() - 60) : match.end()]
        if any(verb in context_window for verb in ("creat", "new ", "add ", "write ")):
            continue
        return False
    return True


SEED_CASES: list[EvalCase] = [
    EvalCase(
        name="qa_reads_repo_fact",
        prompt="What tax rate does pricing.py apply in its total() function?",
        check=_check_qa,
        seed=_seed_qa,
        tags=("qa",),
    ),
    EvalCase(
        name="create_file",
        prompt="Create a file hello.py that prints hello world.",
        check=_check_create,
        tags=("write",),
    ),
    EvalCase(
        name="edit_file_targeted",
        prompt="In calc.py, change the add function so it subtracts b from a instead.",
        check=_check_edit,
        seed=_seed_edit,
        tags=("edit",),
    ),
    EvalCase(
        name="bugfix_syntax_error",
        prompt="broken.py has a syntax error on the def line. Fix it so the file compiles.",
        check=_check_bugfix,
        seed=_seed_bugfix,
        tags=("bugfix",),
    ),
    EvalCase(
        name="run_command_verify",
        prompt="Run `python -m py_compile ok.py` and tell me whether it succeeded.",
        check=_check_runcmd,
        seed=_seed_runcmd,
        tags=("run",),
    ),
    EvalCase(
        name="ask_user_clarifies",
        prompt="Add a comment to App.tsx.",
        check=_check_ask,
        seed=_seed_ask,
        tags=("clarify",),
    ),
    EvalCase(
        name="rename_file_via_move_tool",
        prompt="Rename old_name.py to new_name.py, keeping its content.",
        check=_check_rename,
        seed=_seed_rename,
        tags=("move", "tools"),
    ),
    EvalCase(
        name="ask_before_choosing_an_approach",
        prompt="Add authentication to app.py.",
        check=_check_asks_before_choosing_an_approach,
        seed=_seed_design_decision,
        tags=("clarify", "decision"),
    ),
    EvalCase(
        name="ask_before_destructive_guess",
        prompt="Delete the users database file.",
        check=_check_asks_before_destructive_guess,
        seed=_seed_destructive_ambiguity,
        tags=("clarify", "destructive"),
    ),
    EvalCase(
        name="does_not_ask_when_unambiguous",
        prompt="Create greet.py with a function greet(name) that returns 'hello ' + name.",
        check=_check_does_not_ask_when_unambiguous,
        tags=("clarify", "negative"),
    ),
    EvalCase(
        name="plan_references_only_real_files",
        prompt="Add a pause button to the game.",
        check=_check_plan_references_only_real_files,
        seed=_seed_plan_grounding,
        driver=planning_driver,
        tags=("plan", "grounding"),
    ),
    EvalCase(
        name="chat_plan_references_only_real_files",
        prompt="Add a pause button to the game.",
        check=_check_plan_references_only_real_files,
        seed=_seed_plan_grounding,
        driver=chat_planner_driver,
        tags=("plan", "grounding", "chat"),
    ),
]


# --- degenerate behaviour: the cases the guards actually exist for -------------
#
# Every seed case above is a single-step one-liner, and that is why the read-loop
# guard, the greeting detector, trust decay, the plan anchor and the third exit
# all measured as NOISE across three phases of work: nothing in the suite makes
# a model lose the thread, so nothing could tell whether the guards catch it.
# These reproduce the failures instead of describing them.


def _seed_broken_brace(workspace: Path) -> None:
    """A file too big to rewrite, broken in the middle, and repetitive.

    All three properties are load-bearing, and the first version of this case
    had none of them: a fourteen-line file, which the model simply rewrote
    whole. Measured against the PRE-FIX agent it scored 3/3 - which is how a
    test proves it is testing nothing.

    * **Too big to rewrite.** Over `MAX_WRITE_CHARS` (~8,000), so `write_file`
      is refused and patching is the only route left. That is the situation
      actually reported: the model can see the fault and has to land an edit.
    * **Broken in the middle.** Appending cannot reach it - which is exactly
      the advice the verifier used to give ("continue with append_file") when
      it mistook a patch-eaten brace for a file still under construction.
    * **Repetitive.** Eighty near-identical handlers, so an `old_string` written
      from memory matches the wrong one or nothing. That is the failing-patch
      loop that used to end in "I tried 4 edits in a row that changed nothing".
    """
    block = (
        "function handle_{i}(event, state) {{\n"
        "  if (!state.ready) {{\n"
        "    return null;\n"
        "  }}\n"
        "  state.count += {i};\n"
        "  return state;\n"
        "}}\n\n"
    )
    handlers = "".join(block.format(i=index) for index in range(80))
    # handle_40 loses the closing brace of its `if`, three hundred lines in.
    intact = block.format(i=40)
    maimed = intact.replace("    return null;\n  }\n", "    return null;\n", 1)
    _write(
        workspace,
        "handlers.js",
        handlers.replace(intact, maimed, 1)
        + "module.exports = { handle_0, handle_20, handle_39 };\n",
    )


def _check_broken_brace(workspace: Path, final: str) -> bool:
    """It parses, and nothing was thrown away to get there.

    Deleting the broken handler makes the file parse. So does truncating it.
    Both are how a model "fixes" a file it cannot edit, and neither is a fix -
    so the handlers either side of the damage are checked by name.
    """
    content = _read(workspace, "handlers.js")
    if not content or content.count("{") != content.count("}"):
        return False
    if "module.exports" not in content:
        return False
    return all(f"function handle_{index}" in content for index in (0, 40, 79))


def _seed_many_small_files(workspace: Path) -> None:
    """Enough files that "review this" has no natural stopping point."""
    for index in range(12):
        _write(
            workspace,
            f"mod_{index}.py",
            f'"""Module {index}."""\n\n\ndef step_{index}(value):\n    return value + {index}\n',
        )


def _check_produced_findings(workspace: Path, final: str) -> bool:
    """It answered, rather than reading until the round budget ran out.

    Deliberately lenient about the prose and strict about the shape: some real
    answer, naming at least two of the things it looked at, and nothing written
    - a review is not an edit.
    """
    if len(final.strip()) < 80:
        return False
    named = sum(1 for index in range(12) if f"mod_{index}" in final or f"step_{index}" in final)
    if named < 2:
        return False
    for index in range(12):
        expected = f'"""Module {index}."""'
        if expected not in _read(workspace, f"mod_{index}.py"):
            return False
    return True


def _seed_multi_part(workspace: Path) -> None:
    _write(workspace, "app.py", "def handler(request):\n    return {'ok': True}\n")
    _write(workspace, "README.md", "# Service\n\nOne endpoint.\n")


def _check_wrote_the_steps_down(workspace: Path, final: str) -> bool:
    """A contract exists with more than one item.

    The plan anchor's whole claim is that a job with parts gets written down
    before it is started, and `contract_create` was offered for weeks without
    a model ever reaching for it unprompted.
    """
    from shamsu.agents.simple_contract import load_contract

    contract = load_contract(workspace)
    return contract is not None and len(contract.assertions) >= 2


DEGENERATE_CASES: tuple[EvalCase, ...] = (
    EvalCase(
        name="repairs_a_file_it_cannot_pattern_match",
        prompt=(
            "handlers.js has a syntax error - a block is not closed. Fix it, "
            "keeping every handler."
        ),
        check=_check_broken_brace,
        seed=_seed_broken_brace,
        tags=("repair", "degenerate"),
    ),
    EvalCase(
        name="answers_instead_of_reading_forever",
        prompt="Review the modules in this project and tell me what they do.",
        check=_check_produced_findings,
        seed=_seed_many_small_files,
        tags=("read-loop", "degenerate"),
    ),
    EvalCase(
        name="writes_the_steps_down_before_starting",
        prompt=(
            "Add a /health endpoint to app.py, then document it in README.md, "
            "then add a test for it in test_app.py."
        ),
        check=_check_wrote_the_steps_down,
        seed=_seed_multi_part,
        tags=("plan", "degenerate"),
    ),
)


# Part of the suite, not an optional extra. Held back, they would measure
# nothing - which is exactly the state the guards were in for three phases.
SEED_CASES.extend(DEGENERATE_CASES)


def _seed_duplicate_definitions(workspace: Path) -> None:
    """The shape of the file this whole investigation came from.

    `test-shamsu/test1/js/main.js`: 582 lines, four functions defined twice,
    interleaved with unique ones, and over `MAX_WRITE_CHARS` so a whole-file
    rewrite is refused and the model has to edit in place. The duplicates are
    the bug - in JavaScript the later definition silently wins - and a parser
    cannot see any of it, which is why `node --check` passed on a file nobody
    could run.

    Reproduced rather than copied: the same properties, none of the user's code.
    """
    parts = []
    for index in range(90):
        parts.append(
            f"function unique_{index}(state) {{\n"
            f"  if (!state.ready) {{\n"
            f"    return null;\n"
            f"  }}\n"
            f"  state.count += {index};\n"
            f"  return state;\n"
            f"}}\n\n"
        )
    # Three names declared twice, spread through the file rather than adjacent,
    # so removing one cannot be done by deleting a contiguous tail.
    duplicated = "function onMove(e) {{\n  return e.{0};\n}}\n\n"
    parts.insert(8, duplicated.format("x"))
    parts.insert(70, duplicated.format("clientX"))
    parts.insert(16, "function onShoot(e) {\n  return fire(e);\n}\n\n")
    parts.insert(48, "function onShoot(e) {\n  return fireBullet(e);\n}\n\n")
    parts.insert(24, "function onPause(a) {\n  return pause(a);\n}\n\n")
    parts.insert(56, "function onPause(action) {\n  return togglePause(action);\n}\n\n")
    _write(workspace, "app.js", "".join(parts))


def _check_duplicates_removed(workspace: Path, final: str) -> bool:
    """Each duplicated name declared once, and nothing else gone.

    The second half is what a naive check misses, and it cost this project a
    false alarm AND a real regression on consecutive days: deleting the whole
    tail of a file also leaves one definition of each, and so does deleting a
    function nobody mentioned. Counting is by declaration - INDENTED ONES
    INCLUDED - because a `grep "^function"` reads a nested definition as absent
    and turns a correct fix into an apparent data loss.
    """
    import re

    body = _read(workspace, "app.js")
    if not body:
        return False
    declared = re.findall(r"^[ \t]*function ([A-Za-z0-9_]+)", body, re.MULTILINE)
    counts = {name: declared.count(name) for name in set(declared)}
    for name in ("onMove", "onShoot", "onPause"):
        if counts.get(name) != 1:
            return False
    # Every unique function has to survive. Removing a duplicate is the task;
    # removing anything else is damage that still parses.
    if any(counts.get(f"unique_{index}") != 1 for index in range(90)):
        return False
    return body.count("{") == body.count("}")


SEED_CASES.append(
    EvalCase(
        name="removes_duplicate_definitions_without_losing_anything",
        prompt=(
            "app.js declares onMove, onShoot and onPause twice each. The second "
            "definition silently overrides the first. Remove the duplicates so "
            "each is declared once, and change nothing else."
        ),
        check=_check_duplicates_removed,
        seed=_seed_duplicate_definitions,
        tags=("repair", "duplicates", "degenerate"),
    )
)
