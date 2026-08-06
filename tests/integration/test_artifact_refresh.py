"""Milestone 3's exit condition: artifacts regenerate correctly after source changes.

These run against real repositories on disk -- real git, real files, real
edits -- because the property being verified is about the interaction between
the filesystem, the generators, and the registry. A mocked version of this
would only prove the mocks agree with each other.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from shamsu.artifacts import ArtifactRefresher, ArtifactRegistry
from shamsu.artifacts.generators import FILE_LIST_SOURCE, RepositoryContext
from shamsu.interfaces.artifacts import ArtifactGenerationError
from shamsu.interfaces.enums import ArtifactKind, ArtifactStatus
from shamsu.interfaces.ids import ProjectId
from shamsu.state import ProjectRecord, StateStore

pytestmark = pytest.mark.integration

AUTH = '''"""Authentication."""


def login(user: str) -> bool:
    """Log a user in."""
    return True
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small real repository."""
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "auth.py").write_text(AUTH, encoding="utf-8")
    (root / "src" / "__init__.py").write_text('"""The app."""\n', encoding="utf-8")
    (root / "tests" / "test_auth.py").write_text(
        "def test_login() -> None:\n    assert True\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = []\n\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8"]\n\n'
        "[build-system]\nrequires = []\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


@pytest.fixture
def refresher(repo: Path, tmp_path: Path) -> ArtifactRefresher:
    store = StateStore(tmp_path / "state.db")
    store.upsert_project(ProjectRecord(project_id=ProjectId("p1"), root=str(repo), name="demo"))
    registry = ArtifactRegistry(store, ProjectId("p1"), tmp_path / "artifacts")
    return ArtifactRefresher(registry, repo)


@pytest.fixture
def registry(refresher: ArtifactRefresher) -> ArtifactRegistry:
    return refresher._registry  # noqa: SLF001 - the fixture's whole purpose


# ---------------------------------------------------------------------------
# The exit condition
# ---------------------------------------------------------------------------


class TestRegenerationCycle:
    def test_a_first_pass_builds_everything(
        self, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        report = refresher.refresh()
        assert report.ok, report.failed
        assert report.total_written > 0

        assert registry.get(ArtifactKind.REPOSITORY_MANIFEST, "repository_manifest")
        assert registry.get(ArtifactKind.REPOSITORY_MAP, "repository_map")
        assert registry.get(ArtifactKind.MODULE_CARD, "src/auth.py")

    def test_a_second_pass_does_no_work(self, refresher: ArtifactRefresher) -> None:
        """Nothing changed, so nothing should be rebuilt."""
        first = refresher.refresh()
        second = refresher.refresh()

        assert second.total_written == 0
        assert len(second.unchanged) == first.total_written

    def test_editing_a_file_regenerates_its_card(
        self, repo: Path, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        refresher.refresh()
        before = registry.get(ArtifactKind.MODULE_CARD, "src/auth.py")
        assert before is not None
        assert "login(user: str)" in before.content

        (repo / "src" / "auth.py").write_text(
            AUTH.replace("def login(user: str) -> bool:", "def login(user: str, pw: str) -> bool:"),
            encoding="utf-8",
        )

        report = refresher.refresh()
        after = registry.get(ArtifactKind.MODULE_CARD, "src/auth.py")
        assert after is not None
        assert "login(user: str, pw: str)" in after.content
        assert after.meta.artifact_version == 2
        assert after.meta.status is ArtifactStatus.FRESH
        assert any("src/auth.py" in label for label in report.regenerated)

    def test_an_unrelated_file_is_not_regenerated(
        self, repo: Path, refresher: ArtifactRefresher
    ) -> None:
        """A targeted edit must not rebuild the world."""
        refresher.refresh()
        (repo / "src" / "auth.py").write_text(AUTH + "\n\nEXTRA = 1\n", encoding="utf-8")

        report = refresher.refresh()
        assert not any("src/__init__.py" in label for label in report.regenerated)

    def test_adding_a_module_creates_its_card(
        self, repo: Path, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        """A new module needs a card before anything knows to ask for one."""
        refresher.refresh()
        (repo / "src" / "billing.py").write_text(
            '"""Billing."""\n\n\ndef charge() -> None:\n    """Charge."""\n', encoding="utf-8"
        )

        report = refresher.refresh()
        assert any("src/billing.py" in label for label in report.generated)
        assert registry.get(ArtifactKind.MODULE_CARD, "src/billing.py") is not None

    def test_deleting_a_module_retires_its_card(
        self, repo: Path, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        """A card for a deleted module must not linger as FRESH."""
        refresher.refresh()
        (repo / "src" / "auth.py").unlink()

        report = refresher.refresh()
        meta = registry.get_meta(ArtifactKind.MODULE_CARD, "src/auth.py")
        assert meta is not None
        assert meta.status is ArtifactStatus.INVALIDATED
        assert registry.usable(ArtifactKind.MODULE_CARD, "src/auth.py") is None
        assert any("src/auth.py" in label for label in report.retired)

    def test_a_retired_card_is_still_inspectable(
        self, repo: Path, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        """Invalidated, not deleted: what it said survives for debugging."""
        refresher.refresh()
        (repo / "src" / "auth.py").unlink()
        refresher.refresh()

        artifact = registry.get(ArtifactKind.MODULE_CARD, "src/auth.py")
        assert artifact is not None
        assert "login" in artifact.content

    def test_force_rebuilds_fresh_artifacts(self, refresher: ArtifactRefresher) -> None:
        refresher.refresh()
        report = refresher.refresh(force=True)
        assert report.total_written > 0
        assert report.unchanged == []


class TestRepositoryWideFreshness:
    """Artifacts whose claims span the repository, not just the files they read.

    Regression coverage: the manifest reports a file count and directory list,
    so deleting a module must not leave it FRESH just because `pyproject.toml`
    is unchanged.
    """

    def test_deleting_a_file_updates_the_manifest(
        self, repo: Path, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        refresher.refresh()
        before = json.loads(
            registry.get(ArtifactKind.REPOSITORY_MANIFEST, "repository_manifest").content  # type: ignore[union-attr]
        )

        (repo / "src" / "auth.py").unlink()
        refresher.refresh()

        after = json.loads(
            registry.get(ArtifactKind.REPOSITORY_MANIFEST, "repository_manifest").content  # type: ignore[union-attr]
        )
        assert after["file_count"] == before["file_count"] - 1

    def test_adding_a_file_updates_the_manifest(
        self, repo: Path, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        refresher.refresh()
        before = json.loads(
            registry.get(ArtifactKind.REPOSITORY_MANIFEST, "repository_manifest").content  # type: ignore[union-attr]
        )

        (repo / "src" / "new.py").write_text("X = 1\n", encoding="utf-8")
        refresher.refresh()

        after = json.loads(
            registry.get(ArtifactKind.REPOSITORY_MANIFEST, "repository_manifest").content  # type: ignore[union-attr]
        )
        assert after["file_count"] == before["file_count"] + 1

    def test_the_file_list_source_is_declared(
        self, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        refresher.refresh()
        meta = registry.get_meta(ArtifactKind.REPOSITORY_MANIFEST, "repository_manifest")
        assert meta is not None
        assert FILE_LIST_SOURCE in [ref.path for ref in meta.sources]

    def test_the_synthetic_source_does_not_look_deleted(
        self, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        """Passing raw hashes would invalidate every repository-wide artifact."""
        refresher.refresh()
        refresher.refresh()
        meta = registry.get_meta(ArtifactKind.REPOSITORY_MANIFEST, "repository_manifest")
        assert meta is not None
        assert meta.status is ArtifactStatus.FRESH


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------


class TestManifestContent:
    def test_reports_deterministic_project_facts(
        self, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        refresher.refresh()
        manifest = json.loads(
            registry.get(ArtifactKind.REPOSITORY_MANIFEST, "repository_manifest").content  # type: ignore[union-attr]
        )
        assert manifest["languages"] == ["Python"]
        assert manifest["package_managers"] == ["pip/hatch"]
        assert "pytest" in manifest["test_frameworks"]
        assert "pyproject.toml" in manifest["manifest_files"]

    def test_a_malformed_manifest_does_not_fail_the_artifact(
        self, repo: Path, refresher: ArtifactRefresher
    ) -> None:
        """A broken pyproject.toml is a fact about the repo, not a crash."""
        (repo / "pyproject.toml").write_text("[project\nbroken", encoding="utf-8")
        report = refresher.refresh()
        assert report.ok, report.failed


class TestModuleCardContent:
    def test_records_the_public_interface_from_the_parser(
        self, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        refresher.refresh()
        card = registry.get(ArtifactKind.MODULE_CARD, "src/auth.py")
        assert card is not None
        assert "login(user: str) -> bool" in card.content
        assert "Log a user in." in card.content

    def test_finds_related_tests_by_convention(
        self, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        refresher.refresh()
        card = registry.get(ArtifactKind.MODULE_CARD, "src/auth.py")
        assert card is not None
        assert "tests/test_auth.py" in card.content
        # Labelled, so it is not mistaken for measured coverage.
        assert "not measured coverage" in card.content

    def test_states_what_is_not_yet_computed(
        self, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        """A blank 'Callers' heading would read as 'nothing calls this'.

        That is a structural claim the call graph has not been built to earn.
        Saying so explicitly is the difference between honest and fabricated.
        """
        refresher.refresh()
        card = registry.get(ArtifactKind.MODULE_CARD, "src/auth.py")
        assert card is not None
        assert "Not yet computed" in card.content
        assert "Milestone 8" in card.content

    def test_records_reverse_import_edges(
        self, repo: Path, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        (repo / "src" / "api.py").write_text(
            '"""API."""\n\nfrom src.auth import login\n\n\ndef handler() -> bool:\n'
            "    return login('x')\n",
            encoding="utf-8",
        )
        refresher.refresh()

        card = registry.get(ArtifactKind.MODULE_CARD, "src/auth.py")
        assert card is not None
        assert "src/api.py" in card.content


class TestSymbolCardContent:
    def test_records_location_signature_and_source_hash(
        self, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        refresher.refresh()
        card = registry.get(ArtifactKind.SYMBOL_CARD, "src/auth.py::login")
        assert card is not None
        assert "login(user: str) -> bool" in card.content
        assert "src/auth.py" in card.content
        # Self-describing: the card records which file version it came from,
        # independent of the registry.
        assert "sha256:" in card.content

    def test_private_symbols_get_no_card(
        self, repo: Path, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        """A card per private helper would multiply the set for no gain."""
        (repo / "src" / "auth.py").write_text(
            AUTH + "\n\ndef _internal() -> None: ...\n", encoding="utf-8"
        )
        refresher.refresh()
        assert registry.get(ArtifactKind.SYMBOL_CARD, "src/auth.py::_internal") is None


class TestRepositoryMapContent:
    def test_describes_directories_from_package_docstrings(
        self, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        """Descriptions are read from source, never invented."""
        refresher.refresh()
        card = registry.get(ArtifactKind.REPOSITORY_MAP, "repository_map")
        assert card is not None
        assert "src/" in card.content
        assert "The app." in card.content


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    def test_a_broken_file_does_not_fail_the_pass(
        self, repo: Path, refresher: ArtifactRefresher
    ) -> None:
        """One unparseable file must not stop every other artifact."""
        (repo / "src" / "broken.py").write_text("def oops(:\n", encoding="utf-8")
        report = refresher.refresh()
        assert report.total_written > 0

    def test_a_broken_file_gets_no_fabricated_card(
        self, repo: Path, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        (repo / "src" / "broken.py").write_text("def oops(:\n", encoding="utf-8")
        refresher.refresh()
        assert registry.usable(ArtifactKind.MODULE_CARD, "src/broken.py") is None

    def test_a_missing_symbol_key_is_an_honest_error(self, repo: Path, tmp_path: Path) -> None:
        from shamsu.artifacts.generators import SymbolCardGenerator

        generator = SymbolCardGenerator(RepositoryContext(repo))
        with pytest.raises(ArtifactGenerationError):
            generator.generate("src/auth.py::does_not_exist")

    def test_a_malformed_symbol_key_is_rejected(self, repo: Path) -> None:
        from shamsu.artifacts.generators import SymbolCardGenerator

        generator = SymbolCardGenerator(RepositoryContext(repo))
        with pytest.raises(ArtifactGenerationError):
            generator.generate("no-separator-here")


# ---------------------------------------------------------------------------
# Cancellation and scoping
# ---------------------------------------------------------------------------


class TestRefreshControl:
    def test_a_refresh_is_cancellable(self, refresher: ArtifactRefresher) -> None:
        """A refresh over a large repository must not make a run uninterruptible."""
        from tests.fixtures.fake_model import CancelAfter

        from shamsu.interfaces.cancellation import Cancelled

        with pytest.raises(Cancelled):
            refresher.refresh(cancel=CancelAfter(checks=0))

    def test_kinds_can_be_restricted(
        self, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        refresher.refresh(kinds=[ArtifactKind.REPOSITORY_MANIFEST])
        assert registry.get(ArtifactKind.REPOSITORY_MANIFEST, "repository_manifest")
        assert registry.list_by_kind(ArtifactKind.MODULE_CARD) == []

    def test_a_mutating_tool_can_stale_artifacts_without_a_full_pass(
        self, refresher: ArtifactRefresher, registry: ArtifactRegistry
    ) -> None:
        """The window between 'a file changed' and 'the artifacts know'."""
        refresher.refresh()
        affected = refresher.invalidate_for_paths(["src/auth.py"])
        assert affected

        meta = registry.get_meta(ArtifactKind.MODULE_CARD, "src/auth.py")
        assert meta is not None
        assert meta.status is ArtifactStatus.STALE


class TestScanScope:
    def test_the_archive_is_never_indexed(self, repo: Path, refresher: ArtifactRefresher) -> None:
        """SHAMSU indexes its own repo; cards describing v1 would mislead."""
        legacy = repo / "legacy-code" / "shamsu"
        legacy.mkdir(parents=True)
        (legacy / "chat_loop.py").write_text("class AgentChatLoop: ...\n", encoding="utf-8")

        context = RepositoryContext(repo)
        assert not any(path.startswith("legacy-code") for path in context.paths)

    def test_gitignored_trees_are_not_indexed(
        self, repo: Path, refresher: ArtifactRefresher
    ) -> None:
        (repo / ".gitignore").write_text("vendored/\n", encoding="utf-8")
        (repo / "vendored").mkdir()
        (repo / "vendored" / "other.py").write_text("X = 1\n", encoding="utf-8")

        context = RepositoryContext(repo)
        assert not any(path.startswith("vendored") for path in context.paths)
