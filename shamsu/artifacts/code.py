"""Deterministic repository artifact generation for compact code navigation."""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from shamsu.indexer.policy import SOURCE_SUFFIXES, walk_workspace_files, workspace_manifest

ARTIFACT_ROOT = Path(".shamsu") / "artifacts"
MODULES_DIR = "modules"
SYMBOLS_DIR = "symbols"
SCHEMA_VERSION = 1
ARTIFACT_VERSION = 1
GENERATOR_VERSION = "code-artifacts-v1"
FRESHNESS_INDEX = "freshness_index.json"
CONTRADICTIONS_LOG = "contradictions.jsonl"
REGENERATION_QUEUE = "regeneration_queue.json"
CODE_INDEX = "code_index.json"

CONFIG_NAMES = {
    ".env",
    ".env.example",
    ".gitignore",
    "docker-compose.yml",
    "docker-compose.yaml",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "tsconfig.json",
}


class FreshnessStatus(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"
    INVALIDATED = "INVALIDATED"
    MISSING = "MISSING"
    GENERATION_FAILED = "GENERATION_FAILED"


@dataclass(frozen=True)
class SymbolCard:
    fully_qualified_symbol: str
    path: str
    line_range: tuple[int, int]
    signature: str
    type: str
    purpose: str
    callers: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)
    related_tests: list[str] = field(default_factory=list)
    side_effects: list[str] = field(default_factory=list)
    source_hash: str = ""


@dataclass(frozen=True)
class ModuleCard:
    path: str
    module: str
    purpose: str
    main_symbols: list[str] = field(default_factory=list)
    public_interfaces: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    callers: list[str] = field(default_factory=list)
    callees: list[str] = field(default_factory=list)
    related_tests: list[str] = field(default_factory=list)
    important_configuration: list[str] = field(default_factory=list)
    source_hash: str = ""
    symbol_cards: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ArtifactBuildResult:
    root: Path
    manifest_hash: str
    files_indexed: int
    modules: int
    symbols: int


def artifacts_root(workspace: Path) -> Path:
    return Path(workspace).resolve() / ARTIFACT_ROOT


def ensure_repository_artifacts(workspace: Path) -> ArtifactBuildResult:
    root = artifacts_root(workspace)
    manifest_path = root / "repository_manifest.json"
    current = workspace_manifest(workspace)
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        if (
            data.get("schema_version") == SCHEMA_VERSION
            and data.get("artifact_version") == ARTIFACT_VERSION
            and data.get("generator_version") == GENERATOR_VERSION
            and data.get("freshness_status") == FreshnessStatus.FRESH.value
            and data.get("workspace", {}).get("manifest_hash") == current.get("manifest_hash")
            and _payload_sources_match(workspace, data)
        ):
            return ArtifactBuildResult(
                root=root,
                manifest_hash=str(current.get("manifest_hash", "")),
                files_indexed=int(data.get("file_count", 0) or 0),
                modules=int(data.get("module_count", 0) or 0),
                symbols=int(data.get("symbol_count", 0) or 0),
            )
    try:
        return build_repository_artifacts(workspace)
    except Exception as exc:
        _record_generation_failed(root, exc)
        raise


def build_repository_artifacts(workspace: Path) -> ArtifactBuildResult:
    workspace = Path(workspace).resolve()
    root = artifacts_root(workspace)
    modules_dir = root / MODULES_DIR
    symbols_dir = root / SYMBOLS_DIR
    modules_dir.mkdir(parents=True, exist_ok=True)
    symbols_dir.mkdir(parents=True, exist_ok=True)
    for directory in (modules_dir, symbols_dir):
        for existing in directory.glob("*.json"):
            try:
                existing.unlink()
            except OSError:
                pass

    files = walk_workspace_files(workspace, suffixes=SOURCE_SUFFIXES, indexable_only=True)
    module_cards: list[ModuleCard] = []
    import_edges: list[dict[str, str]] = []
    call_edges: list[dict[str, str]] = []
    symbol_by_fq: dict[str, SymbolCard] = {}
    freshness_records: dict[str, dict[str, Any]] = {}

    for path in files:
        relative = _relative(path, workspace)
        if not relative:
            continue
        module, symbols, imports, calls = _analyze_file(path, relative)
        for symbol in symbols:
            symbol_by_fq[symbol.fully_qualified_symbol] = symbol
        module_cards.append(
            ModuleCard(
                path=relative,
                module=module,
                purpose=_module_purpose(relative, symbols),
                main_symbols=[symbol.fully_qualified_symbol for symbol in symbols[:12]],
                public_interfaces=[
                    symbol.fully_qualified_symbol
                    for symbol in symbols
                    if not Path(symbol.fully_qualified_symbol.split(".")[-1]).name.startswith("_")
                ][:20],
                imports=imports,
                dependencies=_internal_dependencies(imports, files, workspace),
                callees=sorted(calls)[:60],
                important_configuration=_config_notes(path),
                source_hash=_file_hash(path),
            )
        )
        import_edges.extend({"from": relative, "import": item} for item in imports)

    related_tests = _related_tests(module_cards)
    reverse_callers = _reverse_callers(symbol_by_fq)
    finalized_modules: list[ModuleCard] = []
    for module in module_cards:
        module_callers = sorted(
            {
                caller
                for symbol in module.main_symbols
                for caller in reverse_callers.get(symbol, [])
            }
        )
        card = ModuleCard(
            **{
                **asdict(module),
                "callers": module_callers[:60],
                "related_tests": related_tests.get(module.path, []),
                "symbol_cards": [
                    _symbol_card_path(symbol_by_fq[symbol]) for symbol in module.main_symbols if symbol in symbol_by_fq
                ],
            }
        )
        finalized_modules.append(card)
        _write_json_artifact(
            modules_dir / _card_filename(module.path),
            asdict(card),
            artifact_type="module_card",
            source_paths=[card.path],
            workspace=workspace,
            records=freshness_records,
        )

    finalized_symbols: list[SymbolCard] = []
    for symbol in symbol_by_fq.values():
        callers = reverse_callers.get(symbol.fully_qualified_symbol, [])
        card = SymbolCard(
            **{
                **asdict(symbol),
                "callers": callers[:60],
                "related_tests": related_tests.get(symbol.path, []),
            }
        )
        finalized_symbols.append(card)
        _write_json_artifact(
            symbols_dir / _symbol_card_path(card),
            asdict(card),
            artifact_type="symbol_card",
            source_paths=[card.path],
            workspace=workspace,
            records=freshness_records,
        )
        for callee in card.callees:
            call_edges.append({"from": card.fully_qualified_symbol, "to": callee})

    source_paths = [card.path for card in finalized_modules]
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": workspace_manifest(workspace),
        "file_count": len(files),
        "module_count": len(finalized_modules),
        "symbol_count": len(finalized_symbols),
            "artifact_paths": {
                "repository_map": "repository_map.md",
                "code_index": CODE_INDEX,
                "dependency_graph": "dependency_graph.json",
            "api_map": "api_map.json",
            "test_map": "test_map.json",
            "configuration_map": "configuration_map.json",
            "modules": MODULES_DIR + "/",
            "symbols": SYMBOLS_DIR + "/",
        },
        "files": [
            {
                "path": card.path,
                "source_hash": card.source_hash,
                "module_card": f"{MODULES_DIR}/{_card_filename(card.path)}",
                "symbols": card.main_symbols,
            }
            for card in finalized_modules
        ],
    }
    _write_json_artifact(
        root / "repository_manifest.json",
        manifest,
        artifact_type="repository_manifest",
        source_paths=source_paths,
        workspace=workspace,
        records=freshness_records,
    )
    _write_text_artifact(
        root / "repository_map.md",
        _repository_map(finalized_modules),
        artifact_type="repository_map",
        source_paths=source_paths,
        workspace=workspace,
        records=freshness_records,
    )
    _write_json_artifact(
        root / CODE_INDEX,
        _code_index(
            finalized_modules,
            finalized_symbols,
            import_edges,
            call_edges,
            related_tests,
        ),
        artifact_type="code_index",
        source_paths=source_paths,
        workspace=workspace,
        records=freshness_records,
    )
    _write_json_artifact(
        root / "dependency_graph.json",
        {
            "schema_version": SCHEMA_VERSION,
            "imports": import_edges,
            "calls": call_edges,
            "modules": [{"path": card.path, "dependencies": card.dependencies} for card in finalized_modules],
        },
        artifact_type="dependency_graph",
        source_paths=source_paths,
        workspace=workspace,
        records=freshness_records,
    )
    _write_json_artifact(
        root / "api_map.json",
        _api_map(finalized_symbols),
        artifact_type="api_map",
        source_paths=source_paths,
        workspace=workspace,
        records=freshness_records,
    )
    _write_json_artifact(
        root / "test_map.json",
        _test_map(finalized_modules, related_tests),
        artifact_type="test_map",
        source_paths=source_paths,
        workspace=workspace,
        records=freshness_records,
    )
    config_paths = [
        relative
        for path in files
        if (relative := _relative(path, workspace)) and _is_config_path(relative)
    ]
    _write_json_artifact(
        root / "configuration_map.json",
        _configuration_map(workspace, files),
        artifact_type="configuration_map",
        source_paths=config_paths,
        workspace=workspace,
        records=freshness_records,
    )
    _write_json(
        root / FRESHNESS_INDEX,
        {
            "schema_version": SCHEMA_VERSION,
            "artifact_version": ARTIFACT_VERSION,
            "generator_version": GENERATOR_VERSION,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "artifacts": freshness_records,
        },
    )
    return ArtifactBuildResult(
        root=root,
        manifest_hash=str(manifest["workspace"]["manifest_hash"]),
        files_indexed=len(files),
        modules=len(finalized_modules),
        symbols=len(finalized_symbols),
    )


def artifact_brief(workspace: Path, query: str = "", files: list[str] | None = None) -> str:
    try:
        ensure_repository_artifacts(workspace)
    except Exception:
        return ""
    root = artifacts_root(workspace)
    parts: list[str] = []
    repo_map = root / "repository_map.md"
    if repo_map.exists() and _artifact_record_is_fresh(workspace, "repository_map.md"):
        try:
            parts.append(_budget(_strip_text_metadata(repo_map.read_text(encoding="utf-8")), 2500))
        except OSError:
            pass
    for relative in files or []:
        card = root / MODULES_DIR / _card_filename(relative)
        if card.exists():
            try:
                data = json.loads(card.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not _payload_is_fresh(workspace, data, artifact_path=card):
                continue
            parts.append(
                "Module card:\n"
                + _budget(json.dumps(data, ensure_ascii=True, sort_keys=True), 1800)
            )
    if query:
        structural = retrieve_structural_context(workspace, query, files=files or [], limit=4)
        if structural.get("ok"):
            parts.append("Structural retrieval:\n" + _budget(json.dumps(structural, ensure_ascii=True, sort_keys=True), 3000))
        hits = search_artifacts(workspace, query, limit=5)
        if hits:
            parts.append("Artifact search hits:\n" + "\n".join(f"- {hit}" for hit in hits))
    return "\n\n".join(part for part in parts if part).strip()


def retrieve_structural_context(
    workspace: Path,
    query: str,
    *,
    files: list[str] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Return deterministic structural facts before semantic fallback.

    Retrieval order:
    exact path -> text search -> symbol lookup -> references -> call graph ->
    dependency graph -> related tests -> Git history -> semantic fallback.
    """
    workspace = Path(workspace).resolve()
    try:
        ensure_repository_artifacts(workspace)
    except Exception as exc:
        return {"ok": False, "message": str(exc), "query": query}
    index = _load_code_index(workspace)
    if not index:
        return {"ok": False, "message": "code index unavailable", "query": query}
    normalized_files = _normalize_query_files(workspace, query, files or [], index)
    symbol_matches = _symbol_matches(index, query, limit=limit)
    matched_symbols = [index.get("symbols", {}).get(name) for name in symbol_matches]
    matched_symbols = [item for item in matched_symbols if isinstance(item, dict)]
    matched_paths = list(normalized_files)
    for symbol in matched_symbols:
        path = str(symbol.get("path") or "")
        if path and path not in matched_paths:
            matched_paths.append(path)
    text_matches = _structural_text_search(workspace, query, limit=limit)
    for match in text_matches:
        path = str(match.get("path") or "")
        if path and path not in matched_paths:
            matched_paths.append(path)
    modules = index.get("files", {}) if isinstance(index.get("files"), dict) else {}
    module_facts = [modules[path] for path in matched_paths if isinstance(modules.get(path), dict)]
    related_tests = sorted(
        dict.fromkeys(
            test
            for module in module_facts
            for test in module.get("related_tests", [])
            if str(test)
        )
    )[:20]
    related_tests.extend(
        test
        for symbol in matched_symbols
        for test in symbol.get("related_tests", [])
        if str(test) and test not in related_tests
    )
    related_tests = related_tests[:20]
    source = _source_snippets(workspace, matched_paths[:limit], matched_symbols[:limit])
    semantic_hits = search_artifacts(workspace, query, limit=limit)
    return {
        "ok": bool(matched_paths or symbol_matches or text_matches or semantic_hits),
        "query": query,
        "retrieval_order": [
            "exact_path",
            "text_search",
            "symbol_lookup",
            "references",
            "call_graph",
            "dependency_graph",
            "related_tests",
            "git_history",
            "semantic_search_fallback",
        ],
        "exact_paths": normalized_files,
        "text_matches": text_matches,
        "symbols": matched_symbols,
        "references": _references(index, matched_symbols, module_facts),
        "call_graph": _call_graph_context(matched_symbols),
        "dependency_graph": _dependency_context(module_facts),
        "related_tests": related_tests,
        "routes": _route_context(index, matched_paths, matched_symbols),
        "configuration": _configuration_context(workspace, query, matched_paths),
        "impact": _impact_context(matched_symbols, module_facts, related_tests),
        "git_history": _git_history(workspace, matched_paths[:limit]),
        "semantic_search_fallback": semantic_hits,
        "source": source,
    }


def search_artifacts(workspace: Path, query: str, limit: int = 10) -> list[str]:
    root = artifacts_root(workspace)
    try:
        ensure_repository_artifacts(workspace)
    except Exception:
        return []
    terms = [term.lower() for term in re.findall(r"[A-Za-z_][A-Za-z0-9_]+", query)]
    if not terms:
        return []
    scored: list[tuple[int, str]] = []
    for subdir in (MODULES_DIR, SYMBOLS_DIR):
        directory = root / subdir
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                text = path.read_text(encoding="utf-8")
                data = json.loads(text)
            except (OSError, json.JSONDecodeError):
                continue
            if not _payload_is_fresh(workspace, data, artifact_path=path):
                continue
            haystack = text.lower()
            score = sum(haystack.count(term) for term in terms)
            if score:
                label = data.get("fully_qualified_symbol") or data.get("path") or path.name
                purpose = data.get("purpose", "")
                scored.append((score, f"{label}: {purpose}"))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [text for _score, text in scored[:limit]]


def _code_index(
    modules: list[ModuleCard],
    symbols: list[SymbolCard],
    import_edges: list[dict[str, str]],
    call_edges: list[dict[str, str]],
    related_tests: dict[str, list[str]],
) -> dict[str, Any]:
    importers: dict[str, list[str]] = {}
    for module in modules:
        for dep in module.dependencies:
            importers.setdefault(dep, []).append(module.path)
        for imported in module.imports:
            importers.setdefault(imported.lstrip("."), []).append(module.path)
    symbol_lookup: dict[str, list[str]] = {}
    for symbol in symbols:
        short = symbol.fully_qualified_symbol.rsplit(".", 1)[-1]
        symbol_lookup.setdefault(short, []).append(symbol.fully_qualified_symbol)
    return {
        "schema_version": SCHEMA_VERSION,
        "retrieval_order": [
            "exact_path",
            "text_search",
            "symbol_lookup",
            "references",
            "call_graph",
            "dependency_graph",
            "related_tests",
            "git_history",
            "semantic_search_fallback",
        ],
        "files": {
            module.path: {
                "path": module.path,
                "module": module.module,
                "purpose": module.purpose,
                "symbols": module.main_symbols,
                "imports": module.imports,
                "dependencies": module.dependencies,
                "importers": sorted(dict.fromkeys(importers.get(module.module, [])))[:60],
                "callers": module.callers,
                "callees": module.callees,
                "related_tests": related_tests.get(module.path, []),
                "configuration": module.important_configuration,
                "source_hash": module.source_hash,
            }
            for module in modules
        },
        "symbols": {
            symbol.fully_qualified_symbol: {
                **asdict(symbol),
                "definition": {
                    "path": symbol.path,
                    "line_range": list(symbol.line_range),
                    "signature": symbol.signature,
                },
            }
            for symbol in symbols
        },
        "symbol_lookup": {key: sorted(value) for key, value in symbol_lookup.items()},
        "importers": {key: sorted(dict.fromkeys(value)) for key, value in importers.items()},
        "imports": import_edges,
        "calls": call_edges,
    }


def _load_code_index(workspace: Path) -> dict[str, Any]:
    root = artifacts_root(workspace)
    path = root / CODE_INDEX
    if not path.exists() or not _artifact_record_is_fresh(workspace, CODE_INDEX):
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not _payload_is_fresh(workspace, data, artifact_path=path):
        return {}
    return data


def _normalize_query_files(
    workspace: Path,
    query: str,
    files: list[str],
    index: dict[str, Any],
) -> list[str]:
    known_files = set((index.get("files") or {}).keys())
    candidates = list(files)
    candidates.extend(_path_tokens(query))
    raw = query.strip().strip("'\"`")
    if raw:
        candidates.append(raw)
    result: list[str] = []
    for candidate in candidates:
        relative = _normalize_source_path(workspace, candidate)
        if not relative:
            continue
        if relative in known_files or (workspace / relative).is_file():
            if relative not in result:
                result.append(relative)
    return result


def _symbol_matches(index: dict[str, Any], query: str, *, limit: int) -> list[str]:
    symbols = index.get("symbols") if isinstance(index.get("symbols"), dict) else {}
    lookup = index.get("symbol_lookup") if isinstance(index.get("symbol_lookup"), dict) else {}
    terms = _query_terms(query)
    matches: list[str] = []
    raw = query.strip().strip("'\"`")
    if raw in symbols:
        matches.append(raw)
    for term in terms:
        for name in lookup.get(term, []):
            if name not in matches:
                matches.append(name)
    lowered_terms = [term.lower() for term in terms]
    for name, symbol in symbols.items():
        haystack = " ".join(
            [
                name,
                str(symbol.get("signature", "")),
                str(symbol.get("purpose", "")),
                str(symbol.get("path", "")),
            ]
        ).lower()
        if lowered_terms and all(term in haystack for term in lowered_terms):
            if name not in matches:
                matches.append(name)
        if len(matches) >= limit:
            break
    return matches[:limit]


def _structural_text_search(workspace: Path, query: str, *, limit: int) -> list[dict[str, Any]]:
    terms = _query_terms(query)
    if not terms:
        return []
    needle = " ".join(terms).casefold()
    results: list[dict[str, Any]] = []
    for path in walk_workspace_files(workspace, suffixes=SOURCE_SUFFIXES, indexable_only=True):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        relative = _relative(path, workspace)
        if not relative:
            continue
        for line_number, line in enumerate(lines, start=1):
            lowered = line.casefold()
            if needle not in lowered and not all(term.casefold() in lowered for term in terms):
                continue
            results.append(
                {
                    "path": relative,
                    "line": line_number,
                    "preview": line.strip()[:220],
                }
            )
            break
        if len(results) >= limit:
            break
    return results


def _references(
    index: dict[str, Any],
    symbols: list[dict[str, Any]],
    modules: list[dict[str, Any]],
) -> dict[str, Any]:
    importers = index.get("importers") if isinstance(index.get("importers"), dict) else {}
    return {
        "symbol_callers": {
            str(symbol.get("fully_qualified_symbol")): list(symbol.get("callers", []))
            for symbol in symbols
        },
        "module_importers": {
            str(module.get("module")): list(module.get("importers") or importers.get(str(module.get("module")), []))
            for module in modules
        },
    }


def _call_graph_context(symbols: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        str(symbol.get("fully_qualified_symbol")): {
            "calls": list(symbol.get("callees", [])),
            "called_by": list(symbol.get("callers", [])),
        }
        for symbol in symbols
    }


def _dependency_context(modules: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        str(module.get("path")): {
            "module": module.get("module"),
            "imports": list(module.get("imports", [])),
            "dependencies": list(module.get("dependencies", [])),
            "importers": list(module.get("importers", [])),
        }
        for module in modules
    }


def _route_context(index: dict[str, Any], paths: list[str], symbols: list[dict[str, Any]]) -> list[dict[str, Any]]:
    files = index.get("files") if isinstance(index.get("files"), dict) else {}
    route_terms = {"route", "routes", "url", "urls", "endpoint", "view", "router"}
    target_modules = {
        str(files.get(path, {}).get("module", ""))
        for path in paths
        if isinstance(files.get(path), dict)
    }
    target_names = {
        str(symbol.get("fully_qualified_symbol", "")).rsplit(".", 1)[-1]
        for symbol in symbols
    }
    routes: list[dict[str, Any]] = []
    for path, module in files.items():
        text = f"{path} {module.get('module')} {' '.join(module.get('symbols', []))}".lower()
        if not any(term in text for term in route_terms):
            continue
        callees = " ".join(module.get("callees", []))
        imports = " ".join(module.get("imports", []))
        if (
            any(target and target in imports for target in target_modules)
            or any(target and target in callees for target in target_names)
            or not (target_modules or target_names)
        ):
            routes.append(
                {
                    "path": path,
                    "module": module.get("module"),
                    "imports": module.get("imports", [])[:12],
                    "callees": module.get("callees", [])[:12],
                }
            )
    return routes[:10]


def _configuration_context(workspace: Path, query: str, paths: list[str]) -> list[dict[str, Any]]:
    root = artifacts_root(workspace)
    config_path = root / "configuration_map.json"
    if not config_path.exists() or not _artifact_record_is_fresh(workspace, "configuration_map.json"):
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    terms = [term.lower() for term in _query_terms(query)]
    configs = data.get("configuration_files", [])
    if not isinstance(configs, list):
        return []
    selected: list[dict[str, Any]] = []
    for config in configs:
        haystack = f"{config.get('path', '')} {' '.join(config.get('keys', []))}".lower()
        if not terms or any(term in haystack for term in terms) or paths:
            selected.append(
                {
                    "path": config.get("path", ""),
                    "keys": list(config.get("keys", []))[:20],
                }
            )
    return selected[:12]


def _impact_context(
    symbols: list[dict[str, Any]],
    modules: list[dict[str, Any]],
    related_tests: list[str],
) -> dict[str, Any]:
    callers = sorted(
        dict.fromkeys(
            caller
            for symbol in symbols
            for caller in symbol.get("callers", [])
            if str(caller)
        )
    )
    importers = sorted(
        dict.fromkeys(
            importer
            for module in modules
            for importer in module.get("importers", [])
            if str(importer)
        )
    )
    return {
        "likely_breaks": callers[:20] + importers[:20],
        "test_files_to_run": related_tests[:20],
        "risk_signals": sorted(
            dict.fromkeys(
                signal
                for symbol in symbols
                for signal in symbol.get("side_effects", [])
                if str(signal)
            )
        ),
    }


def _source_snippets(
    workspace: Path,
    paths: list[str],
    symbols: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    snippets: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()
    for symbol in symbols:
        path = str(symbol.get("path") or "")
        line_range = symbol.get("line_range") or symbol.get("definition", {}).get("line_range") or [1, 80]
        try:
            start = int(line_range[0])
            end = int(line_range[1])
        except (TypeError, ValueError, IndexError):
            start, end = 1, 80
        snippet = _read_source_range(workspace, path, start, end)
        key = (path, start, end)
        if snippet and key not in seen:
            snippets.append({"path": path, "line_start": start, "line_end": end, "content": snippet})
            seen.add(key)
    for path in paths:
        key = (path, 1, 80)
        if key in seen:
            continue
        snippet = _read_source_range(workspace, path, 1, 80)
        if snippet:
            snippets.append({"path": path, "line_start": 1, "line_end": min(80, len(snippet.splitlines())), "content": snippet})
            seen.add(key)
    return snippets[:8]


def _read_source_range(workspace: Path, relative: str, start: int, end: int) -> str:
    if not relative:
        return ""
    target = (workspace / relative).resolve()
    try:
        target.relative_to(workspace.resolve())
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, ValueError):
        return ""
    start_index = max(0, start - 1)
    end_index = min(len(lines), max(start, end))
    return "\n".join(lines[start_index:end_index])[:6000]


def _git_history(workspace: Path, paths: list[str]) -> list[dict[str, Any]]:
    if not paths:
        return []
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-n", "5", "--", *paths],
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    history: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        commit, _, message = line.partition(" ")
        if commit:
            history.append({"commit": commit, "message": message[:200]})
    return history


def _query_terms(query: str) -> list[str]:
    return [
        term
        for term in re.findall(r"[A-Za-z_][A-Za-z0-9_./-]*", str(query or ""))
        if term not in {".", "/"}
    ][:12]


_PATH_TOKEN_RE = re.compile(
    r"(?<![\w.-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|jsx|json|md|txt|html|css|yml|yaml|toml|ini|cfg|env|sql|sh|ps1)"
)


def _path_tokens(text: str) -> list[str]:
    return [match.group(0) for match in _PATH_TOKEN_RE.finditer(str(text or ""))]


def _analyze_file(path: Path, relative: str) -> tuple[str, list[SymbolCard], list[str], set[str]]:
    if path.suffix.lower() == ".py":
        return _analyze_python(path, relative)
    imports = _text_imports(path)
    return _module_name(relative), [], imports, set()


def _analyze_python(path: Path, relative: str) -> tuple[str, list[SymbolCard], list[str], set[str]]:
    source = _read_text(path)
    module = _module_name(relative)
    file_hash = _hash_text(source)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return module, [], [], set()
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            imports.extend(base + ("." + alias.name if node.module else alias.name) for alias in node.names)
    symbols: list[SymbolCard] = []
    module_calls: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.extend(_symbol_cards_for_node(node, module, relative, source, file_hash))
            module_calls.update(_calls_in(node))
    return module, symbols, sorted(dict.fromkeys(imports)), module_calls


def _symbol_cards_for_node(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
    module: str,
    relative: str,
    source: str,
    file_hash: str,
    parent: str = "",
) -> list[SymbolCard]:
    name = f"{parent}.{node.name}" if parent else f"{module}.{node.name}"
    symbol_type = "class" if isinstance(node, ast.ClassDef) else "function"
    signature = _signature(node)
    source_segment = ast.get_source_segment(source, node) or ""
    card = SymbolCard(
        fully_qualified_symbol=name,
        path=relative,
        line_range=(int(getattr(node, "lineno", 1)), int(getattr(node, "end_lineno", getattr(node, "lineno", 1)))),
        signature=signature,
        type=symbol_type,
        purpose=ast.get_docstring(node) or _purpose_from_name(node.name, symbol_type),
        callees=sorted(_calls_in(node))[:60],
        side_effects=_side_effects(source_segment),
        source_hash=file_hash,
    )
    cards = [card]
    if isinstance(node, ast.ClassDef):
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cards.extend(_symbol_cards_for_node(child, module, relative, source, file_hash, parent=name))
    return cards


def _signature(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(base) for base in node.bases]
        return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}({_arguments_signature(node.args)})"
    return ""


def _arguments_signature(args: ast.arguments) -> str:
    names = [arg.arg for arg in args.posonlyargs + args.args]
    if args.vararg:
        names.append("*" + args.vararg.arg)
    names.extend(arg.arg for arg in args.kwonlyargs)
    if args.kwarg:
        names.append("**" + args.kwarg.arg)
    return ", ".join(names)


def _calls_in(node: ast.AST) -> set[str]:
    calls: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _call_name(child.func)
            if name:
                calls.add(name)
    return calls


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value.func) if isinstance(node.value, ast.Call) else _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _reverse_callers(symbols: dict[str, SymbolCard]) -> dict[str, list[str]]:
    by_short = {name.rsplit(".", 1)[-1]: name for name in symbols}
    callers: dict[str, list[str]] = {}
    for symbol in symbols.values():
        for call in symbol.callees:
            target = by_short.get(call.rsplit(".", 1)[-1])
            if target:
                callers.setdefault(target, []).append(symbol.fully_qualified_symbol)
    return {key: sorted(dict.fromkeys(value)) for key, value in callers.items()}


def _related_tests(modules: list[ModuleCard]) -> dict[str, list[str]]:
    tests = [card.path for card in modules if _is_test_path(card.path)]
    result: dict[str, list[str]] = {}
    for module in modules:
        base = Path(module.path).stem.replace("test_", "").replace("_test", "")
        matches = [test for test in tests if base and base.lower() in test.lower()]
        if not matches and not _is_test_path(module.path):
            matches = [test for test in tests if Path(module.path).parts[-2:-1] and Path(module.path).parts[-2] in test]
        result[module.path] = sorted(dict.fromkeys(matches))[:20]
    return result


def _api_map(symbols: list[SymbolCard]) -> dict[str, Any]:
    public = [
        asdict(symbol)
        for symbol in symbols
        if not symbol.fully_qualified_symbol.rsplit(".", 1)[-1].startswith("_")
    ]
    return {"schema_version": SCHEMA_VERSION, "symbols": public}


def _test_map(modules: list[ModuleCard], related_tests: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tests": [asdict(card) for card in modules if _is_test_path(card.path)],
        "related_tests": related_tests,
    }


def _configuration_map(workspace: Path, files: list[Path]) -> dict[str, Any]:
    configs: list[dict[str, Any]] = []
    for path in files:
        relative = _relative(path, workspace)
        if not relative or not _is_config_path(relative):
            continue
        configs.append(
            {
                "path": relative,
                "source_hash": _file_hash(path),
                "keys": _config_keys(path),
            }
        )
    return {"schema_version": SCHEMA_VERSION, "configuration_files": configs}


def _repository_map(modules: list[ModuleCard]) -> str:
    lines = ["# Repository Map", "", f"Modules indexed: {len(modules)}", ""]
    dirs: dict[str, int] = {}
    for card in modules:
        parent = str(Path(card.path).parent).replace("\\", "/")
        dirs[parent if parent != "." else "/"] = dirs.get(parent if parent != "." else "/", 0) + 1
    lines.append("## Top-Level Areas")
    for directory, count in sorted(dirs.items())[:80]:
        lines.append(f"- `{directory}`: {count} file(s)")
    lines.extend(["", "## Module Cards"])
    for card in modules[:200]:
        symbols = ", ".join(card.main_symbols[:5]) or "no parsed symbols"
        lines.append(f"- `{card.path}`: {card.purpose}; symbols: {symbols}")
    return "\n".join(lines) + "\n"


def _module_purpose(relative: str, symbols: list[SymbolCard]) -> str:
    if symbols:
        names = ", ".join(symbol.fully_qualified_symbol.rsplit(".", 1)[-1] for symbol in symbols[:5])
        return f"Defines {names}."
    if _is_test_path(relative):
        return "Test module."
    if _is_config_path(relative):
        return "Configuration file."
    return f"{Path(relative).suffix.lower().lstrip('.').upper() or 'Workspace'} source file."


def _purpose_from_name(name: str, symbol_type: str) -> str:
    words = " ".join(re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\d+", name.replace("_", " "))).strip()
    return f"{symbol_type.title()} for {words or name}."


def _side_effects(source: str) -> list[str]:
    effects: list[str] = []
    lowered = source.lower()
    if "open(" in lowered or "write_text(" in lowered or "write_bytes(" in lowered:
        effects.append("filesystem")
    if "subprocess" in lowered or ".run(" in lowered:
        effects.append("subprocess")
    if "requests." in lowered or "httpx." in lowered:
        effects.append("network")
    if "os.environ" in lowered:
        effects.append("environment")
    return effects


def _internal_dependencies(imports: list[str], files: list[Path], workspace: Path) -> list[str]:
    modules = {_module_name(_relative(path, workspace)) for path in files if _relative(path, workspace)}
    deps: list[str] = []
    for item in imports:
        normalized = item.lstrip(".")
        for module in modules:
            if normalized == module or module.startswith(normalized + ".") or normalized.startswith(module + "."):
                deps.append(module)
    return sorted(dict.fromkeys(deps))[:50]


def _text_imports(path: Path) -> list[str]:
    text = _read_text(path)[:20000]
    imports: list[str] = []
    for pattern in (
        r"^\s*import\s+['\"]([^'\"]+)['\"]",
        r"^\s*import\s+[^'\"]*from\s+['\"]([^'\"]+)['\"]",
        r"^\s*from\s+([A-Za-z0-9_.]+)\s+import\s+",
        r"^\s*require\(['\"]([^'\"]+)['\"]\)",
    ):
        imports.extend(match.group(1) for match in re.finditer(pattern, text, re.MULTILINE))
    return sorted(dict.fromkeys(imports))[:80]


def _config_notes(path: Path) -> list[str]:
    return _config_keys(path)[:20] if _is_config_path(path.name) else []


def _config_keys(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if path.name == "package.json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        return [key for key in ("scripts", "dependencies", "devDependencies", "name", "version") if key in data]
    if path.name == "pyproject.toml":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return []
        return list(data.keys())[:30]
    keys = []
    for line in text.splitlines()[:200]:
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_.-]+)\s*[:=]", line)
        if match:
            keys.append(match.group(1))
    return sorted(dict.fromkeys(keys))[:40]


def _module_name(relative: str) -> str:
    path = Path(relative)
    stem = path.with_suffix("").as_posix().replace("/", ".")
    return stem.replace(".__init__", "")


def _is_test_path(relative: str) -> bool:
    path = relative.replace("\\", "/").lower()
    name = Path(path).name
    return "/test" in path or name.startswith("test_") or name.endswith("_test.py") or ".test." in name


def _is_config_path(relative: str) -> bool:
    path = relative.replace("\\", "/")
    name = Path(path).name
    return name in CONFIG_NAMES or path.startswith(".github/")


def _card_filename(relative: str) -> str:
    safe = relative.replace("\\", "/").replace("/", "__")
    return safe + ".json"


def _symbol_card_path(card: SymbolCard) -> str:
    digest = hashlib.sha256(card.fully_qualified_symbol.encode("utf-8")).hexdigest()[:10]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", card.fully_qualified_symbol)[-100:]
    return f"{safe}-{digest}.json"


def _file_hash(path: Path) -> str:
    try:
        return _hash_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _relative(path: Path, workspace: Path) -> str:
    try:
        return path.resolve().relative_to(workspace.resolve()).as_posix()
    except (OSError, ValueError):
        return ""


def hash_source_text(text: str) -> str:
    return _hash_text(text)


def load_freshness_index(workspace: Path) -> dict[str, Any]:
    path = artifacts_root(workspace) / FRESHNESS_INDEX
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "artifacts": {}}
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, dict):
        data["artifacts"] = {}
    return data


def mark_artifacts_stale_for_paths(
    workspace: Path,
    source_paths: list[str],
    *,
    reason: str = "source_changed",
) -> dict[str, Any]:
    return _set_artifacts_status_for_paths(workspace, source_paths, FreshnessStatus.STALE, reason=reason)


def invalidate_artifacts_for_paths(
    workspace: Path,
    source_paths: list[str],
    *,
    reason: str = "contradiction",
    observed_hashes: dict[str, str] | None = None,
    source: str = "runtime",
) -> dict[str, Any]:
    return _set_artifacts_status_for_paths(
        workspace,
        source_paths,
        FreshnessStatus.INVALIDATED,
        reason=reason,
        observed_hashes=observed_hashes or {},
        source=source,
    )


def invalidate_artifacts_if_hash_mismatch(
    workspace: Path,
    source_path: str,
    observed_hash: str,
    *,
    source: str = "file.read",
) -> dict[str, Any]:
    relative = _normalize_source_path(workspace, source_path)
    if not relative or not observed_hash:
        return {"affected": [], "scheduled": []}
    index = load_freshness_index(workspace)
    artifacts = index.get("artifacts", {})
    mismatched: list[str] = []
    for artifact_path, record in list(artifacts.items()):
        if relative not in _record_source_paths(record):
            continue
        artifact_hash = str((record.get("source_hashes") or {}).get(relative) or "")
        if artifact_hash and artifact_hash != observed_hash:
            mismatched.append(str(artifact_path))
            _record_contradiction(
                artifacts_root(workspace),
                artifact_path=str(artifact_path),
                source_path=relative,
                artifact_hash=artifact_hash,
                observed_hash=observed_hash,
                reason="fresh source read contradicted artifact source hash",
                source=source,
            )
    if not mismatched:
        return {"affected": [], "scheduled": []}
    result = invalidate_artifacts_for_paths(
        workspace,
        [relative],
        reason="source_hash_mismatch",
        observed_hashes={relative: observed_hash},
        source=source,
    )
    result["contradictions"] = mismatched
    return result


def refresh_artifacts_for_paths(workspace: Path, source_paths: list[str] | None = None) -> ArtifactBuildResult:
    result = build_repository_artifacts(workspace)
    _clear_regeneration_queue(artifacts_root(workspace), source_paths or [])
    return result


def _write_json_artifact(
    path: Path,
    payload: dict[str, Any],
    *,
    artifact_type: str,
    source_paths: list[str],
    workspace: Path,
    records: dict[str, dict[str, Any]],
    confidence: float = 1.0,
) -> None:
    metadata = _artifact_metadata(
        workspace,
        artifact_type=artifact_type,
        source_paths=source_paths,
        confidence=confidence,
    )
    document = {**payload, **metadata}
    _write_json(path, document)
    records[_artifact_relative_path(path, workspace)] = metadata


def _write_text_artifact(
    path: Path,
    text: str,
    *,
    artifact_type: str,
    source_paths: list[str],
    workspace: Path,
    records: dict[str, dict[str, Any]],
    confidence: float = 1.0,
) -> None:
    metadata = _artifact_metadata(
        workspace,
        artifact_type=artifact_type,
        source_paths=source_paths,
        confidence=confidence,
    )
    header = "<!-- shamsu-artifact " + json.dumps(metadata, ensure_ascii=True, sort_keys=True) + " -->"
    _write_text(path, header + "\n" + text)
    records[_artifact_relative_path(path, workspace)] = metadata


def _artifact_metadata(
    workspace: Path,
    *,
    artifact_type: str,
    source_paths: list[str],
    confidence: float,
) -> dict[str, Any]:
    normalized_paths = sorted(
        dict.fromkeys(path for path in (_normalize_source_path(workspace, item) for item in source_paths) if path)
    )
    now = datetime.now(timezone.utc).isoformat()
    return {
        "artifact_id": _artifact_id(artifact_type, normalized_paths),
        "artifact_type": artifact_type,
        "source_paths": normalized_paths,
        "source_hashes": {
            relative: _file_hash(Path(workspace).resolve() / relative)
            for relative in normalized_paths
        },
        "artifact_version": ARTIFACT_VERSION,
        "generator_version": GENERATOR_VERSION,
        "created_at": now,
        "refreshed_at": now,
        "confidence": confidence,
        "freshness_status": FreshnessStatus.FRESH.value,
    }


def _artifact_id(artifact_type: str, source_paths: list[str]) -> str:
    body = artifact_type + "\n" + "\n".join(source_paths)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    return f"{artifact_type}:{digest}"


def _payload_is_fresh(workspace: Path, payload: dict[str, Any], *, artifact_path: Path | None = None) -> bool:
    if payload.get("freshness_status") != FreshnessStatus.FRESH.value:
        return False
    if payload.get("artifact_version") != ARTIFACT_VERSION:
        return False
    if payload.get("generator_version") != GENERATOR_VERSION:
        return False
    if _payload_sources_match(workspace, payload):
        return True
    relative = _artifact_relative_path(artifact_path, workspace) if artifact_path is not None else ""
    _invalidate_payload_mismatches(workspace, payload, artifact_path=relative, source="artifact_consumer")
    return False


def _payload_sources_match(workspace: Path, payload: dict[str, Any]) -> bool:
    source_hashes = payload.get("source_hashes") or {}
    if not isinstance(source_hashes, dict):
        return False
    for relative, recorded_hash in source_hashes.items():
        current_hash = _file_hash(Path(workspace).resolve() / str(relative))
        if current_hash != str(recorded_hash):
            return False
    return True


def _artifact_record_is_fresh(workspace: Path, artifact_path: str) -> bool:
    index = load_freshness_index(workspace)
    record = (index.get("artifacts") or {}).get(artifact_path)
    root = artifacts_root(workspace)
    if not isinstance(record, dict):
        _schedule_regeneration(root, [], "missing_freshness_record")
        return False
    if not (root / artifact_path).exists():
        record["freshness_status"] = FreshnessStatus.MISSING.value
        _save_freshness_index(root, index)
        _schedule_regeneration(root, _record_source_paths(record), "artifact_missing")
        return False
    if record.get("freshness_status") != FreshnessStatus.FRESH.value:
        return False
    if _payload_sources_match(workspace, record):
        return True
    invalidate_artifacts_for_paths(
        workspace,
        _record_source_paths(record),
        reason="source_hash_mismatch",
        source="artifact_consumer",
    )
    return False


def _set_artifacts_status_for_paths(
    workspace: Path,
    source_paths: list[str],
    status: FreshnessStatus,
    *,
    reason: str,
    observed_hashes: dict[str, str] | None = None,
    source: str = "runtime",
) -> dict[str, Any]:
    root = artifacts_root(workspace)
    index = load_freshness_index(workspace)
    artifacts = index.get("artifacts", {})
    normalized = {
        relative
        for item in source_paths
        if (relative := _normalize_source_path(workspace, item))
    }
    if not normalized:
        return {"affected": [], "scheduled": []}
    affected: list[str] = []
    global_types = {
        "repository_manifest",
        "repository_map",
        "code_index",
        "dependency_graph",
        "api_map",
        "test_map",
        "configuration_map",
    }
    for artifact_path, record in list(artifacts.items()):
        if not isinstance(record, dict):
            continue
        record_sources = set(_record_source_paths(record))
        structurally_related = record.get("artifact_type") in global_types
        if not structurally_related and not (record_sources & normalized):
            continue
        affected.append(str(artifact_path))
        _set_artifact_record_status(
            root,
            index,
            artifact_path=str(artifact_path),
            status=status,
            reason=reason,
            observed_hashes=observed_hashes or {},
            source=source,
        )
    _save_freshness_index(root, index)
    scheduled = _schedule_regeneration(root, sorted(normalized), reason)
    return {"affected": sorted(dict.fromkeys(affected)), "scheduled": scheduled}


def _set_artifact_record_status(
    root: Path,
    index: dict[str, Any],
    *,
    artifact_path: str,
    status: FreshnessStatus,
    reason: str,
    observed_hashes: dict[str, str],
    source: str,
) -> None:
    record = (index.get("artifacts") or {}).get(artifact_path)
    if not isinstance(record, dict):
        return
    now = datetime.now(timezone.utc).isoformat()
    record["freshness_status"] = status.value
    record["status_reason"] = reason
    record["status_source"] = source
    record["status_updated_at"] = now
    if observed_hashes:
        record["observed_hashes"] = observed_hashes
    path = root / artifact_path
    if path.suffix.lower() == ".json" and path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        payload.update(
            {
                "freshness_status": status.value,
                "status_reason": reason,
                "status_source": source,
                "status_updated_at": now,
            }
        )
        if observed_hashes:
            payload["observed_hashes"] = observed_hashes
        _write_json(path, payload)
    elif path.suffix.lower() == ".md" and path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        metadata = _text_artifact_metadata(text)
        metadata.update(
            {
                "freshness_status": status.value,
                "status_reason": reason,
                "status_source": source,
                "status_updated_at": now,
            }
        )
        body = _strip_text_metadata(text)
        header = "<!-- shamsu-artifact " + json.dumps(metadata, ensure_ascii=True, sort_keys=True) + " -->"
        _write_text(path, header + "\n" + body)


def _invalidate_payload_mismatches(
    workspace: Path,
    payload: dict[str, Any],
    *,
    artifact_path: str,
    source: str,
) -> None:
    mismatched: dict[str, str] = {}
    root = artifacts_root(workspace)
    for relative, recorded_hash in (payload.get("source_hashes") or {}).items():
        current_hash = _file_hash(Path(workspace).resolve() / str(relative))
        if current_hash != str(recorded_hash):
            mismatched[str(relative)] = current_hash
            _record_contradiction(
                root,
                artifact_path=artifact_path or str(payload.get("artifact_id", "")),
                source_path=str(relative),
                artifact_hash=str(recorded_hash),
                observed_hash=current_hash,
                reason="artifact source hash no longer matches disk",
                source=source,
            )
    if mismatched:
        invalidate_artifacts_for_paths(
            workspace,
            list(mismatched),
            reason="source_hash_mismatch",
            observed_hashes=mismatched,
            source=source,
        )


def _record_contradiction(
    root: Path,
    *,
    artifact_path: str,
    source_path: str,
    artifact_hash: str,
    observed_hash: str,
    reason: str,
    source: str,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "artifact_path": artifact_path,
        "source_path": source_path,
        "artifact_hash": artifact_hash,
        "observed_hash": observed_hash,
        "reason": reason,
        "source": source,
    }
    with (root / CONTRADICTIONS_LOG).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")


def _schedule_regeneration(root: Path, source_paths: list[str], reason: str) -> list[dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True)
    queue_path = root / REGENERATION_QUEUE
    try:
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        queue = {"schema_version": SCHEMA_VERSION, "items": []}
    items = queue.setdefault("items", [])
    keys = {
        (tuple(item.get("source_paths", [])), str(item.get("reason", "")))
        for item in items
        if isinstance(item, dict)
    }
    key = (tuple(sorted(source_paths)), reason)
    if key not in keys:
        items.append(
            {
                "source_paths": sorted(source_paths),
                "reason": reason,
                "scheduled_at": datetime.now(timezone.utc).isoformat(),
                "status": "PENDING",
            }
        )
    _write_json(queue_path, queue)
    return items


def _clear_regeneration_queue(root: Path, source_paths: list[str]) -> None:
    queue_path = root / REGENERATION_QUEUE
    if not queue_path.exists():
        return
    if not source_paths:
        _write_json(queue_path, {"schema_version": SCHEMA_VERSION, "items": []})
        return
    normalized = set(source_paths)
    try:
        queue = json.loads(queue_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    items = [
        item
        for item in queue.get("items", [])
        if not normalized.intersection(set(item.get("source_paths", [])))
    ]
    _write_json(queue_path, {"schema_version": SCHEMA_VERSION, "items": items})


def _record_generation_failed(root: Path, exc: Exception) -> None:
    root.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    failure = {
        "schema_version": SCHEMA_VERSION,
        "artifact_version": ARTIFACT_VERSION,
        "generator_version": GENERATOR_VERSION,
        "refreshed_at": now,
        "freshness_status": FreshnessStatus.GENERATION_FAILED.value,
        "error": str(exc),
        "artifacts": {},
    }
    _write_json(root / FRESHNESS_INDEX, failure)


def _artifact_relative_path(path: Path | None, workspace: Path) -> str:
    if path is None:
        return ""
    try:
        return path.resolve().relative_to(artifacts_root(workspace).resolve()).as_posix()
    except (OSError, ValueError):
        return path.as_posix()


def _normalize_source_path(workspace: Path, source_path: str) -> str:
    if not source_path:
        return ""
    candidate = Path(source_path)
    if candidate.is_absolute():
        return _relative(candidate, workspace)
    return candidate.as_posix().replace("\\", "/").lstrip("./")


def _record_source_paths(record: dict[str, Any]) -> list[str]:
    paths = record.get("source_paths")
    if not isinstance(paths, list):
        return []
    return [str(path).replace("\\", "/") for path in paths if str(path)]


def _save_freshness_index(root: Path, index: dict[str, Any]) -> None:
    _write_json(root / FRESHNESS_INDEX, index)


def _text_artifact_metadata(text: str) -> dict[str, Any]:
    first = text.splitlines()[0] if text else ""
    match = re.match(r"<!--\s*shamsu-artifact\s+(\{.*\})\s*-->", first)
    if not match:
        return {}
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _strip_text_metadata(text: str) -> str:
    lines = text.splitlines()
    if lines and re.match(r"<!--\s*shamsu-artifact\s+\{.*\}\s*-->", lines[0]):
        return "\n".join(lines[1:]).lstrip("\n")
    return text


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _budget(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"
