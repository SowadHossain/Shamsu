"""The `project.run` tool: run *this* project, not a fixed list of linters.

`check.run` ships a closed allowlist — ruff, mypy, tsc, npm build, cargo, go.
That is right for the checks every project of a given language shares, and it
is the reason four `EvidenceKind` members still had no producing tool: nothing
could apply a migration, start a service, or smoke-test a program. Outside a
lint-and-unit-test repository the harness could verify nothing at all, while
the planner's vocabulary happily mapped "verify the migration applies" onto a
requirement the gate could never open.

The shape is `check.run`'s, for the reason plan §24.3 gives: **the model picks a
key, never a command line.** There is no string for it to smuggle `; rm -rf /`
into, because there is no string. What changes is where the keys come from.

**Commands are the project's, discovered or declared.** `.shamsu/commands.toml`
is the explicit form; without one, the tool reads `package.json` scripts and
`Makefile` targets, which are the two places a project already writes down how
to run itself. Nothing is inferred from prose and nothing is composed by a
model.

**A declared command is still checked.** `.shamsu/commands.toml` is a file in
the repository, and a repository is an input — the adversarial suite's
"malicious instruction in repository docs" is the same threat wearing different
clothes. Every command goes through `security/commands.py`, which was written
for exactly this and had no caller until now: anything `CRITICAL` is refused at
load time and never becomes a key.

**Evidence is per-command and declared, never guessed.** A command says what
passing it proves. `migrate` proving `MIGRATION_APPLIED` is the project's
statement about its own command, not an inference from its name — and
`Tool.ok` still refuses any kind the contract does not declare, so the blast
radius of a wrong declaration stops at this tool's own three kinds.
"""

from __future__ import annotations

import json
import re
import shlex
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from shamsu.interfaces.cancellation import CancellationToken
from shamsu.interfaces.enums import EvidenceKind, Phase, Risk
from shamsu.interfaces.tools import ToolContract, ToolResult
from shamsu.security.commands import explain, is_blocked
from shamsu.tools.base import Tool
from shamsu.tools.process import run_process

#: Where a project declares its own commands.
COMMANDS_FILE = ".shamsu/commands.toml"

#: How much output is kept. A server that logs a line per request would
#: otherwise spend the whole context budget on a successful start-up.
MAX_OUTPUT_LINES = 60

#: What a declared command may claim to prove. Deliberately excludes
#: `TESTS_PASSED`, `LINT_PASSED`, `TYPECHECK_PASSED` and `BUILD_SUCCEEDED`:
#: those have dedicated tools whose output is *parsed*, and letting an
#: arbitrary command mint them would route around that parsing.
CLAIMABLE: Mapping[str, EvidenceKind] = {
    "smoke_test": EvidenceKind.SMOKE_TEST_PASSED,
    "health_check": EvidenceKind.HEALTH_CHECK_PASSED,
    "migration": EvidenceKind.MIGRATION_APPLIED,
}

#: Commands discovered from a project file get this, because a `package.json`
#: script called `start` says nothing about what running it proves.
_DEFAULT_PROVES = EvidenceKind.SMOKE_TEST_PASSED

#: Keys whose *name* the project has already told us the meaning of. Applied
#: only to discovered commands; a declared one says for itself.
_BY_NAME: tuple[tuple[re.Pattern[str], EvidenceKind], ...] = (
    (re.compile(r"migrat"), EvidenceKind.MIGRATION_APPLIED),
    (re.compile(r"health|ping|status"), EvidenceKind.HEALTH_CHECK_PASSED),
)


@dataclass(frozen=True)
class ProjectCommand:
    """One runnable command: what it is, and what running it proves."""

    key: str
    argv: tuple[str, ...]
    proves: EvidenceKind
    describes: str

    @property
    def rendered(self) -> str:
        return " ".join(self.argv)


def load_commands(workspace: Path) -> dict[str, ProjectCommand]:
    """Every command this project offers, declared first then discovered.

    A declaration wins over a discovery of the same key: the file is the
    project saying what it means, and the discovery is this module guessing
    from a script name.
    """
    discovered = {**_from_package_json(workspace), **_from_makefile(workspace)}
    discovered.update(_from_commands_file(workspace))
    return {key: command for key, command in discovered.items() if not is_blocked(command.rendered)}


def rejected_commands(workspace: Path) -> tuple[tuple[str, str], ...]:
    """Commands refused at load time, as `(key, why)`.

    Surfaced rather than dropped in silence. A project whose `commands.toml`
    asks for something blocked should be told, and a repository that acquired
    such an entry between runs is exactly the adversarial case worth seeing.
    """
    refused: list[tuple[str, str]] = []
    for key, command in _from_commands_file(workspace).items():
        if is_blocked(command.rendered):
            refused.append((key, explain(command.rendered)))
    return tuple(refused)


def _read(path: Path) -> str:
    """A project file's text, tolerating a byte-order mark.

    `utf-8-sig` strips a leading BOM and is otherwise identical to `utf-8`.
    Without it, a `commands.toml` saved by any Windows editor — or by
    PowerShell 5.1's `Out-File -Encoding utf8`, which writes one by default —
    fails to parse, and the caller's `except TOMLDecodeError` swallows it. The
    project then silently offers no commands at all and `project.run` is never
    registered, with nothing anywhere saying why. `json.loads` rejects a BOM
    the same way, so `package.json` is read through here too.
    """
    return path.read_text(encoding="utf-8-sig")


def _from_commands_file(workspace: Path) -> dict[str, ProjectCommand]:
    """`.shamsu/commands.toml` — the explicit declaration.

    ```toml
    [commands.migrate]
    run = "python manage.py migrate --noinput"
    proves = "migration"
    description = "apply database migrations"
    ```
    """
    path = workspace / COMMANDS_FILE
    if not path.is_file():
        return {}

    try:
        document = tomllib.loads(_read(path))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        # A malformed declaration file must not stop the run: the project
        # simply offers no commands, which is the state every project was in
        # before this tool existed.
        return {}

    section = document.get("commands")
    if not isinstance(section, dict):
        return {}

    commands: dict[str, ProjectCommand] = {}
    for key, value in section.items():
        if not isinstance(value, dict):
            continue
        line = value.get("run")
        if not isinstance(line, str) or not line.strip():
            continue
        try:
            argv = tuple(shlex.split(line))
        except ValueError:
            continue
        if not argv:
            continue

        claimed = value.get("proves")
        commands[str(key)] = ProjectCommand(
            key=str(key),
            argv=argv,
            proves=CLAIMABLE.get(str(claimed), _DEFAULT_PROVES),
            describes=str(value.get("description") or key),
        )
    return commands


def _from_package_json(workspace: Path) -> dict[str, ProjectCommand]:
    """npm scripts. The project already wrote down how to run itself."""
    path = workspace / "package.json"
    if not path.is_file():
        return {}
    try:
        document = json.loads(_read(path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}

    scripts = document.get("scripts") if isinstance(document, dict) else None
    if not isinstance(scripts, dict):
        return {}

    return {
        str(name): ProjectCommand(
            key=str(name),
            # `npm run <name>` rather than the script body: npm resolves local
            # binaries and the body may contain shell syntax this tool
            # deliberately never interprets.
            argv=("npm", "run", str(name), "--silent"),
            proves=_infer(str(name)),
            describes=f"npm run {name}",
        )
        for name in scripts
        if isinstance(name, str) and name
    }


def _from_makefile(workspace: Path) -> dict[str, ProjectCommand]:
    """Make targets, read from the left of the colon and nothing else."""
    path = workspace / "Makefile"
    if not path.is_file():
        return {}
    try:
        # `utf-8-sig` for the same reason as `_read`: a leading BOM would
        # otherwise glue itself to the first target name, so the anchored
        # pattern below would miss it.
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}

    commands: dict[str, ProjectCommand] = {}
    for match in re.finditer(r"^([A-Za-z0-9][\w.-]*)\s*:(?!=)", text, re.MULTILINE):
        target = match.group(1)
        commands[target] = ProjectCommand(
            key=target,
            argv=("make", target),
            proves=_infer(target),
            describes=f"make {target}",
        )
    return commands


def _infer(name: str) -> EvidenceKind:
    lowered = name.lower()
    for pattern, kind in _BY_NAME:
        if pattern.search(lowered):
            return kind
    return _DEFAULT_PROVES


class ProjectRunInput(BaseModel):
    command: str = Field(
        default="",
        description="Which of the project's own commands to run. A key, not a command line.",
    )


class ProjectRunTool(Tool[ProjectRunInput]):
    """Run one of the project's own commands and report what it proved."""

    input_model = ProjectRunInput

    contract = ToolContract(
        name="project.run",
        purpose=(
            "Run one of this project's own commands (start it, migrate it, "
            "smoke-test it). Choose a key from the list; there is no free-form "
            "command line."
        ),
        # VERIFY and REPAIR, matching `test.run` and `check.run`. Running the
        # project during AUTHOR would let a step claim verification before its
        # edit is finished.
        allowed_phases=frozenset({Phase.VERIFY, Phase.REPAIR}),
        # Higher than `check.run`: a linter reads, and this executes whatever
        # the project says. `approval_required` stays off because the *set* of
        # commands is bounded by the project and screened at load time —
        # the approval gate belongs on the workspace, not on every call.
        risk=Risk.HIGH,
        reversible=False,
        timeout_seconds=300.0,
        max_output_bytes=12_000,
        produces_evidence=frozenset(CLAIMABLE.values()),
    )

    def __init__(
        self, workspace: Path, commands: Mapping[str, ProjectCommand] | None = None
    ) -> None:
        self._workspace = Path(workspace).resolve()
        self._commands = dict(commands) if commands is not None else load_commands(self._workspace)

    @property
    def commands(self) -> Sequence[str]:
        return tuple(sorted(self._commands))

    @property
    def available(self) -> bool:
        """Whether this project offers anything to run.

        The registration question. A tool with no commands is one more entry in
        a list a 7B has to discriminate among on every turn — and adding a
        tenth tool to every change step measurably took the §31.1 suite from
        5/7 to 3/7. Capability is not free; an empty one is pure cost.
        """
        return bool(self._commands)

    async def run(self, arguments: ProjectRunInput, cancel: CancellationToken) -> ToolResult:
        started = time.monotonic()
        cancel.raise_if_cancelled()

        if not self._commands:
            return self.failed(
                "this project declares no runnable commands. Add "
                f"{COMMANDS_FILE} with a [commands.<name>] section, or a "
                "package.json script, or a Makefile target.",
                started=started,
            )

        command = self._commands.get(arguments.command)
        if command is None:
            return self.failed(
                f"unknown command {arguments.command!r}; this project offers: "
                f"{', '.join(self.commands)}",
                started=started,
            )

        try:
            # No per-command timeout: the gateway enforces the contract's, and a
            # second bound that nothing reads would be a promise this tool
            # cannot keep.
            completed = await run_process(command.argv, cwd=self._workspace, cancel=cancel)
        except FileNotFoundError:
            return self.failed(
                f"{command.argv[0]} is not installed or not on PATH, so "
                f"{command.describes} could not run. This is an environment "
                "problem, not a code problem.",
                started=started,
            )
        except OSError as exc:
            return self.failed(f"could not run {command.describes}: {exc}", started=started)

        body = _trim(completed.combined)

        if completed.ok:
            headline = f"{command.describes} succeeded"
            return self.ok(
                f"{headline}\n{body}" if body else headline,
                started=started,
                evidence={command.proves},
            )

        headline = f"{command.describes} failed (exit {completed.exit_code})"
        return self.failed(f"{headline}\n{body}" if body else headline, started=started)


def _trim(text: str) -> str:
    """Keep the head and tail of a long run, dropping the repetitive middle.

    Both ends carry information: a program announces what it is doing first and
    fails last, and a traceback's final line is the one that says why.
    """
    lines = text.splitlines()
    if len(lines) <= MAX_OUTPUT_LINES:
        return text.strip()

    head = lines[: MAX_OUTPUT_LINES - 20]
    tail = lines[-20:]
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join([*head, f"... [{omitted} lines omitted] ...", *tail]).strip()


__all__ = [
    "CLAIMABLE",
    "COMMANDS_FILE",
    "MAX_OUTPUT_LINES",
    "ProjectCommand",
    "ProjectRunInput",
    "ProjectRunTool",
    "load_commands",
    "rejected_commands",
]
