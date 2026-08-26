"""Headless entry point for one small-harness SHAMSU turn."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence


from shamsu.agents.simple_chat import SimpleChatLoop, build_simple_tools
from shamsu.cli.prompt_label import SURFACE_CLI
from shamsu.llm.ollama_client import default_ollama_client
from shamsu.runtime.models import initialize_model_tier
from shamsu.runtime.timeouts import TimeoutConfig
from shamsu.safety.approval_context import approval_override
from shamsu.session.manager import SessionManager
from shamsu.types import ApprovalRequest

ApprovalMode = Literal["allow", "deny"]


@dataclass(frozen=True)
class HeadlessRunResult:
    schema_version: int
    run_id: str
    session_id: str
    turn_id: str
    status: str
    route: str
    final_response: str
    error: str
    duration_s: float
    timeout_phase: str
    dry_run: bool = False
    approvals: list[dict[str, Any]] = field(default_factory=list)
    planned_actions: list[dict[str, Any]] = field(default_factory=list)
    planned_mutations: list[dict[str, Any]] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    changed_files: list[dict[str, str]] = field(default_factory=list)
    transaction_ids: list[str] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    artifact_integrity: dict[str, bool] = field(default_factory=dict)
    run_validation: dict[str, Any] = field(default_factory=dict)
    contract: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class _ApprovalScript:
    def __init__(self, mode: ApprovalMode | Sequence[bool]) -> None:
        self.records: list[dict[str, Any]] = []
        self._mode = mode
        self._answers = list(mode) if not isinstance(mode, str) else []

    def __call__(self, request: ApprovalRequest) -> bool:
        if self._answers:
            approved = bool(self._answers.pop(0))
            source = "script"
        else:
            approved = self._mode == "allow"
            source = str(self._mode)
        self.records.append(
            {
                "action_type": request.action_type,
                "description": request.description,
                "approved": approved,
                "source": source,
            }
        )
        return approved


async def run_prompt(
    workspace: Path,
    prompt: str,
    *,
    approval: ApprovalMode | Sequence[bool] = "deny",
    timeout_s: float = 300.0,
    session: str | None = None,
    dry_run: bool = False,
) -> HeadlessRunResult:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace does not exist or is not a directory: {root}")
    clean_prompt = prompt.lstrip("\ufeff").strip()
    if not clean_prompt:
        raise ValueError("Prompt must not be empty")

    initialize_model_tier(root)
    manager = SessionManager(root)
    logger = manager.resume_session(session) if session else manager.continue_or_create("Headless Run")
    user_event = logger.append_message("user", clean_prompt, source=SURFACE_CLI)
    manager.maybe_auto_title(logger, clean_prompt)

    started = time.perf_counter()
    approvals = _ApprovalScript(approval)
    error = ""
    timeout_phase = ""
    status = "success"
    final_response = ""

    async def _turn() -> str:
        tools = build_simple_tools(
            root,
            main_loop=asyncio.get_running_loop(),
            console_approval=approvals,
            session_logger=logger,
        )
        loop = SimpleChatLoop(
            root,
            client=default_ollama_client(timeout_config=TimeoutConfig.from_env()),
            tools=tools,
            session_logger=logger,
            source=SURFACE_CLI,
        )
        result = await loop.run(clean_prompt)
        return result.final.strip()

    if dry_run:
        status = "dry_run"
        final_response = "Dry run is not implemented in the small harness."
    else:
        with approval_override(approvals):
            try:
                final_response = await asyncio.wait_for(
                    _turn(),
                    timeout=max(float(timeout_s), 0.01),
                )
            except TimeoutError:
                status = "timed_out"
                timeout_phase = "request"
                error = f"Request timed out after {timeout_s:g}s"
            except Exception as exc:  # noqa: BLE001
                status = "failed"
                error = f"{type(exc).__name__}: {exc}"

    if final_response:
        logger.append_message("assistant", final_response, source=SURFACE_CLI)

    return HeadlessRunResult(
        schema_version=1,
        run_id="",
        session_id=logger.session_id,
        turn_id=str(user_event.get("timestamp", "")),
        status=status,
        route="simple-chat",
        final_response=final_response,
        error=error,
        duration_s=time.perf_counter() - started,
        timeout_phase=timeout_phase,
        dry_run=dry_run,
        approvals=approvals.records,
    )


def run_cli(args: Any) -> int:
    try:
        result = asyncio.run(
            run_prompt(
                Path(args.workspace) if args.workspace else Path.cwd(),
                args.prompt,
                approval=args.approval,
                timeout_s=args.timeout,
                session=args.session,
                dry_run=bool(getattr(args, "dry_run", False)),
            )
        )
    except Exception as exc:  # noqa: BLE001
        result = HeadlessRunResult(
            schema_version=1,
            run_id="",
            session_id="",
            turn_id="",
            status="failed",
            route="simple-chat",
            final_response="",
            error=f"{type(exc).__name__}: {exc}",
            duration_s=0.0,
            timeout_phase="startup",
        )
    if getattr(args, "output", "text") == "json":
        print(json.dumps(result.to_dict(), ensure_ascii=True))
    else:
        if result.final_response:
            print(result.final_response)
        if result.error:
            print(result.error)
    return 0 if result.status in {"success", "dry_run"} else 1
