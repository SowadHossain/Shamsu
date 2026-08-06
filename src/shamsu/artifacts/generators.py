"""Deterministic artifact generators.

Four generators, matching plan sections 15.1-15.4: repository manifest,
repository map, module cards, symbol cards.

Every claim these produce traces to a parser, a manifest file, or the
filesystem. Where something is genuinely not computable yet -- callers and
callees need the reference graph from Milestone 8 -- the card says so
explicitly. That matters more than it sounds: an empty "Callers" heading reads
as *nothing calls this*, which is a fabricated structural claim. "Not yet
computed" is honest; a blank section is not.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from shamsu.artifacts.hashing import hash_file, hash_text, scan_repository
from shamsu.artifacts.python_source import (
    DEFAULT_SOURCE_ROOTS,
    ExtractedModule,
    extract_python,
    module_path_for,
)
from shamsu.interfaces.artifacts import (
    ArtifactGenerationError,
    GeneratedArtifact,
    SourceRef,
)
from shamsu.interfaces.enums import ArtifactKind

#: Synthetic source path standing for "the set of files in the repository".
#:
#: Repository-wide artifacts -- the manifest and the map -- make claims that
#: depend on *which files exist*, not just on the contents of the few files
#: they read. Without this, deleting a module leaves the manifest's file count
#: and directory list FRESH and wrong, because `pyproject.toml` did not change.
#:
#: The angle brackets keep it from colliding with a real repository-relative
#: path. `RepositoryContext.freshness_map()` supplies its value, so
#: invalidation treats it exactly like any other source.
FILE_LIST_SOURCE = "<repository:file-list>"

MANIFEST_GENERATOR_VERSION = "repository-manifest/1"
MAP_GENERATOR_VERSION = "repository-map/1"
MODULE_CARD_GENERATOR_VERSION = "module-card/1"
SYMBOL_CARD_GENERATOR_VERSION = "symbol-card/1"

#: Language per file extension. Only what the manifest reports on -- this is a
#: summary for the model, not a linguist's census.
_LANGUAGES: Mapping[str, str] = {
    ".py": "Python",
    ".pyi": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".jsx": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".rs": "Rust",
    ".go": "Go",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".hpp": "C++",
    ".cs": "C#",
    ".swift": "Swift",
    ".sh": "Shell",
    ".sql": "SQL",
    ".html": "HTML",
    ".css": "CSS",
}

#: Manifest filename -> package manager.
_PACKAGE_MANAGERS: Mapping[str, str] = {
    "pyproject.toml": "pip/hatch",
    "requirements.txt": "pip",
    "Pipfile": "pipenv",
    "poetry.lock": "poetry",
    "uv.lock": "uv",
    "package.json": "npm",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "Cargo.toml": "cargo",
    "go.mod": "go modules",
    "Gemfile": "bundler",
    "composer.json": "composer",
    "pom.xml": "maven",
    "build.gradle": "gradle",
}


class RepositoryContext:
    """One scan of the repository, shared by every generator.

    Generators are constructed per refresh and share this, so a pass costs one
    filesystem walk and one parse per file rather than one per artifact. On a
    large repository that is the difference between a refresh that runs after
    every change and one that gets switched off.
    """

    def __init__(self, root: Path, *, use_git: bool = True) -> None:
        self.root = Path(root).resolve()
        self.hashes: dict[str, str] = scan_repository(self.root, use_git=use_git)
        self._parsed: dict[str, ExtractedModule] = {}
        self._internal_prefixes: tuple[str, ...] | None = None
        self.source_roots = self._detect_source_roots()

    def _detect_source_roots(self) -> tuple[str, ...]:
        """Which packaging roots to strip when deriving module paths.

        A directory is a packaging root only if it is NOT itself a package.
        `src/pkg/mod.py` imports as `pkg.mod` in a src-layout project, but as
        `src.pkg.mod` when `src/__init__.py` exists -- and getting this wrong
        silently breaks every import edge the module cards report.
        """
        return tuple(
            root
            for root in DEFAULT_SOURCE_ROOTS
            if f"{root}__init__.py" not in self.hashes
            and any(path.startswith(root) for path in self.hashes)
        )

    def module_path(self, path: str) -> str:
        """Dotted module path, using the roots that actually apply here."""
        return module_path_for(path, self.source_roots)

    # -- files -------------------------------------------------------------

    @property
    def paths(self) -> Sequence[str]:
        return tuple(self.hashes)

    def python_paths(self) -> Sequence[str]:
        return tuple(path for path in self.hashes if path.endswith(".py"))

    def read(self, path: str) -> str:
        try:
            return (self.root / path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ArtifactGenerationError(f"cannot read {path}: {exc}") from exc

    def source_ref(self, path: str) -> SourceRef:
        """A hash-carrying reference to a file, for artifact provenance."""
        known = self.hashes.get(path)
        return SourceRef(path=path, content_hash=known or hash_file(self.root / path))

    def file_list_digest(self) -> str:
        """A hash of the set of indexed paths.

        Changes when a file is added, removed, or renamed -- and notably not
        when one is merely edited, which is what the per-file hashes already
        cover.
        """
        return hash_text("\n".join(sorted(self.hashes)))

    def file_list_ref(self) -> SourceRef:
        """The synthetic source repository-wide artifacts depend on."""
        return SourceRef(path=FILE_LIST_SOURCE, content_hash=self.file_list_digest())

    def freshness_map(self) -> dict[str, str]:
        """Per-file hashes plus the synthetic file-list entry.

        What `ArtifactRegistry.recompute_status` must be given. Passing the raw
        `hashes` would make every repository-wide artifact look like it
        referenced a deleted file.
        """
        return {**self.hashes, FILE_LIST_SOURCE: self.file_list_digest()}

    # -- python ------------------------------------------------------------

    def parse(self, path: str) -> ExtractedModule:
        """Parse a Python file once per refresh."""
        cached = self._parsed.get(path)
        if cached is None:
            cached = extract_python(path, self.read(path))
            self._parsed[path] = cached
        return cached

    def internal_prefixes(self) -> tuple[str, ...]:
        """Top-level package names belonging to this project.

        Derived from the modules actually present, so telling an internal
        import from a third-party one needs no configuration.
        """
        if self._internal_prefixes is None:
            prefixes = {
                self.module_path(path).split(".")[0]
                for path in self.python_paths()
                if self.module_path(path)
            }
            self._internal_prefixes = tuple(sorted(prefix for prefix in prefixes if prefix))
        return self._internal_prefixes

    def module_index(self) -> Mapping[str, str]:
        """Dotted module path -> file path, for resolving import edges."""
        return {
            self.module_path(path): path for path in self.python_paths() if self.module_path(path)
        }

    def importers_of(self, module_path: str) -> Sequence[str]:
        """Files whose imports name `module_path`.

        A real reverse-dependency edge, computed from parsed imports. Not the
        full call graph -- that is Milestone 8 -- but enough to answer "what
        might this change break?" at module granularity.
        """
        found: list[str] = []
        for path in self.python_paths():
            module = self.parse(path)
            if module.parse_error:
                continue
            for imported in module.imports:
                if imported == module_path or imported.startswith(f"{module_path}."):
                    found.append(path)
                    break
        return tuple(sorted(found))

    def related_tests(self, path: str) -> Sequence[str]:
        """Test files plausibly covering `path`.

        Convention-based: `store.py` matches `test_store.py` / `store_test.py`.
        Named "plausibly" on purpose -- real coverage mapping needs the call
        graph, and the card labels this as convention-derived so it is not
        mistaken for measured coverage.
        """
        stem = Path(path).stem
        if stem in ("__init__", "__main__"):
            stem = Path(path).parent.name
        if not stem:
            return ()

        wanted = {f"test_{stem}.py", f"{stem}_test.py", f"test_{stem}s.py"}
        return tuple(
            sorted(
                candidate
                for candidate in self.hashes
                if Path(candidate).name in wanted and candidate != path
            )
        )


# ---------------------------------------------------------------------------
# 15.1 Repository manifest
# ---------------------------------------------------------------------------


class RepositoryManifestGenerator:
    """Plan section 15.1. What kind of project is this, and how is it run?"""

    kind = ArtifactKind.REPOSITORY_MANIFEST
    generator_version = MANIFEST_GENERATOR_VERSION
    KEY = "repository_manifest"

    def __init__(self, context: RepositoryContext) -> None:
        self._context = context

    def keys(self) -> Sequence[str]:
        return (self.KEY,)

    def generate(self, key: str) -> GeneratedArtifact:
        context = self._context
        sources: list[str] = []

        languages = self._languages()
        managers, manifest_files = self._package_managers()
        sources.extend(manifest_files)

        build, run, test = self._commands(manifest_files)
        docker = self._docker_files()
        env_files = self._env_files()
        sources.extend(docker)

        payload = {
            "name": context.root.name,
            "languages": languages,
            "package_managers": managers,
            "manifest_files": manifest_files,
            "entry_points": self._entry_points(),
            "major_directories": self._major_directories(),
            "test_frameworks": self._test_frameworks(manifest_files),
            "build_commands": build,
            "run_commands": run,
            "test_commands": test,
            "environment_files": env_files,
            "docker": docker,
            "file_count": len(context.hashes),
            "generator_version": self.generator_version,
        }

        refs = [context.file_list_ref()]
        refs += [context.source_ref(path) for path in sorted(set(sources))]

        return GeneratedArtifact(
            content=json.dumps(payload, indent=2, sort_keys=True) + "\n",
            sources=tuple(refs),
        )

    # -- extraction --------------------------------------------------------

    def _languages(self) -> list[str]:
        """Languages present, most files first."""
        counts: dict[str, int] = {}
        for path in self._context.hashes:
            language = _LANGUAGES.get(Path(path).suffix.lower())
            if language:
                counts[language] = counts.get(language, 0) + 1
        return [name for name, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]

    def _package_managers(self) -> tuple[list[str], list[str]]:
        managers: set[str] = set()
        files: list[str] = []
        for path in self._context.hashes:
            manager = _PACKAGE_MANAGERS.get(Path(path).name)
            # Root-level manifests only. A fixture package.json three levels
            # deep in a test tree does not make this an npm project.
            if manager and Path(path).parent == Path():
                managers.add(manager)
                files.append(path)
        return sorted(managers), sorted(files)

    def _entry_points(self) -> list[str]:
        """Plausible ways in: `__main__.py`, conventional app/server files."""
        wanted = {"__main__.py", "main.py", "app.py", "cli.py", "server.py", "index.js"}
        return sorted(
            path
            for path in self._context.hashes
            if Path(path).name in wanted and "test" not in path
        )

    def _major_directories(self) -> list[str]:
        """Top two levels holding source, by file count."""
        counts: dict[str, int] = {}
        for path in self._context.hashes:
            parts = Path(path).parts
            if len(parts) < 2:
                continue
            key = "/".join(parts[:2]) if len(parts) > 2 else parts[0]
            counts[key] = counts.get(key, 0) + 1
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        return [name for name, count in ranked if count > 1][:20]

    def _test_frameworks(self, manifest_files: Sequence[str]) -> list[str]:
        found: set[str] = set()
        for path in manifest_files:
            if Path(path).name != "pyproject.toml":
                continue
            config = self._toml(path)
            dev = (config.get("project", {}).get("optional-dependencies") or {}).values()
            declared = [dep for group in dev for dep in group if isinstance(dep, str)] + list(
                config.get("project", {}).get("dependencies") or []
            )
            for dep in declared:
                for name in ("pytest", "unittest", "nose"):
                    if isinstance(dep, str) and dep.startswith(name):
                        found.add(name)
            if "pytest" in config.get("tool", {}):
                found.add("pytest")

        if not found and any(Path(path).name.startswith("test_") for path in self._context.hashes):
            found.add("pytest")
        return sorted(found)

    def _commands(self, manifest_files: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
        """Build, run, and test commands read from project manifests."""
        build: list[str] = []
        run: list[str] = []
        test: list[str] = []

        for path in manifest_files:
            name = Path(path).name
            if name == "pyproject.toml":
                config = self._toml(path)
                if config.get("build-system"):
                    build.append("python -m build")
                if config.get("tool", {}).get("pytest"):
                    test.append("pytest")
                for script in config.get("project", {}).get("scripts") or {}:
                    run.append(str(script))
            elif name == "package.json":
                config = self._json(path)
                for script in config.get("scripts") or {}:
                    if script in ("build", "compile"):
                        build.append(f"npm run {script}")
                    elif script in ("start", "dev", "serve"):
                        run.append(f"npm run {script}")
                    elif script in ("test", "test:unit"):
                        test.append(f"npm run {script}")

        return sorted(set(build)), sorted(set(run)), sorted(set(test))

    def _docker_files(self) -> list[str]:
        wanted = ("Dockerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml")
        return sorted(path for path in self._context.hashes if Path(path).name in wanted)

    def _env_files(self) -> list[str]:
        return sorted(
            path
            for path in self._context.hashes
            if Path(path).name.startswith(".env") or Path(path).name == "env.example"
        )

    # `Any` rather than `object` in these two return types is deliberate, not a
    # shortcut. A third-party manifest has no schema this code controls, so the
    # value type genuinely is unknown; `object` would only force a cast at every
    # access and claim a precision that does not exist. Every read below is
    # defensive (`.get(...) or {}`, `isinstance` guards) precisely because of it.

    def _toml(self, path: str) -> dict[str, Any]:
        try:
            return tomllib.loads(self._context.read(path))
        except (tomllib.TOMLDecodeError, ArtifactGenerationError):
            # A malformed manifest is a fact about the repo, not a reason to
            # fail the whole artifact. It contributes nothing and says nothing.
            return {}

    def _json(self, path: str) -> dict[str, Any]:
        try:
            loaded = json.loads(self._context.read(path))
        except (json.JSONDecodeError, ArtifactGenerationError):
            return {}
        return loaded if isinstance(loaded, dict) else {}


# ---------------------------------------------------------------------------
# 15.2 Repository map
# ---------------------------------------------------------------------------


class RepositoryMapGenerator:
    """Plan section 15.2. A compact directory map with descriptions."""

    kind = ArtifactKind.REPOSITORY_MAP
    generator_version = MAP_GENERATOR_VERSION
    KEY = "repository_map"

    #: Beyond this depth a map stops being a map and becomes a file listing.
    MAX_DEPTH = 3

    def __init__(self, context: RepositoryContext) -> None:
        self._context = context

    def keys(self) -> Sequence[str]:
        return (self.KEY,)

    def generate(self, key: str) -> GeneratedArtifact:
        context = self._context
        directories: dict[str, list[str]] = {}

        for path in sorted(context.hashes):
            parent = str(Path(path).parent).replace("\\", "/")
            if parent == ".":
                parent = "(root)"
            if parent != "(root)" and len(Path(parent).parts) > self.MAX_DEPTH:
                parent = "/".join(Path(parent).parts[: self.MAX_DEPTH])
            directories.setdefault(parent, []).append(path)

        lines = [
            f"# Repository map: {context.root.name}",
            "",
            f"{len(context.hashes)} indexed files across {len(directories)} directories.",
            "",
        ]

        used_sources: list[str] = []
        for directory in sorted(directories):
            files = directories[directory]
            lines.append(f"## {directory}/" if directory != "(root)" else "## (root)")
            lines.append(f"{len(files)} file(s)")

            description, source = self._describe(directory, files)
            if description:
                lines.append(description)
                if source:
                    used_sources.append(source)

            entry = self._entry_point(files)
            if entry:
                lines.append(f"Entry: {entry}")
            lines.append("")

        # The map's structure depends on which files exist, not only on the
        # `__init__.py` files it read descriptions from.
        refs = [context.file_list_ref()]
        refs += [context.source_ref(path) for path in sorted(set(used_sources))]

        return GeneratedArtifact(content="\n".join(lines), sources=tuple(refs))

    def _describe(self, directory: str, files: Sequence[str]) -> tuple[str, str | None]:
        """A description taken from the package docstring, if there is one.

        Read from source, never invented. A directory with no docstring gets no
        description rather than a guess about its purpose.
        """
        for candidate in files:
            if Path(candidate).name != "__init__.py":
                continue
            module = self._context.parse(candidate)
            if module.summary:
                return module.summary, candidate
        return "", None

    @staticmethod
    def _entry_point(files: Sequence[str]) -> str | None:
        for candidate in files:
            if Path(candidate).name in ("__main__.py", "main.py", "index.js", "app.py"):
                return candidate
        return None


# ---------------------------------------------------------------------------
# 15.3 Module cards
# ---------------------------------------------------------------------------


class ModuleCardGenerator:
    """Plan section 15.3. One card per Python module."""

    kind = ArtifactKind.MODULE_CARD
    generator_version = MODULE_CARD_GENERATOR_VERSION

    #: Modules below this size rarely earn a card; their content fits in the
    #: repository map. Keeps the artifact set proportional to the codebase.
    MIN_SYMBOLS = 1

    def __init__(self, context: RepositoryContext) -> None:
        self._context = context

    def keys(self) -> Sequence[str]:
        """Every Python module with at least one extractable symbol."""
        keys: list[str] = []
        for path in self._context.python_paths():
            module = self._context.parse(path)
            if module.parse_error:
                continue
            if len(module.symbols) >= self.MIN_SYMBOLS:
                keys.append(path)
        return tuple(keys)

    def generate(self, key: str) -> GeneratedArtifact:
        context = self._context
        module = context.parse(key)

        if module.parse_error:
            raise ArtifactGenerationError(f"{key} could not be parsed: {module.parse_error}")

        internal = context.internal_prefixes()
        external_deps = module.external_imports(internal)
        internal_deps = module.internal_imports(internal)
        importers = context.importers_of(context.module_path(key))
        tests = context.related_tests(key)

        lines = [
            f"# {context.module_path(key) or key}",
            "",
            f"**Path:** `{key}`",
            "",
        ]

        if module.summary:
            lines += ["## Purpose", "", module.summary, ""]

        public = module.public_symbols
        if public:
            lines += ["## Public interface", ""]
            for symbol in public:
                suffix = f" — {symbol.summary}" if symbol.summary else ""
                lines.append(
                    f"- `{symbol.signature}` ({symbol.kind}, L{symbol.line_start}){suffix}"
                )
            lines.append("")

        private = [symbol for symbol in module.symbols if not symbol.is_public]
        if private:
            lines += [
                "## Internal symbols",
                "",
                ", ".join(f"`{symbol.qualified_name}`" for symbol in private),
                "",
            ]

        if internal_deps:
            lines += ["## Depends on (internal)", ""]
            lines += [f"- `{name}`" for name in internal_deps]
            lines.append("")

        if external_deps:
            lines += ["## Depends on (external)", ""]
            lines += [f"- `{name}`" for name in sorted(set(external_deps))]
            lines.append("")

        lines += ["## Imported by", ""]
        if importers:
            lines += [f"- `{path}`" for path in importers]
        else:
            lines.append("Nothing in this repository imports it.")
        lines.append("")

        lines += ["## Related tests", ""]
        if tests:
            lines += [f"- `{path}`" for path in tests]
            lines.append("")
            lines.append("_Matched by filename convention, not measured coverage._")
        else:
            lines.append("None found by filename convention.")
        lines.append("")

        # Deliberately explicit. A blank "Callers" heading would read as
        # "nothing calls this", which is a structural claim we have not earned.
        lines += [
            "## Not yet computed",
            "",
            "- Call graph (callers/callees at symbol granularity) — Milestone 8",
            "- Measured test coverage — Milestone 8",
            "- Known risks — requires failure history",
            "",
        ]

        sources = [context.source_ref(key)]
        sources += [context.source_ref(path) for path in importers]

        return GeneratedArtifact(
            content="\n".join(lines),
            # Deduplicated by path: an importer that is also the module itself
            # would otherwise violate the sources primary key.
            sources=tuple({ref.path: ref for ref in sources}.values()),
        )


# ---------------------------------------------------------------------------
# 15.4 Symbol cards
# ---------------------------------------------------------------------------


class SymbolCardGenerator:
    """Plan section 15.4. One card per public top-level symbol.

    Scoped to public top-level symbols on purpose. A card for every private
    helper would multiply the artifact count several-fold while adding nothing
    the module card does not already carry.
    """

    kind = ArtifactKind.SYMBOL_CARD
    generator_version = SYMBOL_CARD_GENERATOR_VERSION

    def __init__(self, context: RepositoryContext) -> None:
        self._context = context

    def keys(self) -> Sequence[str]:
        keys: list[str] = []
        for path in self._context.python_paths():
            module = self._context.parse(path)
            if module.parse_error:
                continue
            for symbol in module.symbols:
                if symbol.is_public and symbol.kind in ("function", "async function", "class"):
                    keys.append(f"{path}::{symbol.qualified_name}")
        return tuple(keys)

    def generate(self, key: str) -> GeneratedArtifact:
        path, _, qualified = key.partition("::")
        if not qualified:
            raise ArtifactGenerationError(f"malformed symbol key: {key!r}")

        context = self._context
        module = context.parse(path)
        if module.parse_error:
            raise ArtifactGenerationError(f"{path} could not be parsed: {module.parse_error}")

        symbol = next((item for item in module.symbols if item.qualified_name == qualified), None)
        if symbol is None:
            raise ArtifactGenerationError(f"{qualified} not found in {path}")

        methods = [
            item for item in module.symbols if item.qualified_name.startswith(f"{qualified}.")
        ]
        source_ref = context.source_ref(path)

        lines = [
            f"# {context.module_path(path)}.{qualified}",
            "",
            f"**Kind:** {symbol.kind}",
            f"**Path:** `{path}` L{symbol.line_start}–{symbol.line_end}",
            f"**Signature:** `{symbol.signature}`",
            # The hash makes the card self-describing: it records exactly which
            # version of the file it was built from, independent of the registry.
            f"**Source hash:** `{source_ref.content_hash}`",
            "",
        ]

        if symbol.decorators:
            lines += [
                "**Decorators:** " + ", ".join(f"`@{d}`" for d in symbol.decorators),
                "",
            ]

        if symbol.summary:
            lines += ["## Purpose", "", symbol.summary, ""]

        if methods:
            lines += ["## Methods", ""]
            for method in methods:
                suffix = f" — {method.summary}" if method.summary else ""
                lines.append(f"- `{method.signature}` (L{method.line_start}){suffix}")
            lines.append("")

        tests = context.related_tests(path)
        lines += ["## Related tests", ""]
        if tests:
            lines += [f"- `{candidate}`" for candidate in tests]
            lines.append("")
            lines.append("_Matched by filename convention, not measured coverage._")
        else:
            lines.append("None found by filename convention.")
        lines.append("")

        lines += [
            "## Not yet computed",
            "",
            "- Callers and callees — Milestone 8",
            "- Side effects — requires effect analysis",
            "- Related configuration — Milestone 8",
            "",
        ]

        return GeneratedArtifact(content="\n".join(lines), sources=(source_ref,))


__all__ = [
    "FILE_LIST_SOURCE",
    "MANIFEST_GENERATOR_VERSION",
    "MAP_GENERATOR_VERSION",
    "MODULE_CARD_GENERATOR_VERSION",
    "SYMBOL_CARD_GENERATOR_VERSION",
    "ModuleCardGenerator",
    "RepositoryContext",
    "RepositoryManifestGenerator",
    "RepositoryMapGenerator",
    "SymbolCardGenerator",
]
