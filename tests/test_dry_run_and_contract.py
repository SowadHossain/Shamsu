"""Dry run that actually previews, and a contract that checks the job got done.

Both close gaps the 2026-07-20 dogfood found and the first fix pass left open:

* `--dry-run` was deny-mode with a different name. Every approval gate said no,
  so a create-file prompt produced ZERO planned actions - the agent looked for
  a file that by definition did not exist yet and gave up. A dry run has to let
  the agent believe the write worked, or there is nothing to preview.
* `validate_run` is structural. It returned `ok: true` for the run that built
  the wrong product AND the run that destroyed a file, because every artifact
  was present and parseable. Well-formed is not correct.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.safety.dry_run import DryRunRecorder, dry_run, get_recorder
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.verify import contract as run_contract

EXISTING = "x = 1\n"


@pytest.fixture()
def tools(tmp_path: Path) -> AgentToolRegistry:
    (tmp_path / "existing.py").write_text(EXISTING, encoding="utf-8")
    return AgentToolRegistry(tmp_path, approval_func=lambda _request: True)


# --- dry run ------------------------------------------------------------------


def test_dry_run_reports_success_so_the_agent_keeps_planning(tools, tmp_path: Path):
    """The load-bearing behavior. A denial stops a well-behaved agent, which is
    why deny-mode produced no preview - the tool must look like it worked."""
    with dry_run() as recorder:
        tools.set_dry_run(recorder)
        result = tools.execute("write_file", {"filepath": "new.md", "content": "hello\n"})

    assert result.ok is True
    assert result.data["dry_run"] is True
    assert not (tmp_path / "new.md").exists()
    assert [entry.path for entry in recorder.planned] == ["new.md"]


def test_dry_run_never_touches_the_disk(tools, tmp_path: Path):
    with dry_run() as recorder:
        tools.set_dry_run(recorder)
        tools.execute("write_file", {"filepath": "existing.py", "content": "x = 999\n"})
        tools.execute("edit_file", {"filepath": "existing.py", "old_string": "x", "new_string": "y"})
        tools.execute("delete_file", {"filepath": "existing.py"})
        tools.execute("move_file", {"source": "existing.py", "destination": "moved.py"})

    assert (tmp_path / "existing.py").read_text(encoding="utf-8") == EXISTING
    assert not (tmp_path / "moved.py").exists()
    assert [entry.action for entry in recorder.planned] == ["overwrite", "edit", "delete", "move"]


def test_dry_run_distinguishes_create_from_overwrite(tools):
    with dry_run() as recorder:
        tools.set_dry_run(recorder)
        tools.execute("write_file", {"filepath": "brand_new.py", "content": "a = 1\n"})
        tools.execute("write_file", {"filepath": "existing.py", "content": "a = 1\n"})

    assert [entry.action for entry in recorder.planned] == ["create", "overwrite"]


def test_dry_run_summary_lists_the_plan():
    recorder = DryRunRecorder()
    recorder.record("create", "a.md")
    recorder.record("overwrite", "b.py")

    summary = recorder.summary()

    assert "2 file change(s) planned, none applied" in summary
    assert "would create a.md" in summary
    assert "would overwrite b.py" in summary


def test_dry_run_summary_is_honest_when_nothing_was_planned():
    assert "planned no file changes" in DryRunRecorder().summary()


def test_recorder_is_absent_outside_a_dry_run():
    assert get_recorder() is None
    with dry_run():
        assert get_recorder() is not None
    assert get_recorder() is None


def test_read_only_outranks_dry_run(tools, tmp_path: Path):
    """Both can be set. A refusal is the stronger statement, and pretending a
    forbidden write succeeded would be a lie the agent then reasons from."""
    tools.set_read_only(True)
    with dry_run() as recorder:
        tools.set_dry_run(recorder)
        result = tools.execute("write_file", {"filepath": "new.md", "content": "x"})

    assert result.ok is False
    assert recorder.planned == []


def test_dry_run_only_is_a_dry_run_not_a_read_only_ban():
    """Found live: "dry run only" was in the read-only regex, so a `--dry-run`
    create-file hit the HARD read-only deny before the recorder could preview -
    producing an empty plan. A dry run plans the change; it does not refuse it."""
    from shamsu.safety import read_only

    prompt = "Dry run only: create a file named x.txt with the text probe."
    assert read_only.is_dry_run(prompt) is True
    assert read_only.applies(prompt) is False  # NOT a blanket read-only ban
    # A genuine read-only ban is unaffected.
    assert read_only.applies("do not modify files") is True
    assert read_only.is_dry_run("do not modify files") is False


# --- scoped read-only enforcement ---------------------------------------------


def test_scoped_read_only_allows_the_named_file_and_blocks_the_rest(tools, tmp_path: Path):
    """"Create X, do not modify any other files" - X must be writable, or the
    request fails from the opposite direction to ignoring the constraint."""
    tools.set_allowed_write_paths(["notes.md"])

    allowed = tools.execute("write_file", {"filepath": "notes.md", "content": "hi\n"})
    blocked = tools.execute("write_file", {"filepath": "existing.py", "content": "x = 2\n"})

    assert allowed.ok is True
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "hi\n"
    assert blocked.ok is False
    assert "allowed changes only to" in blocked.message
    assert (tmp_path / "existing.py").read_text(encoding="utf-8") == EXISTING


def test_clearing_the_scope_restores_normal_writes(tools, tmp_path: Path):
    tools.set_allowed_write_paths(["notes.md"])
    tools.set_allowed_write_paths(None)

    assert tools.execute("write_file", {"filepath": "existing.py", "content": "x = 2\n"}).ok is True


# --- the contract -------------------------------------------------------------


def test_contract_reads_the_promises_out_of_a_prompt():
    contract = run_contract.derive(
        "Create a new file named notes.md. Do not modify any other files."
    )

    assert contract.requested_paths == ("notes.md",)
    assert contract.scoped_read_only is True
    assert contract.read_only is False


def test_contract_ignores_files_that_are_only_being_read():
    """"Inspect qa_probe.py" names a file but requests no change to it -
    demanding it be written would fail every read-only run."""
    assert run_contract.derive("Inspect qa_probe.py and tell me what it does.").requested_paths == ()


def test_contract_fails_the_run_that_built_the_wrong_product():
    """Verbatim evidence from run_2026-07-20_16-21-13_d83f, which
    `validate_run` passed as `ok: true`."""
    contract = run_contract.derive(
        "Create a new file named shamsu_smoke_note.md in this workspace. "
        "Do not modify any other files."
    )

    result = run_contract.check(
        contract,
        changed_files=[
            {"path": ".gitignore", "change": "created"},
            {"path": "client/src/LandingPage.js", "change": "created"},
        ],
    )

    assert result.ok is False
    assert any("only_requested_files_changed" in v for v in result.violations)
    assert any("requested_files_were_written" in v for v in result.violations)


def test_contract_passes_the_same_run_done_correctly():
    contract = run_contract.derive(
        "Create a new file named shamsu_smoke_note.md. Do not modify any other files."
    )

    result = run_contract.check(
        contract, changed_files=[{"path": "shamsu_smoke_note.md", "change": "created"}]
    )

    assert result.ok is True
    assert all(item["passed"] for item in result.checks)


def test_contract_fails_the_run_that_destroyed_a_file():
    """run_2026-07-20_16-25-40_7630: reported success, validated ok, ate a file."""
    contract = run_contract.derive("Run qa_probe.py and tell me the output. Do not change files.")

    result = run_contract.check(
        contract, changed_files=[{"path": "qa_probe.py", "change": "modified"}]
    )

    assert result.ok is False
    assert any("read_only_respected" in v for v in result.violations)


def test_contract_fails_a_dry_run_that_planned_nothing():
    """The original dry-run failure: safe, but useless - no preview produced."""
    contract = run_contract.derive(
        "Dry run only: create dry_run_should_not_exist.txt with the text probe.", dry_run=True
    )

    result = run_contract.check(contract, changed_files=[], planned_mutations=[])

    assert result.ok is False
    assert any("dry_run_produced_a_plan" in v for v in result.violations)


def test_contract_passes_a_dry_run_that_previewed_the_change():
    contract = run_contract.derive(
        "Dry run only: create dry_run_should_not_exist.txt with the text probe.", dry_run=True
    )

    result = run_contract.check(
        contract,
        changed_files=[],
        planned_mutations=[{"action": "create", "path": "dry_run_should_not_exist.txt"}],
    )

    assert result.ok is True


def test_contract_fails_a_dry_run_that_actually_wrote():
    contract = run_contract.derive("Dry run only: create notes.md.", dry_run=True)

    result = run_contract.check(
        contract,
        changed_files=[{"path": "notes.md", "change": "created"}],
        planned_mutations=[{"action": "create", "path": "notes.md"}],
    )

    assert result.ok is False
    assert any("dry_run_changed_nothing" in v for v in result.violations)


def test_a_prompt_with_no_promises_is_not_judged():
    """Most prompts state nothing checkable. Inventing expectations for them
    would produce exactly the false failures that make a gate get ignored."""
    result = run_contract.check(run_contract.derive("what is recursion?"), changed_files=[])

    assert result.ok is True
    assert result.checked is False


def test_dotfiles_survive_normalization():
    """`lstrip("./")` strips dot CHARACTERS, so `.gitignore` became `gitignore`
    and no longer matched itself - and `.gitignore` is exactly what a runaway
    build creates."""
    assert run_contract.requested_paths("update the .gitignore file") == (".gitignore",)

    result = run_contract.check(
        run_contract.derive("create notes.md. Do not modify any other files."),
        changed_files=[{"path": ".gitignore", "change": "created"}],
    )

    assert any(".gitignore" in v for v in result.violations)


def test_a_sentence_boundary_is_not_a_filename():
    """The reason the dotfile allowlist is named rather than a loose pattern:
    "workspace.Put one sentence" must not yield a requested file called
    `.Put`, which would fail the contract over a missing space."""
    assert run_contract.requested_paths(
        "Create notes.md in this workspace.Put one short sentence in it."
    ) == ("notes.md",)


def test_a_source_file_is_not_a_write_target():
    """"Build the converter described in PRD.md. Create converter.py" used to
    flag PRD.md (the source it read) as an unwritten output, failing a genuine
    build. A file named only as a source is an input, not a target. Found live
    2026-07-21 dogfooding the PRD build flow."""
    assert run_contract.requested_paths(
        "Build the converter described in PRD.md. Create converter.py."
    ) == ("converter.py",)
    # A spec/PRD/readme filename is never a target on its own.
    assert run_contract.requested_paths("implement the app from spec.md") == ()
    assert run_contract.requested_paths("based on README.md write app.py") == ("app.py",)


def test_prd_build_contract_passes_when_the_target_is_written():
    result = run_contract.check(
        run_contract.derive("Build the converter described in PRD.md. Create converter.py."),
        changed_files=[{"path": "converter.py", "change": "created"}],
    )

    assert result.ok is True
