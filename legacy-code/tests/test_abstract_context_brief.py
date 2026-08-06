from __future__ import annotations

from pathlib import Path

from shamsu.abstract.context import build_codebase_memory_brief
from shamsu.abstract.service import AbstractService
from tests.test_abstract_service import FakeCodebaseMemoryAdapter


class _QueryableFakeAdapter(FakeCodebaseMemoryAdapter):
    def get_exports(self, workspace: Path, path: str) -> dict:
        return {"ok": True, "results": [{"name": "gameLoop"}, {"name": "Session"}]}

    def get_imports(self, workspace: Path, path: str) -> dict:
        return {"ok": True, "results": [{"name": "os"}]}


def test_brief_is_empty_when_codebase_memory_unavailable(tmp_path):
    service = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=False))

    brief = build_codebase_memory_brief(tmp_path, ["shamsu/foo.py"], service=service)

    assert brief == ""


def test_brief_is_empty_when_no_target_paths(tmp_path):
    service = AbstractService(tmp_path, adapter=_QueryableFakeAdapter(available=True))

    brief = build_codebase_memory_brief(tmp_path, [], service=service)

    assert brief == ""


def test_brief_prefers_compact_facts_over_full_files(tmp_path):
    service = AbstractService(tmp_path, adapter=_QueryableFakeAdapter(available=True))

    brief = build_codebase_memory_brief(tmp_path, ["shamsu/loop.py"], service=service)

    assert "gameLoop" in brief
    assert "Session" in brief
    assert "os" in brief
    # Compact facts, not a full file dump: no source-looking multi-line body.
    assert len(brief.splitlines()) <= 4
