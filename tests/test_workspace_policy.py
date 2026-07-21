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
