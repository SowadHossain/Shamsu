"""Does this text even parse?

The cheapest possible answer to "did the model write something real", asked
before the bytes reach disk. It exists because of a live run that reported
COMPLETE for this:

    def greet(name):
        return 'Hello, {}!'

That one parses, so this module would not have caught it — the point is the
much larger class it *does* catch. A 7B truncated mid-function, a stray
markdown fence copied into the file, an unbalanced brace from a salvaged tool
call: all of them used to be written out, registered as `FILE_CHANGED`, and
counted toward completion. The file existed, so the gate opened.

**Checked before the write, not after.** Rolling back a bad write is possible
(`PatchUndo` does it) but leaves a trap: the model retries `mode="create"` and
is told the file already exists, so it cannot fix what it just broke. Refusing
up front means the failure is clean and the retry is the obvious one.

**Silence means "no opinion", never "fine".** An extension with no parser here
returns `None`, identical to a file that parsed. That asymmetry is deliberate:
this is a filter for definite breakage, not a certificate of correctness, and
treating an unknown type as verified is how the last false-success got in.
"""

from __future__ import annotations

import ast
import builtins
import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

#: Names the interpreter injects into every module, which no file defines.
_DUNDERS = frozenset(
    {"__name__", "__file__", "__doc__", "__package__", "__spec__", "__loader__", "__debug__"}
)

#: Extensions carrying a syntax we can check in-process. Kept small on purpose
#: — every entry is a parser we promise not to be wrong about. A shell-out to
#: a real linter belongs in `check.run`, which the agent invokes deliberately;
#: this runs on every single write and has to stay microseconds cheap.
_PYTHON = {".py", ".pyi"}
_JSON = {".json"}

#: C-family sources, checked for balance rather than parsed. There is no
#: JavaScript parser in the standard library and shelling out to `node --check`
#: on every write would break this module's cost promise, but the failure that
#: actually happens — a small model stopping mid-file — leaves an unterminated
#: string or an unclosed brace, and that is detectable by lexing alone.
_BRACED = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css", ".scss"}

#: Everything with a comment syntax we can recognise, for the stub check below.
#: The value is `(line_comment_prefixes, block_comment_pairs)`.
_COMMENTS: Mapping[str, tuple[tuple[str, ...], tuple[tuple[str, str], ...]]] = {
    ".py": (
        ("#",),
        (),
    ),
    ".pyi": (
        ("#",),
        (),
    ),
    ".sql": (("--", "#"), (("/*", "*/"),)),
    ".js": (("//",), (("/*", "*/"),)),
    ".mjs": (("//",), (("/*", "*/"),)),
    ".cjs": (("//",), (("/*", "*/"),)),
    ".jsx": (("//",), (("/*", "*/"),)),
    ".ts": (("//",), (("/*", "*/"),)),
    ".tsx": (("//",), (("/*", "*/"),)),
    ".css": ((), (("/*", "*/"),)),
    ".scss": (("//",), (("/*", "*/"),)),
    ".yml": (("#",), ()),
    ".yaml": (("#",), ()),
    ".toml": (("#",), ()),
    ".sh": (("#",), ()),
}

#: A UTF-8 byte-order mark, decoded. It is an *encoding* marker, not source,
#: and both `compile()` and `json.loads()` reject it while the interpreter and
#: every real JSON reader accept it — Python reads source as `utf-8-sig`, so a
#: BOM'd module imports perfectly well.
#:
#: Left in, it wedges a file permanently. A `models.py` saved once by Notepad
#: or by PowerShell 5.1's `Set-Content -Encoding utf8` carries one, and from
#: then on every append is refused with *"invalid non-printable character
#: U+FEFF at line 1"* — a complaint about bytes the model did not write and
#: cannot reach from an append. One live build burned ten tool calls and
#: blocked on exactly that.
_BOM = "﻿"


def probe_syntax(path: str, content: str, *, workspace: Path | None = None) -> str | None:
    """Return why `content` is unusable at `path`, or `None` for no objection.

    `None` means "nothing detectable is wrong" — it does not mean the code is
    correct, and no caller should treat it as evidence of anything.

    Given a `workspace`, local imports are resolved too. That check needs to
    know what else exists on disk, which is why it is optional rather than
    folded into the parse.
    """
    suffix = PurePosixPath(path.replace("\\", "/")).suffix.lower()

    # Dropped before any parser sees it. Only a *leading* mark is an encoding
    # marker; one in the middle of a file is real (and still refused) content.
    content = content.removeprefix(_BOM)

    # Language-agnostic, and first: a file whose entire content is commentary
    # is a promise, not an implementation, whatever it is written in.
    stub = _only_commentary(path, suffix, content)
    if stub is not None:
        return stub

    if suffix in _PYTHON:
        broken = (
            _python(path, content)
            or _redefinitions(path, content)
            or _self_reference(path, content)
            or _undefined_names(path, content)
        )
        if broken is not None:
            return broken
        return _local_imports(path, content, workspace) if workspace is not None else None
    if suffix in _JSON:
        return _json(path, content)
    if suffix in _BRACED:
        broken = _balanced(path, content) or _commented_out_body(path, content)
        if broken is not None:
            return broken
        return _relative_imports(path, content, workspace) if workspace is not None else None
    return None


def _only_commentary(path: str, suffix: str, content: str) -> str | None:
    """Catch a file that promises the work instead of doing it.

    Asked to reproduce the PRD's PostgreSQL schema into `db/schema.sql`, a 7B
    wrote exactly this and stopped:

        -- This file will contain the PostgreSQL schema for the OpenBazaar Marketplace
        -- Generated from OpenBazaar_Marketplace_PRD.docx, Section 5.2

    Two comment lines. `.sql` has no parser here, so the probe had no opinion,
    `file_changed` and `git_diff_reviewed` were both earned, and the run
    reported COMPLETE with no schema anywhere in the repository.

    **An empty file is not a stub.** `__init__.py`, `.gitkeep` and `py.typed`
    are all deliberately empty and all legitimate, so emptiness is left alone.
    What is refused is a file with content, all of which is commentary — the
    shape of an intention that never became code.

    Only extensions whose comment syntax is listed are judged; anything else
    gets no opinion, as everywhere else in this module.
    """
    syntax = _COMMENTS.get(suffix)
    if syntax is None or not content.strip():
        return None

    line_markers, block_markers = syntax
    body = content
    for opener, closer in block_markers:
        body, unterminated = _strip_blocks(body, opener, closer)
        if unterminated:
            # An unclosed `/*` swallows the rest of the file, which would make
            # any truncated source look like pure commentary. That is a
            # different failure with a better diagnosis available, so this
            # check declines and leaves it to `_balanced`.
            return None

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not any(stripped.startswith(marker) for marker in line_markers):
            return None  # Real content found; nothing to say.

    return (
        f"{path} was not written: every line in it is a comment, so it "
        f"describes the work rather than doing it.\n"
        "Send the actual contents of the file."
    )


def _strip_blocks(text: str, opener: str, closer: str) -> tuple[str, bool]:
    """`(text without block comments, whether one was left unterminated)`."""
    out: list[str] = []
    rest = text
    while True:
        start = rest.find(opener)
        if start < 0:
            out.append(rest)
            return "".join(out), False
        out.append(rest[:start])
        end = rest.find(closer, start + len(opener))
        if end < 0:
            return "".join(out), True
        rest = rest[end + len(closer) :]


def _python(path: str, content: str) -> str | None:
    try:
        compile(content, path, "exec")
    except SyntaxError as exc:
        # `exc.text` is the offending source line. Quoting it back matters more
        # than the message: a small model given "SyntaxError: invalid syntax"
        # rewrites the whole file, and given the actual line, fixes the line.
        where = f"line {exc.lineno}" if exc.lineno else "an unknown line"
        detail = (exc.text or "").strip()
        shown = f"\n    {detail}" if detail else ""
        return (
            f"{path} was not written: it is not valid Python. "
            f"{exc.msg} at {where}.{shown}\n"
            "Fix the syntax and send the corrected content. A common cause is "
            "output that stopped early — check the file is complete, with every "
            "bracket, quote, and block closed."
        )
    except ValueError as exc:
        # compile() raises this for embedded NULs, among others.
        return f"{path} was not written: {exc}"
    return None


def _redefinitions(path: str, content: str) -> str | None:
    """Catch a module that defines the same top-level name twice.

    The append that would not stop. Asked to add an `Item` model to a file that
    already held `User` and `Category`, a 7B appended a full `class Item` — then
    appended it again, and again, and a fourth time, each attempt a slightly
    different draft of the same class. Every write was valid Python, so
    `_python` passed it; `_undefined_names` is scope-blind by design, so the
    first `class Item` bound the name for all four. Each append earned
    `FILE_CHANGED`, and the run reported COMPLETE on a module that dies at
    import with `NameError: name 'Item' is not defined`.

    Python permits redefinition, which is exactly why nothing else catches this.
    It is also, in a source file, almost never meant: the second definition
    silently discards the first, so the only working copy is whichever landed
    last. As a signal for "this write went wrong" it is close to perfect, and it
    is one pass over the module's own top level.

    **Top level only, and definitions only.** A method redefined in a class
    body, a class defined once per branch of an `if`, a fallback in an `except
    ImportError` — none of those are in `tree.body`, so none are flagged.
    Repeated assignment is skipped too: rebinding a module-level name is
    ordinary. `@overload` is exempt, being the one case where repeating a `def`
    is the intended spelling.
    """
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):  # pragma: no cover - `_python` ran first
        return None

    seen: dict[str, int] = {}
    repeated: list[tuple[str, int, int]] = []
    shadowed: list[tuple[str, int, int]] = []
    for item in tree.body:
        if isinstance(item, ast.Import | ast.ImportFrom):
            # An import that rebinds a name this file already defines. The
            # OpenBazaar build wrote `class User(AbstractUser)` and then, in a
            # later append, `from django.contrib.auth.models import User` —
            # silently replacing the project's own user model with Django's.
            # Flagged only in this direction: an import *then* a definition is
            # ordinary (subclassing under the same name is a common idiom).
            for alias in item.names:
                bound = alias.asname or alias.name.split(".")[0]
                first = seen.get(bound)
                if first is not None:
                    shadowed.append((bound, first, item.lineno))
            continue
        if not isinstance(item, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if any(_is_overload(decorator) for decorator in item.decorator_list):
            continue
        first = seen.get(item.name)
        if first is None:
            seen[item.name] = item.lineno
        else:
            repeated.append((item.name, first, item.lineno))

    # The duplicate definition is reported first when both are present: it is
    # the larger breakage, and it is usually what dragged the stray import in.
    if repeated:
        name, first, again = repeated[0]
        # A name repeated four times contributes three entries, all with the
        # same name; listing it again as "also redefined" is noise the model
        # has to parse past. Only genuinely different names belong here.
        others = ", ".join(sorted({n for n, _, _ in repeated[1:] if n != name}))
        total = sum(1 for n, _, _ in repeated if n == name) + 1
        times = f" ({total} times in total)" if total > 2 else ""
        also = f" (also redefined: {others})" if others else ""
        return (
            f"{path} was not written: it defines '{name}' twice — at line {first} "
            f"and again at line {again}{times}{also}. The second definition replaces "
            f"the first, so most of this file is unreachable.\n"
            "This usually means an append repeated work the file already contained. "
            "Read the file, then send the whole corrected module with exactly one "
            f"definition of '{name}' — do not append another copy."
        )

    if shadowed:
        name, first, again = shadowed[0]
        return (
            f"{path} was not written: the import at line {again} rebinds '{name}', "
            f"which this file already defines at line {first}. From that line on, "
            f"'{name}' means the imported one and this file's own is unreachable.\n"
            f"Remove that import — '{name}' is defined here — or import it under a "
            "different name with 'as'."
        )

    return None


def _self_reference(path: str, content: str) -> str | None:
    """Catch a class that uses its own name while its body is still running.

    A class's name is not bound until its body finishes, so this is a certain
    `NameError` at import — the same one the OpenBazaar build hit twice. Told
    to add an `Item` model, the 7B nested a second model inside it:

        class Item(models.Model):
            ...
            class Pricing(models.Model):
                item = models.OneToOneField(Item, on_delete=models.CASCADE)

    Valid syntax, and `_undefined_names` is scope-blind by design, so `Item`
    counted as bound. Django died on import with *"name 'Item' is not
    defined"*, and the step reported COMPLETE.

    **Only where evaluation is immediate.** A method body referring to the
    enclosing class is the normal way to write a factory or a `classmethod`,
    and it runs long after the name exists, so anything inside a `def` is
    skipped. So are annotations, which `from __future__ import annotations`
    makes lazy and which are a common place to name the enclosing class
    legitimately. What is left is a value evaluated *during* the class body —
    a field, a default, a base class, a nested class's attribute — where the
    name cannot possibly be bound yet.
    """
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):  # pragma: no cover - `_python` ran first
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        hit = _uses_own_name(node)
        if hit is not None:
            return (
                f"{path} was not written: class '{node.name}' uses its own name "
                f"at line {hit}, inside its own body. The name is not bound until "
                f"the class statement finishes, so this raises "
                f"NameError: name '{node.name}' is not defined when the module is "
                f"imported.\n"
                "Move it out to the top level, or refer to it lazily — a string "
                "forward reference, or code inside a method that runs later."
            )
    return None


def _uses_own_name(cls: ast.ClassDef) -> int | None:
    """The line where `cls` loads its own name during its body, if any.

    Prunes explicitly rather than using `ast.walk`, which yields every
    descendant and so would happily search inside the method bodies this has to
    ignore. Skipping a node there skips the node, not its subtree.
    """
    pending: list[ast.AST] = list(cls.body)
    while pending:
        node = pending.pop()

        # Deferred until after the class exists: the body of any function, and
        # a lambda's expression. Their decorators and defaults are *not*
        # deferred, but those are evaluated before the name is bound too, so
        # they are left in the queue below.
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            pending.extend(node.decorator_list)
            pending.extend(node.args.defaults)
            continue
        if isinstance(node, ast.Lambda):
            pending.extend(node.args.defaults)
            continue

        # `x: Item` is lazy under `from __future__ import annotations` and is a
        # normal way to name the enclosing class. The assigned value is not.
        if isinstance(node, ast.AnnAssign):
            if node.value is not None:
                pending.append(node.value)
            continue

        if isinstance(node, ast.Name) and node.id == cls.name and isinstance(node.ctx, ast.Load):
            return node.lineno

        pending.extend(ast.iter_child_nodes(node))
    return None


def _is_overload(decorator: ast.expr) -> bool:
    """Whether a decorator is `@overload` or `@typing.overload`."""
    if isinstance(decorator, ast.Name):
        return decorator.id == "overload"
    return isinstance(decorator, ast.Attribute) and decorator.attr == "overload"


def _undefined_names(path: str, content: str) -> str | None:
    """Catch a name the file uses and never defines or imports.

    The check that closes the loop on the two before it. Told its import was
    wrong twice, the agent stopped importing `Storage` altogether and kept
    calling `Storage('tasks.json')` — valid syntax, no bad import, and
    `NameError` on the first run. The task reported COMPLETE.

    **Deliberately scope-blind.** Every binding anywhere in the file counts as
    binding everywhere: a name assigned inside one function is treated as
    defined for all of them. That is not Python's rule, and getting Python's
    rule right means implementing closures, comprehension scopes, globals and
    class bodies — a second pyflakes, running on every write. Ignoring scope
    instead makes the check strictly *weaker* than the truth, which is the
    direction an always-on gate must err in: it will miss a genuine
    use-before-assignment, and it will never refuse a correct file.

    So this flags one thing only, and flags it with certainty: a name used
    here that is bound nowhere in this file and is not a builtin.
    """
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):  # pragma: no cover - `_python` ran first
        return None

    bound: set[str] = set(dir(builtins)) | _DUNDERS
    used: dict[str, int] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Load):
                used.setdefault(node.id, node.lineno)
            else:
                bound.add(node.id)
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            bound.update(a.asname or a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global | ast.Nonlocal):
            bound.update(node.names)
        elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)

    missing = sorted((line, name) for name, line in used.items() if name not in bound)
    if not missing:
        return None

    line, name = missing[0]
    others = ", ".join(sorted({n for _, n in missing[1:]}))
    also = f" (also: {others})" if others else ""
    return (
        f"{path} was not written: it uses '{name}' at line {line}, which is "
        f"never defined or imported in this file{also}. Add the missing import "
        f"or definition and send the corrected content."
    )


def _local_imports(path: str, content: str, workspace: Path) -> str | None:
    """Catch an import of a sibling module that is not actually there.

    From a live build: the agent wrote `Storage.py`, then wrote `cli.py`
    opening with `from storage import Storage`. Valid Python, parses fine, and
    `ModuleNotFoundError` on the first run — the file it meant is one capital
    letter away. Windows made it worse by hiding the mismatch from the
    filesystem while `import` stayed case-sensitive.

    **Only names that look local are judged.** A top-level name matching no
    file here is assumed to be a third-party or stdlib package and passes
    without comment. The check can therefore say "you meant Storage" but never
    "requests does not exist", which is not its business and not knowable from
    the workspace alone.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:  # pragma: no cover - `_python` ran first
        return None

    stems = {entry.stem: entry.name for entry in workspace.glob("*.py")}
    folded = {stem.casefold(): stem for stem in stems}
    siblings = _siblings(path, workspace)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = [node.module.split(".")[0]] if node.module and node.level == 0 else []
        elif isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        else:
            continue

        for name in names:
            if name not in stems:
                # Python 3 has no implicit relative import, so a bare
                # `from models import Order` inside `marketplace/services.py`
                # is `ModuleNotFoundError` even though `marketplace/models.py`
                # is sitting right next to it. A 7B writes this constantly —
                # it did so in the OpenBazaar build, and the whole module
                # stopped importing.
                if name in siblings:
                    package = PurePosixPath(path.replace("\\", "/")).parent
                    return (
                        f"{path} was not written: it imports '{name}', which is not "
                        f"importable from here. Python has no implicit relative "
                        f"import, so this raises ModuleNotFoundError even though "
                        f"{package}/{siblings[name]} is next to it.\n"
                        f"Use a relative import instead: from .{name} import ..."
                    )
                if name.casefold() not in folded:
                    continue
                actual = folded[name.casefold()]
                return (
                    f"{path} was not written: it imports '{name}', but the file here "
                    f"is '{stems[actual]}' — import is case-sensitive even where the "
                    f"filesystem is not. Use '{actual}' and send the corrected content."
                )

            # The module is real. Is what it is being asked for real too?
            if not isinstance(node, ast.ImportFrom):
                continue
            absent = _absent(workspace / stems[name], node)
            if absent:
                have = ", ".join(sorted(_defines(workspace / stems[name]))) or "nothing"
                return (
                    f"{path} was not written: {stems[name]} does not define "
                    f"{', '.join(absent)}. It defines: {have}."
                    f"{_where(workspace, absent, stems, exclude=path)}"
                )

    return None


def _siblings(path: str, workspace: Path) -> dict[str, str]:
    """Modules sitting beside the file being written, by import name.

    Empty for a file at the workspace root: there, "sibling" and "top-level
    module" are the same thing, and `stems` already covers it. This exists only
    for the package case, which is most real projects and which the root glob
    could not see at all.
    """
    parent = PurePosixPath(path.replace("\\", "/")).parent
    if parent in (PurePosixPath("."), PurePosixPath("")):
        return {}
    directory = workspace / parent
    if not directory.is_dir():
        return {}
    here = PurePosixPath(path.replace("\\", "/")).name
    return {entry.stem: entry.name for entry in directory.glob("*.py") if entry.name != here}


def _absent(module: Path, node: ast.ImportFrom) -> tuple[str, ...]:
    """Names this import asks a local module for that it does not provide.

    The follow-up to the case-mismatch check, and from the same build: told the
    module name was wrong, the agent moved the import to a module that exists
    and asked it for a class it does not contain — `from tasks import Storage,
    TaskList`, where `Storage` lives in `Storage.py`. The module resolved, so
    the earlier check passed, and it still died with `ImportError` on the first
    run.

    Re-exports count as provided, so anything the target itself imports is
    treated as importable from it. A module that cannot be read or parsed
    provides everything as far as this is concerned — an unreadable neighbour
    is not evidence against the file being written.
    """
    if not module.exists():
        return ()
    provided = _defines(module) | _imported(module)
    return tuple(a.name for a in node.names if a.name != "*" and a.name not in provided)


def _where(workspace: Path, absent: tuple[str, ...], stems: dict[str, str], *, exclude: str) -> str:
    """Name the file that actually defines each missing symbol.

    Without this the message ended "import each name from the file that
    defines it", which states the rule and withholds the answer. A 7B given
    that retried the identical bad import twice and burned the step. The
    workspace already knows where `Storage` lives; saying so turns a refusal
    into an instruction.

    Definitions only, and never the file being written. Re-exports are good
    enough to import *from*, but "where does this live" has one honest answer,
    and the first draft gave two wrong ones — it offered `cli.py` as the home
    of `TaskList` because the half-written `cli.py` imported it.
    """
    excluded = PurePosixPath(exclude.replace("\\", "/")).name
    found: list[str] = []
    for name in absent:
        for stem, filename in sorted(stems.items()):
            if filename == excluded or name not in _defines(workspace / filename):
                continue
            found.append(f"{name} is defined in {filename} — use: from {stem} import {name}")
            break
    return "\n" + "\n".join(found) if found else ""


def _defines(module: Path) -> frozenset[str]:
    """Top-level names a module declares itself."""
    return _top_level(module, imports=False)


def _imported(module: Path) -> frozenset[str]:
    """Names a module imports, and so re-exports."""
    return _top_level(module, imports=True)


def _top_level(module: Path, *, imports: bool) -> frozenset[str]:
    try:
        tree = ast.parse(module.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return frozenset()

    names: set[str] = set()
    for item in tree.body:
        if imports:
            if isinstance(item, ast.Import | ast.ImportFrom):
                names.update(a.asname or a.name.split(".")[0] for a in item.names)
            continue
        if isinstance(item, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(item.name)
        elif isinstance(item, ast.Assign):
            names.update(t.id for t in item.targets if isinstance(t, ast.Name))
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            names.add(item.target.id)
    return frozenset(names)


def _balanced(path: str, content: str) -> str | None:
    """Catch a C-family source that stops in the middle of itself.

    There is no JavaScript parser in the standard library, and `node --check`
    is a subprocess this module has promised not to become. But the failure
    that actually happens does not need a parser: a 7B that runs out of output
    leaves an unterminated string, an unclosed brace, or a block comment with
    no end, and lexing finds all three.

    Before this, `.js` was in the same position `.sql` was: no opinion at all,
    so an empty file and a file ending `function f() { return {` were both
    written out and both earned `file_changed`.

    **It reports only what it is certain of**, in keeping with the rest of this
    module. Regular-expression literals are the one genuinely ambiguous piece
    of JavaScript lexing — `/` is division or the start of a regex depending on
    what came before — so where the heuristic cannot be sure, the whole check
    returns `None` rather than risk refusing a correct file.
    """
    verdict = _scan(content)
    if verdict is None:
        return None

    what, line = verdict
    return (
        f"{path} was not written: it ends with {what}, so the file is "
        f"incomplete — the problem starts at line {line}.\n"
        "This usually means the output stopped early. Send the whole file, "
        "with every bracket, quote, and comment closed."
    )


#: Openers to their closers, for the balance scan.
_PAIRS = {"(": ")", "[": "]", "{": "}"}

#: Characters after which a `/` begins a regular expression rather than a
#: division. The standard heuristic: division follows a *value*, a regex
#: follows an operator or a statement boundary.
_BEFORE_REGEX = set("(,=:[!&|?{};+-*%~^<>\n\t ")


def _scan(content: str) -> tuple[str, int] | None:
    """`(what is unfinished, line)` or None for balanced-or-unsure."""
    stack: list[tuple[str, int]] = []
    index = 0
    line = 1
    previous = ""
    length = len(content)

    while index < length:
        char = content[index]

        if char == "\n":
            line += 1
            index += 1
            previous = "\n"
            continue

        # Comments.
        if char == "/" and index + 1 < length:
            following = content[index + 1]
            if following == "/":
                index = content.find("\n", index)
                if index < 0:
                    return None
                continue
            if following == "*":
                end = content.find("*/", index + 2)
                if end < 0:
                    return "an unclosed /* block comment", line
                line += content.count("\n", index, end)
                index = end + 2
                previous = "/"
                continue
            # A regex literal, or division. Where it could be a regex, this
            # check gives up entirely rather than guess wrong.
            if previous in _BEFORE_REGEX or previous == "":
                return None

        # Strings and template literals.
        if char in "\"'`":
            end = _close_quote(content, index)
            if end is None:
                kind = "template literal" if char == "`" else "string"
                return f"an unterminated {kind}", line
            line += content.count("\n", index, end)
            index = end + 1
            previous = char
            continue

        if char in _PAIRS:
            stack.append((char, line))
        elif char in _PAIRS.values():
            if not stack or _PAIRS[stack[-1][0]] != char:
                # Mismatched. Real, but also what a mis-lexed regex looks like,
                # so it is not worth a refusal.
                return None
            stack.pop()

        if not char.isspace():
            previous = char
        index += 1

    if stack:
        opener, opened = stack[-1]
        return f"an unclosed '{opener}'", opened
    return None


def _close_quote(content: str, start: int) -> int | None:
    """Index of the quote closing the one at `start`, or None if never closed."""
    quote = content[start]
    index = start + 1
    while index < len(content):
        char = content[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index
        # A plain string cannot span lines; a template literal can.
        if char == "\n" and quote != "`":
            return None
        index += 1
    return None


def _commented_out_body(path: str, content: str) -> str | None:
    """Catch a block whose only contents are a comment describing the work.

    `_only_commentary` catches a whole *file* of prose; this catches the same
    evasion one level down, which is what a model reaches for once the file has
    to contain a real import and a real signature. Twice in one build, asked
    for the bidding rule, it produced exactly this:

        export async function placeBid(itemId, bidderId, amount) {
          // Step one: await query with the text SELECT current_highest_bid ...
        }

    Valid JavaScript, balanced, the import resolves, the signature is right —
    and `placeBid` returns `undefined`. Every existing check passed it and the
    run reported COMPLETE.

    **An empty block is left alone; a block emptied by comments is not.**
    `function noop() {}` and `catch (e) {}` are deliberate and common, so
    emptiness alone says nothing. What is refused is a body that *has* content
    and whose content is entirely commentary — the same distinction, and the
    same reasoning, as the file-level check.
    """
    blank = _without_comments(content)
    for match in re.finditer(r"\{([^{}]*)\}", blank):
        if match.group(1).strip():
            continue  # Not empty once comments are gone.
        original = content[match.start() + 1 : match.end() - 1]
        if not original.strip():
            continue  # Genuinely empty in the source too, which is fine.
        line = content.count("\n", 0, match.start()) + 1
        return (
            f"{path} was not written: the block starting at line {line} contains "
            f"nothing but a comment, so it describes what the code should do "
            f"instead of doing it.\n"
            "Write the statements themselves and send the file again."
        )
    return None


def _without_comments(content: str) -> str:
    """`content` with every comment blanked out, positions preserved.

    Characters are replaced rather than removed so offsets into the result
    still index the original, which is what lets the caller compare a block's
    stripped contents against its real ones.
    """
    out = list(content)
    index = 0
    length = len(content)
    while index < length:
        char = content[index]
        if char in "\"'`":
            end = _close_quote(content, index)
            index = length if end is None else end + 1
            continue
        if char == "/" and index + 1 < length:
            following = content[index + 1]
            if following == "/":
                end = content.find("\n", index)
                end = length if end < 0 else end
                for position in range(index, end):
                    out[position] = " "
                index = end
                continue
            if following == "*":
                end = content.find("*/", index + 2)
                end = length if end < 0 else end + 2
                for position in range(index, end):
                    if out[position] != "\n":
                        out[position] = " "
                index = end
                continue
        index += 1
    return "".join(out)


#: `from './x.js'`, `require('../y')`, `import('./z.js')` — the specifier only.
_RELATIVE_IMPORT = re.compile(
    r"""(?:\bfrom\s*|\brequire\s*\(\s*|\bimport\s*\(\s*)['"](\.{1,2}/[^'"]*)['"]""",
)


def _relative_imports(path: str, content: str, workspace: Path) -> str | None:
    """Catch a relative import that points at nothing.

    The JavaScript half of the check `_local_imports` performs for Python, and
    it found the same mistake within minutes of being needed. Asked for
    `src/auction.js` importing the sibling `src/db.js`, the model wrote:

        import { query } from '../db.js';

    One directory too far up. Node raises `ERR_MODULE_NOT_FOUND` the moment the
    module is loaded, and nothing before this said a word — `.js` had no checks
    at all until the balance scan, and balance has no opinion about paths.

    **Only relative specifiers are judged.** A bare `import express from
    'express'` names a package, whose presence depends on `node_modules` and is
    not this module's business — the same rule that stops the Python check
    second-guessing `requests`. Extensionless specifiers are resolved the way
    Node and bundlers do, trying the common suffixes and an `index` file, and
    anything still unresolved is reported with the sibling that most likely was
    meant.
    """
    for match in _RELATIVE_IMPORT.finditer(content):
        specifier = match.group(1)
        base = (PurePosixPath(path.replace("\\", "/")).parent / specifier).as_posix()
        if _resolves(workspace, base):
            continue

        line = content.count("\n", 0, match.start()) + 1
        wanted = PurePosixPath(base).name
        hint = _sibling_named(workspace, path, wanted)
        suggestion = f"\nDid you mean '{hint}'?" if hint else ""
        return (
            f"{path} was not written: it imports '{specifier}' at line {line}, "
            f"which does not exist. Node raises ERR_MODULE_NOT_FOUND for a "
            f"relative path that resolves to no file.{suggestion}"
        )
    return None


def _resolves(workspace: Path, base: str) -> bool:
    """Whether `base`, relative to the workspace, names a module Node would find."""
    try:
        candidate = workspace / base
        if candidate.is_file():
            return True
        if candidate.is_dir():
            return any((candidate / f"index{s}").is_file() for s in (".js", ".mjs", ".ts", ".tsx"))
        return any(
            candidate.with_name(candidate.name + s).is_file()
            for s in (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".json")
        )
    except OSError:  # pragma: no cover - a path the filesystem rejects outright
        return False


def _sibling_named(workspace: Path, path: str, wanted: str) -> str | None:
    """A relative specifier that would have resolved, if one obviously does."""
    stem = PurePosixPath(wanted).stem
    here = PurePosixPath(path.replace("\\", "/")).parent
    for folder, prefix in ((here, "./"), (here.parent, "../")):
        try:
            directory = workspace / folder
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if entry.is_file() and entry.stem == stem:
                    return f"{prefix}{entry.name}"
        except OSError:  # pragma: no cover - unreadable directory
            continue
    return None


def _json(path: str, content: str) -> str | None:
    try:
        json.loads(content)
    except json.JSONDecodeError as exc:
        return (
            f"{path} was not written: it is not valid JSON. "
            f"{exc.msg} at line {exc.lineno}, column {exc.colno}. "
            "Send the corrected content, with every brace and bracket closed."
        )
    return None


__all__ = ["probe_syntax"]
