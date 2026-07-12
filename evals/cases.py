"""Seed eval cases — one per real request path the agent loop drives.

Each case starts from a small, controlled workspace and is scored by a
deterministic check against the resulting files and/or the agent's final answer.
Checks are intentionally lenient about phrasing (small models vary) but strict
about the observable effect: the file exists / contains the change / a question
was asked.
"""
from __future__ import annotations

from pathlib import Path

from evals.harness import EvalCase


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
    lowered = final.lower()
    return "exit 0" in lowered or "exited with 0" in lowered or "no error" in lowered or "success" in lowered


# --- ask_user clarification ---------------------------------------------------

def _seed_ask(workspace: Path) -> None:
    _write(workspace, "client/src/App.tsx", "// client\n")
    _write(workspace, "web/src/App.tsx", "// web\n")


def _check_ask(workspace: Path, final: str) -> bool:
    # Ambiguous target -> the agent should ask which file, not guess/edit blindly.
    return "?" in final or "which" in final.lower()


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
]
