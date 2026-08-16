"""Structural retrieval over a Python repository.

Plan §18 fixes the retrieval order, and the order is the design:

    exact path → exact text → symbol index → reference graph → call graph
    → related tests → dependency graph → git history → semantic fallback

Semantic search is stage 9 and a *fallback*. "Who calls this?" has a correct
answer; an embedding similarity score is not it.

**Built on stdlib `ast`, not tree-sitter.** The plan names tree-sitter, and a
tree-sitter backend is the right way to add other languages later — the
`CodeIndex` protocol exists so that can happen without touching callers. For
Python it would be a step backwards: `ast` is the language's own parser, so it
is exact where tree-sitter is approximate, and it costs no dependency and no
bundled grammar. v2's dependency surface is deliberately small and earns
additions per milestone. This one has not been earned yet.

**What this index does not claim.** Python name binding is not statically
decidable, so references and callers are matched *by name*. Two classes with a
`save` method are indistinguishable here. That over-approximates, which is the
safe direction for scoping a change — an extra file is one the agent reads and
discards, a missed one is a silently unsafe edit — but it is never proof.
`ImpactReport.truncated` and `SearchHit.provenance` exist so a caller can tell
what it is holding.
"""

from __future__ import annotations

import fnmatch
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from shamsu.artifacts.hashing import scan_repository
from shamsu.artifacts.python_source import (
    ExtractedModule,
    ExtractedSymbol,
    extract_python,
    module_path_for,
)
from shamsu.interfaces.code_intelligence import ImpactReport, LineRange, SearchHit, SymbolRef
from shamsu.security.paths import workspace_key

#: How far `impact` walks the caller graph. A change's reach is unbounded in
#: principle; a report nobody can read is not more useful than a bounded one
#: that says it was truncated.
MAX_IMPACT_DEPTH = 3
MAX_IMPACT_MODULES = 40

#: How much of a matching line is carried into a hit. A hit competes for the
#: source-code budget, and a 400-character line would evict three real ones.
EXCERPT_CHARS = 240


class PythonCodeIndex:
    """A deterministic structural index of the Python files in a workspace.

    Satisfies `shamsu.interfaces.code_intelligence.CodeIndex`.
    """

    def __init__(self, workspace: Path, *, use_git: bool = True) -> None:
        self._root = Path(workspace).resolve()
        self._use_git = use_git

        self._hashes: dict[str, str] = {}
        self._modules: dict[str, ExtractedModule] = {}
        self._sources: dict[str, str] = {}
        self._by_name: dict[str, list[SymbolRef]] = defaultdict(list)
        self._by_qualified: dict[str, list[SymbolRef]] = defaultdict(list)
        self._callers: dict[str, set[str]] = defaultdict(set)
        self._ready = False

    # -- building ----------------------------------------------------------

    def build(self) -> PythonCodeIndex:
        """Scan and parse the workspace. Idempotent."""
        self._hashes = scan_repository(self._root, use_git=self._use_git)
        self._modules.clear()
        self._sources.clear()
        self._by_name.clear()
        self._by_qualified.clear()
        self._callers.clear()

        for path in self._hashes:
            if not path.endswith(".py"):
                continue
            try:
                source = (self._root / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                # An unreadable file is skipped, not fatal. A repository with
                # one bad file still deserves an index of the rest.
                continue

            self._sources[path] = source
            module = extract_python(path, source)
            self._modules[path] = module
            self._register(module)

        self._ready = True
        return self

    def _register(self, module: ExtractedModule) -> None:
        for symbol in module.symbols:
            reference = _to_ref(module, symbol)
            self._by_name[symbol.name].append(reference)
            self._by_qualified[f"{module.module_path}.{symbol.qualified_name}"].append(reference)
            for called in symbol.calls:
                self._callers[called].add(reference.qualified_name)

    def is_ready(self) -> bool:
        """Whether the index is built *and* still matches what is on disk.

        Re-scans hashes rather than trusting a marker. v1 gated on a marker
        file that could disagree with the actual index, so the agent silently
        answered structural questions from a stale one.
        """
        if not self._ready:
            return False
        return scan_repository(self._root, use_git=self._use_git) == self._hashes

    @property
    def indexed_files(self) -> Sequence[str]:
        return tuple(sorted(self._modules))

    def symbols_in(self, path: str) -> Sequence[SymbolRef]:
        """Every symbol defined in one file."""
        module = self._modules.get(workspace_key(path))
        if module is None:
            return ()
        return tuple(_to_ref(module, symbol) for symbol in module.symbols)

    @property
    def parse_failures(self) -> Sequence[str]:
        """Files that could not be parsed, so a caller can say so honestly."""
        return tuple(sorted(path for path, module in self._modules.items() if module.parse_error))

    # -- stage 1: paths ----------------------------------------------------

    def find_file(self, pattern: str) -> Sequence[str]:
        """Exact path, then glob, then basename. Most specific match first."""
        needle = workspace_key(pattern)
        if needle in self._hashes:
            return (needle,)

        globbed = [path for path in self._hashes if fnmatch.fnmatch(path, needle)]
        if globbed:
            return tuple(sorted(globbed))

        return tuple(sorted(path for path in self._hashes if Path(path).name == Path(needle).name))

    # -- stage 2: text -----------------------------------------------------

    def search_text(self, query: str, *, limit: int = 20) -> Sequence[SearchHit]:
        """Literal substring search. Case-sensitive, because code is."""
        if not query:
            return ()

        hits: list[SearchHit] = []
        for path in sorted(self._sources):
            for number, line in enumerate(self._sources[path].splitlines(), start=1):
                if query in line:
                    hits.append(
                        SearchHit(
                            path=path,
                            lines=LineRange(start=number, end=number),
                            excerpt=line.strip()[:EXCERPT_CHARS],
                            score=1.0,
                            provenance="text",
                        )
                    )
                    if len(hits) >= limit:
                        return tuple(hits)
        return tuple(hits)

    # -- stage 3: symbols --------------------------------------------------

    def lookup_symbol(self, name: str) -> Sequence[SymbolRef]:
        """Exact qualified name first, then bare name, then case-insensitive.

        Ordered narrowest-first so an exact answer is never diluted by fuzzy
        ones. A caller taking `[0]` gets the best match, not an arbitrary one.
        """
        if name in self._by_qualified:
            return tuple(self._by_qualified[name])

        bare = name.rsplit(".", 1)[-1]
        if bare in self._by_name:
            matches = self._by_name[bare]
            if "." in name:
                # `Class.method` — keep only symbols whose own qualified name
                # ends that way, so `Other.method` does not answer for it.
                narrowed = [ref for ref in matches if ref.qualified_name.endswith(name)]
                if narrowed:
                    return tuple(narrowed)
            return tuple(matches)

        lowered = name.lower()
        return tuple(
            reference
            for key, refs in sorted(self._by_name.items())
            if key.lower() == lowered
            for reference in refs
        )

    # -- stage 4: references -----------------------------------------------

    def references(self, symbol: SymbolRef) -> Sequence[SearchHit]:
        """Every *use* of the symbol's name, excluding its own definition."""
        target = symbol.qualified_name.rsplit(".", 1)[-1]
        hits: list[SearchHit] = []

        for path in sorted(self._modules):
            module = self._modules[path]
            lines = self._sources.get(path, "").splitlines()
            seen: set[int] = set()

            for reference in module.references:
                if reference.name != target or reference.line in seen:
                    continue
                if path == symbol.path and symbol.lines.start <= reference.line <= symbol.lines.end:
                    # Recursion and self-reference inside the definition are
                    # not "somewhere else that would break".
                    continue
                seen.add(reference.line)
                excerpt = lines[reference.line - 1].strip() if reference.line <= len(lines) else ""
                hits.append(
                    SearchHit(
                        path=path,
                        lines=LineRange(start=reference.line, end=reference.line),
                        excerpt=excerpt[:EXCERPT_CHARS],
                        score=1.0,
                        provenance="reference",
                    )
                )

        return tuple(hits)

    # -- stage 5: call graph -----------------------------------------------

    def callers(self, symbol: SymbolRef) -> Sequence[SymbolRef]:
        """Symbols that call this one, matched by name."""
        target = symbol.qualified_name.rsplit(".", 1)[-1]
        found: list[SymbolRef] = []

        for qualified in sorted(self._callers.get(target, set())):
            for reference in self._by_qualified.get(qualified, ()):
                if reference != symbol:
                    found.append(reference)
        return tuple(found)

    def callees(self, symbol: SymbolRef) -> Sequence[SymbolRef]:
        """Symbols this one calls that are defined in the repository.

        Calls to anything outside the repository are dropped rather than
        returned as unresolved names: a `SymbolRef` must point at a real
        location, and inventing one for `len` would break that promise.
        """
        extracted = self._extracted(symbol)
        if extracted is None:
            return ()

        found: list[SymbolRef] = []
        for called in extracted.calls:
            for candidate in self._by_name.get(called, ()):
                if candidate != symbol:
                    found.append(candidate)
        return tuple(found)

    # -- stage 6: related tests --------------------------------------------

    def related_tests(self, path: str) -> Sequence[str]:
        """Test files that reach this file, strongest evidence first.

        Three rules, and the middle one is the one that matters in practice:

        1. The test imports the module directly. A fact.
        2. The test imports a *package* containing it and uses a name the
           module defines. Also grounded — `from shamsu.verification import
           digest_test_output` never mentions `digest.py`, and a rule that only
           matched full module paths would call that file untested.
        3. The test's filename follows the `test_<stem>` convention. A
           heuristic, consulted last, and only when neither import rule fired.
        """
        normalised = workspace_key(path)
        module = self._modules.get(normalised)
        module_path = module.module_path if module else module_path_for(normalised)
        stem = Path(normalised).stem
        # Top-level names only. Methods collide constantly across unrelated
        # classes — `record`, `run`, `close` — and a single collision would
        # relate a module to every test in the repository. A test that
        # exercises a module names something at its top level.
        defined = (
            {symbol.name for symbol in module.symbols if "." not in symbol.qualified_name}
            if module
            else set()
        )

        direct: list[str] = []
        via_package: list[str] = []
        by_name: list[str] = []

        for candidate, parsed in sorted(self._modules.items()):
            if candidate == normalised or not _is_test_path(candidate):
                continue

            if any(
                imported in (module_path, stem) or imported.endswith(f".{stem}")
                for imported in parsed.imports
            ):
                direct.append(candidate)
                continue

            if defined and any(_is_prefix_of(imported, module_path) for imported in parsed.imports):
                used = {reference.name for reference in parsed.references}
                if used & defined:
                    via_package.append(candidate)
                    continue

            if Path(candidate).stem in (f"test_{stem}", f"{stem}_test"):
                by_name.append(candidate)

        return tuple(direct + via_package + by_name)

    # -- stages 4-7: impact ------------------------------------------------

    def impact(self, symbol: SymbolRef) -> ImpactReport:
        """What a change to this symbol could reach.

        Breadth-first over callers, bounded in both depth and breadth. The
        bounds are reported rather than hidden: a truncated report is not proof
        that nothing else is affected, and a caller that treats it as one has
        made exactly the mistake this field exists to prevent.
        """
        direct = self.callers(symbol)

        modules: list[str] = []
        seen: set[str] = {symbol.qualified_name}
        frontier = list(direct)
        truncated = False

        for _ in range(MAX_IMPACT_DEPTH):
            if not frontier:
                break
            next_frontier: list[SymbolRef] = []
            for reference in frontier:
                if reference.qualified_name in seen:
                    continue
                seen.add(reference.qualified_name)
                if reference.path not in modules:
                    if len(modules) >= MAX_IMPACT_MODULES:
                        truncated = True
                        break
                    modules.append(reference.path)
                next_frontier.extend(self.callers(reference))
            if truncated:
                break
            frontier = next_frontier
        else:
            truncated = truncated or bool(frontier)

        tests: list[str] = []
        for path in [symbol.path, *modules]:
            for test in self.related_tests(path):
                if test not in tests:
                    tests.append(test)

        return ImpactReport(
            symbol=symbol,
            direct_callers=tuple(direct),
            transitive_modules=tuple(modules),
            related_tests=tuple(tests),
            truncated=truncated,
        )

    # -- internals ---------------------------------------------------------

    def _extracted(self, symbol: SymbolRef) -> ExtractedSymbol | None:
        module = self._modules.get(symbol.path)
        if module is None:
            return None
        local = symbol.qualified_name
        if module.module_path and local.startswith(f"{module.module_path}."):
            local = local[len(module.module_path) + 1 :]
        for candidate in module.symbols:
            if candidate.qualified_name == local:
                return candidate
        return None


def _to_ref(module: ExtractedModule, symbol: ExtractedSymbol) -> SymbolRef:
    return SymbolRef(
        qualified_name=f"{module.module_path}.{symbol.qualified_name}"
        if module.module_path
        else symbol.qualified_name,
        path=module.path,
        lines=LineRange(start=symbol.line_start, end=symbol.line_end),
        kind=symbol.kind,
        signature=symbol.signature or None,
    )


def _is_prefix_of(imported: str, module_path: str) -> bool:
    """Whether `imported` is the module itself or a package containing it."""
    return bool(module_path) and (imported == module_path or module_path.startswith(f"{imported}."))


def _is_test_path(path: str) -> bool:
    """Whether a path is a test file, by the conventions v2 targets first."""
    normalised = path.replace("\\", "/")
    name = Path(normalised).name
    return (
        normalised.startswith("tests/")
        or "/tests/" in normalised
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name == "conftest.py"
    )


__all__ = [
    "EXCERPT_CHARS",
    "MAX_IMPACT_DEPTH",
    "MAX_IMPACT_MODULES",
    "PythonCodeIndex",
]
