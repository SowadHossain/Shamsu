"""The artifact registry: content on disk, freshness in SQLite.

The split is deliberate. Artifact *content* lives under `.shamsu/artifacts/`
so it stays human-readable, greppable, and diffable -- someone debugging a bad
decision should be able to `cat` the module card the model was shown. Artifact
*metadata* lives in SQLite because freshness, versioning, and provenance need
to be queryable and transactional.

The registry is authoritative for status. If the database says an artifact
exists and the file is gone, the artifact is MISSING -- not silently
regenerated, not treated as fresh.

Invalidation is the whole point of this module. An artifact that quietly goes
out of date is worse than no artifact: the model is handed a confident,
structured, wrong claim about the code.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from shamsu.interfaces.artifacts import (
    Artifact,
    ArtifactMeta,
    Contradiction,
    SourceRef,
)
from shamsu.interfaces.enums import ArtifactKind, ArtifactStatus
from shamsu.interfaces.ids import ArtifactId, ProjectId
from shamsu.state.records import new_id, utcnow
from shamsu.state.store import StateStore

#: Filename extension per artifact kind. Markdown where a human is the second
#: reader, JSON where a machine is.
_SUFFIX: Mapping[ArtifactKind, str] = {
    ArtifactKind.REPOSITORY_MANIFEST: ".json",
    ArtifactKind.REPOSITORY_MAP: ".md",
    ArtifactKind.MODULE_CARD: ".md",
    ArtifactKind.SYMBOL_CARD: ".md",
    ArtifactKind.DEPENDENCY_GRAPH: ".json",
    ArtifactKind.API_MAP: ".json",
    ArtifactKind.DATABASE_SCHEMA: ".json",
    ArtifactKind.TEST_MAP: ".json",
    ArtifactKind.CONFIGURATION_MAP: ".json",
    ArtifactKind.TASK_PACKET: ".json",
    ArtifactKind.CHANGE_MANIFEST: ".json",
    ArtifactKind.FAILURE_CAPSULE: ".json",
}

#: Subdirectory per kind. Singletons sit at the top level so their paths match
#: the ones named in the plan (`.shamsu/artifacts/repository_map.md`).
_SUBDIR: Mapping[ArtifactKind, str] = {
    ArtifactKind.MODULE_CARD: "modules",
    ArtifactKind.SYMBOL_CARD: "symbols",
    ArtifactKind.TASK_PACKET: "tasks",
    ArtifactKind.CHANGE_MANIFEST: "tasks",
    ArtifactKind.FAILURE_CAPSULE: "tasks",
}


#: Characters Windows forbids in a filename. `:` is the one that mattered: a
#: symbol key is `path::symbol`, so every symbol card raised `OSError: [Errno
#: 22] Invalid argument` and no symbol card could be written on Windows at all.
#: The control range is included because a key ultimately derives from file
#: contents, and a stray byte should not become an unopenable file.
_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f]')

#: Device names Windows reserves at *any* extension — `NUL.md` is still the
#: null device. Compared against the stem before the first dot.
_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)

#: Room for the digest and suffix inside the 255-byte limit every major
#: filesystem shares.
_MAX_STEM = 120


def content_filename(kind: ArtifactKind, key: str) -> str:
    """Repository-relative path for an artifact's content file.

    Keys become flat filenames with `/` as `__`, matching the plan's
    `modules/apps__api__auth.md`. Flat beats nested here: a module card for
    `a/b/c.py` and one for `a/b` cannot collide as directory-vs-file, and
    listing a kind is one readdir.

    **A key that will not fit a filename gets a digest.** Replacing `:` with
    `_` alone would make `a:b` and `a_b` the same file, and two symbol cards
    silently overwriting each other is worse than the crash it replaced. So
    anything that had to be altered — an illegal character, a reserved device
    name, an over-long stem — carries a hash of the *original* key, which
    restores uniqueness and stays stable across runs.

    Keys needing no alteration are untouched, so the documented
    `modules/apps__api__auth.md` form still holds for the common case.
    """
    flat = key.replace("/", "__").replace("\\", "__").strip("._ ") or "_root"
    safe = _ILLEGAL.sub("_", flat)

    altered = safe != flat or safe.split(".")[0].upper() in _RESERVED
    if len(safe) > _MAX_STEM:
        safe, altered = safe[:_MAX_STEM], True
    if altered:
        digest = sha256(key.encode("utf-8")).hexdigest()[:12]
        safe = f"{safe}-{digest}"

    stem = f"{safe}{_SUFFIX.get(kind, '.json')}"
    subdir = _SUBDIR.get(kind)
    return f"{subdir}/{stem}" if subdir else stem


class ArtifactRegistry:
    """Tracks artifacts for one project.

    Scoped to a project at construction so the `ArtifactStore` protocol's
    methods stay free of a project argument they would carry everywhere.
    """

    def __init__(
        self,
        store: StateStore,
        project_id: ProjectId,
        artifacts_dir: Path,
    ) -> None:
        self._store = store
        self._project_id = project_id
        self._dir = Path(artifacts_dir)

    @property
    def directory(self) -> Path:
        return self._dir

    # -- reads -------------------------------------------------------------

    def get(self, kind: ArtifactKind, key: str) -> Artifact | None:
        """Fetch an artifact, whatever its status.

        A registered artifact whose content file has vanished is reported as
        MISSING with empty content rather than as absent. The distinction
        matters: "never built" and "built then lost" call for different
        responses, and only the second is a sign something is wrong.
        """
        meta = self.get_meta(kind, key)
        if meta is None:
            return None

        path = self._content_path(kind, key)
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            if meta.status is not ArtifactStatus.MISSING:
                self._set_status(meta.artifact_id, ArtifactStatus.MISSING)
                meta = meta.model_copy(update={"status": ArtifactStatus.MISSING})
            return Artifact(meta=meta, content="")

        return Artifact(meta=meta, content=content)

    def get_meta(self, kind: ArtifactKind, key: str) -> ArtifactMeta | None:
        with self._store.reading() as connection:
            row = connection.execute(
                "SELECT * FROM artifact_records WHERE project_id=? AND kind=? AND key=?",
                (self._project_id, kind.value, key),
            ).fetchone()
            if row is None:
                return None
            sources = self._sources_for(connection, row["artifact_id"])
        return self._meta_from_row(row, sources)

    def list_by_kind(self, kind: ArtifactKind) -> Sequence[ArtifactMeta]:
        """Metadata for every artifact of a kind, without loading content."""
        with self._store.reading() as connection:
            rows = connection.execute(
                "SELECT * FROM artifact_records WHERE project_id=? AND kind=? ORDER BY key",
                (self._project_id, kind.value),
            ).fetchall()
            return [
                self._meta_from_row(row, self._sources_for(connection, row["artifact_id"]))
                for row in rows
            ]

    def stale(self) -> Sequence[ArtifactMeta]:
        """Artifacts needing regeneration.

        Ordered by status then key so the caller regenerates deterministically.
        INVALIDATED comes before STALE: an invalidated artifact is actively
        unusable, a stale one is merely suspect.
        """
        wanted = (
            ArtifactStatus.INVALIDATED.value,
            ArtifactStatus.STALE.value,
            ArtifactStatus.MISSING.value,
            ArtifactStatus.GENERATION_FAILED.value,
        )
        with self._store.reading() as connection:
            rows = connection.execute(
                f"SELECT * FROM artifact_records WHERE project_id=? "  # noqa: S608
                f"AND status IN ({','.join('?' * len(wanted))}) "
                "ORDER BY CASE status WHEN 'invalidated' THEN 0 WHEN 'missing' THEN 1 "
                "WHEN 'generation_failed' THEN 2 ELSE 3 END, key",
                (self._project_id, *wanted),
            ).fetchall()
            return [
                self._meta_from_row(row, self._sources_for(connection, row["artifact_id"]))
                for row in rows
            ]

    def usable(self, kind: ArtifactKind, key: str) -> Artifact | None:
        """An artifact only if it may enter a compiled frame.

        The call the context compiler should make. Returns None for
        INVALIDATED, MISSING, and GENERATION_FAILED, so an unusable artifact
        cannot reach the model through an accidental `get`.
        """
        artifact = self.get(kind, key)
        if artifact is None or not artifact.meta.usable:
            return None
        return artifact

    # -- writes ------------------------------------------------------------

    def put(
        self,
        kind: ArtifactKind,
        key: str,
        content: str,
        sources: Sequence[SourceRef],
        *,
        generator_version: str,
        confidence: float = 1.0,
        status: ArtifactStatus = ArtifactStatus.FRESH,
    ) -> ArtifactMeta:
        """Store or replace an artifact, writing content and metadata together.

        Regenerating bumps `artifact_version` and preserves `created_at`, so
        "how many times has this been rebuilt?" stays answerable.
        """
        now = utcnow()
        path = self._content_path(kind, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        relative = content_filename(kind, key)
        existing = self.get_meta(kind, key)

        with self._store.transaction() as connection:
            if existing is None:
                artifact_id = ArtifactId(new_id())
                connection.execute(
                    """
                    INSERT INTO artifact_records (
                        artifact_id, project_id, kind, key, content_path,
                        artifact_version, generator_version, status, confidence,
                        created_at, refreshed_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        artifact_id,
                        self._project_id,
                        kind.value,
                        key,
                        relative,
                        1,
                        generator_version,
                        status.value,
                        confidence,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                version = 1
            else:
                artifact_id = existing.artifact_id
                version = existing.artifact_version + 1
                connection.execute(
                    """
                    UPDATE artifact_records SET
                        content_path=?, artifact_version=?, generator_version=?,
                        status=?, confidence=?, refreshed_at=?
                    WHERE artifact_id=?
                    """,
                    (
                        relative,
                        version,
                        generator_version,
                        status.value,
                        confidence,
                        now.isoformat(),
                        artifact_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM artifact_sources WHERE artifact_id=?", (artifact_id,)
                )

            connection.executemany(
                "INSERT INTO artifact_sources (artifact_id, path, content_hash) VALUES (?,?,?)",
                [(artifact_id, ref.path, ref.content_hash) for ref in sources],
            )

        return ArtifactMeta(
            artifact_id=artifact_id,
            kind=kind,
            key=key,
            sources=tuple(sources),
            artifact_version=version,
            generator_version=generator_version,
            created_at=existing.created_at if existing else now,
            refreshed_at=now,
            status=status,
            confidence=confidence,
        )

    def record_generation_failure(
        self, kind: ArtifactKind, key: str, *, generator_version: str, detail: str
    ) -> ArtifactMeta:
        """Register that generation failed.

        Recorded as an artifact with GENERATION_FAILED status and no content,
        never as a guess. A missing artifact is a known unknown; a fabricated
        one is a lie the compiler would happily forward to the model.
        """
        return self.put(
            kind,
            key,
            "",
            [],
            generator_version=generator_version,
            confidence=0.0,
            status=ArtifactStatus.GENERATION_FAILED,
        ).model_copy(update={"status": ArtifactStatus.GENERATION_FAILED})

    # -- invalidation ------------------------------------------------------

    def recompute_status(self, current_hashes: Mapping[str, str]) -> Sequence[ArtifactId]:
        """Re-evaluate freshness against the current repository state.

        Rules, in priority order:

        * A source that no longer exists -> INVALIDATED. The artifact describes
          something that is gone; nothing about it can be trusted.
        * A source whose hash changed -> STALE. Still probably mostly right,
          usable with a warning, queued for regeneration.
        * All sources match -> FRESH, but only if the artifact was not already
          invalidated or failed. Recomputing must not resurrect an artifact
          that was invalidated for a reason unrelated to file hashes.

        Returns the artifacts whose status changed.
        """
        changed: list[ArtifactId] = []

        with self._store.transaction() as connection:
            rows = connection.execute(
                "SELECT artifact_id, status FROM artifact_records WHERE project_id=?",
                (self._project_id,),
            ).fetchall()

            for row in rows:
                artifact_id = ArtifactId(row["artifact_id"])
                current = ArtifactStatus(row["status"])

                sources = [
                    (str(source["path"]), str(source["content_hash"]))
                    for source in connection.execute(
                        "SELECT path, content_hash FROM artifact_sources WHERE artifact_id=?",
                        (artifact_id,),
                    ).fetchall()
                ]

                target = self._evaluate(sources, current_hashes, current)
                if target is not current:
                    connection.execute(
                        "UPDATE artifact_records SET status=? WHERE artifact_id=?",
                        (target.value, artifact_id),
                    )
                    changed.append(artifact_id)

        return changed

    @staticmethod
    def _evaluate(
        sources: Sequence[tuple[str, str]],
        current_hashes: Mapping[str, str],
        current: ArtifactStatus,
    ) -> ArtifactStatus:
        """Decide a status from `(path, recorded_hash)` pairs.

        Pure and takes plain tuples rather than database rows, so the
        invalidation rules -- the most consequential logic in this module --
        can be tested without a database at all.
        """
        # An artifact with no sources makes no falsifiable claim about files;
        # hash comparison cannot say anything about it either way.
        if not sources:
            return current

        drifted = False
        for path, recorded in sources:
            actual = current_hashes.get(path)
            if actual is None:
                return ArtifactStatus.INVALIDATED
            if actual != recorded:
                drifted = True

        if drifted:
            return ArtifactStatus.STALE
        # Do not resurrect an artifact invalidated for a non-hash reason.
        if current in (ArtifactStatus.INVALIDATED, ArtifactStatus.GENERATION_FAILED):
            return current
        return ArtifactStatus.FRESH

    def invalidate(self, kind: ArtifactKind, keys: Sequence[str]) -> None:
        """Force-invalidate specific artifacts."""
        if not keys:
            return
        with self._store.transaction() as connection:
            connection.executemany(
                "UPDATE artifact_records SET status=? WHERE project_id=? AND kind=? AND key=?",
                [
                    (ArtifactStatus.INVALIDATED.value, self._project_id, kind.value, key)
                    for key in keys
                ],
            )

    def invalidate_by_path(self, paths: Sequence[str]) -> Sequence[ArtifactId]:
        """Invalidate every artifact derived from any of `paths`.

        Called after a mutating tool touches files. Marks STALE rather than
        INVALIDATED because the file still exists -- the artifact is out of
        date, not describing something that vanished.

        Returns the affected artifacts.
        """
        if not paths:
            return []

        placeholders = ",".join("?" * len(paths))
        with self._store.transaction() as connection:
            rows = connection.execute(
                "SELECT DISTINCT r.artifact_id FROM artifact_records r "  # noqa: S608
                "JOIN artifact_sources s ON s.artifact_id = r.artifact_id "
                f"WHERE r.project_id=? AND s.path IN ({placeholders}) "
                "AND r.status = ?",
                (self._project_id, *paths, ArtifactStatus.FRESH.value),
            ).fetchall()

            affected = [ArtifactId(row["artifact_id"]) for row in rows]
            if affected:
                connection.executemany(
                    "UPDATE artifact_records SET status=? WHERE artifact_id=?",
                    [(ArtifactStatus.STALE.value, artifact_id) for artifact_id in affected],
                )
        return affected

    def record_contradiction(self, contradiction: Contradiction) -> None:
        """Record that a fresh observation disagreed with a stored artifact.

        Plan section 17.2 fixes the resolution and it is not negotiable: the
        fresh result wins, the artifact is invalidated, the contradiction is
        recorded, regeneration is queued. This method does all four.
        """
        with self._store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO artifact_contradictions (
                    contradiction_id, artifact_id, observed_at, artifact_claim,
                    fresh_observation, source_tool
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    new_id(),
                    contradiction.artifact_id,
                    contradiction.observed_at.isoformat(),
                    contradiction.artifact_claim,
                    contradiction.fresh_observation,
                    contradiction.source_tool,
                ),
            )
            connection.execute(
                "UPDATE artifact_records SET status=? WHERE artifact_id=?",
                (ArtifactStatus.INVALIDATED.value, contradiction.artifact_id),
            )

    def contradictions(self, artifact_id: ArtifactId | None = None) -> Sequence[Contradiction]:
        """Recorded contradictions, for the freshness-error metric."""
        with self._store.reading() as connection:
            if artifact_id is None:
                rows = connection.execute(
                    "SELECT * FROM artifact_contradictions ORDER BY observed_at"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM artifact_contradictions WHERE artifact_id=? "
                    "ORDER BY observed_at",
                    (artifact_id,),
                ).fetchall()

        return [
            Contradiction(
                artifact_id=ArtifactId(row["artifact_id"]),
                observed_at=datetime.fromisoformat(row["observed_at"]),
                artifact_claim=row["artifact_claim"],
                fresh_observation=row["fresh_observation"],
                source_tool=row["source_tool"],
            )
            for row in rows
        ]

    def invalidate_by_generator(self, kind: ArtifactKind, generator_version: str) -> int:
        """Mark artifacts built by a superseded generator as stale.

        A generator change invalidates artifacts whose *sources* never changed,
        which is exactly why `generator_version` is tracked separately from
        `artifact_version`. Without this, a fixed extraction bug would leave
        every previously-built card confidently wrong.

        Returns the number of artifacts marked.
        """
        with self._store.transaction() as connection:
            cursor = connection.execute(
                "UPDATE artifact_records SET status=? "
                "WHERE project_id=? AND kind=? AND generator_version != ? AND status=?",
                (
                    ArtifactStatus.STALE.value,
                    self._project_id,
                    kind.value,
                    generator_version,
                    ArtifactStatus.FRESH.value,
                ),
            )
            return int(cursor.rowcount)

    # -- internals ---------------------------------------------------------

    def _content_path(self, kind: ArtifactKind, key: str) -> Path:
        return self._dir / content_filename(kind, key)

    def _set_status(self, artifact_id: ArtifactId, status: ArtifactStatus) -> None:
        with self._store.transaction() as connection:
            connection.execute(
                "UPDATE artifact_records SET status=? WHERE artifact_id=?",
                (status.value, artifact_id),
            )

    @staticmethod
    def _sources_for(connection: sqlite3.Connection, artifact_id: str) -> tuple[SourceRef, ...]:
        rows = connection.execute(
            "SELECT path, content_hash FROM artifact_sources WHERE artifact_id=? ORDER BY path",
            (artifact_id,),
        ).fetchall()
        return tuple(SourceRef(path=row["path"], content_hash=row["content_hash"]) for row in rows)

    @staticmethod
    def _meta_from_row(row: sqlite3.Row, sources: tuple[SourceRef, ...]) -> ArtifactMeta:
        return ArtifactMeta(
            artifact_id=ArtifactId(row["artifact_id"]),
            kind=ArtifactKind(row["kind"]),
            key=row["key"],
            sources=sources,
            artifact_version=row["artifact_version"],
            generator_version=row["generator_version"],
            created_at=datetime.fromisoformat(row["created_at"]),
            refreshed_at=datetime.fromisoformat(row["refreshed_at"]),
            status=ArtifactStatus(row["status"]),
            confidence=row["confidence"],
        )


__all__ = ["ArtifactRegistry", "content_filename"]
