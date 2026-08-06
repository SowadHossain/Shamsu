"""Versioned, source-traceable repository artifacts.

The primary long-codebase compression mechanism: artifacts turn a repository
too large to read into structured units small enough to prompt with.

Content lives on disk under `.shamsu/artifacts/` so it stays human-readable and
diffable. Freshness metadata lives in SQLite so it stays queryable and
transactional. The registry is authoritative for status.

The discipline that makes artifacts safe rather than dangerous:

* Every artifact records the files it was derived from and their content hashes
  at build time, so "is this still true?" is answered by comparison, not by
  asking a model or trusting an mtime.
* `generator_version` is tracked separately from `artifact_version`, because a
  fixed extraction bug invalidates artifacts whose sources never changed.
* Only FRESH and STALE may reach the model, and STALE must be labelled.
* Generation failure is recorded as GENERATION_FAILED, never as a guess.

Milestone 3. See plan sections 14-17.
"""

from shamsu.artifacts.generators import (
    ModuleCardGenerator,
    RepositoryContext,
    RepositoryManifestGenerator,
    RepositoryMapGenerator,
    SymbolCardGenerator,
)
from shamsu.artifacts.hashing import (
    DEFAULT_IGNORED_DIRS,
    DEFAULT_IGNORED_SUFFIXES,
    changed_paths,
    git_listed_files,
    hash_bytes,
    hash_file,
    hash_paths,
    hash_text,
    is_ignored,
    iter_files,
    scan_repository,
)
from shamsu.artifacts.python_source import (
    ExtractedModule,
    ExtractedSymbol,
    extract_python,
    module_path_for,
)
from shamsu.artifacts.refresh import ArtifactRefresher, RefreshReport
from shamsu.artifacts.registry import ArtifactRegistry, content_filename

__all__ = [
    "DEFAULT_IGNORED_DIRS",
    "DEFAULT_IGNORED_SUFFIXES",
    "ArtifactRefresher",
    "ArtifactRegistry",
    "ExtractedModule",
    "ExtractedSymbol",
    "ModuleCardGenerator",
    "RefreshReport",
    "RepositoryContext",
    "RepositoryManifestGenerator",
    "RepositoryMapGenerator",
    "SymbolCardGenerator",
    "changed_paths",
    "content_filename",
    "extract_python",
    "git_listed_files",
    "hash_bytes",
    "hash_file",
    "hash_paths",
    "hash_text",
    "is_ignored",
    "iter_files",
    "module_path_for",
    "scan_repository",
]
