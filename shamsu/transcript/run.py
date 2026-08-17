"""Runnable entry point for the transcript build route.

Deliberately standalone. It does not touch ``cli/repl.py`` (17k lines) or the PRD
orchestrator, so this route can be run, measured and compared against the
existing one without putting a single existing code path at risk.

    python -m shamsu.transcript.run <workspace> "build a task tracker in Flask"
    python -m shamsu.transcript.run <workspace> --prd requirements.docx
    python -m shamsu.transcript.run <workspace> "..." --plan-only
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.llm.manager import LLMManager
from shamsu.runtime.models import model_for_role
from shamsu.safety.approval import ApprovalRequest
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.transcript.build import MAX_SLICES, TranscriptBuilder


def _auto_approve(request: ApprovalRequest) -> bool:
    """Approve writes without prompting.

    The route is non-interactive by design: it is a build loop, and a per-file
    prompt would make a 40-file project unrunnable. The registry's write gate,
    path sandbox and read-only enforcement all still apply — this answers only
    "may SHAMSU write without asking", which is the flag the CLI already exposes
    as `--approval allow`.
    """
    return True


def _read_request(workspace: Path, request: str, prd: str | None) -> str:
    if not prd:
        return request
    source = Path(prd)
    if not source.is_absolute():
        source = workspace / prd
    if not source.exists():
        raise SystemExit(f"PRD not found: {source}")
    suffix = source.suffix.lower()
    if suffix in {".docx", ".pdf"}:
        from shamsu.tools.workspace import extract_document_text

        text = extract_document_text(source)
    else:
        text = source.read_text(encoding="utf-8", errors="replace")
    if request:
        return f"{request}\n\n{text}"
    return text


async def _main(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    request = _read_request(workspace, args.request or "", args.prd)
    if not request.strip():
        raise SystemExit("Nothing to build: pass a request or --prd.")

    model = args.model or model_for_role("coder")
    ledger = ActionLedger(workspace)
    registry = AgentToolRegistry(
        workspace,
        approval_func=_auto_approve,
        action_ledger=ledger,
    )
    llm = LLMManager(action_ledger=ledger)

    def progress(message: str) -> None:
        print(f"  · {message}", flush=True)

    builder = TranscriptBuilder(
        workspace,
        registry,
        llm,
        model,
        max_tokens=args.ctx,
        on_progress=progress,
    )

    print(f"model     {model}")
    print(f"workspace {workspace}")
    print(f"context   {args.ctx} tokens\n")

    if args.plan_only:
        plan = await builder.plan(request)
        print(plan)
        return 0

    report = await builder.run(request, max_slices=args.max_slices)

    if args.save_transcript:
        # The whole conversation, exactly as the model saw it. A build that ends
        # early is almost always explained by the last two turns, and without
        # this the only way to find out is to reproduce the run.
        target = Path(args.save_transcript)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "\n\n".join(
                f"===== {m['role'].upper()} =====\n{m['content']}"
                for m in builder.transcript.messages()
            ),
            encoding="utf-8",
        )
        print(f"\ntranscript -> {target}")

    print("\n" + "=" * 68)
    print(f"stopped     {report.stopped_reason}")
    print(f"completed   {report.completed}")
    print(f"milestones  {len(report.slices)}")
    print(f"model calls {report.model_calls}")
    print(f"files       {len(report.files)}")
    print(f"duration    {report.duration_s:.1f}s")
    print(f"cache reuse {report.cache_reuse_ratio:.1%}  (state frame scores ~0%)")
    print("=" * 68)
    for outcome in report.slices:
        flag = "ok " if outcome.ok else "FAIL"
        print(
            f"  [{flag}] milestone {outcome.index:<2} "
            f"{len(outcome.files_written):>2} files  "
            f"{outcome.model_calls} call(s)  "
            f"{outcome.repairs} repair(s)  "
            f"{outcome.duration_s:>6.1f}s  {outcome.verify_status}"
        )
        if outcome.verify_error:
            print(f"         {outcome.verify_error.splitlines()[0][:90]}")
    if report.files:
        print("\nfiles written:")
        for path in report.files:
            print(f"  {path}")
    return 0 if report.completed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shamsu.transcript.run")
    parser.add_argument("workspace")
    parser.add_argument("request", nargs="?", default="")
    parser.add_argument("--prd", help="file whose text becomes the requirements")
    parser.add_argument("--model", help="override the model (default: coder role)")
    parser.add_argument("--ctx", type=int, default=32768)
    parser.add_argument("--max-slices", type=int, default=MAX_SLICES)
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument("--save-transcript", help="write the full conversation to this path")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
