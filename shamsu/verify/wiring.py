"""Deterministic cross-layer wiring checks for generated full-stack projects."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

WIRING_COMMAND = "shamsu verify wiring"

_SOURCE_SUFFIXES = frozenset(
    {".html", ".js", ".jsx", ".prisma", ".py", ".sql", ".svelte", ".ts", ".tsx", ".vue"}
)
_IGNORED_DIRS = frozenset(
    {
        ".git",
        ".shamsu",
        ".venv",
        "build",
        "coverage",
        "dist",
        "evals",
        "fixtures",
        "node_modules",
        "test",
        "tests",
        "venv",
    }
)
_FRONTEND_HINTS = frozenset(
    {"client", "components", "frontend", "hooks", "pages", "src", "ui", "views"}
)
_BACKEND_HINTS = frozenset(
    {"api", "app", "backend", "controllers", "routes", "server", "services"}
)

_FETCH_RE = re.compile(
    r"\bfetch\s*\(\s*([\"'`])(?P<path>/(?:api|v\d+)[^\"'`]*)\1",
    re.IGNORECASE,
)
_CLIENT_METHOD_RE = re.compile(
    r"\b(?:axios|api|client|http)\s*\.\s*(?P<method>get|post|put|patch|delete)"
    r"\s*\(\s*([\"'`])(?P<path>/(?:api|v\d+)[^\"'`]*)\2",
    re.IGNORECASE,
)
_JS_ROUTE_RE = re.compile(
    r"\b(?P<object>app|router|server|fastify)\s*\.\s*"
    r"(?P<method>get|post|put|patch|delete|all|route)\s*"
    r"\(\s*([\"'`])(?P<path>/[^\"'`]*)\3",
    re.IGNORECASE,
)
_PY_ROUTE_RE = re.compile(
    r"@\s*(?:app|router|blueprint|bp)\s*\.\s*"
    r"(?P<method>get|post|put|patch|delete|route)\s*"
    r"\(\s*([\"'])(?P<path>/[^\"']*)\2",
    re.IGNORECASE,
)
_DJANGO_ROUTE_RE = re.compile(
    r"\bpath\s*\(\s*([\"'])(?P<path>(?:api/|v\d+/)[^\"']*)\1",
    re.IGNORECASE,
)
_ROUTER_PREFIX_RE = re.compile(
    r"\bapp\s*\.\s*use\s*\(\s*([\"'`])(?P<prefix>/[^\"'`]*)\1\s*,\s*"
    r"(?P<object>[A-Za-z_$][\w$]*)",
    re.IGNORECASE,
)

_SQL_TABLE_RE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?"
    r"(?P<table>[A-Za-z_][\w]*)",
    re.IGNORECASE,
)
_NAMED_TABLE_RE = re.compile(
    r"\b(?:sqliteTable|pgTable|Table)\s*\(\s*([\"'])(?P<table>[A-Za-z_][\w]*)\1",
)
_PRISMA_MODEL_RE = re.compile(r"^\s*model\s+(?P<table>[A-Za-z_][\w]*)\s*\{", re.MULTILINE)
_TABLENAME_RE = re.compile(
    r"\b__tablename__\s*=\s*([\"'])(?P<table>[A-Za-z_][\w]*)\1",
)
_SQL_QUERY_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+[\"'`\[]?"
    r"(?P<table>[A-Za-z_][\w]*)",
    re.IGNORECASE,
)
_PRISMA_QUERY_RE = re.compile(
    r"\bprisma\s*\.\s*(?P<table>[A-Za-z_][\w]*)\s*\.\s*"
    r"(?:find|create|update|delete|upsert|count)",
    re.IGNORECASE,
)
_STRING_LITERAL_RE = re.compile(
    r"'''(?P<triple_single>.*?)'''"
    r'|"""(?P<triple_double>.*?)"""'
    r"|`(?P<backtick>(?:\\.|[^`\\])*)`"
    r"|'(?P<single>(?:\\.|[^'\\])*)'"
    r'|"(?P<double>(?:\\.|[^"\\])*)"',
    re.DOTALL,
)


@dataclass(frozen=True)
class WiringDiagnostic:
    file: str
    line: int
    message: str
    kind: str

    def render(self) -> str:
        return f"{self.file}:{self.line}: error: {self.message}"


@dataclass(frozen=True)
class _AssetRef:
    """A file one document points at: `<script src>`, `<link href>`, `<img src>`."""

    file: str
    line: int
    target: str
    attribute: str


@dataclass(frozen=True)
class _Symbol:
    """A top-level JavaScript declaration, and where it was declared."""

    file: str
    line: int
    name: str
    keyword: str


@dataclass(frozen=True)
class WiringResult:
    diagnostics: tuple[WiringDiagnostic, ...] = ()
    frontend_calls: int = 0
    backend_routes: int = 0
    schema_tables: int = 0
    query_tables: int = 0
    asset_refs: int = 0
    js_symbols: int = 0

    @property
    def ok(self) -> bool:
        return not self.diagnostics

    @property
    def has_surface(self) -> bool:
        return bool(
            self.frontend_calls
            or self.backend_routes
            or self.schema_tables
            or self.query_tables
            or self.asset_refs
            or self.js_symbols
        )

    def stderr(self) -> str:
        return "\n".join(item.render() for item in self.diagnostics)


@dataclass(frozen=True)
class _RouteUse:
    file: str
    line: int
    method: str
    path: str


@dataclass(frozen=True)
class _TableUse:
    file: str
    line: int
    table: str


def verify_wiring(project_root: Path | str) -> WiringResult:
    root = Path(project_root).resolve()
    sources = _source_files(root)
    frontend_calls: list[_RouteUse] = []
    backend_routes: list[_RouteUse] = []
    schema_tables: set[str] = set()
    query_tables: list[_TableUse] = []
    asset_refs: list[_AssetRef] = []
    js_symbols: list[_Symbol] = []
    js_calls: list[_Symbol] = []
    js_bound: set[str] = set()

    for path in sources:
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        frontend_calls.extend(_frontend_calls(relative, text))
        backend_routes.extend(_backend_routes(relative, text))
        schema_tables.update(_declared_tables(text))
        query_tables.extend(_query_tables(relative, text))
        if path.suffix.lower() in {".html", ".htm"}:
            asset_refs.extend(_asset_refs(relative, text))
        elif path.suffix.lower() == ".js":
            js_symbols.extend(_js_symbols(relative, text))
            js_calls.extend(_js_calls(relative, text))
            # Bound names pool across the WHOLE project, because plain scripts
            # share one scope and a helper defined in one file is callable from
            # every other. Scoping this per file would report each module's use
            # of its neighbours' functions as undefined.
            js_bound |= _js_bound_names(text)

    diagnostics: list[WiringDiagnostic] = []
    if frontend_calls and _has_backend_surface(sources, root, backend_routes):
        declared = {(route.method, route.path) for route in backend_routes}
        declared_paths = {route.path for route in backend_routes}
        for call in frontend_calls:
            if (
                (call.method, call.path) in declared
                or ("ANY", call.path) in declared
                or call.path in declared_paths
            ):
                continue
            diagnostics.append(
                WiringDiagnostic(
                    file=call.file,
                    line=call.line,
                    kind="frontend_backend_route",
                    message=(
                        f"frontend {call.method} route {call.path!r} has no matching "
                        "backend route declaration"
                    ),
                )
            )

    if schema_tables:
        normalized_schema = {_table_key(table) for table in schema_tables}
        for query in query_tables:
            if _table_key(query.table) in normalized_schema or _is_system_table(query.table):
                continue
            diagnostics.append(
                WiringDiagnostic(
                    file=query.file,
                    line=query.line,
                    kind="backend_schema_table",
                    message=(
                        f"database query references table {query.table!r}, but no matching "
                        "schema/model declaration was found"
                    ),
                )
            )

    for asset in asset_refs:
        if _asset_exists(root, asset.file, asset.target):
            continue
        diagnostics.append(
            WiringDiagnostic(
                file=asset.file,
                line=asset.line,
                kind="missing_asset",
                message=(
                    f"{asset.attribute}={asset.target!r} does not resolve to a file "
                    "in this project; the browser will 404 on it"
                ),
            )
        )

    diagnostics.extend(_unreferenced_scripts(root, sources, asset_refs))
    diagnostics.extend(_redeclarations(js_symbols))
    diagnostics.extend(_undefined_helpers(js_calls, js_bound))

    return WiringResult(
        diagnostics=tuple(_dedupe_diagnostics(diagnostics)),
        frontend_calls=len(frontend_calls),
        backend_routes=len(backend_routes),
        schema_tables=len(schema_tables),
        query_tables=len(query_tables),
        asset_refs=len(asset_refs),
        js_symbols=len(js_symbols),
    )


def has_wiring_surface(project_root: Path | str) -> bool:
    return verify_wiring(project_root).has_surface


# -- assets a document points at -------------------------------------------
#
# The cheapest check in this file and the one that would have saved the whole
# 2026-08-24 snake-game run. `index.html` was written at 19:45:43 with
# `<script src="game.js">`; `js/game.js` was created at 20:02:59, seventeen
# minutes later. Nothing ever re-checked the path, so the page 404'd on its
# only script tag and not one line of the project's 1,800 lines ever ran - and
# the contract still reported seven of nine assertions passed.

_ASSET_REF = re.compile(
    r"(?is)<(?:script|link|img|source|iframe|audio|video)\b[^>]*?"
    r"\b(src|href)\s*=\s*[\"']([^\"'>]+)"
)

#: Targets no filesystem check can settle. A URL belongs to a server, a data:
#: URI carries its own bytes, and a bare fragment is this document.
_NOT_A_LOCAL_FILE = re.compile(
    r"(?i)^(?:[a-z][a-z0-9+.-]*:|//|#|\{|\$\{)"
)


def _asset_refs(file: str, text: str) -> list[_AssetRef]:
    found: list[_AssetRef] = []
    for match in _ASSET_REF.finditer(text):
        target = match.group(2).strip()
        if not target or _NOT_A_LOCAL_FILE.match(target):
            continue
        found.append(
            _AssetRef(
                file=file,
                line=_line_number(text, match.start()),
                target=target.split("?")[0].split("#")[0],
                attribute=match.group(1).lower(),
            )
        )
    return found


def _asset_exists(root: Path, document: str, target: str) -> bool:
    """Resolve *target* the way a browser would, relative to *document*."""
    if target.startswith("/"):
        candidate = root / target.lstrip("/")
    else:
        candidate = (root / document).parent / target
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return False
    return resolved.exists()


# -- one JavaScript name, declared twice ------------------------------------
#
# Eight modules loaded as plain <script> share ONE global scope, so the same
# `const` in two of them is not a style problem: it is
# `SyntaxError: Identifier 'GameState' has already been declared`, and the
# second file does not run at all. Every file passed `node --check` on its own,
# which is exactly why nothing caught it - the defect only exists in the
# combination.

_JS_TOP_LEVEL = re.compile(
    r"(?m)^(?:export\s+)?(const|let|var|function|class)\s+"
    r"([A-Za-z_$][A-Za-z0-9_$]*)"
)

#: A module system gives every file its own scope, so nothing below applies.
_IS_MODULE = re.compile(
    r"(?m)^\s*(?:import\s|export\s|export\{)"
)


def _js_symbols(file: str, text: str) -> list[_Symbol]:
    if _IS_MODULE.search(text):
        return []
    return [
        _Symbol(
            file=file,
            line=_line_number(text, match.start()),
            name=match.group(2),
            keyword=match.group(1),
        )
        for match in _JS_TOP_LEVEL.finditer(text)
    ]


#: Only these collide fatally. Two `function`s with one name is legal - the
#: second silently wins - and calling that an error would fire on every project
#: with a `main()` in two files. `const`, `let` and `class` throw.
_FATAL_REDECLARATION = frozenset({"const", "let", "class"})


#: A `.js` nobody has to reference: bundlers and module graphs reach these
#: without a `<script src>`, so demanding one would report every Vite project as
#: broken. Matched on the path, because that is what distinguishes a module in a
#: build from a script meant to be loaded by a page.
_BUNDLED_HINTS = ("node_modules/", "/src/", "src/", "dist/", "build/", ".min.js")


def _unreferenced_scripts(
    root: Path, sources: list[Path], asset_refs: list[_AssetRef]
) -> list[WiringDiagnostic]:
    """A script the project wrote and no page loads.

    The other half of `missing_asset`, which catches a `<script src>` pointing
    at nothing. This catches the reverse and it is the more expensive one,
    because it is SILENT: nothing 404s, nothing throws, the file simply never
    runs and the feature it holds is quietly absent.

    Live 2026-08-31, `F:\\voice-demo`. Asked to split one big `game.js` into
    parts, the agent wrote `sounds.js` - 74 lines, correct, parsing - and never
    added a `<script src="sounds.js">`. `index.html` still loaded `game.js`
    alone, so the request was reported as done, the file existed to prove it,
    and not one line of it ever executed. The duplicate-class check fired on the
    same pair and said nothing about this: two files can collide in scope only
    if both are loaded, and here only one ever was.

    Only fires where the project has an HTML page that loads SOMETHING. A
    workspace with no `<script src>` anywhere is a bundler project or a library,
    and neither owes any file a script tag.
    """
    pages = [path for path in sources if path.suffix.lower() in {".html", ".htm"}]
    if not pages:
        return []
    loaded = {
        (root / ref.file).parent.joinpath(ref.target).resolve()
        if not ref.target.startswith("/")
        else (root / ref.target.lstrip("/")).resolve()
        for ref in asset_refs
        if ref.attribute == "src"
    }
    # No script tag anywhere means nothing here is loaded by a page at all -
    # a module graph, not a broken one.
    if not loaded:
        return []
    diagnostics: list[WiringDiagnostic] = []
    for path in sources:
        if path.suffix.lower() != ".js":
            continue
        relative = path.relative_to(root).as_posix()
        if any(hint in f"/{relative}" for hint in _BUNDLED_HINTS):
            continue
        if path.resolve() in loaded:
            continue
        page = pages[0].relative_to(root).as_posix()
        diagnostics.append(
            WiringDiagnostic(
                file=relative,
                line=1,
                kind="unreferenced_script",
                message=(
                    f"{relative} is never loaded: no page has a "
                    f'<script src="..."> pointing at it, so none of it runs. '
                    f"Add it to {page} with patch_file, before the script that "
                    "uses it - or delete the file if it is not needed."
                ),
            )
        )
    return diagnostics


def _redeclarations(symbols: list[_Symbol]) -> list[WiringDiagnostic]:
    by_name: dict[str, list[_Symbol]] = {}
    for symbol in symbols:
        by_name.setdefault(symbol.name, []).append(symbol)

    diagnostics: list[WiringDiagnostic] = []
    for name, uses in by_name.items():
        files = sorted({use.file for use in uses})
        if len(files) < 2:
            continue
        fatal = [use for use in uses if use.keyword in _FATAL_REDECLARATION]
        if not fatal:
            continue
        latest = max(fatal, key=lambda use: (use.file, use.line))
        others = ", ".join(f for f in files if f != latest.file)
        # NAME THE NEXT CALL. This diagnostic was correct and useless: live
        # 2026-08-31 in `F:\voice-demo` it reported a duplicate `SoundManager`
        # across game.js and sounds.js four times, and the model answered with
        # twenty-six failed edits - seventeen `patch_file`, nine
        # `replace_symbol` - every one of them refused for a good reason, and
        # four turns ending on "I tried 4 edits in a row that changed nothing".
        #
        # It knew WHAT was wrong and never what to DO, which is the one lesson
        # this project keeps relearning: naming the exact next call took 42s
        # where vague guidance cost 674s and a failure. Every other message in
        # this harness ends with the call to make; this one ended with the
        # diagnosis.
        #
        # Deliberately the DUPLICATE's file, not the original's: the copy that
        # came second is the one safe to remove, and "delete the class" without
        # saying which copy is how a model ends up deleting the only one - which
        # `replace_symbol` then refuses, which is exactly the loop above.
        first = next(f for f in files if f != latest.file)
        diagnostics.append(
            WiringDiagnostic(
                file=latest.file,
                line=latest.line,
                kind="js_redeclaration",
                message=(
                    f"{latest.keyword} {name!r} is also declared in {others}; loaded "
                    "together as plain scripts they share one scope, and the second "
                    "file will not run at all. Keep ONE copy: if "
                    f"{latest.file} is the version you want, delete {name!r} from "
                    f"{first} with patch_file and make sure the page loads "
                    f"{latest.file} first; otherwise delete it from {latest.file} "
                    "instead. Do not edit both copies to match - two copies is "
                    "the fault."
                ),
            )
        )
    return diagnostics



#: Comments and string bodies, blanked before anything reads the code.
#:
#: Without this the call scanner reported `body()` from the line
#: `// Check collision with body (skip first few segments...)`. A gate that
#: cries wolf on prose is a gate people learn to ignore, which is worse than
#: no gate. Newlines survive so every line number stays true.
_JS_NOISE = re.compile(
    r"(?s)/\*.*?\*/|//[^\n]*"
    
)
_JS_LITERAL = re.compile(
    r"'(?:\\.|[^\\'\n])*'|\"(?:\\.|[^\\\"\n])*\"|`(?:\\.|[^\\`])*`"
)


def _strip_js_noise(text: str) -> str:
    """Blank comments and string bodies, keeping every newline in place."""

    def blank(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _JS_LITERAL.sub(blank, _JS_NOISE.sub(blank, text))

# -- a helper every file calls and no file defines --------------------------
#
# `playSound` was called eleven times across five modules of the 2026-08-24
# snake game and defined in none of them - the sound manager exports
# `SoundManager.play`. Every file passed `node --check`, because a call to an
# undefined name is valid JavaScript right up until it runs. The contract
# asserted the opposite in prose: "Has playSound() function (line 48)". There
# is no such function at line 48 or anywhere else in the project.
#
# Reported only when the name is called from TWO OR MORE files. A callback
# parameter, a local closure, a name bound in some way no regex here models -
# all of those live in one file, and one file is where the false positives
# are. A helper that three modules call is a helper somebody forgot to write.

_JS_CALL = re.compile(
    r"(?<![.\w$])([A-Za-z_$][A-Za-z0-9_$]*)\s*\("
)

#: Everything a name can be bound by, so a local is never mistaken for a gap.
_JS_BINDING = re.compile(
    r"(?:(?:const|let|var|function|class)\s+([A-Za-z_$][A-Za-z0-9_$]*))"
    r"|(?:function\s*\*\s*[A-Za-z0-9_$]*\s*\(([^)]*)\))"
    r"|(?:\(([^()]*)\)\s*=>)"
    r"|(?:([A-Za-z_$][A-Za-z0-9_$]*)\s*\(([^()]*)\)\s*\{)"
    r"|(?:catch\s*\(\s*([A-Za-z_$][A-Za-z0-9_$]*))"
)

#: Called like a function, but not a function anybody writes.
_JS_KEYWORDS = frozenset(
    {
        "if", "for", "while", "switch", "catch", "return", "typeof", "function",
        "new", "delete", "void", "in", "of", "do", "else", "case", "throw",
        "await", "yield", "super", "this", "class", "const", "let", "var",
    }
)

#: The standard library and the browser. Not exhaustive, and it does not need
#: to be - anything missed is still filtered by the two-file rule.
_JS_GLOBALS = frozenset(
    {
        "Array", "Boolean", "Date", "Error", "Function", "JSON", "Map", "Math",
        "Number", "Object", "Promise", "Proxy", "RegExp", "Set", "String",
        "Symbol", "WeakMap", "WeakSet", "BigInt", "Intl", "Reflect",
        "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
        "decodeURIComponent", "encodeURI", "decodeURI", "structuredClone",
        "setTimeout", "setInterval", "clearTimeout", "clearInterval",
        "queueMicrotask", "requestAnimationFrame", "cancelAnimationFrame",
        "fetch", "alert", "confirm", "prompt", "console", "document", "window",
        "navigator", "localStorage", "sessionStorage", "location", "history",
        "screen", "AudioContext", "webkitAudioContext", "Audio", "Image",
        "Worker", "Blob", "File", "FileReader", "FormData", "Headers",
        "Request", "Response", "URL", "URLSearchParams", "AbortController",
        "Event", "CustomEvent", "EventTarget", "MutationObserver",
        "IntersectionObserver", "ResizeObserver", "CanvasRenderingContext2D",
        "Path2D", "DOMParser", "TextEncoder", "TextDecoder", "atob", "btoa",
        "escape", "unescape", "require", "import", "process", "Buffer",
        "module", "exports", "describe", "it", "test", "expect",
        "beforeEach", "afterEach",
        "Float32Array", "Float64Array", "Int8Array", "Int16Array", "Int32Array",
        "Uint8Array", "Uint8ClampedArray", "Uint16Array", "Uint32Array",
        "BigInt64Array", "BigUint64Array", "ArrayBuffer", "DataView",
    }
)


def _js_bound_names(text: str) -> set[str]:
    """Every identifier this file binds, however it binds it."""
    text = _strip_js_noise(text)
    names: set[str] = set()
    for match in _JS_BINDING.finditer(text):
        for group in match.groups():
            if not group:
                continue
            for part in group.split(","):
                # Strips defaults, rest and destructuring down to the name.
                cleaned = part.split("=")[0].strip().lstrip(".").strip("{} []")
                if cleaned and cleaned.isidentifier():
                    names.add(cleaned)
    return names


#: `new Something(...)`. Skipped, and the reason is that the globals list can
#: never be finished. `Float32Array` was missing from it and the check reported
#: a browser builtin as a helper nobody wrote - on `demo-3/asteroid`, where the
#: real defects are elsewhere. A constructor is either a platform global or a
#: `class` this project declares, and `_JS_BINDING` already catches the second;
#: what remains is a list nobody can keep complete, so it is not guessed at.
_PRECEDED_BY_NEW = re.compile(r"(?:^|[^A-Za-z0-9_$])new\s+$")


def _js_calls(file: str, text: str) -> list[_Symbol]:
    """Bare calls - `foo()`, never `obj.foo()`, which is a different claim."""
    text = _strip_js_noise(text)
    return [
        _Symbol(
            file=file,
            line=_line_number(text, match.start(1)),
            name=match.group(1),
            keyword="call",
        )
        for match in _JS_CALL.finditer(text)
        if match.group(1) not in _JS_KEYWORDS
        and not _PRECEDED_BY_NEW.search(text[: match.start(1)])
    ]


def _undefined_helpers(
    calls: list[_Symbol], bound: set[str]
) -> list[WiringDiagnostic]:
    by_name: dict[str, list[_Symbol]] = {}
    for call in calls:
        if call.name in bound or call.name in _JS_GLOBALS:
            continue
        by_name.setdefault(call.name, []).append(call)

    diagnostics: list[WiringDiagnostic] = []
    for name, uses in sorted(by_name.items()):
        files = sorted({use.file for use in uses})
        if len(files) < 2:
            continue
        first = min(uses, key=lambda use: (use.file, use.line))
        diagnostics.append(
            WiringDiagnostic(
                file=first.file,
                line=first.line,
                kind="undefined_helper",
                message=(
                    f"{name}() is called from {len(files)} files "
                    f"({len(uses)} times) and defined in none of them"
                ),
            )
        )
    return diagnostics

def _source_files(root: Path) -> list[Path]:
    result: list[Path] = []
    try:
        candidates = root.rglob("*")
        for path in candidates:
            if not path.is_file() or path.suffix.lower() not in _SOURCE_SUFFIXES:
                continue
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if any(part.lower() in _IGNORED_DIRS for part in relative.parts):
                continue
            lowered_name = path.name.lower()
            if (
                lowered_name.startswith("test_")
                or ".test." in lowered_name
                or ".spec." in lowered_name
            ):
                continue
            result.append(path)
    except OSError:
        return []
    return result


def _frontend_calls(file: str, text: str) -> list[_RouteUse]:
    if not _is_frontend_file(file, text):
        return []
    calls: list[_RouteUse] = []
    for match in _FETCH_RE.finditer(text):
        calls.append(
            _RouteUse(
                file=file,
                line=_line_number(text, match.start()),
                method=_fetch_method(text, match.end()),
                path=_route_key(match.group("path")),
            )
        )
    for match in _CLIENT_METHOD_RE.finditer(text):
        calls.append(
            _RouteUse(
                file=file,
                line=_line_number(text, match.start()),
                method=match.group("method").upper(),
                path=_route_key(match.group("path")),
            )
        )
    return [call for call in calls if call.path]


def _backend_routes(file: str, text: str) -> list[_RouteUse]:
    routes: list[_RouteUse] = []
    prefixes = {
        match.group("object"): _route_key(match.group("prefix"))
        for match in _ROUTER_PREFIX_RE.finditer(text)
    }
    for match in _JS_ROUTE_RE.finditer(text):
        path = _route_key(match.group("path"))
        object_name = match.group("object")
        if object_name in prefixes and not path.startswith(prefixes[object_name]):
            path = _route_key(f"{prefixes[object_name]}/{path.lstrip('/')}")
        method = match.group("method").upper()
        routes.append(
            _RouteUse(
                file=file,
                line=_line_number(text, match.start()),
                method="ANY" if method in {"ALL", "ROUTE"} else method,
                path=path,
            )
        )
    for match in _PY_ROUTE_RE.finditer(text):
        method = match.group("method").upper()
        routes.append(
            _RouteUse(
                file=file,
                line=_line_number(text, match.start()),
                method="ANY" if method == "ROUTE" else method,
                path=_route_key(match.group("path")),
            )
        )
    for match in _DJANGO_ROUTE_RE.finditer(text):
        routes.append(
            _RouteUse(
                file=file,
                line=_line_number(text, match.start()),
                method="ANY",
                path=_route_key(f"/{match.group('path')}"),
            )
        )
    return [route for route in routes if route.path]


def _declared_tables(text: str) -> set[str]:
    tables = {match.group("table") for match in _SQL_TABLE_RE.finditer(text)}
    tables.update(match.group("table") for match in _NAMED_TABLE_RE.finditer(text))
    tables.update(match.group("table") for match in _PRISMA_MODEL_RE.finditer(text))
    tables.update(match.group("table") for match in _TABLENAME_RE.finditer(text))
    return tables


def _query_tables(file: str, text: str) -> list[_TableUse]:
    uses: list[_TableUse] = []
    fragments = (
        [(text, 0)]
        if Path(file).suffix.lower() == ".sql"
        else [
            (body, start)
            for body, start in _string_literals(text)
            if _looks_like_sql_literal(text, start, body)
        ]
    )
    for fragment, start in fragments:
        if not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", fragment, re.IGNORECASE):
            continue
        for match in _SQL_QUERY_RE.finditer(fragment):
            table = match.group("table")
            if _is_sql_keyword(table):
                continue
            uses.append(
                _TableUse(
                    file,
                    _line_number(text, start + match.start()),
                    table,
                )
            )
    uses.extend(
        _TableUse(file, _line_number(text, match.start()), match.group("table"))
        for match in _PRISMA_QUERY_RE.finditer(text)
    )
    return uses


def _string_literals(text: str) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    groups = ("triple_single", "triple_double", "backtick", "single", "double")
    for match in _STRING_LITERAL_RE.finditer(text):
        for group in groups:
            body = match.group(group)
            if body:
                result.append((body, match.start(group)))
                break
    return result


def _looks_like_sql_literal(text: str, start: int, body: str) -> bool:
    if not re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", body, re.IGNORECASE):
        return False
    prefix = text[max(0, start - 180) : start]
    return bool(
        re.search(
            r"\b(?:execute|executemany|query|prepare|raw|sql)\s*\([^()]*$",
            prefix,
            re.IGNORECASE,
        )
        or re.search(
            r"\b(?:query|sql|statement)\s*=\s*$",
            prefix,
            re.IGNORECASE,
        )
    )


def _is_frontend_file(file: str, text: str) -> bool:
    path = Path(file)
    lowered_parts = {part.lower() for part in path.parts[:-1]}
    if lowered_parts & _BACKEND_HINTS:
        return False
    if path.stem.lower() in _BACKEND_HINTS and not lowered_parts & _FRONTEND_HINTS:
        return False
    suffix = path.suffix.lower()
    if suffix in {".html", ".jsx", ".svelte", ".tsx", ".vue"}:
        return True
    if lowered_parts & _FRONTEND_HINTS:
        return True
    lowered = text.lower()
    return "react" in lowered or "document." in lowered or "window." in lowered


def _has_backend_surface(
    sources: list[Path],
    root: Path,
    routes: list[_RouteUse],
) -> bool:
    if routes:
        return True
    for path in sources:
        relative = path.relative_to(root)
        parts = {part.lower() for part in relative.parts}
        stem = path.stem.lower()
        if parts & _BACKEND_HINTS or stem in _BACKEND_HINTS:
            if not _is_frontend_file(relative.as_posix(), ""):
                return True
    return False


def _fetch_method(text: str, end: int) -> str:
    tail = text[end : end + 240]
    match = re.search(r"\bmethod\s*:\s*([\"'])(GET|POST|PUT|PATCH|DELETE)\1", tail, re.I)
    return match.group(2).upper() if match else "GET"


def _route_key(path: str) -> str:
    value = str(path or "").strip()
    value = value.split("?", 1)[0].split("#", 1)[0]
    value = re.sub(r"\$\{[^}]+\}|:[A-Za-z_][\w]*|\{[^}]+\}|\[[^\]]+\]", "{}", value)
    value = re.sub(r"/+", "/", f"/{value.lstrip('/')}")
    return value.rstrip("/") or "/"


def _table_key(table: str) -> str:
    value = re.sub(r"[^a-z0-9]", "", table.lower())
    if value.endswith("ies"):
        return f"{value[:-3]}y"
    if value.endswith("s") and not value.endswith("ss"):
        return value[:-1]
    return value


def _is_system_table(table: str) -> bool:
    return table.lower() in {"sqlite_master", "sqlite_sequence", "information_schema"}


def _is_sql_keyword(value: str) -> bool:
    return value.lower() in {
        "from",
        "join",
        "select",
        "set",
        "values",
        "where",
    }


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _dedupe_diagnostics(items: list[WiringDiagnostic]) -> list[WiringDiagnostic]:
    seen: set[tuple[str, int, str]] = set()
    result: list[WiringDiagnostic] = []
    for item in items:
        key = (item.file, item.line, item.message)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result
