"""Artifact hashing, registry, and invalidation.

The property under test throughout is that an artifact cannot quietly go out of
date. A stale structural claim is worse than a missing one: the model receives
something confident, structured, and wrong, and has no way to tell.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shamsu.artifacts import (
    ArtifactRegistry,
    changed_paths,
    content_filename,
    hash_bytes,
    hash_file,
    hash_text,
    is_ignored,
    scan_repository,
)
from shamsu.artifacts.hashing import LARGE_FILE_BYTES, git_listed_files
from shamsu.artifacts.registry import ArtifactRegistry as Registry
from shamsu.interfaces.artifacts import Contradiction, SourceRef
from shamsu.interfaces.enums import ArtifactKind, ArtifactStatus
from shamsu.interfaces.ids import ProjectId
from shamsu.state import ProjectRecord, StateStore

GEN = "test-generator/1"


@pytest.fixture
def store() -> StateStore:
    store = StateStore(":memory:")
    store.upsert_project(
        ProjectRecord(project_id=ProjectId("p1"), root="/workspace/demo", name="demo")
    )
    return store


@pytest.fixture
def registry(store: StateStore, tmp_path: Path) -> ArtifactRegistry:
    return ArtifactRegistry(store, ProjectId("p1"), tmp_path / "artifacts")


def _source(path: str, content: str) -> SourceRef:
    return SourceRef(path=path, content_hash=hash_text(content))


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


class TestHashing:
    def test_same_content_hashes_the_same(self) -> None:
        assert hash_text("def login(): ...") == hash_text("def login(): ...")

    def test_different_content_hashes_differently(self) -> None:
        assert hash_text("a") != hash_text("b")

    def test_hashes_are_prefixed_so_the_scheme_can_change(self) -> None:
        assert hash_bytes(b"x").startswith("sha256:")

    def test_file_hash_matches_text_hash(self, tmp_path: Path) -> None:
        path = tmp_path / "auth.py"
        path.write_text("def login(): ...", encoding="utf-8")
        assert hash_file(path) == hash_text("def login(): ...")

    def test_a_missing_file_hashes_to_a_stable_marker(self, tmp_path: Path) -> None:
        """An unreadable file must read as *changed*, not silently unchanged."""
        assert hash_file(tmp_path / "gone.py") == "missing:"

    def test_a_huge_file_is_recorded_by_size(self, tmp_path: Path) -> None:
        """Hashing a 40 MB generated file every refresh buys nothing."""
        path = tmp_path / "big.txt"
        path.write_bytes(b"x" * (LARGE_FILE_BYTES + 10))
        assert hash_file(path).startswith("size:")

    def test_mtime_alone_does_not_change_the_hash(self, tmp_path: Path) -> None:
        """Why hashes and not timestamps: touch moves mtime, not content."""
        path = tmp_path / "a.py"
        path.write_text("same", encoding="utf-8")
        before = hash_file(path)
        path.touch()
        assert hash_file(path) == before


class TestIgnoreRules:
    @pytest.mark.parametrize(
        "path",
        [
            ".git/config",
            "node_modules/left-pad/index.js",
            "__pycache__/mod.cpython-311.pyc",
            ".venv/lib/site-packages/x.py",
            "src/app.pyc",
            "assets/logo.png",
            "legacy-code/shamsu/agents/chat_loop.py",
        ],
    )
    def test_ignored(self, path: str) -> None:
        assert is_ignored(Path(path)) is True

    @pytest.mark.parametrize(
        "path", ["src/shamsu/state/store.py", "README.md", "pyproject.toml", "tests/conftest.py"]
    )
    def test_kept(self, path: str) -> None:
        assert is_ignored(Path(path)) is False

    def test_the_archive_is_excluded_by_default(self) -> None:
        """SHAMSU indexes its own repo; artifacts about v1 would be misleading."""
        assert is_ignored(Path("legacy-code/shamsu/llm/output.py")) is True


class TestScanning:
    def test_walks_a_plain_directory(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1", encoding="utf-8")
        (tmp_path / "README.md").write_text("# hi", encoding="utf-8")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "junk.js").write_text("junk", encoding="utf-8")

        scanned = scan_repository(tmp_path, use_git=False)
        assert set(scanned) == {"src/app.py", "README.md"}

    def test_paths_are_posix_style(self, tmp_path: Path) -> None:
        """An artifact built on Windows must stay comparable to a Linux one."""
        (tmp_path / "a" / "b").mkdir(parents=True)
        (tmp_path / "a" / "b" / "c.py").write_text("x", encoding="utf-8")
        assert "a/b/c.py" in scan_repository(tmp_path, use_git=False)

    def test_symlinks_are_not_followed(self, tmp_path: Path) -> None:
        """A link into a sibling checkout would make the walk unbounded."""
        (tmp_path / "real.py").write_text("x", encoding="utf-8")
        try:
            (tmp_path / "link.py").symlink_to(tmp_path / "real.py")
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        assert set(scan_repository(tmp_path, use_git=False)) == {"real.py"}

    def test_git_view_respects_gitignore(self, tmp_path: Path) -> None:
        """The project already declares what belongs to it. Use that."""
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            pytest.skip("git unavailable")

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
        (tmp_path / ".gitignore").write_text("vendored/\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("x = 1", encoding="utf-8")
        (tmp_path / "vendored").mkdir()
        (tmp_path / "vendored" / "other.py").write_text("not ours", encoding="utf-8")

        scanned = scan_repository(tmp_path)
        assert "app.py" in scanned
        assert "vendored/other.py" not in scanned

    def test_untracked_new_files_are_still_indexed(self, tmp_path: Path) -> None:
        """A file the agent just created must be visible before it is committed."""
        if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
            pytest.skip("git unavailable")

        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
        (tmp_path / "brand_new.py").write_text("x = 1", encoding="utf-8")
        assert "brand_new.py" in scan_repository(tmp_path)

    def test_falls_back_when_not_a_git_repository(self, tmp_path: Path) -> None:
        assert git_listed_files(tmp_path / "does-not-exist") is None
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        assert "a.py" in scan_repository(tmp_path)


class TestChangeDetection:
    def test_classifies_added_modified_and_removed(self) -> None:
        """Kept separate because they mean different things for invalidation."""
        previous = {"a.py": "h1", "b.py": "h2", "c.py": "h3"}
        current = {"a.py": "h1", "b.py": "CHANGED", "d.py": "h4"}
        added, modified, removed = changed_paths(previous, current)
        assert added == frozenset({"d.py"})
        assert modified == frozenset({"b.py"})
        assert removed == frozenset({"c.py"})

    def test_no_change_is_empty(self) -> None:
        same = {"a.py": "h1"}
        assert changed_paths(same, dict(same)) == (frozenset(), frozenset(), frozenset())


# ---------------------------------------------------------------------------
# Content paths
# ---------------------------------------------------------------------------


class TestContentPaths:
    def test_module_keys_flatten_as_the_plan_specifies(self) -> None:
        assert content_filename(ArtifactKind.MODULE_CARD, "apps/api/auth") == (
            "modules/apps__api__auth.md"
        )

    def test_singletons_sit_at_the_top_level(self) -> None:
        assert content_filename(ArtifactKind.REPOSITORY_MAP, "repository_map") == (
            "repository_map.md"
        )

    def test_json_kinds_get_a_json_suffix(self) -> None:
        assert content_filename(ArtifactKind.TEST_MAP, "test_map").endswith(".json")

    def test_nested_keys_cannot_collide_as_file_versus_directory(self) -> None:
        """`a/b` and `a/b/c` must not fight over the same path."""
        first = content_filename(ArtifactKind.MODULE_CARD, "a/b")
        second = content_filename(ArtifactKind.MODULE_CARD, "a/b/c")
        assert first != second
        assert "/" not in first.removeprefix("modules/")


# ---------------------------------------------------------------------------
# Registry storage
# ---------------------------------------------------------------------------


class TestRegistryStorage:
    def test_round_trips_content_and_metadata(self, registry: Registry) -> None:
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "# auth\n\nHandles login.",
            [_source("src/auth.py", "def login(): ...")],
            generator_version=GEN,
        )

        artifact = registry.get(ArtifactKind.MODULE_CARD, "src/auth")
        assert artifact is not None
        assert artifact.content == "# auth\n\nHandles login."
        assert artifact.meta.status is ArtifactStatus.FRESH
        assert artifact.meta.artifact_version == 1
        assert artifact.meta.sources[0].path == "src/auth.py"

    def test_content_is_written_where_a_human_can_read_it(
        self, registry: Registry, tmp_path: Path
    ) -> None:
        """Someone debugging a bad decision should be able to cat the card."""
        registry.put(ArtifactKind.MODULE_CARD, "src/auth", "# auth", [], generator_version=GEN)
        assert (tmp_path / "artifacts" / "modules" / "src__auth.md").read_text() == "# auth"

    def test_unknown_artifacts_are_none(self, registry: Registry) -> None:
        assert registry.get(ArtifactKind.MODULE_CARD, "never/built") is None

    def test_regeneration_bumps_version_and_keeps_creation_time(self, registry: Registry) -> None:
        first = registry.put(ArtifactKind.MODULE_CARD, "src/auth", "v1", [], generator_version=GEN)
        second = registry.put(ArtifactKind.MODULE_CARD, "src/auth", "v2", [], generator_version=GEN)
        assert second.artifact_version == 2
        assert second.created_at == first.created_at
        assert registry.get(ArtifactKind.MODULE_CARD, "src/auth").content == "v2"  # type: ignore[union-attr]

    def test_regeneration_replaces_sources_rather_than_accumulating(
        self, registry: Registry
    ) -> None:
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "v1",
            [_source("src/auth.py", "a"), _source("src/old.py", "b")],
            generator_version=GEN,
        )
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "v2",
            [_source("src/auth.py", "a")],
            generator_version=GEN,
        )
        meta = registry.get_meta(ArtifactKind.MODULE_CARD, "src/auth")
        assert meta is not None
        assert [ref.path for ref in meta.sources] == ["src/auth.py"]

    def test_listing_a_kind_is_ordered_and_content_free(self, registry: Registry) -> None:
        for key in ["src/b", "src/a", "src/c"]:
            registry.put(ArtifactKind.MODULE_CARD, key, "x", [], generator_version=GEN)
        assert [m.key for m in registry.list_by_kind(ArtifactKind.MODULE_CARD)] == [
            "src/a",
            "src/b",
            "src/c",
        ]

    def test_a_lost_content_file_reports_missing_not_absent(
        self, registry: Registry, tmp_path: Path
    ) -> None:
        """'Never built' and 'built then lost' need different responses."""
        registry.put(ArtifactKind.MODULE_CARD, "src/auth", "# auth", [], generator_version=GEN)
        (tmp_path / "artifacts" / "modules" / "src__auth.md").unlink()

        artifact = registry.get(ArtifactKind.MODULE_CARD, "src/auth")
        assert artifact is not None
        assert artifact.meta.status is ArtifactStatus.MISSING
        assert artifact.content == ""

    def test_generation_failure_is_recorded_not_guessed(self, registry: Registry) -> None:
        """A missing artifact is a known unknown; a fabricated one is a lie."""
        meta = registry.record_generation_failure(
            ArtifactKind.SYMBOL_CARD, "src/auth.login", generator_version=GEN, detail="parse error"
        )
        assert meta.status is ArtifactStatus.GENERATION_FAILED
        assert meta.confidence == 0.0
        assert registry.usable(ArtifactKind.SYMBOL_CARD, "src/auth.login") is None


# ---------------------------------------------------------------------------
# Usability gating
# ---------------------------------------------------------------------------


class TestUsability:
    @pytest.mark.parametrize("status", [ArtifactStatus.FRESH, ArtifactStatus.STALE])
    def test_fresh_and_stale_may_reach_the_model(
        self, registry: Registry, status: ArtifactStatus
    ) -> None:
        registry.put(
            ArtifactKind.MODULE_CARD, "src/a", "x", [], generator_version=GEN, status=status
        )
        assert registry.usable(ArtifactKind.MODULE_CARD, "src/a") is not None

    @pytest.mark.parametrize(
        "status",
        [
            ArtifactStatus.INVALIDATED,
            ArtifactStatus.MISSING,
            ArtifactStatus.GENERATION_FAILED,
        ],
    )
    def test_unusable_statuses_cannot_reach_the_model(
        self, registry: Registry, status: ArtifactStatus
    ) -> None:
        registry.put(
            ArtifactKind.MODULE_CARD, "src/a", "x", [], generator_version=GEN, status=status
        )
        assert registry.usable(ArtifactKind.MODULE_CARD, "src/a") is None
        # ...but it is still inspectable, so a human can see what went wrong.
        assert registry.get(ArtifactKind.MODULE_CARD, "src/a") is not None


# ---------------------------------------------------------------------------
# Invalidation -- the core of the module
# ---------------------------------------------------------------------------


class TestFreshnessRecomputation:
    def test_unchanged_sources_stay_fresh(self, registry: Registry) -> None:
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "x",
            [_source("src/auth.py", "original")],
            generator_version=GEN,
        )
        changed = registry.recompute_status({"src/auth.py": hash_text("original")})
        assert changed == []
        assert registry.get_meta(ArtifactKind.MODULE_CARD, "src/auth").status is (  # type: ignore[union-attr]
            ArtifactStatus.FRESH
        )

    def test_a_modified_source_makes_it_stale(self, registry: Registry) -> None:
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "x",
            [_source("src/auth.py", "original")],
            generator_version=GEN,
        )
        changed = registry.recompute_status({"src/auth.py": hash_text("edited")})
        assert len(changed) == 1
        assert registry.get_meta(ArtifactKind.MODULE_CARD, "src/auth").status is (  # type: ignore[union-attr]
            ArtifactStatus.STALE
        )

    def test_a_deleted_source_invalidates(self, registry: Registry) -> None:
        """The artifact describes something gone; nothing about it is trustworthy."""
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "x",
            [_source("src/auth.py", "original")],
            generator_version=GEN,
        )
        registry.recompute_status({})
        assert registry.get_meta(ArtifactKind.MODULE_CARD, "src/auth").status is (  # type: ignore[union-attr]
            ArtifactStatus.INVALIDATED
        )

    def test_one_changed_source_of_several_is_enough(self, registry: Registry) -> None:
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "x",
            [_source("src/a.py", "a"), _source("src/b.py", "b")],
            generator_version=GEN,
        )
        registry.recompute_status({"src/a.py": hash_text("a"), "src/b.py": hash_text("b CHANGED")})
        assert registry.get_meta(ArtifactKind.MODULE_CARD, "src/auth").status is (  # type: ignore[union-attr]
            ArtifactStatus.STALE
        )

    def test_deletion_outranks_modification(self, registry: Registry) -> None:
        """A gone source is worse news than a changed one; report the worse."""
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "x",
            [_source("src/a.py", "a"), _source("src/gone.py", "b")],
            generator_version=GEN,
        )
        registry.recompute_status({"src/a.py": hash_text("a CHANGED")})
        assert registry.get_meta(ArtifactKind.MODULE_CARD, "src/auth").status is (  # type: ignore[union-attr]
            ArtifactStatus.INVALIDATED
        )

    def test_recomputing_does_not_resurrect_an_invalidated_artifact(
        self, registry: Registry
    ) -> None:
        """It may have been invalidated by a contradiction, not by a hash."""
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "x",
            [_source("src/auth.py", "original")],
            generator_version=GEN,
            status=ArtifactStatus.INVALIDATED,
        )
        registry.recompute_status({"src/auth.py": hash_text("original")})
        assert registry.get_meta(ArtifactKind.MODULE_CARD, "src/auth").status is (  # type: ignore[union-attr]
            ArtifactStatus.INVALIDATED
        )

    def test_a_stale_artifact_returns_to_fresh_when_the_edit_is_reverted(
        self, registry: Registry
    ) -> None:
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "x",
            [_source("src/auth.py", "original")],
            generator_version=GEN,
        )
        registry.recompute_status({"src/auth.py": hash_text("edited")})
        registry.recompute_status({"src/auth.py": hash_text("original")})
        assert registry.get_meta(ArtifactKind.MODULE_CARD, "src/auth").status is (  # type: ignore[union-attr]
            ArtifactStatus.FRESH
        )

    def test_sourceless_artifacts_are_left_alone(self, registry: Registry) -> None:
        """They make no falsifiable claim about files, so hashes say nothing."""
        registry.put(ArtifactKind.TASK_PACKET, "t1", "{}", [], generator_version=GEN)
        registry.recompute_status({})
        assert registry.get_meta(ArtifactKind.TASK_PACKET, "t1").status is (  # type: ignore[union-attr]
            ArtifactStatus.FRESH
        )


class TestInvalidationRules:
    """The rules themselves, without a database.

    `_evaluate` takes plain `(path, hash)` pairs precisely so the most
    consequential logic in the module can be exercised directly.
    """

    @staticmethod
    def _decide(
        sources: list[tuple[str, str]],
        current: dict[str, str],
        status: ArtifactStatus = ArtifactStatus.FRESH,
    ) -> ArtifactStatus:
        return Registry._evaluate(sources, current, status)  # noqa: SLF001

    def test_matching_hashes_are_fresh(self) -> None:
        assert self._decide([("a.py", "h1")], {"a.py": "h1"}) is ArtifactStatus.FRESH

    def test_a_changed_hash_is_stale(self) -> None:
        assert self._decide([("a.py", "h1")], {"a.py": "h2"}) is ArtifactStatus.STALE

    def test_an_absent_path_is_invalidated(self) -> None:
        assert self._decide([("a.py", "h1")], {}) is ArtifactStatus.INVALIDATED

    def test_absence_outranks_drift(self) -> None:
        """A gone source is worse news than a changed one; report the worse."""
        assert (
            self._decide([("a.py", "h1"), ("b.py", "h2")], {"a.py": "CHANGED"})
            is ArtifactStatus.INVALIDATED
        )

    def test_no_sources_leaves_the_status_untouched(self) -> None:
        for status in ArtifactStatus:
            assert self._decide([], {"a.py": "h1"}, status) is status

    @pytest.mark.parametrize(
        "status", [ArtifactStatus.INVALIDATED, ArtifactStatus.GENERATION_FAILED]
    )
    def test_matching_hashes_do_not_resurrect(self, status: ArtifactStatus) -> None:
        """It may have been invalidated by a contradiction, not by a hash."""
        assert self._decide([("a.py", "h1")], {"a.py": "h1"}, status) is status

    def test_a_stale_artifact_recovers_when_the_edit_is_reverted(self) -> None:
        assert (
            self._decide([("a.py", "h1")], {"a.py": "h1"}, ArtifactStatus.STALE)
            is ArtifactStatus.FRESH
        )


class TestPathInvalidation:
    def test_editing_a_file_stales_its_artifacts(self, registry: Registry) -> None:
        """What runs after every mutating tool call."""
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "x",
            [_source("src/auth.py", "a")],
            generator_version=GEN,
        )
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/other",
            "y",
            [_source("src/other.py", "b")],
            generator_version=GEN,
        )

        affected = registry.invalidate_by_path(["src/auth.py"])
        assert len(affected) == 1
        assert registry.get_meta(ArtifactKind.MODULE_CARD, "src/auth").status is (  # type: ignore[union-attr]
            ArtifactStatus.STALE
        )
        assert registry.get_meta(ArtifactKind.MODULE_CARD, "src/other").status is (  # type: ignore[union-attr]
            ArtifactStatus.FRESH
        )

    def test_one_edit_stales_every_artifact_derived_from_it(self, registry: Registry) -> None:
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "x",
            [_source("src/auth.py", "a")],
            generator_version=GEN,
        )
        registry.put(
            ArtifactKind.SYMBOL_CARD,
            "src/auth.login",
            "y",
            [_source("src/auth.py", "a")],
            generator_version=GEN,
        )
        assert len(registry.invalidate_by_path(["src/auth.py"])) == 2

    def test_an_empty_path_list_does_nothing(self, registry: Registry) -> None:
        assert registry.invalidate_by_path([]) == []

    def test_explicit_invalidation(self, registry: Registry) -> None:
        registry.put(ArtifactKind.MODULE_CARD, "src/a", "x", [], generator_version=GEN)
        registry.invalidate(ArtifactKind.MODULE_CARD, ["src/a"])
        assert registry.get_meta(ArtifactKind.MODULE_CARD, "src/a").status is (  # type: ignore[union-attr]
            ArtifactStatus.INVALIDATED
        )


class TestGeneratorVersioning:
    def test_a_new_generator_stales_old_artifacts(self, registry: Registry) -> None:
        """A fixed extraction bug must not leave old cards confidently wrong.

        This is why generator_version is tracked separately from
        artifact_version: these artifacts' sources never changed.
        """
        registry.put(
            ArtifactKind.MODULE_CARD,
            "src/a",
            "x",
            [_source("src/a.py", "a")],
            generator_version="module-card/1",
        )
        assert registry.invalidate_by_generator(ArtifactKind.MODULE_CARD, "module-card/2") == 1
        assert registry.get_meta(ArtifactKind.MODULE_CARD, "src/a").status is (  # type: ignore[union-attr]
            ArtifactStatus.STALE
        )

    def test_the_same_generator_leaves_artifacts_alone(self, registry: Registry) -> None:
        registry.put(ArtifactKind.MODULE_CARD, "src/a", "x", [], generator_version="module-card/1")
        assert registry.invalidate_by_generator(ArtifactKind.MODULE_CARD, "module-card/1") == 0


# ---------------------------------------------------------------------------
# Contradictions
# ---------------------------------------------------------------------------


class TestContradictions:
    def test_a_contradiction_invalidates_and_is_recorded(self, registry: Registry) -> None:
        """Plan section 17.2: fresh wins, artifact invalidated, contradiction kept."""
        meta = registry.put(
            ArtifactKind.MODULE_CARD,
            "src/auth",
            "exports login()",
            [_source("src/auth.py", "a")],
            generator_version=GEN,
        )

        registry.record_contradiction(
            Contradiction(
                artifact_id=meta.artifact_id,
                observed_at=datetime.now(UTC),
                artifact_claim="auth exports login()",
                fresh_observation="grep found no def login",
                source_tool="code.search",
            )
        )

        assert registry.get_meta(ArtifactKind.MODULE_CARD, "src/auth").status is (  # type: ignore[union-attr]
            ArtifactStatus.INVALIDATED
        )
        assert registry.usable(ArtifactKind.MODULE_CARD, "src/auth") is None

        recorded = registry.contradictions(meta.artifact_id)
        assert len(recorded) == 1
        assert recorded[0].source_tool == "code.search"

    def test_contradictions_outlive_regeneration(self, registry: Registry) -> None:
        """The rate of these is an evaluation metric, so it must not be erased."""
        meta = registry.put(ArtifactKind.MODULE_CARD, "src/auth", "v1", [], generator_version=GEN)
        registry.record_contradiction(
            Contradiction(
                artifact_id=meta.artifact_id,
                observed_at=datetime.now(UTC),
                artifact_claim="claim",
                fresh_observation="reality",
                source_tool="file.read",
            )
        )
        registry.put(ArtifactKind.MODULE_CARD, "src/auth", "v2", [], generator_version=GEN)
        assert len(registry.contradictions()) == 1


# ---------------------------------------------------------------------------
# Regeneration queue
# ---------------------------------------------------------------------------


class TestRegenerationQueue:
    def test_stale_lists_everything_needing_work(self, registry: Registry) -> None:
        registry.put(ArtifactKind.MODULE_CARD, "fresh", "x", [], generator_version=GEN)
        registry.put(
            ArtifactKind.MODULE_CARD,
            "stale",
            "x",
            [],
            generator_version=GEN,
            status=ArtifactStatus.STALE,
        )
        registry.put(
            ArtifactKind.MODULE_CARD,
            "bad",
            "x",
            [],
            generator_version=GEN,
            status=ArtifactStatus.INVALIDATED,
        )

        keys = [meta.key for meta in registry.stale()]
        assert "fresh" not in keys
        assert set(keys) == {"stale", "bad"}

    def test_invalidated_is_queued_before_stale(self, registry: Registry) -> None:
        """Invalidated is actively unusable; stale is merely suspect."""
        registry.put(
            ArtifactKind.MODULE_CARD,
            "aaa-stale",
            "x",
            [],
            generator_version=GEN,
            status=ArtifactStatus.STALE,
        )
        registry.put(
            ArtifactKind.MODULE_CARD,
            "zzz-invalid",
            "x",
            [],
            generator_version=GEN,
            status=ArtifactStatus.INVALIDATED,
        )
        assert [meta.key for meta in registry.stale()] == ["zzz-invalid", "aaa-stale"]
