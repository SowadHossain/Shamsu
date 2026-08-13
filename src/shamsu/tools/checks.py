"""The `check.run` tool: lint, type-check, and build.

Seven of the eleven `EvidenceKind` members had no tool that could produce them.
The consequence was concrete rather than theoretical: `CLAIM_REQUIREMENTS` maps
`build_succeeds` onto `BUILD_SUCCEEDED`, and the planner's vocabulary maps the
phrase "lint passes" onto `LINT_PASSED` — so a plan step asking for either
acquired a requirement that **no execution could ever satisfy**. The gate
refused forever, correctly and uselessly. This closes three of the seven; the
remaining four (health check, smoke test, migration, schema) belong to
milestones that do not exist yet.

Shaped exactly like `test.run`, and for the same reason (plan §24.3): the model
picks a *command key* from a small allowlist, never a command line. There is no
string for a model to smuggle `; rm -rf /` into, because there is no string.
`security/commands.py` exists for the milestones that must eventually accept a
real command line; this is not one of them.

**Evidence is per-command, not per-tool.** A contract declares the union of
what its tool can produce, but a successful `ruff` run proves linting and
nothing else. Granting `TYPECHECK_PASSED` for it would be the exact forgery the
evidence architecture exists to prevent — so each command carries its own
evidence kind, and `Tool.ok` refuses any kind the contract did not declare.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from shamsu.interfaces.cancellation import CancellationToken
from shamsu.interfaces.enums import EvidenceKind, Phase, Risk
from shamsu.interfaces.tools import ToolContract, ToolResult
from shamsu.security.paths import PathEscape, PathSandbox
from shamsu.tools.base import Tool
from shamsu.tools.process import run_process

#: The interpreter running SHAMSU, for the same three reasons `test.run` uses
#: it: there is no `python3` on Windows (the `WindowsApps` shim prints "Python
#: was not found" and exits), a `python3` that does exist is usually not the
#: one SHAMSU runs under, and inside a venv `sys.executable` is what a
#: developer means by "the project's Python".
_PYTHON = sys.executable or "python3"

#: How much of a check's output is kept. Linters are chatty and a full run
#: against a large repository would otherwise spend the whole context budget on
#: findings the agent will fix one at a time anyway.
MAX_OUTPUT_LINES = 60


@dataclass(frozen=True)
class Check:
    """One allowlisted check: what to run, and what passing it proves."""

    argv: tuple[str, ...]
    evidence: EvidenceKind
    describes: str

    #: Whether a target path may be appended. False for whole-project commands
    #: (`npm run build`) where a trailing path is meaningless or harmful.
    accepts_target: bool = True


#: Command key -> what it runs. Nothing here interpolates model input; a target
#: path, when given, is sandbox-resolved and appended as a separate argument.
DEFAULT_CHECKS: Mapping[str, Check] = {
    "ruff": Check(
        argv=(_PYTHON, "-m", "ruff", "check"),
        evidence=EvidenceKind.LINT_PASSED,
        describes="ruff lint",
    ),
    "ruff-format": Check(
        argv=(_PYTHON, "-m", "ruff", "format", "--check"),
        evidence=EvidenceKind.LINT_PASSED,
        describes="ruff formatting",
    ),
    "mypy": Check(
        argv=(_PYTHON, "-m", "mypy"),
        evidence=EvidenceKind.TYPECHECK_PASSED,
        describes="mypy type check",
    ),
    "python-compile": Check(
        argv=(_PYTHON, "-m", "compileall", "-q"),
        evidence=EvidenceKind.BUILD_SUCCEEDED,
        describes="python bytecode compile",
    ),
    "eslint": Check(
        argv=("npx", "--no-install", "eslint", "."),
        evidence=EvidenceKind.LINT_PASSED,
        describes="eslint",
    ),
    "tsc": Check(
        argv=("npx", "--no-install", "tsc", "--noEmit"),
        evidence=EvidenceKind.TYPECHECK_PASSED,
        describes="typescript type check",
        accepts_target=False,
    ),
    "npm-build": Check(
        argv=("npm", "run", "build", "--silent"),
        evidence=EvidenceKind.BUILD_SUCCEEDED,
        describes="npm build",
        accepts_target=False,
    ),
    "cargo-check": Check(
        argv=("cargo", "check", "--quiet"),
        evidence=EvidenceKind.TYPECHECK_PASSED,
        describes="cargo check",
        accepts_target=False,
    ),
    "cargo-build": Check(
        argv=("cargo", "build", "--quiet"),
        evidence=EvidenceKind.BUILD_SUCCEEDED,
        describes="cargo build",
        accepts_target=False,
    ),
    "go-build": Check(
        argv=("go", "build", "./..."),
        evidence=EvidenceKind.BUILD_SUCCEEDED,
        describes="go build",
        accepts_target=False,
    ),
}


class CheckRunInput(BaseModel):
    command: str = Field(
        default="ruff",
        description="Which allowlisted check to run. A key, not a shell command line.",
    )
    target: str = Field(
        default="",
        description="Optional file or directory to narrow the check to. Workspace-relative.",
    )


class CheckRunTool(Tool[CheckRunInput]):
    """Run a lint, type-check, or build command and report what it proved."""

    input_model = CheckRunInput

    contract = ToolContract(
        name="check.run",
        purpose=(
            "Run a lint, type-check, or build check. Choose a command key (e.g. "
            "'ruff', 'mypy', 'npm-build') and optionally narrow it to a path."
        ),
        # VERIFY and REPAIR, matching `test.run`. Running a check during AUTHOR
        # would let a step claim verification before its edit is finished.
        allowed_phases=frozenset({Phase.VERIFY, Phase.REPAIR}),
        risk=Risk.MEDIUM,
        reversible=True,
        timeout_seconds=600.0,
        max_output_bytes=12_000,
        # The union of what any allowlisted command can prove. One run grants
        # only its own command's kind -- see the module docstring.
        produces_evidence=frozenset(
            {
                EvidenceKind.LINT_PASSED,
                EvidenceKind.TYPECHECK_PASSED,
                EvidenceKind.BUILD_SUCCEEDED,
            }
        ),
    )

    def __init__(
        self,
        workspace: Path,
        checks: Mapping[str, Check] | None = None,
    ) -> None:
        self._workspace = Path(workspace).resolve()
        self._sandbox = PathSandbox(workspace)
        self._checks: dict[str, Check] = dict(checks or DEFAULT_CHECKS)

    @property
    def commands(self) -> Sequence[str]:
        return tuple(sorted(self._checks))

    async def run(self, arguments: CheckRunInput, cancel: CancellationToken) -> ToolResult:
        started = time.monotonic()
        cancel.raise_if_cancelled()

        check = self._checks.get(arguments.command)
        if check is None:
            return self.failed(
                f"unknown check {arguments.command!r}; available: {', '.join(self.commands)}",
                started=started,
            )

        argv = check.argv
        if arguments.target:
            if not check.accepts_target:
                return self.failed(
                    f"{arguments.command} runs against the whole project and takes no target",
                    started=started,
                )
            try:
                resolved = self._sandbox.resolve(arguments.target)
            except PathEscape as exc:
                return self.failed(str(exc), started=started)
            if not resolved.exists():
                return self.failed(
                    f"{arguments.target}: no such file or directory", started=started
                )
            argv = (*argv, self._sandbox.relative(resolved))

        try:
            completed = await run_process(argv, cwd=self._workspace, cancel=cancel)
        except FileNotFoundError:
            # Naming the executable matters: "ruff is not installed" is
            # actionable, and "check failed" is not.
            return self.failed(
                f"{argv[0]} is not installed or not on PATH, so {check.describes} "
                "could not run. This is an environment problem, not a code problem.",
                started=started,
            )
        except OSError as exc:
            return self.failed(f"could not run {check.describes}: {exc}", started=started)

        body = _trim(completed.combined)

        if completed.ok:
            headline = f"{check.describes} passed"
            output = f"{headline}\n{body}" if body else headline
            # Only this command's evidence. `Tool.ok` verifies it against the
            # contract, so a mistyped kind fails loudly rather than quietly
            # granting proof of something that was never checked.
            return self.ok(output, started=started, evidence={check.evidence})

        # A failing check is a successful *tool* call with a negative result.
        # `failed` attaches no evidence, which is exactly right.
        headline = f"{check.describes} failed (exit {completed.exit_code})"
        return self.failed(f"{headline}\n{body}" if body else headline, started=started)


def _trim(text: str) -> str:
    """Keep the head and tail of a long report, dropping the repetitive middle.

    Both ends carry information a linter run needs: the first findings are what
    the agent will fix, and the last line is usually the count that says whether
    it is nearly done or has barely started.
    """
    lines = text.splitlines()
    if len(lines) <= MAX_OUTPUT_LINES:
        return text.strip()

    head = lines[: MAX_OUTPUT_LINES - 15]
    tail = lines[-15:]
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join([*head, f"... [{omitted} lines omitted] ...", *tail]).strip()


__all__ = ["DEFAULT_CHECKS", "MAX_OUTPUT_LINES", "Check", "CheckRunInput", "CheckRunTool"]
