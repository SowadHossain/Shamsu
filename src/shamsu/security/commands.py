"""Command risk classification.

Migrated from `legacy-code/shamsu/safety/commands.py:classify_command` with two
deliberate changes, both in the safe direction.

**An unrecognised command is HIGH, not MEDIUM.** v1 defaulted unknown commands
to MEDIUM with the comment "unknown commands default to requiring approval" —
but MEDIUM was also the level for `pip install`, so an unrecognised command and
a routine dependency install were indistinguishable to every policy above.
Unknown means *unknown*: the classifier has nothing to say about it, and that
deserves the strongest gate rather than a middling one.

**Blocked patterns are matched on the raw string, before normalisation.** v1
normalised first and matched blocked patterns on the original — nearly right,
but the ordering was incidental rather than stated. Here it is the first thing
that happens and it is documented, because a normalisation rule that
accidentally rewrote `sudo rm -rf /` into something unrecognised would
otherwise turn a block into an approval prompt.

**Where this is used.** Not by `test.run`, which takes an allowlisted command
*key* and never a string — plan §24.3, and the reason there is no injection
surface there. This exists for the milestones that must accept a real command
line (Docker, database migrations, package managers) and for classifying
commands read out of a project's own manifests before offering them.
"""

from __future__ import annotations

import re

from shamsu.interfaces.enums import Risk

#: Commands that read or test without changing anything outside build output.
SAFE_PREFIXES: frozenset[str] = frozenset(
    {
        "pytest",
        "python -m pytest",
        "python manage.py test",
        "npm test",
        "npm run build",
        "npm run lint",
        "cargo test",
        "cargo check",
        "go test",
        "git status",
        "git diff",
        "git log",
        "git show",
        "make test",
        "ruff check",
        "mypy",
    }
)

#: Commands that change the environment but are ordinary development actions.
MEDIUM_PREFIXES: frozenset[str] = frozenset(
    {
        "pip install",
        "pip3 install",
        "python -m pip install",
        "python -m venv",
        "uv venv",
        "uv pip install",
        "npm install",
        "npm ci",
        "poetry add",
        "cargo build",
        "go build",
        "git checkout",
        "git merge",
        "git stash",
        "python manage.py migrate",
        "python manage.py makemigrations",
        "alembic upgrade",
    }
)

#: Never run, whatever the approval policy says. Matched against the raw
#: command before any normalisation.
#:
#: `sudo` and `su` carry word boundaries. v1's `r"sudo"` was unanchored, so
#: `python sudoku.py` was blocked as a privilege escalation. Harmless in
#: direction — a false block is safe — but a rule that fires on nonsense is a
#: rule someone eventually relaxes, and this is not one to relax.
BLOCKED_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"rm\s+-[a-z]*r[a-z]*f?\s+[/~]",
        r"rm\s+-[a-z]*f[a-z]*r?\s+[/~]",
        r"\bsudo\b",
        r"\bsu\s",
        r"chmod\s+-R\s+777",
        r"\bdd\s+if=",
        r"\bmkfs\b",
        r"\bshutdown\b",
        r"\breboot\b",
        r"kill\s+-9\s+-1",
        r":\(\)\{.*\}",  # fork bomb
        r"curl[^|]*\|\s*(ba)?sh",
        r"wget[^|]*\|\s*(ba)?sh",
        r">\s*/dev/sd",
        r"\bgit\s+push\s+.*--force",
        r"\bgit\s+reset\s+--hard\s+origin",
    )
)

#: Wrappers that do not change what a command *does*. Stripped so
#: `poetry run pytest` classifies as the `pytest` it is.
_WRAPPERS = re.compile(r"^(?:poetry|uv|pdm|hatch)\s+run\s+", re.IGNORECASE)
_VENV_PYTHON = re.compile(
    r"^(?:\"[^\"]*[\\/])?\.?venv[\\/](?:Scripts|bin)[\\/](?P<tool>python|pip)(?:\.exe)?\"?",
    re.IGNORECASE,
)

#: Shell syntax that visibly writes. Used to answer "would this mutate the
#: workspace?" separately from "how risky is it?" -- a SAFE command with a
#: redirection is not safe.
_WRITES = re.compile(r"(?<![<>])>{1,2}(?!&)|\btee\b|\bmv\b|\bcp\b|\brm\b|\bmkdir\b|\btruncate\b")


def classify_command(command: str) -> Risk:
    """Risk level for a shell command.

    Ordered: blocked first on the raw text, then the write check, then the
    allowlists. An unrecognised command is `Risk.HIGH`.
    """
    raw = command or ""

    if any(pattern.search(raw) for pattern in BLOCKED_PATTERNS):
        return Risk.CRITICAL

    view = _normalise(raw)
    if not view:
        return Risk.HIGH

    if any(view.startswith(prefix) for prefix in SAFE_PREFIXES):
        # A read-only command with a redirection is not read-only. Checked
        # after the allowlist rather than before, so the reason is "it writes"
        # rather than "it is unknown".
        return Risk.MEDIUM if writes_to_workspace(raw) else Risk.LOW

    if any(view.startswith(prefix) for prefix in MEDIUM_PREFIXES):
        return Risk.MEDIUM

    return Risk.HIGH


def writes_to_workspace(command: str) -> bool:
    """Whether shell syntax in the command visibly writes files."""
    return bool(_WRITES.search(command or ""))


def is_blocked(command: str) -> bool:
    """Whether the command is refused outright, whatever the approval policy."""
    return classify_command(command) is Risk.CRITICAL


def explain(command: str) -> str:
    """Why a command got its classification, in one line for a refusal message."""
    risk = classify_command(command)
    if risk is Risk.CRITICAL:
        return "This command is blocked outright and cannot be approved."
    if risk is Risk.HIGH:
        return (
            "This command is not on any allowlist. Unrecognised commands need "
            "explicit approval every time."
        )
    if risk is Risk.MEDIUM:
        if writes_to_workspace(command):
            return "This command writes to the workspace; it needs approval."
        return "This command changes the environment; it needs approval."
    return "This command only reads or tests."


def _normalise(command: str) -> str:
    """Strip wrappers that do not change what runs. Never used for blocking."""
    view = _WRAPPERS.sub("", command.strip())
    view = _VENV_PYTHON.sub(lambda match: match.group("tool"), view)
    return " ".join(view.lower().split())


__all__ = [
    "BLOCKED_PATTERNS",
    "MEDIUM_PREFIXES",
    "SAFE_PREFIXES",
    "classify_command",
    "explain",
    "is_blocked",
    "writes_to_workspace",
]
