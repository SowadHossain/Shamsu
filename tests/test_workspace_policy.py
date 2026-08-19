from __future__ import annotations

from pathlib import Path

from shamsu.agents.plan_mode import workspace_source_files
from shamsu.indexer.policy import ensure_cbm_ignore, workspace_manifest
from shamsu.indexer.walker import FileWalker
from shamsu.llm.manager import _context_preview
from shamsu.retriever.semantic import SemanticIndex
from shamsu.tools.workspace import WorkspaceTool
from shamsu.types import ContextPack
from shamsu.abstract.service import AbstractService
from tests.test_abstract_service import FakeCodebaseMemoryAdapter


def _embed(texts: list[str]) -> list[list[float]]:
    return [[1.0, 0.0] for _text in texts]


def test_retrieval_consumers_share_internal_directory_exclusions(tmp_path: Path):
    (tmp_path / "app.py").write_text("def visible():\n    return True\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    for directory in (".shamsu", "node_modules", ".venv", ".codebase-memory"):
        target = tmp_path / directory
        target.mkdir(parents=True)
        (target / "hidden.py").write_text("def hidden():\n    pass\n", encoding="utf-8")

    walked = {path.relative_to(tmp_path).as_posix() for path in FileWalker(tmp_path).discover()}
    planned = set(workspace_source_files(tmp_path))
    found = {path.relative_to(tmp_path).as_posix() for path in WorkspaceTool(tmp_path).find_files(".py")}
    semantic = set(SemanticIndex(tmp_path, embed=_embed)._source_files())

    for paths in (walked, planned, found, semantic):
        assert "app.py" in paths
        assert not any("hidden.py" in path for path in paths)
    assert ".github/workflows/ci.yml" in walked


def test_workspace_manifest_is_stable_for_internal_artifacts(tmp_path: Path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    before = workspace_manifest(tmp_path)
    internal = tmp_path / ".shamsu" / "runs"
    internal.mkdir(parents=True)
    (internal / "events.jsonl").write_text("{}\n", encoding="utf-8")

    assert workspace_manifest(tmp_path) == before


def test_cbmignore_is_policy_owned_but_preserves_user_content(tmp_path: Path):
    path = tmp_path / ".cbmignore"
    path.write_text("secret/\n", encoding="utf-8")

    first = ensure_cbm_ignore(tmp_path)
    second = ensure_cbm_ignore(tmp_path)
    content = path.read_text(encoding="utf-8")

    assert first["ok"] is True
    assert second["changed"] is False
    assert content.startswith("secret/\n")
    assert content.count("BEGIN SHAMSU MANAGED EXCLUSIONS") == 1
    assert ".shamsu/" in content


def test_context_preview_records_the_code_memory_index_version(tmp_path: Path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter()).ensure_ready()
    pack = ContextPack(
        task_id="task-1",
        step_id=1,
        specialist="qa",
        user_request="Explain app.py",
    )

    preview = _context_preview(pack, tmp_path)

    assert preview["code_memory_index"]["exists"] is True
    assert preview["code_memory_index"]["stale"] is False
    assert preview["code_memory_index"]["manifest_hash"]
    assert preview["code_memory_index"]["policy_version"] == 1


# --- vendored clones must not answer for this project (G2) --------------
#
# Reproduced live on this repo before the fix:
#   graph_search("message_tokens")
#     -> _estimate_emitted_message_tokens
#        other peoples work/SmallCTL/src/smallctl/context/assembler.py
# Wrong function, wrong project; the real one in shamsu/context/budget.py was
# missed entirely.


def _repo(root: Path, relative: str) -> Path:
    directory = root / relative
    (directory / ".git").mkdir(parents=True)
    (directory / "assembler.py").write_text("def message_tokens():\n    ...\n", encoding="utf-8")
    return directory


def test_a_clone_inside_the_workspace_is_not_indexed(tmp_path: Path):
    from shamsu.indexer.policy import nested_repository_dirs

    _repo(tmp_path, "reference/smallcode")
    _repo(tmp_path, "other peoples work/SmallCTL")
    (tmp_path / "shamsu").mkdir()
    (tmp_path / "shamsu" / "budget.py").write_text("def message_tokens():\n    ...\n", encoding="utf-8")

    found = nested_repository_dirs(tmp_path)

    assert "reference/smallcode/" in found
    assert "other peoples work/SmallCTL/" in found
    assert not any(f.startswith("shamsu/") for f in found), "this project's own code stays"


def test_a_directory_called_reference_is_not_excluded_by_its_name(tmp_path: Path):
    """The obvious fix - adding `reference/` to DEFAULT_EXCLUDED_DIRS - would
    silently drop a directory of that name from every other workspace, because
    that set is matched against every path part."""
    from shamsu.indexer.policy import nested_repository_dirs

    (tmp_path / "reference").mkdir()
    (tmp_path / "reference" / "tables.py").write_text("RATES = {}\n", encoding="utf-8")

    assert nested_repository_dirs(tmp_path) == []


def test_the_workspaces_own_git_is_not_mistaken_for_a_nested_one(tmp_path: Path):
    from shamsu.indexer.policy import nested_repository_dirs

    (tmp_path / ".git").mkdir()
    (tmp_path / "src").mkdir()

    assert nested_repository_dirs(tmp_path) == []


def test_nested_repositories_reach_the_generated_cbmignore(tmp_path: Path):
    from shamsu.indexer.policy import ensure_cbm_ignore

    _repo(tmp_path, "reference/smallcode")

    ensure_cbm_ignore(tmp_path)

    assert "reference/smallcode/" in (tmp_path / ".cbmignore").read_text(encoding="utf-8")


def test_a_user_rule_outside_the_managed_block_survives_regeneration(tmp_path: Path):
    """`legacy-code/` is this repo's own archived tree - no `.git`, so the
    nested-repository rule cannot see it, and not a name worth hardcoding into
    every workspace either."""
    from shamsu.indexer.policy import ensure_cbm_ignore

    ensure_cbm_ignore(tmp_path)
    path = tmp_path / ".cbmignore"
    path.write_text(path.read_text(encoding="utf-8") + "\nlegacy-code/\n", encoding="utf-8")

    _repo(tmp_path, "reference/smallcode")
    ensure_cbm_ignore(tmp_path)

    body = path.read_text(encoding="utf-8")
    assert "legacy-code/" in body
    assert "reference/smallcode/" in body


def test_the_scan_is_bounded(tmp_path: Path):
    """A deep tree must not turn ignore-file generation into a full walk."""
    from shamsu.indexer.policy import NESTED_REPO_SCAN_DEPTH, nested_repository_dirs

    deep = tmp_path.joinpath(*[f"level{i}" for i in range(NESTED_REPO_SCAN_DEPTH + 3)])
    (deep / ".git").mkdir(parents=True)

    assert nested_repository_dirs(tmp_path) == []
