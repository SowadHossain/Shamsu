from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shamsu.cli.noninteractive import _ApprovalScript, _workspace_snapshot, run_cli, run_prompt
from shamsu.cli.repl import parse_args
from shamsu.safety.approval_context import get_approval_override
from shamsu.types import ApprovalRequest


def test_parse_run_command_contract():
    args = parse_args(
        [
            "run",
            "--workspace",
            "sample-project",
            "--prompt",
            "where are you working?",
            "--output",
            "json",
            "--approval",
            "allow",
            "--timeout",
            "12",
            "--dry-run",
        ]
    )

    assert args.command == "run"
    assert args.prompt == "where are you working?"
    assert args.output == "json"
    assert args.approval == "allow"
    assert args.timeout == 12
    assert args.dry_run is True


def test_workspace_snapshot_includes_root_code_memory_policy_file(tmp_path: Path):
    (tmp_path / ".cbmignore").write_text("node_modules/\n", encoding="utf-8")

    snapshot = _workspace_snapshot(tmp_path)

    assert ".cbmignore" in snapshot


def test_scripted_approvals_are_deterministic_and_default_to_deny():
    script = _ApprovalScript([True, False])
    request = ApprovalRequest(
        action_type="file_write",
        description="write a file",
        risk_level="medium",
    )

    assert script(request) is True
    assert script(request) is False
    assert script(request) is False
    assert [record["source"] for record in script.records] == [
        "script",
        "script",
        "policy:deny",
    ]
    assert script.records[0]["request"]["risk_level"] == "medium"


def test_dry_run_approval_policy_always_denies_and_preserves_full_preview():
    script = _ApprovalScript("dry-run")
    request = ApprovalRequest(
        action_type="file_edit",
        description="edit configuration",
        risk_level="medium",
        preview="- old\n+ new",
        working_dir="C:/workspace",
        reason="Fix the configured value.",
        target_paths=["settings.py"],
    )

    assert script(request) is False
    assert script.records == [
        {
            "request": {
                "action_type": "file_edit",
                "description": "edit configuration",
                "risk_level": "medium",
                "preview": "- old\n+ new",
                "working_dir": "C:/workspace",
                "reason": "Fix the configured value.",
                "target_paths": ["settings.py"],
            },
            "action_type": "file_edit",
            "description": "edit configuration",
            "approved": False,
            "decision_scope": "none",
            "source": "policy:dry-run",
        }
    ]


@pytest.mark.asyncio
async def test_headless_runner_uses_real_dispatch_and_writes_complete_artifacts(tmp_path: Path):
    result = await run_prompt(tmp_path, "what folder are you in?")

    assert result.status == "success"
    assert result.route == "workspace.location"
    assert str(tmp_path.resolve()) in result.final_response
    assert result.run_id
    assert result.session_id
    assert result.turn_id
    assert "workspace.location" in result.operations
    assert result.changed_files == []
    assert result.artifact_integrity["manifest"] is True
    assert result.artifact_integrity["events"] is True
    assert result.artifact_integrity["decisions"] is True
    assert result.artifact_integrity["tool_calls"] is True
    assert result.artifact_integrity["model_calls"] is True
    assert result.artifact_integrity["mutations"] is True
    assert result.artifact_integrity["context_preview"] is True
    assert result.artifact_integrity["contexts"] is True
    assert result.artifact_integrity["final_output"] is True
    assert result.artifact_integrity["summary"] is True
    assert result.run_validation["ok"] is True
    assert result.run_validation["counts"]["decisions"] == 1

    manifest = json.loads(Path(result.artifacts["manifest"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "success"
    assert manifest["prompt_preview"] == "what folder are you in?"


@pytest.mark.asyncio
async def test_headless_runner_reports_a_request_timeout(tmp_path: Path, monkeypatch):
    import shamsu.cli.repl as repl

    async def never_finishes(*args, **kwargs):
        await __import__("asyncio").sleep(1)

    monkeypatch.setattr(repl, "_handle_request", never_finishes)
    result = await run_prompt(tmp_path, "wait forever", timeout_s=0.01)

    assert result.status == "timed_out"
    assert result.timeout_phase == "request"
    assert "timed out" in result.error.lower()
    assert result.artifact_integrity["summary"] is True
    assert result.artifact_integrity["final_output"] is True


@pytest.mark.asyncio
async def test_headless_runner_flushes_memory_queue_on_exit(tmp_path: Path, monkeypatch):
    import shamsu.cli.noninteractive as noninteractive

    calls: list[bool] = []
    monkeypatch.setattr(noninteractive, "flush_memory_queues", lambda: calls.append(True) or True)

    await run_prompt(tmp_path, "what folder are you in?")

    assert calls == [True]


@pytest.mark.asyncio
async def test_headless_dry_run_records_mutation_preview_without_changing_files(
    tmp_path: Path, monkeypatch
):
    import shamsu.cli.repl as repl

    target = tmp_path / "planned.txt"

    async def request_mutation(*args, **kwargs):
        approval = get_approval_override()
        assert approval is not None
        approved = approval(
            ApprovalRequest(
                action_type="file_write",
                description="Create planned.txt",
                risk_level="medium",
                preview="planned content",
                working_dir=str(tmp_path),
                reason="Exercise the dry-run boundary.",
                target_paths=["planned.txt"],
            )
        )
        if approved:
            target.write_text("planned content", encoding="utf-8")

    monkeypatch.setattr(repl, "_handle_request", request_mutation)
    result = await run_prompt(tmp_path, "create planned.txt", approval="allow", dry_run=True)

    assert result.status == "dry_run"
    assert result.dry_run is True
    assert result.changed_files == []
    assert target.exists() is False
    # This test stubs `_handle_request`, so it exercises the APPROVAL boundary:
    # a side-effecting action that reaches a gate under dry run is still
    # refused. It never touches the tool registry, so no mutation is planned -
    # hence the "planned no file changes" summary. The recorder path (writes
    # reporting synthetic success so the agent keeps planning) is covered in
    # tests/test_dry_run_and_contract.py.
    assert result.final_response == "Dry run complete: the agent planned no file changes."
    assert result.planned_mutations == []
    assert result.planned_actions[0]["target_paths"] == ["planned.txt"]
    assert result.approvals[0]["source"] == "policy:dry-run"


def test_cli_setup_failure_is_machine_readable_json(tmp_path: Path, capsys):
    args = SimpleNamespace(
        workspace=str(tmp_path / "missing"),
        prompt="where am i",
        approval="deny",
        timeout=1,
        session=None,
        output="json",
    )

    assert run_cli(args) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["timeout_phase"] == "setup"
    assert "Workspace does not exist" in payload["error"]
