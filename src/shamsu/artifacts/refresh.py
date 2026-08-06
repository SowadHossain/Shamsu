"""Keeping artifacts true after the repository changes.

The refresh pass, in order:

1. Scan the repository for current content hashes.
2. Recompute freshness -- changed sources go STALE, deleted ones INVALIDATED.
3. Retire artifacts whose subject no longer exists.
4. Regenerate everything stale, invalidated, missing, or never built.
5. Report what happened.

The ordering matters. Recomputing before generating means a pass never
regenerates an artifact it is about to discover is fine, and retiring before
generating means a deleted module's card does not get rebuilt from thin air.

This is Milestone 3's exit condition: artifacts regenerate correctly after
source changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from shamsu.artifacts.generators import (
    ModuleCardGenerator,
    RepositoryContext,
    RepositoryManifestGenerator,
    RepositoryMapGenerator,
    SymbolCardGenerator,
)
from shamsu.artifacts.registry import ArtifactRegistry
from shamsu.interfaces.artifacts import ArtifactGenerationError, GeneratedArtifact
from shamsu.interfaces.cancellation import CancellationToken, NullCancellationToken
from shamsu.interfaces.enums import ArtifactKind, ArtifactStatus


@dataclass
class RefreshReport:
    """What a refresh pass did.

    Returned rather than logged so a caller -- a test, a CLI, the runtime --
    can assert on it. `failed` is a first-class field: a pass that could not
    build some artifacts succeeded partially, and saying so beats both a
    silent gap and a raised exception that discards the work that did succeed.
    """

    scanned_files: int = 0
    generated: list[str] = field(default_factory=list)
    regenerated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def total_written(self) -> int:
        return len(self.generated) + len(self.regenerated)

    @property
    def ok(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        parts = [
            f"{self.scanned_files} files scanned",
            f"{len(self.generated)} new",
            f"{len(self.regenerated)} regenerated",
            f"{len(self.unchanged)} unchanged",
        ]
        if self.retired:
            parts.append(f"{len(self.retired)} retired")
        if self.failed:
            parts.append(f"{len(self.failed)} FAILED")
        return ", ".join(parts)


class ArtifactRefresher:
    """Builds and maintains a project's artifacts."""

    def __init__(self, registry: ArtifactRegistry, root: Path, *, use_git: bool = True) -> None:
        self._registry = registry
        self._root = Path(root).resolve()
        self._use_git = use_git

    def refresh(
        self,
        *,
        kinds: Sequence[ArtifactKind] | None = None,
        cancel: CancellationToken | None = None,
        force: bool = False,
    ) -> RefreshReport:
        """Run one refresh pass.

        Args:
            kinds: Restrict to these artifact kinds. Defaults to all four.
            cancel: Observed between artifacts. A refresh over a large
                repository is exactly the kind of long operation that must not
                make a run uninterruptible.
            force: Regenerate even artifacts that are already fresh.
        """
        token = cancel or NullCancellationToken()
        report = RefreshReport()

        token.raise_if_cancelled()
        context = RepositoryContext(self._root, use_git=self._use_git)
        report.scanned_files = len(context.hashes)

        # Step 2: freshness before generation, so nothing is rebuilt that a
        # hash comparison is about to show is fine.
        #
        # `freshness_map` rather than raw `hashes`: repository-wide artifacts
        # depend on the synthetic file-list source, and passing the raw map
        # would make every one of them look like it referenced a deleted file.
        self._registry.recompute_status(context.freshness_map())

        generators = self._generators(context, kinds)

        for generator in generators:
            token.raise_if_cancelled()

            # A generator whose extraction changed invalidates everything it
            # ever produced, including artifacts whose sources never changed.
            self._registry.invalidate_by_generator(generator.kind, generator.generator_version)

            current_keys = set(generator.keys())
            self._retire_absent(generator.kind, current_keys, report)

            for key in sorted(current_keys):
                token.raise_if_cancelled()
                self._refresh_one(generator, key, report, force=force)

        return report

    # -- internals ---------------------------------------------------------

    def _generators(
        self, context: RepositoryContext, kinds: Sequence[ArtifactKind] | None
    ) -> Sequence[
        RepositoryManifestGenerator
        | RepositoryMapGenerator
        | ModuleCardGenerator
        | SymbolCardGenerator
    ]:
        available: list[
            RepositoryManifestGenerator
            | RepositoryMapGenerator
            | ModuleCardGenerator
            | SymbolCardGenerator
        ] = [
            RepositoryManifestGenerator(context),
            RepositoryMapGenerator(context),
            ModuleCardGenerator(context),
            SymbolCardGenerator(context),
        ]
        if kinds is None:
            return available
        wanted = set(kinds)
        return [generator for generator in available if generator.kind in wanted]

    def _retire_absent(
        self, kind: ArtifactKind, current_keys: set[str], report: RefreshReport
    ) -> None:
        """Invalidate artifacts whose subject no longer exists.

        A card for a deleted module must not linger as FRESH. It is
        invalidated rather than deleted, so the fact that it once existed --
        and what it said -- survives for debugging.

        Reporting and invalidating are separate steps because step 2 may have
        already invalidated the artifact by hash comparison. It is still
        retired -- the report describes what happened to the artifact, not
        which code path got there first.
        """
        absent = [
            meta for meta in self._registry.list_by_kind(kind) if meta.key not in current_keys
        ]
        if not absent:
            return

        needs_invalidating = [
            meta.key for meta in absent if meta.status is not ArtifactStatus.INVALIDATED
        ]
        if needs_invalidating:
            self._registry.invalidate(kind, needs_invalidating)

        report.retired.extend(f"{kind.value}:{meta.key}" for meta in absent)

    def _refresh_one(
        self,
        generator: RepositoryManifestGenerator
        | RepositoryMapGenerator
        | ModuleCardGenerator
        | SymbolCardGenerator,
        key: str,
        report: RefreshReport,
        *,
        force: bool,
    ) -> None:
        label = f"{generator.kind.value}:{key}"
        existing = self._registry.get_meta(generator.kind, key)

        if not force and existing is not None and existing.status is ArtifactStatus.FRESH:
            report.unchanged.append(label)
            return

        try:
            built: GeneratedArtifact = generator.generate(key)
        except ArtifactGenerationError as exc:
            # Recorded as GENERATION_FAILED, never as a guess. A missing
            # artifact is a known unknown; a fabricated one is a silent lie.
            self._registry.record_generation_failure(
                generator.kind,
                key,
                generator_version=generator.generator_version,
                detail=str(exc),
            )
            report.failed.append((label, str(exc)))
            return

        self._registry.put(
            generator.kind,
            key,
            built.content,
            built.sources,
            generator_version=generator.generator_version,
            confidence=built.confidence,
        )

        if existing is None:
            report.generated.append(label)
        else:
            report.regenerated.append(label)

    def invalidate_for_paths(self, paths: Sequence[str]) -> Sequence[str]:
        """Mark artifacts derived from `paths` as stale.

        Called by the tool gateway after a mutating tool runs, so the window
        between "a file changed" and "the artifacts know" is a single call
        rather than a whole refresh pass.
        """
        return [str(artifact_id) for artifact_id in self._registry.invalidate_by_path(paths)]


__all__ = ["ArtifactRefresher", "RefreshReport"]
