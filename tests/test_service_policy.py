"""The default loop enforces policies even when a model chooses the wrong tool."""
from __future__ import annotations

import pytest

from shamsu.agents.simple_chat import SimpleChatLoop, build_simple_tools
from shamsu.safety.approval_context import approval_override
from shamsu.safety.dry_run import dry_run


class ScriptedModel:
    def __init__(self):
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return {"message": {"role": "assistant", "content": "", "tool_calls": [{
                "function": {"name": "write_file", "arguments": {
                    "filepath": "policy.txt", "content": "A proposed file.\n",
                }},
            }]}}
        return {"message": {"role": "assistant", "content": "The request has finished."}}


def loop_for(workspace, monkeypatch):
    monkeypatch.setattr(SimpleChatLoop, "_refresh_code_graph", lambda self: None)
    monkeypatch.setattr(SimpleChatLoop, "_warn_if_spilled_to_ram", lambda self: None)
    return SimpleChatLoop(
        workspace, client=ScriptedModel(),
        tools=build_simple_tools(workspace, console_approval=lambda request: True),
        model_name="qwen2.5-coder:3b-instruct", max_rounds=3, verify_changes=False,
    )


@pytest.mark.asyncio
async def test_default_loop_honors_explicit_denial(tmp_path, monkeypatch):
    loop = loop_for(tmp_path, monkeypatch)
    with approval_override(lambda request: False):
        await loop.run("Create policy.txt with a short note.")
    assert not (tmp_path / "policy.txt").exists()


@pytest.mark.asyncio
async def test_default_loop_previews_dry_run_without_writing(tmp_path, monkeypatch):
    loop = loop_for(tmp_path, monkeypatch)
    with dry_run() as recorder:
        await loop.run("Create policy.txt with a short note.")
    assert not (tmp_path / "policy.txt").exists()
    assert any(item.path == "policy.txt" for item in recorder.planned)


@pytest.mark.asyncio
async def test_web_review_constraint_survives_a_wrong_write_call(tmp_path, monkeypatch):
    loop = loop_for(tmp_path, monkeypatch)
    await loop.run("Review only. Do not create, edit, or delete any files.")
    assert not (tmp_path / "policy.txt").exists()


def test_search_cannot_change_its_root_to_an_external_folder(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "sentinel.py").write_text("UNIQUE_PRIVATE_SENTINEL = 1\n")
    result = loop_for(workspace, monkeypatch)._execute(
        "search_files", {"path": str(outside), "query": "UNIQUE_PRIVATE_SENTINEL", "mode": "keyword"},
    )
    assert not result.ok
    assert "UNIQUE_PRIVATE_SENTINEL = 1" not in result.message
