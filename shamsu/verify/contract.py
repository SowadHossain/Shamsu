"""What the prompt promised, checked against what the run actually did.

`action_ledger.store.validate_run` answers "is this run well-formed?" - every
artifact present, every JSONL line parseable. It passed on all seven prompts of
the 2026-07-20 dogfood, including the run that built the wrong product and the
one that destroyed a file. Structural validity says nothing about whether the
agent did the job.

This module adds the missing half: a small contract derived from the prompt
before the run, checked against filesystem evidence after it.

    "Create shamsu_smoke_note.md ... do not modify any other files"
      -> requested: shamsu_smoke_note.md, scoped: yes
      -> FAIL if that file was not created
      -> FAIL if anything else changed

Deliberately narrow. It only asserts things the prompt states outright - which
files were named, whether changes were forbidden - because a check that guesses
at intent produces false failures, and a validation layer nobody trusts is
worse than none. Semantic correctness of file CONTENT is out of scope; that
needs the verify gate or a human.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from shamsu.safety import read_only

# A file-like token: `notes.md`, `src/app.tsx`, `client\\src\\Page.js`.
#
# The extension must be all-lowercase or all-uppercase. Title-case is the
# signature of a missing space after a full stop - "this workspace.Put one
# sentence in it" otherwise yields a requested file called `workspace.Put`, and
# the contract then fails a perfectly good run because a phantom file was never
# written. Real extensions do not look like that; sentence boundaries do.
_FILE_TOKEN_RE = re.compile(
    r"[\w][\w./\\-]*\.(?:[a-z0-9_]{1,12}|[A-Z0-9_]{1,12})\b"
)
# Extensionless dotfiles, which the pattern above cannot match (`.gitignore` is
# a dot plus a name, with no trailing `.ext`). A NAMED allowlist rather than a
# loose `\.[a-z]+` alternative on purpose: a loose one would also match the
# ".Put" in "workspace.Put one sentence in it", inventing a requested file and
# failing the contract over a typo. A check that guesses is worse than no check.
_DOTFILE_RE = re.compile(
    r"\.(?:gitignore|gitattributes|gitmodules|dockerignore|editorconfig|env|"
    r"npmrc|nvmrc|prettierrc|eslintrc|babelrc)\b",
    re.IGNORECASE,
)
# Verbs that make a named file the TARGET of the request rather than a
# reference to read. "explain qa_probe.py" names a file but requests no change.
_WRITE_VERB_RE = re.compile(
    r"\b(creat|writ|sav|add|generat|mak|edit|updat|modif|chang|fix|delet|remov|renam|mov)"
    r"(?:e|es|ed|ing)?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RunContract:
    """The checkable promises in a prompt."""

    read_only: bool = False
    scoped_read_only: bool = False
    requested_paths: tuple[str, ...] = ()
    dry_run: bool = False

    @property
    def has_expectations(self) -> bool:
        return bool(
            self.read_only or self.scoped_read_only or self.requested_paths or self.dry_run
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_only": self.read_only,
            "scoped_read_only": self.scoped_read_only,
            "requested_paths": list(self.requested_paths),
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True)
class ContractResult:
    ok: bool
    checked: bool
    contract: dict[str, Any] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "contract": self.contract,
            "checks": self.checks,
            "violations": self.violations,
        }


def _strip_leading_dot_slash(path: str) -> str:
    """Drop a leading `./` without eating a dotfile's dot.

    `lstrip("./")` strips any leading `.` or `/` CHARACTER, so `.gitignore`
    becomes `gitignore` and stops matching itself - a real mismatch, since
    `.gitignore` is one of the files a stray build actually creates.
    """
    while path.startswith("./"):
        path = path[2:]
    return path.lstrip("/")


def _normalize(path: str) -> str:
    return _strip_leading_dot_slash(str(path or "").replace("\\", "/")).lower()


# A filename right after one of these is a SOURCE being read, not a target to
# write: "the app described in PRD.md", "build from spec.md", "based on
# README". Counting it as a requested write target failed a legitimate PRD
# build - "Build the converter described in PRD.md. Create converter.py" flagged
# PRD.md (the source) as an unwritten output. Observed live 2026-07-21.
#
# Bare "in"/"of"/"within"/"inside" are DELIBERATELY excluded: "In calc.py, fix
# the subtract function" makes calc.py the TARGET (the file being edited), not a
# source - and excluding it let a no-op fix pass the contract silently. The PRD
# case is still covered by `_SOURCE_FILENAME_RE` (prd/readme/spec) regardless of
# preposition, so only the genuinely source-marking phrases stay here.
_SOURCE_PREPOSITION_RE = re.compile(
    r"\b(?:from|per|using|described\s+in|defined\s+in|based\s+on|"
    r"according\s+to|read|reading|see|refer\s+to|referenced\s+in)\s+$",
    re.IGNORECASE,
)
# Spec/source documents are inputs to a build, never its output.
_SOURCE_FILENAME_RE = re.compile(r"(?:^|/)(?:prd|readme|spec|requirements?)\b", re.IGNORECASE)


def _is_source_reference(text: str, token_start: int, candidate: str) -> bool:
    if _SOURCE_FILENAME_RE.search(candidate):
        return True
    preceding = text[:token_start]
    return bool(_SOURCE_PREPOSITION_RE.search(preceding))


def requested_paths(prompt: str) -> tuple[str, ...]:
    """Files the prompt asks to be created or changed, in order of appearance.

    Read-only clauses are masked first: "do not modify any other files" must
    never contribute a target, and its own wording carries no filename anyway.
    A file named only as a SOURCE ("described in PRD.md", "from spec.md") is an
    input, not an output, and is excluded - otherwise a PRD build fails its own
    contract for not "writing" the PRD it was reading.
    """
    text = read_only.strip(prompt or "")
    if not _WRITE_VERB_RE.search(text):
        return ()
    spans: list[tuple[int, str]] = [
        (match.start(), _strip_leading_dot_slash(match.group(0).replace("\\", "/")))
        for match in _FILE_TOKEN_RE.finditer(text)
    ]
    spans.extend((match.start(), match.group(0)) for match in _DOTFILE_RE.finditer(text))
    seen: list[str] = []
    for position, candidate in sorted(spans):
        if candidate in seen or _is_source_reference(text, position, candidate):
            continue
        seen.append(candidate)
    return tuple(seen)


def derive(prompt: str, *, dry_run: bool = False) -> RunContract:
    """Read the prompt's checkable promises. Never touches the model."""
    return RunContract(
        read_only=read_only.applies(prompt),
        scoped_read_only=read_only.is_scoped(prompt),
        requested_paths=requested_paths(prompt),
        dry_run=bool(dry_run),
    )


def check(
    contract: RunContract,
    *,
    changed_files: Iterable[dict[str, str]],
    planned_mutations: Iterable[dict[str, Any]] = (),
) -> ContractResult:
    """Compare the contract against what the filesystem shows actually happened.

    `changed_files` is the headless before/after hash diff - real evidence, not
    the model's account of itself, which is the entire point.
    """
    changed = [dict(entry) for entry in changed_files]
    changed_paths = {_normalize(entry.get("path", "")) for entry in changed}
    changed_paths.discard("")
    wanted = {_normalize(path) for path in contract.requested_paths}
    planned = [dict(entry) for entry in planned_mutations]

    checks: list[dict[str, Any]] = []
    violations: list[str] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": passed, "detail": detail})
        if not passed:
            violations.append(f"{name}: {detail}")

    if not contract.has_expectations:
        return ContractResult(ok=True, checked=False, contract=contract.to_dict())

    if contract.read_only:
        record(
            "read_only_respected",
            not changed_paths,
            "no files changed"
            if not changed_paths
            else f"prompt forbade file changes but {len(changed_paths)} changed: "
            + ", ".join(sorted(changed_paths)),
        )

    if contract.dry_run:
        record(
            "dry_run_changed_nothing",
            not changed_paths,
            "no files changed"
            if not changed_paths
            else "dry run modified the workspace: " + ", ".join(sorted(changed_paths)),
        )
        record(
            "dry_run_produced_a_plan",
            bool(planned) or not wanted,
            f"{len(planned)} planned change(s)"
            if planned
            else "the prompt asked for a file change but the dry run planned none",
        )

    if contract.scoped_read_only and wanted:
        collateral = sorted(changed_paths - wanted)
        record(
            "only_requested_files_changed",
            not collateral,
            "no collateral changes"
            if not collateral
            else "prompt allowed only " + ", ".join(sorted(wanted))
            + " but these also changed: " + ", ".join(collateral),
        )

    if wanted and not contract.read_only and not contract.dry_run:
        missing = sorted(wanted - changed_paths)
        record(
            "requested_files_were_written",
            not missing,
            "all requested files written"
            if not missing
            else "the prompt asked for " + ", ".join(missing) + " but "
            + (
                "nothing was written"
                if not changed_paths
                else "these were written instead: " + ", ".join(sorted(changed_paths))
            ),
        )

    return ContractResult(
        ok=not violations,
        checked=True,
        contract=contract.to_dict(),
        checks=checks,
        violations=violations,
    )
