"""The artifact seam.

Artifacts are the primary long-codebase compression mechanism: they turn a
repository too large to read into structured units small enough to prompt with.

The hard requirement is traceability. Every artifact records the source paths
and content hashes it was derived from, so the runtime can tell -- without
asking a model -- whether it is still true.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from shamsu.interfaces.enums import ArtifactKind, ArtifactStatus
from shamsu.interfaces.ids import ArtifactId


class SourceRef(BaseModel):
    """A file an artifact was derived from, and its content hash at that time."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(description="Repository-relative POSIX path.")
    content_hash: str = Field(description="Hash of the file when the artifact was built.")


class ArtifactMeta(BaseModel):
    """Freshness metadata carried by every artifact (plan section 17)."""

    model_config = ConfigDict(frozen=True)

    artifact_id: ArtifactId
    kind: ArtifactKind
    key: str = Field(description="Identity within the kind, e.g. a module path for a module card.")

    sources: tuple[SourceRef, ...] = Field(
        description="Every file this artifact claims to describe."
    )
    artifact_version: int = Field(
        ge=1, description="Bumped on every regeneration of this artifact."
    )
    generator_version: str = Field(
        description=(
            "Version of the code that produced it. Distinct from artifact_version: a "
            "generator change invalidates every artifact it ever produced, even ones "
            "whose sources did not change."
        )
    )

    created_at: datetime
    refreshed_at: datetime
    status: ArtifactStatus
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "1.0 for purely deterministic extraction. Lower when a model supplied "
            "part of the content, so the compiler can rank structural facts above "
            "model-written summaries."
        ),
    )

    @property
    def usable(self) -> bool:
        """Whether this may enter a compiled frame at all.

        Stale artifacts are usable but must be labelled. Invalidated, missing,
        and failed ones must not be sent to the model in any form.
        """
        return self.status in (ArtifactStatus.FRESH, ArtifactStatus.STALE)


class Artifact(BaseModel):
    """Metadata plus content."""

    model_config = ConfigDict(frozen=True)

    meta: ArtifactMeta
    content: str = Field(description="Rendered form: JSON or Markdown per artifact kind.")


class Contradiction(BaseModel):
    """A recorded disagreement between an artifact and a fresh tool result.

    Resolution is fixed by plan section 17.2 and is not negotiable: the fresh
    result wins, the artifact is invalidated, the contradiction is recorded,
    and regeneration is queued. Recording it matters because the rate of these
    is the `artifact_freshness_error_rate` evaluation metric.
    """

    model_config = ConfigDict(frozen=True)

    artifact_id: ArtifactId
    observed_at: datetime
    artifact_claim: str
    fresh_observation: str
    source_tool: str


@runtime_checkable
class ArtifactStore(Protocol):
    """Persistence and freshness tracking for derived artifacts."""

    def get(self, kind: ArtifactKind, key: str) -> Artifact | None:
        """Fetch one artifact, whatever its status. Returns None if never built."""
        ...

    def put(self, artifact: Artifact) -> None:
        """Store or replace an artifact."""
        ...

    def list_by_kind(self, kind: ArtifactKind) -> Sequence[ArtifactMeta]:
        """Metadata for every artifact of a kind, without loading content."""
        ...

    def recompute_status(self, current_hashes: Mapping[str, str]) -> Sequence[ArtifactId]:
        """Re-evaluate freshness against the current repository state.

        Given path -> content hash for the working tree, mark artifacts whose
        sources changed as stale and those whose sources vanished as
        invalidated. Returns the artifacts whose status changed.
        """
        ...

    def invalidate(self, kind: ArtifactKind, keys: Sequence[str]) -> None:
        """Force-invalidate artifacts, e.g. after a mutating tool call."""
        ...

    def record_contradiction(self, contradiction: Contradiction) -> None:
        """Record that a fresh observation disagreed with a stored artifact."""
        ...

    def stale(self) -> Sequence[ArtifactMeta]:
        """Artifacts needing regeneration, most depended-upon first."""
        ...


class GeneratedArtifact(BaseModel):
    """A generator's output, before the registry assigns it an identity.

    Distinct from `Artifact` because identity, version, and freshness belong to
    the registry. A generator that had to mint its own `ArtifactId` would
    either duplicate the registry's bookkeeping or contradict it.
    """

    model_config = ConfigDict(frozen=True)

    content: str
    sources: tuple[SourceRef, ...] = Field(
        description="Every file this artifact's claims were derived from, with hashes."
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


@runtime_checkable
class ArtifactGenerator(Protocol):
    """Builds one kind of artifact from the repository.

    Structural facts must come from deterministic analysis -- parsers, git,
    test discovery, manifest readers. A model may add a prose summary, but it
    may never be the source of a symbol name, a path, or a dependency edge.
    """

    @property
    def kind(self) -> ArtifactKind: ...

    @property
    def generator_version(self) -> str:
        """Bumped whenever extraction changes.

        Tracked separately from an artifact's own version because a fixed
        extraction bug invalidates every artifact this generator ever produced,
        including ones whose sources never changed.
        """
        ...

    def keys(self) -> Sequence[str]:
        """Every key this generator can currently produce.

        Lets the refresher discover new artifacts -- a newly added module needs
        a card before anything knows to ask for one.
        """
        ...

    def generate(self, key: str) -> GeneratedArtifact:
        """Build the artifact for `key`.

        Raises:
            ArtifactGenerationError: recorded as GENERATION_FAILED rather than
                producing an artifact with invented content.
        """
        ...


class ArtifactGenerationError(Exception):
    """Generation failed. The artifact is recorded as GENERATION_FAILED.

    Never substitute a guess. A missing artifact is a known unknown; a fabricated
    one is a silent lie the compiler will happily send to the model.
    """


__all__ = [
    "Artifact",
    "ArtifactGenerationError",
    "ArtifactGenerator",
    "ArtifactMeta",
    "ArtifactStore",
    "Contradiction",
    "GeneratedArtifact",
    "SourceRef",
]
