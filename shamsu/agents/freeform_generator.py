"""Template-free generation: build any project directly from the PRD.

When no template fits (a CMS, a bespoke service, a CLI, anything), this derives
a small file plan from the PRD contract, generates each file with the model,
then runs the SAME verify -> strict-repair -> honest-report tail as the template
paths. Templates are accelerators; this is the general case.

Trust boundaries:
  - The verifier is chosen DETERMINISTICALLY from the detected stack + the
    generated files (never the model's own command), so success can't be faked.
  - No verifier for the stack => the result is reported as generated-but-
    UNVERIFIED and is never called a success.
  - Every write is transaction-backed and path-sandboxed; the strict RepairLoop
    handles failures with rollback.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from json_repair import repair_json

from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.patch.transactions import TransactionWorkspace
from shamsu.prd.contract import PRDContract
from shamsu.repair.loop import RepairLoop
from shamsu.repair.prompt import enforce_final_response
from shamsu.repair.proposer_llm import LLMProposer
from shamsu.repair.types import RepairResult
from shamsu.repair.verifiers import CommandVerifier
from shamsu.safety.sandbox import Sandbox
from shamsu.session.manager import SessionLogger
from shamsu.tools.executor import CommandRunner
from shamsu.types import ProjectSpec

_MAX_FILES = 30
_BUILD_TIMEOUT_SECONDS = 600

GENERATION_PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "stack": {"type": "string"},
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "purpose": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    "required": ["files"],
}

FILE_CONTENT_SCHEMA: dict = {
    "type": "object",
    "properties": {"content": {"type": "string"}},
    "required": ["content"],
}

PLAN_SYSTEM = """You are SHAMSU planning a small project from a PRD, from scratch (no template).
Output ONLY JSON: {"stack": string, "files": [{"path": string, "purpose": string}]}.
Rules:
- Choose the simplest stack that satisfies the PRD (prefer the PRD's stated stack).
- List the MINIMUM files needed to build and run it. Use relative paths only.
- Include the build/dependency manifest (package.json / requirements.txt) when relevant.
- No prose outside the JSON.
"""

FILE_SYSTEM = """You are SHAMSU writing ONE file of a project from a PRD.
Output ONLY JSON: {"content": "<the full file contents>"}.
Rules:
- Implement exactly what the PRD needs for THIS file. Keep it self-consistent
  with the other planned files (imports/exports must line up).
- Keep it compiling/runnable. No prose outside the JSON.
"""


class GenerateJSON(Protocol):
    def __call__(self, system: str, user: str, schema: dict) -> str: ...


class CommandRunnerLike(Protocol):
    def run(self, command: str, cwd: Path) -> tuple[int, str, str]: ...


@dataclass(frozen=True)
class PlannedFile:
    path: str
    purpose: str = ""


@dataclass
class GenerationPlan:
    stack: str = ""
    files: list[PlannedFile] = field(default_factory=list)


@dataclass
class FreeformRunResult:
    target_dir: Path
    stack: str = ""
    written_files: list[str] = field(default_factory=list)
    verify_command: str = ""
    repair_result: RepairResult | None = None
    exit_code: int = 0
    verified: bool = False
    success: bool = False
    final_message: str = ""
    error: str = ""


class FreeformGenerator:
    def __init__(
        self,
        workspace_root: Path,
        generate: GenerateJSON,
        *,
        command_runner: CommandRunnerLike | None = None,
        session_logger: SessionLogger | None = None,
        max_repair_attempts: int = 3,
        max_files: int = _MAX_FILES,
        build_timeout: int = _BUILD_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.generate = generate
        self.command_runner = command_runner
        self.session_logger = session_logger
        self.max_repair_attempts = max_repair_attempts
        self.max_files = max_files
        self.build_timeout = build_timeout
        self.sandbox = Sandbox(self.workspace_root)
        self.transactions = TransactionWorkspace(self.workspace_root)

    def run(self, project: ProjectSpec, target_dir: Path | str) -> FreeformRunResult:
        contract: PRDContract | None = getattr(project, "prd_contract", None)
        target = self.sandbox.validate(target_dir)
        target.mkdir(parents=True, exist_ok=True)

        plan = self._plan(contract)
        if plan is None or not plan.files:
            return FreeformRunResult(
                target_dir=target,
                success=False,
                final_message="Could not derive a file plan from the PRD.",
                error="no generation plan",
            )

        written = self._generate_files(contract, plan, target)
        if not written:
            return FreeformRunResult(
                target_dir=target,
                stack=plan.stack,
                success=False,
                final_message="No files were generated from the PRD.",
                error="no files generated",
            )

        verify_command = _default_verify(plan.stack, contract, written)
        if not verify_command:
            # Honest outcome: we cannot build-verify this stack, so we never
            # claim success. The files were generated and are on disk.
            msg = (
                f"Generated {len(written)} file(s) from the PRD, but no build verifier is "
                f"available for stack '{plan.stack or 'unknown'}', so this is UNVERIFIED."
            )
            return FreeformRunResult(
                target_dir=target,
                stack=plan.stack,
                written_files=written,
                verified=False,
                success=False,
                final_message=msg,
                error="no verifier for stack",
            )

        runner = self.command_runner or CommandRunner(
            self.workspace_root,
            approval_func=lambda _request: True,
            timeout_seconds=self.build_timeout,
            session_logger=self.session_logger,
        )
        verifier = CommandVerifier(verify_command, runner, target)
        repair_result = RepairLoop(
            target,
            verifier,
            LLMProposer(self.generate),
            max_attempts=self.max_repair_attempts,
            session_logger=self.session_logger,
            digest=DiagnosticDigest(target),
        ).run()

        success = repair_result.exit_code == 0 and repair_result.success
        final_message = enforce_final_response(repair_result.final_message, repair_result.exit_code)
        return FreeformRunResult(
            target_dir=target,
            stack=plan.stack,
            written_files=written,
            verify_command=verify_command,
            repair_result=repair_result,
            exit_code=repair_result.exit_code,
            verified=True,
            success=success,
            final_message=final_message,
            error="" if success else f"Build/verify failed (exit code {repair_result.exit_code}).",
        )

    # -- helpers ---------------------------------------------------------------

    def _plan(self, contract: PRDContract | None) -> GenerationPlan | None:
        brief = contract.render_brief() if contract is not None else "(no PRD contract)"
        prompt = (
            f"{brief}\n\n## Task\nProduce the minimal file plan (JSON) to build and run this "
            "project from scratch."
        )
        try:
            raw = self.generate(PLAN_SYSTEM, prompt, GENERATION_PLAN_SCHEMA)
        except Exception:
            return None
        data = _loads(raw or "")
        if not isinstance(data, dict):
            return None
        files: list[PlannedFile] = []
        for item in data.get("files", []) or []:
            if not isinstance(item, dict):
                continue
            path = _safe_rel_path(str(item.get("path") or ""))
            if path:
                files.append(PlannedFile(path=path, purpose=str(item.get("purpose") or "")))
        return GenerationPlan(stack=str(data.get("stack") or "").strip(), files=_dedupe_files(files))

    def _generate_files(
        self, contract: PRDContract | None, plan: GenerationPlan, target: Path
    ) -> list[str]:
        brief = contract.render_brief() if contract is not None else "(no PRD contract)"
        plan_text = "\n".join(f"- {f.path}: {f.purpose}" for f in plan.files)
        written: list[str] = []
        for planned in plan.files[: self.max_files]:
            content = self._generate_one(brief, plan, plan_text, planned)
            if content is None:
                continue
            file_path = (target / planned.path).resolve()
            try:
                file_path.relative_to(target)
            except ValueError:
                continue  # escaped the target dir; skip
            rel = file_path.relative_to(self.workspace_root).as_posix()
            transaction_id = self.transactions.begin(
                reason=f"Freeform: generate {planned.path}",
                operations=[{"op": "edit_file", "path": rel, "dest_path": "", "reason": planned.purpose}],
                destructive=False,
            )
            self.transactions.backup_file(transaction_id, rel)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
            self.transactions.record_after(transaction_id, rel)
            self.transactions.finalize(transaction_id, "applied")
            written.append(planned.path)
            self._log("freeform.file_written", {"path": planned.path})
        return written

    def _generate_one(
        self, brief: str, plan: GenerationPlan, plan_text: str, planned: PlannedFile
    ) -> str | None:
        prompt = (
            f"{brief}\n\n"
            f"## Stack\n{plan.stack or 'unspecified'}\n\n"
            f"## Full file plan\n{plan_text}\n\n"
            f"## File to write now\n{planned.path} - {planned.purpose}\n\n"
            '## Task\nReturn JSON {"content": "..."} with the full contents of this file only.'
        )
        try:
            raw = self.generate(FILE_SYSTEM, prompt, FILE_CONTENT_SCHEMA)
        except Exception:
            return None
        data = _loads(raw or "")
        if isinstance(data, dict):
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                return content
        return None

    def _log(self, event_type: str, payload: dict) -> None:
        if self.session_logger:
            self.session_logger.log(
                event_type, payload, f"Freeform: {event_type}", workflow_id="freeform"
            )


# --- deterministic verifier selection ----------------------------------------

def _default_verify(stack: str, contract: PRDContract | None, written: list[str]) -> str:
    """Pick a trustworthy build/syntax verifier from the stack + generated files.
    Never uses a model-proposed command. Returns "" when nothing can verify."""
    stack_l = (stack or "").lower()
    hint = (contract.stack_hint if contract is not None else "") or ""
    py_files = [f for f in written if f.endswith(".py")]
    has_package_json = any(f.endswith("package.json") for f in written)
    has_requirements = any(f.endswith("requirements.txt") for f in written)

    if has_package_json or "node" in stack_l or "node" in hint or "vite" in stack_l:
        return "npm install && npm run build"
    if py_files or "python" in stack_l or "django" in stack_l or hint in {"python", "django"}:
        if has_requirements:
            # Install deps then byte-compile everything (a real import-time gate
            # is unsafe to run blindly; py_compile is a deterministic syntax gate).
            return "pip install -r requirements.txt && python3 -m py_compile " + " ".join(py_files)
        if py_files:
            return "python3 -m py_compile " + " ".join(py_files)
    return ""


def _safe_rel_path(path: str) -> str:
    cleaned = path.strip().lstrip("/").replace("\\", "/")
    if not cleaned or ".." in cleaned.split("/"):
        return ""
    return cleaned


def _dedupe_files(files: list[PlannedFile]) -> list[PlannedFile]:
    seen: set[str] = set()
    result: list[PlannedFile] = []
    for f in files:
        if f.path in seen:
            continue
        seen.add(f.path)
        result.append(f)
    return result


def _loads(text: str) -> object | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(repair_json(text))
    except Exception:
        return None
