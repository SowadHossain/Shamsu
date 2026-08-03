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
files were named, whether changes were forbidden, and explicitly named Python
symbols - because a check that guesses at intent produces false failures, and a
validation layer nobody trusts is worse than none. General semantic correctness
of file content remains the verify gate's job.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
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
    r"(?:[A-Za-z]:[\\/])?[\w][\w./\\-]*\.(?:[a-z0-9_]{1,12}|[A-Z0-9_]{1,12})\b"
)
_NON_PATH_FILE_TOKENS = {
    "bun.js",
    "d3.js",
    "deno.js",
    "node.js",
    "next.js",
    "nuxt.js",
    "three.js",
    "vue.js",
}
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
_NAMED_DIRECTORY_RE = re.compile(
    r"\b(?:new\s+)?(?:folder|directory)\s+(?:named|called)\s+"
    r"[\x60\"']?(?P<path>[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)*)[\x60\"']?",
    re.IGNORECASE,
)
# Verbs that make a named file the TARGET of the request rather than a
# reference to read. "explain qa_probe.py" names a file but requests no change.
_WRITE_VERB_RE = re.compile(
    r"\b(creat|writ|sav|add|generat|mak|edit|updat|modif|chang|fix|delet|remov|renam|mov|"
    r"build|implement|scaffold|seed)"
    r"(?:e|es|ed|ing)?\b",
    re.IGNORECASE,
)
_PYTHON_FUNCTION_REQUEST_RE = re.compile(
    r"\b(?:add|create|write|implement|define)\s+"
    r"(?:(?:a|an|the)\s+)?"
    r"(?P<symbol>[A-Za-z_]\w*)\s*\([^)]*\)\s+"
    r"(?:function|method)\s+(?:to|in|inside)\s+"
    r"(?P<path>(?:[A-Za-z]:[\\/])?[\w][\w./\\-]*\.py)\b",
    re.IGNORECASE,
)
_TEST_SOURCE_PREPOSITION_RE = re.compile(
    r"\b(?:write|generate|create|add)\s+(?:pytest\s+)?tests?\s+for\s+$",
    re.IGNORECASE,
)
_TEST_GENERATION_RE = re.compile(
    r"\b(?:write|generate|create|add)\s+(?:pytest\s+)?tests?\s+for\s+"
    r"(?P<path>(?:[A-Za-z]:[\\/])?[\w][\w./\\-]*\.(?:py|js|jsx|ts|tsx|mjs|cjs))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RunContract:
    """The checkable promises in a prompt."""

    read_only: bool = False
    scoped_read_only: bool = False
    requested_paths: tuple[str, ...] = ()
    required_python_symbols: tuple[tuple[str, str], ...] = ()
    allowed_collateral_paths: tuple[str, ...] = ()
    dry_run: bool = False

    @property
    def has_expectations(self) -> bool:
        return bool(
            self.read_only
            or self.scoped_read_only
            or self.requested_paths
            or self.required_python_symbols
            or self.dry_run
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_only": self.read_only,
            "scoped_read_only": self.scoped_read_only,
            "requested_paths": list(self.requested_paths),
            "required_python_symbols": [
                {"path": path, "symbol": symbol}
                for path, symbol in self.required_python_symbols
            ],
            "allowed_collateral_paths": list(self.allowed_collateral_paths),
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
    r"according\s+to|read|reading|see|refer\s+to|referenced\s+in|"
    r"(?:existing\s+)?(?:insert|query|declaration|definition|configuration|schema)\s+in)\s+$",
    re.IGNORECASE,
)
# Spec/source documents are inputs to a build, never its output. The marker
# word can sit anywhere in the basename separated by _ - . or / (a `\b` alone
# misses `CANVAS_LITE_PRD.pdf` because underscores are word characters - that
# exact name was counted as an unwritten output in the 2026-08-01 dogfood).
_SOURCE_FILENAME_RE = re.compile(
    r"(?:^|[/_\-. ])(?:prd|readme|spec|requirements?)(?:[/_\-. ]|$)", re.IGNORECASE
)
# Document formats SHAMSU reads but never writes; a .pdf named in a build
# prompt is requirements input, not a deliverable.
_NON_OUTPUT_EXTENSIONS = {".pdf", ".docx", ".doc"}
_SOURCE_PREDICATE_RE = re.compile(
    r"^\s+(?:declares?|defines?|contains?|documents?|describes?|shows?|lists?|"
    r"specifies?|says?|has|uses)\b",
    re.IGNORECASE,
)
_QUOTED_SOURCE_PREFIX_RE = re.compile(
    r"\b(?:from|using|read|reading|see|refer\s+to)\s+[\x60\"'][^\x60\"']*$",
    re.IGNORECASE,
)
_QUOTED_RUNNER_COMMAND_RE = re.compile(
    r"(?P<quote>[`'\"])(?:python3?|py|pytest|npm|npx|node|pnpm|yarn|cargo|go|dotnet)\b"
    r"[^`'\"\n]*(?P=quote)",
    re.IGNORECASE,
)
# A file token sitting in the argument position of an UNQUOTED runner command
# ("then run python manage.py check") is the command's input, not a file the
# prompt asks to be written; without this, manage.py became a phantom requested
# output. Matched against the text immediately before the token: an execution
# verb, a runner, then only command-shaped tokens (flags, dotted/slashed
# paths, or another runner name) - a bare prose word in between ("with python
# and save it as game.py") breaks the chain so real targets are kept.
_RUNNER_COMMAND_PREFIX_RE = re.compile(
    r"\b(?:run(?:ning)?|rerun|re-run|execut(?:e|ing)|invok(?:e|ing)|via|with)\s+"
    r"(?:python3?|py|pytest|npm|npx|node|pnpm|yarn|cargo|go|dotnet)\b"
    r"(?:\s+(?:-{1,2}[\w=-]+|[\w.\\/-]*[./\\][\w.\\/-]*|python3?|py|pytest|npm|npx|node|pnpm|yarn|cargo|go|dotnet)){0,4}"
    r"\s+$",
    re.IGNORECASE,
)
_SOURCE_DOCUMENT_PREFIX_RE = re.compile(
    r"\b(?:read|open|inspect|use|using|from)\b[^.!?\n]{0,80}"
    r"\b(?:prd|product\s+requirements?|requirements?\s+document|spec(?:ification)?)\b"
    r"[^.!?\n]{0,40}$",
    re.IGNORECASE,
)
_PROTECTED_PATH_PREFIX_RE = re.compile(
    r"\b(?:canonical|protected|preserved|retained)\b[^.!?\n]{0,60}"
    r"(?:file|path)?\s*(?:is|:)?\s*['\"`]?$"
    r"|\b(?:keep|preserve|retain|leave)\b[^.!?\n]{0,50}$",
    re.IGNORECASE,
)


def _names_a_file(text: str, token_start: int, candidate: str) -> bool:
    """Whether a dotted token is a real path rather than a module or attribute.

    `_FILE_TOKEN_RE` accepts anything shaped `name.ext`, which is also the shape
    of `config.settings`, `django.contrib.admin` and `sys.argv`. A prompt that
    merely NAMES a settings module ("set DJANGO_SETTINGS_MODULE to
    config.settings ... write manage.py") therefore promised two files it never
    meant, and every such run failed its own contract while doing exactly what
    was asked. The routing layer already solved this for its own extractor;
    this reuses that judgement so the two cannot drift apart.
    """
    from shamsu.routing.operations import _IMPORT_CONTEXT_RE, looks_like_real_file

    if not looks_like_real_file(candidate):
        return False
    return not _IMPORT_CONTEXT_RE.search(text[:token_start])


def _is_source_reference(text: str, token_start: int, candidate: str) -> bool:
    if _SOURCE_FILENAME_RE.search(candidate):
        return True
    preceding = text[:token_start]
    following = text[token_start + len(candidate) :]
    return bool(
        _SOURCE_PREPOSITION_RE.search(preceding)
        or _QUOTED_SOURCE_PREFIX_RE.search(preceding)
        or _SOURCE_DOCUMENT_PREFIX_RE.search(preceding)
        or _TEST_SOURCE_PREPOSITION_RE.search(preceding)
        or _SOURCE_PREDICATE_RE.search(following)
        or _PROTECTED_PATH_PREFIX_RE.search(preceding)
        or _RUNNER_COMMAND_PREFIX_RE.search(preceding)
    )


def _requested_path(candidate: str, workspace: Path | None) -> str:
    candidate_path = Path(candidate)
    if workspace is not None and candidate_path.is_absolute():
        try:
            return candidate_path.resolve().relative_to(Path(workspace).resolve()).as_posix()
        except ValueError:
            pass
    return _strip_leading_dot_slash(candidate.replace("\\", "/"))


def requested_paths(prompt: str, workspace: Path | None = None) -> tuple[str, ...]:
    """Files the prompt asks to be created or changed, in order of appearance.

    Read-only clauses are masked first: "do not modify any other files" must
    never contribute a target, and its own wording carries no filename anyway.
    A file named only as a SOURCE ("described in PRD.md", "from spec.md") is an
    input, not an output, and is excluded - otherwise a PRD build fails its own
    contract for not "writing" the PRD it was reading.
    """
    text = _QUOTED_RUNNER_COMMAND_RE.sub(" ", read_only.strip(prompt or ""))
    if not _WRITE_VERB_RE.search(text):
        return ()
    spans: list[tuple[int, str]] = [
        (match.start(), _requested_path(match.group(0), workspace))
        for match in _FILE_TOKEN_RE.finditer(text)
        if _names_a_file(text, match.start(), match.group(0))
    ]
    spans.extend((match.start(), match.group(0)) for match in _DOTFILE_RE.finditer(text))
    spans.extend(
        (match.start("path"), _requested_path(match.group("path").rstrip("."), workspace))
        for match in _NAMED_DIRECTORY_RE.finditer(text)
    )
    seen: list[str] = []
    for position, candidate in sorted(spans):
        if (
            candidate in seen
            or _normalize(candidate) in _NON_PATH_FILE_TOKENS
            or Path(candidate).suffix.lower() in _NON_OUTPUT_EXTENSIONS
            or _is_source_reference(text, position, candidate)
        ):
            continue
        seen.append(candidate)
    return tuple(seen)


def derive(
    prompt: str, *, dry_run: bool = False, workspace: Path | None = None
) -> RunContract:
    """Read the prompt's checkable promises. Never touches the model."""
    paths = list(requested_paths(prompt, workspace))
    for path in generated_test_paths(prompt, workspace):
        if path not in paths:
            paths.append(path)
    return RunContract(
        read_only=read_only.applies(prompt),
        scoped_read_only=read_only.is_scoped(prompt),
        requested_paths=tuple(paths),
        required_python_symbols=required_python_symbols(prompt, workspace),
        allowed_collateral_paths=allowed_collateral_paths(prompt),
        dry_run=bool(dry_run),
    )


def generated_test_paths(
    prompt: str,
    workspace: Path | None = None,
) -> tuple[str, ...]:
    """Return deterministic test outputs implied by an explicit source path."""
    outputs: list[str] = []
    for match in _TEST_GENERATION_RE.finditer(read_only.strip(prompt or "")):
        source = _requested_path(match.group("path"), workspace)
        path = Path(source)
        name = path.name.casefold()
        if name.startswith("test_") or ".test." in name or ".spec." in name:
            output = source
        elif path.suffix.casefold() == ".py":
            output = f"tests/test_{path.stem}.py"
        else:
            output = f"tests/{path.stem}.test{path.suffix}"
        if output not in outputs:
            outputs.append(output)
    return tuple(outputs)


def required_python_symbols(
    prompt: str,
    workspace: Path | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return Python functions the prompt explicitly requires in a named file."""
    text = read_only.strip(prompt or "")
    seen: list[tuple[str, str]] = []
    for match in _PYTHON_FUNCTION_REQUEST_RE.finditer(text):
        item = (
            _requested_path(match.group("path"), workspace),
            match.group("symbol"),
        )
        if item not in seen:
            seen.append(item)
    return tuple(seen)


def allowed_collateral_paths(prompt: str) -> tuple[str, ...]:
    """Workspace paths explicitly exempted from a scoped no-collateral clause."""
    lowered = (prompt or "").lower()
    allowed: list[str] = []
    if ".shamsu" in lowered:
        allowed.append(".shamsu")
    if ".cbmignore" in lowered:
        allowed.append(".cbmignore")
    return tuple(allowed)


def check(
    contract: RunContract,
    *,
    changed_files: Iterable[dict[str, str]],
    planned_mutations: Iterable[dict[str, Any]] = (),
    outcome: str = "",
    workspace: Path | None = None,
) -> ContractResult:
    """Compare the contract against what the filesystem shows actually happened.

    `changed_files` is the headless before/after hash diff - real evidence, not
    the model's account of itself, which is the entire point.
    """
    changed = [dict(entry) for entry in changed_files]
    changed_paths = {_normalize(entry.get("path", "")) for entry in changed}
    changed_paths.discard("")
    wanted = {_normalize(path) for path in contract.requested_paths}
    allowed_collateral = {_normalize(path) for path in contract.allowed_collateral_paths}
    planned = [dict(entry) for entry in planned_mutations]
    planned_paths = {
        _normalize(str(entry.get("path") or entry.get("filepath") or ""))
        for entry in planned
    }
    planned_paths.discard("")

    def within(path: str, scope: str) -> bool:
        if path == scope or path.startswith(scope.rstrip("/") + "/"):
            return True
        # A user may name a sole project-root file by basename while the tool
        # records its path under an explicitly named project directory.
        return "/" not in scope and path.endswith("/" + scope)

    def covered(scope: str, paths: set[str]) -> bool:
        return any(within(path, scope) for path in paths)

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
        if wanted and planned:
            unexpected = sorted(
                path for path in planned_paths if not any(within(path, scope) for scope in wanted)
            )
            missing = sorted(scope for scope in wanted if not covered(scope, planned_paths))
            record(
                "dry_run_planned_requested_paths",
                not unexpected and not missing,
                "planned paths match the request"
                if not unexpected and not missing
                else "planned paths drifted from the request"
                + (f"; unexpected: {', '.join(unexpected)}" if unexpected else "")
                + (f"; missing: {', '.join(missing)}" if missing else ""),
            )

    if contract.scoped_read_only and wanted:
        collateral = sorted(
            path
            for path in changed_paths
            if not any(
                within(path, scope) for scope in (wanted | allowed_collateral)
            )
        )
        record(
            "only_requested_files_changed",
            not collateral,
            "no collateral changes"
            if not collateral
            else "prompt allowed only " + ", ".join(sorted(wanted))
            + " but these also changed: " + ", ".join(collateral),
        )

    terminal_without_execution = not changed_paths and outcome in {
        "denied",
        "cancelled",
        "timed_out",
        "needs_input",
        "failed",
    }
    if wanted and not contract.read_only and not contract.dry_run and not terminal_without_execution:
        missing = sorted(scope for scope in wanted if not covered(scope, changed_paths))
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

    if (
        contract.required_python_symbols
        and not contract.read_only
        and not contract.dry_run
        and not terminal_without_execution
    ):
        if workspace is None:
            record(
                "requested_python_symbols_exist",
                False,
                "workspace was not provided for Python symbol validation",
            )
        else:
            root = Path(workspace).resolve()
            missing_symbols: list[str] = []
            invalid_files: list[str] = []
            for relative_path, symbol in contract.required_python_symbols:
                candidate = (root / relative_path).resolve()
                try:
                    candidate.relative_to(root)
                    tree = ast.parse(candidate.read_text(encoding="utf-8"))
                except (OSError, SyntaxError, UnicodeError, ValueError):
                    invalid_files.append(relative_path)
                    continue
                defined = {
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                if symbol not in defined:
                    missing_symbols.append(f"{relative_path}:{symbol}")
            passed = not missing_symbols and not invalid_files
            detail_parts: list[str] = []
            if invalid_files:
                detail_parts.append(
                    "unreadable or invalid Python: " + ", ".join(sorted(invalid_files))
                )
            if missing_symbols:
                detail_parts.append(
                    "missing requested symbols: " + ", ".join(sorted(missing_symbols))
                )
            record(
                "requested_python_symbols_exist",
                passed,
                "all explicitly requested Python symbols exist"
                if passed
                else "; ".join(detail_parts),
            )

    return ContractResult(
        ok=not violations,
        checked=True,
        contract=contract.to_dict(),
        checks=checks,
        violations=violations,
    )
