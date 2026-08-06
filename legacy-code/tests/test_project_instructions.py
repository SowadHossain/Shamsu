"""Standing per-workspace rules, and staying grounded after eviction.

Two gaps this covers, both absent before:

1. SHAMSU had no instruction file at all. _system_prompt was a static string plus
   the workspace path, so a rule stated once ("PostgreSQL 16, never SQLite") lived
   only in the first user message - the first thing the budget trimmer evicts.
2. After eviction the model's only record of its own writes was the rolling
   summary, a paraphrase, so it re-derived file state from memory instead of disk.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.agents.project_instructions import (
    MAX_INSTRUCTION_CHARS,
    find_instruction_file,
    load_project_instructions,
)
from shamsu.tools.agent_tools import AgentToolRegistry


class SilentClient:
    async def chat(self, model, messages, stream, options, **kwargs):
        return {"message": {"content": "Nothing to do.", "tool_calls": []}}


def _loop(tmp_path: Path) -> AgentChatLoop:
    return AgentChatLoop(
        tmp_path,
        client=SilentClient(),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _request: True),
        model_name="qwen2.5-coder:7b-instruct",
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_no_instruction_file_yields_nothing(tmp_path: Path):
    assert find_instruction_file(tmp_path) is None
    assert load_project_instructions(tmp_path) == ""


def test_shamsu_md_is_loaded_and_marked_as_outranking_defaults(tmp_path: Path):
    (tmp_path / "SHAMSU.md").write_text(
        "- PostgreSQL 16 only. Never SQLite.\n", encoding="utf-8"
    )

    block = load_project_instructions(tmp_path)

    assert "PostgreSQL 16 only. Never SQLite." in block
    assert "outrank your general defaults" in block
    assert "SHAMSU.md" in block


def test_shamsu_md_wins_over_agents_md(tmp_path: Path):
    (tmp_path / "SHAMSU.md").write_text("native rules\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("shared rules\n", encoding="utf-8")

    block = load_project_instructions(tmp_path)

    assert "native rules" in block
    assert "shared rules" not in block


def test_agents_md_is_honoured_so_an_existing_repo_needs_no_duplicate(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("shared rules\n", encoding="utf-8")

    assert "shared rules" in load_project_instructions(tmp_path)


def test_a_dot_shamsu_instructions_file_is_the_last_fallback(tmp_path: Path):
    (tmp_path / ".shamsu").mkdir()
    (tmp_path / ".shamsu" / "instructions.md").write_text("hidden rules\n", encoding="utf-8")

    assert "hidden rules" in load_project_instructions(tmp_path)


def test_an_empty_instruction_file_contributes_nothing(tmp_path: Path):
    (tmp_path / "SHAMSU.md").write_text("   \n\n", encoding="utf-8")

    assert load_project_instructions(tmp_path) == ""


def test_an_oversized_file_is_truncated_visibly_not_silently(tmp_path: Path):
    """A 7B must keep room to read and rewrite an actual source file."""
    (tmp_path / "SHAMSU.md").write_text("x" * (MAX_INSTRUCTION_CHARS + 5000), encoding="utf-8")

    block = load_project_instructions(tmp_path)

    assert "truncated by SHAMSU" in block
    assert len(block) < MAX_INSTRUCTION_CHARS + 1000


# ---------------------------------------------------------------------------
# Injection: every turn, not once
# ---------------------------------------------------------------------------


def test_instructions_reach_the_system_prompt_at_construction(tmp_path: Path):
    (tmp_path / "SHAMSU.md").write_text("- Never use SQLite.\n", encoding="utf-8")

    loop = _loop(tmp_path)

    assert "Never use SQLite." in loop.state.system_prompt


@pytest.mark.asyncio
async def test_instructions_added_mid_session_take_effect_on_the_next_turn(tmp_path: Path):
    """Re-read per turn, so the file is authoritative rather than a snapshot."""
    loop = _loop(tmp_path)
    assert "Never use SQLite." not in loop.state.system_prompt

    (tmp_path / "SHAMSU.md").write_text("- Never use SQLite.\n", encoding="utf-8")
    await loop.run("say nothing")

    assert "Never use SQLite." in loop.state.system_prompt


@pytest.mark.asyncio
async def test_edited_instructions_replace_the_old_ones_rather_than_stacking(tmp_path: Path):
    rules = tmp_path / "SHAMSU.md"
    rules.write_text("- Use MySQL.\n", encoding="utf-8")
    loop = _loop(tmp_path)

    rules.write_text("- Use PostgreSQL.\n", encoding="utf-8")
    await loop.run("say nothing")

    assert "Use PostgreSQL." in loop.state.system_prompt
    assert "Use MySQL." not in loop.state.system_prompt


# ---------------------------------------------------------------------------
# Post-eviction re-grounding
# ---------------------------------------------------------------------------


def test_regrounding_reads_current_bytes_from_disk(tmp_path: Path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "models.py").write_text("class User: pass\n", encoding="utf-8")
    loop = _loop(tmp_path)

    block = loop._regrounding_block(["core/models.py"])

    assert "core/models.py" in block
    assert "class User: pass" in block
    assert "trust THIS over anything you remember" in block


def test_regrounding_keeps_only_the_most_recent_files(tmp_path: Path):
    for index in range(6):
        (tmp_path / f"f{index}.py").write_text(f"# file {index}\n", encoding="utf-8")
    loop = _loop(tmp_path)

    block = loop._regrounding_block([f"f{index}.py" for index in range(6)])

    # The three most recent, not all six - the window is not free.
    assert "# file 5" in block
    assert "# file 3" in block
    assert "# file 0" not in block


def test_regrounding_truncates_a_large_file(tmp_path: Path):
    (tmp_path / "big.py").write_text("y" * 9000, encoding="utf-8")
    loop = _loop(tmp_path)

    block = loop._regrounding_block(["big.py"])

    assert "truncated at" in block
    assert len(block) < 9000


def test_regrounding_skips_missing_and_escaping_paths(tmp_path: Path):
    loop = _loop(tmp_path)

    assert loop._regrounding_block(["does_not_exist.py"]) == ""
    assert loop._regrounding_block(["../../../etc/passwd"]) == ""


def test_regrounding_is_empty_when_nothing_was_written(tmp_path: Path):
    assert _loop(tmp_path)._regrounding_block([]) == ""
