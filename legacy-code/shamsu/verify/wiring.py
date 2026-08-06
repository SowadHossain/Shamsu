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
class WiringResult:
    diagnostics: tuple[WiringDiagnostic, ...] = ()
    frontend_calls: int = 0
    backend_routes: int = 0
    schema_tables: int = 0
    query_tables: int = 0

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

    return WiringResult(
        diagnostics=tuple(_dedupe_diagnostics(diagnostics)),
        frontend_calls=len(frontend_calls),
        backend_routes=len(backend_routes),
        schema_tables=len(schema_tables),
        query_tables=len(query_tables),
    )


def has_wiring_surface(project_root: Path | str) -> bool:
    return verify_wiring(project_root).has_surface


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
