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
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from json_repair import repair_json

from shamsu.action_ledger.context import get_current_run
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
from shamsu.verify.gate import default_verify_command

_MAX_FILES = 30
_BUILD_TIMEOUT_SECONDS = 600
_GENERATION_TIMEOUT_SECONDS = 900
_CONVENTIONAL_EXTENSIONLESS = {
    "dockerfile",
    "makefile",
    "license",
    "procfile",
}
_SKIP_EXTENSIONLESS_DIRS = {
    "assets",
    "migrations",
}
_DIRECTORY_INDEX_EXTENSIONS = {
    "cli": ".ts",
    "commands": ".ts",
    "components": ".tsx",
    "data": ".ts",
    "database": ".ts",
    "db": ".ts",
    "hooks": ".ts",
    "lib": ".ts",
    "middleware": ".ts",
    "models": ".ts",
    "pages": ".tsx",
    "routes": ".ts",
    "schemas": ".ts",
    "services": ".ts",
    "store": ".ts",
    "types": ".ts",
    "utils": ".ts",
    "views": ".tsx",
}
_EXTENSIONLESS_FILE_REWRITES = {
    "app": ".tsx",
    "client": ".ts",
    "main": ".ts",
    "server": ".ts",
    "styles": ".css",
    "style": ".css",
    "theme": ".css",
    "tsconfig": ".json",
}
_REGENERATE_SOURCE_EXTENSIONS = frozenset(
    {".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css"}
)
_PYTHON_CLI_COMMANDS = ("seed", "add", "list", "summary", "export")
_PYTHON_CLI_RESERVED_OPTIONS = {"db", "out", "help"}

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
- Planned paths must be files, not directories. Use extensions such as .js, .ts,
  .tsx, .json, .html, .css, .md unless the file is conventionally extensionless.
- For Node/Vite/React projects, include a root index.html, package.json, and every
  config/source file required by the package.json build script.
- No prose outside the JSON.
"""

FILE_SYSTEM = """You are SHAMSU writing ONE file of a project from a PRD.
Output ONLY JSON: {"content": "<the full file contents>"}.
Rules:
- Implement exactly what the PRD needs for THIS file. Keep it self-consistent
  with the other planned files (imports/exports must line up).
- Keep it compiling/runnable. No prose outside the JSON.
- The content value must be raw file content only: no Markdown heading with the
  filename, no fenced code block, no explanation.
- Finish the entire file. Never stop in the middle of a string, expression,
  function, object, or JSON structure.
- When writing source-code string literals that contain newlines, use escaped
  backslash-n sequences in the source code, not literal line breaks inside the
  quoted string.
- For CLIs, implement the exact acceptance command syntax. If an option appears
  after a subcommand, accept it there; do not put it only on the root parser.
"""

REPAIR_FILE_SYSTEM = """You are SHAMSU repairing ONE generated source file after verification failed.
Output ONLY JSON: {"content": "<the complete corrected file contents>"}.
Rules:
- Use the PRD, current file content, and verifier failure to rewrite this file.
- Preserve working behavior unless it conflicts with the PRD or verifier.
- Return the complete file from first line to last line, not a patch.
- Keep it compiling/runnable. No prose outside the JSON.
- Finish the entire file. Never stop in the middle of a string, expression,
  function, object, or JSON structure.
- For CLIs, implement the exact failing command syntax. If an option appears
  after a subcommand, accept it there; do not put it only on the root parser.
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
        generation_timeout: int = _GENERATION_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.generate = generate
        self.command_runner = command_runner
        self.session_logger = session_logger
        self.max_repair_attempts = max_repair_attempts
        self.max_files = max_files
        self.build_timeout = build_timeout
        self.generation_timeout = generation_timeout
        self.sandbox = Sandbox(self.workspace_root)
        self.transactions = TransactionWorkspace(self.workspace_root)

    def run(self, project: ProjectSpec, target_dir: Path | str) -> FreeformRunResult:
        contract: PRDContract | None = getattr(project, "prd_contract", None)
        target = self.sandbox.validate(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.generation_timeout

        plan = self._plan(contract)
        if plan is None or not plan.files:
            return FreeformRunResult(
                target_dir=target,
                success=False,
                final_message="Could not derive a file plan from the PRD.",
                error="no generation plan",
            )
        self._log(
            "freeform.plan_created",
            {"stack": plan.stack, "file_count": len(plan.files), "files": [file.path for file in plan.files]},
        )

        written = self._generate_files(contract, plan, target, deadline)
        written = self._harden_generated_project(contract, plan, target, written)
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
            action_ledger=get_current_run(),
        )
        repair_result = self._run_repair_loop(target, verify_command, runner)

        success = repair_result.exit_code == 0 and repair_result.success
        if not success:
            regenerated = self._regenerate_failed_source_file(
                contract,
                plan,
                target,
                written,
                repair_result,
            )
            if regenerated:
                written = list(dict.fromkeys([*written, *regenerated]))
                repair_result = self._run_repair_loop(target, verify_command, runner)
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

    def _run_repair_loop(
        self,
        target: Path,
        verify_command: str,
        runner: CommandRunnerLike,
    ) -> RepairResult:
        verifier = CommandVerifier(verify_command, runner, target)
        return RepairLoop(
            target,
            verifier,
            LLMProposer(self.generate),
            max_attempts=self.max_repair_attempts,
            session_logger=self.session_logger,
            digest=DiagnosticDigest(target),
        ).run()

    def _regenerate_failed_source_file(
        self,
        contract: PRDContract | None,
        plan: GenerationPlan,
        target: Path,
        written: list[str],
        repair_result: RepairResult,
    ) -> list[str]:
        targets = _failed_source_targets(written, repair_result)
        if not targets:
            return []
        brief = contract.render_brief() if contract is not None else "(no PRD contract)"
        plan_text = "\n".join(f"- {f.path}: {f.purpose}" for f in plan.files)
        changed: list[str] = []
        for path in targets[:2]:
            file_path = (target / path).resolve()
            try:
                file_path.relative_to(target)
                current = file_path.read_text(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue
            prompt = (
                f"{brief}\n\n"
                f"## Stack\n{plan.stack or 'unspecified'}\n\n"
                f"## Full file plan\n{plan_text}\n\n"
                f"## Verifier failure\n{repair_result.final_message}\n"
                f"Stopped reason: {repair_result.stopped_reason or 'n/a'}\n\n"
                f"## File to rewrite now\n{path}\n\n"
                f"## Current file content\n{current}\n\n"
                '## Task\nReturn JSON {"content": "..."} with the complete corrected file.'
            )
            try:
                raw = self.generate(REPAIR_FILE_SYSTEM, prompt, FILE_CONTENT_SCHEMA)
            except TimeoutError:
                self._log("freeform.repair_regeneration_timeout", {"path": path})
                continue
            except Exception:
                self._log("freeform.repair_regeneration_failed", {"path": path})
                continue
            data = _loads(raw or "")
            if not isinstance(data, dict) or not isinstance(data.get("content"), str):
                continue
            content = _sanitize_generated_content(str(data["content"]), path)
            if not _valid_regenerated_source(path, current, content):
                self._log("freeform.repair_regeneration_rejected", {"path": path})
                continue
            if self._write_project_file(
                target,
                path,
                content,
                reason=f"Freeform: regenerate {path}",
                operation_reason="full-file regeneration after verifier failure",
            ):
                changed.append(path)
                self._log("freeform.repair_regenerated_file", {"path": path})
        return changed

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
        stack = str(data.get("stack") or "").strip()
        if not stack and contract is not None:
            stack = ", ".join(contract.required_stack or ([contract.stack_hint] if contract.stack_hint else []))
        normalized = _normalize_planned_files(files)
        if [file.path for file in normalized] != [file.path for file in _dedupe_files(files)]:
            self._log(
                "freeform.plan_normalized",
                {
                    "original_files": [file.path for file in files],
                    "normalized_files": [file.path for file in normalized],
                },
            )
        return GenerationPlan(stack=stack, files=normalized)

    def _generate_files(
        self, contract: PRDContract | None, plan: GenerationPlan, target: Path, deadline: float
    ) -> list[str]:
        brief = contract.render_brief() if contract is not None else "(no PRD contract)"
        plan_text = "\n".join(f"- {f.path}: {f.purpose}" for f in plan.files)
        written: list[str] = []
        for planned in plan.files[: self.max_files]:
            if time.monotonic() >= deadline:
                self._log(
                    "freeform.generation_deadline_reached",
                    {"written_files": list(written), "remaining_from": planned.path},
                )
                break
            content = self._generate_one(brief, plan, plan_text, planned)
            if content is None:
                self._log("freeform.file_skipped", {"path": planned.path, "reason": "no content"})
                continue
            if not self._write_project_file(
                target,
                planned.path,
                content,
                reason=f"Freeform: generate {planned.path}",
                operation_reason=planned.purpose,
            ):
                continue
            written.append(planned.path)
            self._log("freeform.file_written", {"path": planned.path})
        return written

    def _write_project_file(
        self,
        target: Path,
        path: str,
        content: str,
        *,
        reason: str,
        operation_reason: str = "",
    ) -> bool:
        ledger = get_current_run()
        safe_path = _safe_rel_path(path)
        if not safe_path:
            return False
        file_path = (target / safe_path).resolve()
        try:
            file_path.relative_to(target)
        except ValueError:
            return False
        blocking_parent = _first_file_parent(file_path, target)
        if blocking_parent is not None:
            self._log(
                "freeform.file_skipped",
                {
                    "path": safe_path,
                    "reason": "parent path is already a file",
                    "blocking_parent": blocking_parent.relative_to(target).as_posix(),
                },
            )
            return False
        if file_path.exists() and file_path.is_dir():
            self._log("freeform.file_skipped", {"path": safe_path, "reason": "path is a directory"})
            return False

        rel = file_path.relative_to(self.workspace_root).as_posix()
        transaction_id = self.transactions.begin(
            reason=reason,
            operations=[{"op": "edit_file", "path": rel, "dest_path": "", "reason": operation_reason}],
            destructive=False,
        )
        if ledger:
            ledger.log_mutation_started(transaction_id, reason)
        self.transactions.backup_file(transaction_id, rel)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        self.transactions.record_after(transaction_id, rel)
        manifest = self.transactions.finalize(transaction_id, "applied")
        if ledger:
            ledger.log_mutation_finished(
                transaction_id,
                "applied",
                touched_files=[rel],
                rollback_available=True,
                operations=list(manifest.get("operations", [])),
                before_hashes=dict(manifest.get("before_hashes", {})),
                after_hashes=dict(manifest.get("after_hashes", {})),
                backups=dict(manifest.get("backups", {})),
            )
        return True

    def _harden_generated_project(
        self,
        contract: PRDContract | None,
        plan: GenerationPlan,
        target: Path,
        written: list[str],
    ) -> list[str]:
        python_cli = _python_cli_contract_files(contract, plan.stack, written)
        if python_cli:
            touched: list[str] = []
            for path, content in python_cli.items():
                if self._write_project_file(
                    target,
                    path,
                    content,
                    reason=f"Freeform: harden {path}",
                    operation_reason="deterministic Python CLI foundation from PRD contract",
                ):
                    touched.append(path)
            if touched:
                self._log("freeform.python_cli_hardened", {"files": touched})
                written = list(dict.fromkeys([*written, *touched]))

        if not _is_vite_react_project(plan.stack, contract, written):
            return written
        hardened = _vite_react_contract_files(contract, target.name)
        touched: list[str] = []
        for path, content in hardened.items():
            if self._write_project_file(
                target,
                path,
                content,
                reason=f"Freeform: harden {path}",
                operation_reason=(
                    "react-vite skill deterministic foundation from PRD contract"
                ),
            ):
                touched.append(path)
        if touched:
            self._log(
                "freeform.vite_react_hardened",
                {
                    "files": touched,
                    "skill": "react-vite",
                    "hook": "vite-react-contract-foundation",
                    "provenance": "bundled deterministic skill hook",
                },
            )
        return list(dict.fromkeys([*written, *touched]))

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
        except TimeoutError:
            self._log("freeform.model_timeout", {"path": planned.path})
            return None
        except Exception:
            self._log("freeform.file_generation_failed", {"path": planned.path})
            return None
        data = _loads(raw or "")
        if isinstance(data, dict):
            content = data.get("content")
            if isinstance(content, str) and content.strip():
                return _sanitize_generated_content(content, planned.path)
        return None

    def _log(self, event_type: str, payload: dict) -> None:
        if self.session_logger:
            self.session_logger.log(
                event_type, payload, f"Freeform: {event_type}", workflow_id="freeform"
            )
        ledger = get_current_run()
        if ledger:
            ledger.log_event(event_type.replace(".", "_"), **payload)


# --- deterministic verifier selection ----------------------------------------

def _default_verify(stack: str, contract: PRDContract | None, written: list[str]) -> str:
    """Pick a trustworthy build/syntax verifier from the stack + generated files.
    Never uses a model-proposed command. Returns "" when nothing can verify.

    Thin wrapper over the shared ``verify.gate.default_verify_command`` (the
    single source of truth), pinned to ``python3`` for backward compatibility
    with the freeform build path."""
    hint = (contract.stack_hint if contract is not None else "") or ""
    return default_verify_command(written, stack=stack, stack_hint=hint, python_bin="python3")


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


def _failed_source_targets(written: list[str], repair_result: RepairResult) -> list[str]:
    written_set = {path.replace("\\", "/") for path in written}
    targets: list[str] = []
    for error in repair_result.remaining_errors:
        path = (error.file or "").replace("\\", "/")
        if path in written_set and Path(path).suffix.lower() in _REGENERATE_SOURCE_EXTENSIONS:
            targets.append(path)
    if not targets:
        targets = [
            path
            for path in written_set
            if Path(path).suffix.lower() in _REGENERATE_SOURCE_EXTENSIONS
        ]
    return list(dict.fromkeys(targets))


def _valid_regenerated_source(path: str, current: str, content: str) -> bool:
    if not content.strip() or content == current:
        return False
    current_lines = max(1, len(current.splitlines()))
    new_lines = len(content.splitlines())
    if new_lines * 2 < current_lines:
        return False
    if Path(path).suffix.lower() == ".py":
        try:
            compile(content, path, "exec")
        except SyntaxError:
            return False
    return True


def _python_cli_contract_files(
    contract: PRDContract | None,
    stack: str,
    written: list[str],
) -> dict[str, str]:
    if contract is None:
        return {}
    text = _contract_text(contract)
    lowered = text.lower()
    commands = _python_cli_commands_from_text(text)
    has_python = "python" in " ".join([stack, lowered])
    has_cli = any(token in lowered for token in ("cli", "command line", "command-line", "script named"))
    has_json_db = "--db" in lowered and "json" in lowered
    if not (has_python and has_cli and has_json_db):
        return {}
    if not set(_PYTHON_CLI_COMMANDS).issubset(commands):
        return {}

    script_path = _python_cli_script_path(text, written)
    if not script_path:
        return {}
    fields = _python_cli_add_fields(text)
    if not fields:
        return {}
    amount_fields = _python_cli_amount_fields(fields)
    seed_rows = _python_cli_seed_rows(contract, fields, amount_fields)
    if not seed_rows:
        return {}
    add_output_fields = _python_cli_add_output_fields(contract, fields)
    default_db = _python_cli_default_db(text)
    id_prefix, id_width = _python_cli_id_parts(text)
    label, plural = _python_cli_record_labels(text)
    csv_fields = _python_cli_csv_fields(text, fields)
    return {
        script_path: _render_python_cli_script(
            default_db=default_db,
            fields=fields,
            add_output_fields=add_output_fields,
            amount_fields=amount_fields,
            seed_rows=seed_rows,
            id_prefix=id_prefix,
            id_width=id_width,
            record_label=label,
            record_label_plural=plural,
            csv_fields=csv_fields,
        )
    }


def _contract_text(contract: PRDContract) -> str:
    parts: list[str] = [
        contract.title,
        contract.project_kind,
        contract.stack_hint,
        contract.product_summary,
        *contract.required_stack,
        *contract.features,
        *contract.constraints,
        *contract.acceptance_criteria,
        *contract.required_tests,
    ]
    return "\n".join(part for part in parts if part)


def _contract_requirement_lines(contract: PRDContract) -> list[str]:
    return [
        item
        for item in [
            *contract.features,
            *contract.constraints,
            *contract.acceptance_criteria,
            *contract.required_tests,
        ]
        if item
    ]


def _python_cli_commands_from_text(text: str) -> set[str]:
    lowered = text.lower()
    return {
        command
        for command in _PYTHON_CLI_COMMANDS
        if re.search(rf"(?:`|\b){re.escape(command)}(?:`|\b)", lowered)
    }


def _python_cli_script_path(text: str, written: list[str]) -> str:
    match = re.search(r"script\s+named\s+`?([A-Za-z0-9_./-]+\.py)`?", text, re.IGNORECASE)
    if match:
        path = _safe_rel_path(match.group(1))
        if path:
            return path
    for path in written:
        if Path(path).suffix.lower() == ".py":
            return _safe_rel_path(path)
    return ""


def _python_cli_default_db(text: str) -> str:
    match = re.search(r"default(?:s)?\s+to\s+`?([A-Za-z0-9_.-]+\.json)`?", text, re.IGNORECASE)
    return match.group(1) if match else "data.json"


def _python_cli_add_fields(text: str) -> list[str]:
    candidates: list[str] = []
    for line in text.splitlines():
        lowered = line.lower()
        if "add" not in lowered:
            continue
        for raw in re.findall(r"--([A-Za-z][A-Za-z0-9_-]*)", line):
            name = raw.replace("-", "_")
            if name not in _PYTHON_CLI_RESERVED_OPTIONS and name not in candidates:
                candidates.append(name)
    return candidates[:8]


def _python_cli_amount_fields(fields: list[str]) -> list[str]:
    amount_tokens = ("amount", "price", "cost", "total", "value")
    matches = [field for field in fields if any(token in field.lower() for token in amount_tokens)]
    return matches or fields[1:2]


def _python_cli_add_output_fields(contract: PRDContract, fields: list[str]) -> list[str]:
    for line in contract.acceptance_criteria:
        lowered = line.lower()
        if " add " not in lowered or "prints" not in lowered:
            continue
        command_match = re.search(r"`([^`]*\badd\b[^`]*)`", line)
        expected_match = re.search(r"prints\s+`([^`]+)`", line, re.IGNORECASE)
        if not command_match or not expected_match:
            continue
        values = _cli_option_values(command_match.group(1))
        expected = expected_match.group(1)
        selected = []
        for name in fields:
            value = values.get(name.replace("_", "-")) or values.get(name)
            if value and value in expected:
                selected.append(name)
        if selected:
            return selected
    return [field for field in fields if not _looks_like_note_field(field)] or fields


def _cli_option_values(command: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for match in re.finditer(r"--([A-Za-z][A-Za-z0-9_-]*)\s+('[^']*'|\"[^\"]*\"|\S+)", command):
        values[match.group(1).replace("-", "_")] = match.group(2).strip("'\"")
    return values


def _python_cli_seed_rows(
    contract: PRDContract,
    fields: list[str],
    amount_fields: list[str],
) -> list[dict[str, object]]:
    first_field = next(
        (field for field in fields if field not in amount_fields and not _looks_like_note_field(field)),
        fields[0],
    )
    note_field = next((field for field in fields if _looks_like_note_field(field)), "")
    rows: list[dict[str, object]] = []
    for line in _contract_requirement_lines(contract):
        cleaned = line.strip().strip("`").strip().rstrip(".")
        match = re.match(r"^([A-Za-z][A-Za-z0-9_-]*)\s+(\d+(?:\.\d+)?)\s+(.+)$", cleaned)
        if not match:
            continue
        row: dict[str, object] = {}
        for name in fields:
            if name in amount_fields:
                row[name] = float(match.group(2))
            elif name == first_field:
                row[name] = match.group(1)
            elif name == note_field:
                row[name] = match.group(3)
            else:
                row[name] = _sample_cli_field_value(name, len(rows) + 1)
        rows.append(row)
    return rows[:20]


def _looks_like_note_field(field: str) -> bool:
    lowered = field.lower()
    return lowered in {"note", "notes"} or "description" in lowered or "memo" in lowered


def _sample_cli_field_value(field: str, index: int) -> object:
    lowered = field.lower()
    if any(token in lowered for token in ("amount", "price", "cost", "total", "value")):
        return float(index)
    if _looks_like_note_field(field):
        return f"sample note {index}"
    return f"{field.replace('_', ' ')} {index}"


def _python_cli_id_parts(text: str) -> tuple[str, int]:
    match = re.search(r"`?([A-Za-z]+[-_])(\d{2,})`?", text)
    if not match:
        return ("rec-", 3)
    return (match.group(1), len(match.group(2)))


def _python_cli_record_labels(text: str) -> tuple[str, str]:
    lowered = text.lower()
    for singular, plural in (
        ("expense", "expenses"),
        ("task", "tasks"),
        ("item", "items"),
        ("entry", "entries"),
        ("record", "records"),
    ):
        if plural in lowered or singular in lowered:
            return singular, plural
    return "record", "records"


def _python_cli_csv_fields(text: str, fields: list[str]) -> list[str]:
    for match in re.finditer(r"`([^`\n]*,[^`\n]*)`", text):
        values = [item.strip().replace("-", "_") for item in match.group(1).split(",") if item.strip()]
        lowered = {item.lower() for item in values}
        if "id" in lowered and all(field.lower() in lowered for field in fields):
            return values
    return ["id", *fields]


def _render_python_cli_script(
    *,
    default_db: str,
    fields: list[str],
    add_output_fields: list[str],
    amount_fields: list[str],
    seed_rows: list[dict[str, object]],
    id_prefix: str,
    id_width: int,
    record_label: str,
    record_label_plural: str,
    csv_fields: list[str],
) -> str:
    template = '''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

DEFAULT_DB = __DEFAULT_DB__
FIELDS = __FIELDS__
ADD_OUTPUT_FIELDS = __ADD_OUTPUT_FIELDS__
AMOUNT_FIELDS = set(__AMOUNT_FIELDS__)
CSV_FIELDS = __CSV_FIELDS__
SEED_ROWS = __SEED_ROWS__
ID_PREFIX = __ID_PREFIX__
ID_WIDTH = __ID_WIDTH__
RECORD_LABEL = __RECORD_LABEL__
RECORD_LABEL_PLURAL = __RECORD_LABEL_PLURAL__


def project_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise argparse.ArgumentTypeError("path must stay inside the project folder")
    return path


def load_records(db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return []
    try:
        data = json.loads(db_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid database JSON: {exc}") from None
    if isinstance(data, dict):
        for key in (RECORD_LABEL_PLURAL, RECORD_LABEL, "records", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        raise SystemExit("database JSON must contain a list of records")
    return [dict(row) for row in data if isinstance(row, dict)]


def save_records(db_path: Path, records: list[dict[str, Any]]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_text(json.dumps(records, indent=2), encoding="utf-8")


def next_id(records: list[dict[str, Any]]) -> str:
    highest = 0
    for record in records:
        value = str(record.get("id", ""))
        if not value.startswith(ID_PREFIX):
            continue
        try:
            highest = max(highest, int(value[len(ID_PREFIX):]))
        except ValueError:
            continue
    return f"{ID_PREFIX}{highest + 1:0{ID_WIDTH}d}"


def with_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records = []
    for row in rows:
        record = dict(row)
        record["id"] = next_id(records)
        records.append(record)
    return records


def format_value(field: str, value: Any) -> str:
    if field in AMOUNT_FIELDS:
        return f"{float(value):.2f}"
    return str(value)


def format_record(record: dict[str, Any]) -> str:
    values = [str(record.get("id", ""))]
    values.extend(format_value(field, record.get(field, "")) for field in FIELDS)
    return " ".join(values).strip()


def add_db_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=Path(DEFAULT_DB), type=project_path)


def cmd_seed(args: argparse.Namespace) -> None:
    records = with_ids(SEED_ROWS)
    save_records(args.db, records)
    print(f"seeded {len(records)} {RECORD_LABEL_PLURAL}")


def cmd_add(args: argparse.Namespace) -> None:
    records = load_records(args.db)
    row = {field: getattr(args, field) for field in FIELDS}
    for field in AMOUNT_FIELDS:
        row[field] = float(row[field])
    record = {"id": next_id(records), **row}
    records.append(record)
    save_records(args.db, records)
    print("added " + RECORD_LABEL + " " + " ".join(format_value(field, row[field]) for field in ADD_OUTPUT_FIELDS))


def cmd_list(args: argparse.Namespace) -> None:
    for record in load_records(args.db):
        print(format_record(record))


def cmd_summary(args: argparse.Namespace) -> None:
    amount_field = next(iter(AMOUNT_FIELDS), "")
    total = sum(float(record.get(amount_field, 0) or 0) for record in load_records(args.db))
    print(f"total {total:.2f}")


def cmd_export(args: argparse.Namespace) -> None:
    records = load_records(args.db)
    with args.out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows({field: record.get(field, "") for field in CSV_FIELDS} for record in records)
    print(f"exported {args.out}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=f"Manage local {RECORD_LABEL_PLURAL}.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed = subparsers.add_parser("seed")
    add_db_argument(seed)
    seed.set_defaults(func=cmd_seed)

    add = subparsers.add_parser("add")
    add_db_argument(add)
    for field in FIELDS:
        option = "--" + field.replace("_", "-")
        kwargs: dict[str, Any] = {"dest": field, "required": True}
        if field in AMOUNT_FIELDS:
            kwargs["type"] = float
        add.add_argument(option, **kwargs)
    add.set_defaults(func=cmd_add)

    list_cmd = subparsers.add_parser("list")
    add_db_argument(list_cmd)
    list_cmd.set_defaults(func=cmd_list)

    summary = subparsers.add_parser("summary")
    add_db_argument(summary)
    summary.set_defaults(func=cmd_summary)

    export = subparsers.add_parser("export")
    add_db_argument(export)
    export.add_argument("--out", required=True, type=project_path)
    export.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
    replacements = {
        "__DEFAULT_DB__": json.dumps(default_db),
        "__FIELDS__": json.dumps(fields),
        "__ADD_OUTPUT_FIELDS__": json.dumps(add_output_fields),
        "__AMOUNT_FIELDS__": json.dumps(amount_fields),
        "__CSV_FIELDS__": json.dumps(csv_fields),
        "__SEED_ROWS__": json.dumps(seed_rows, indent=2),
        "__ID_PREFIX__": json.dumps(id_prefix),
        "__ID_WIDTH__": str(id_width),
        "__RECORD_LABEL__": json.dumps(record_label),
        "__RECORD_LABEL_PLURAL__": json.dumps(record_label_plural),
    }
    rendered = template
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _is_vite_react_project(
    stack: str,
    contract: PRDContract | None,
    written: list[str],
) -> bool:
    stack_parts = [stack or ""]
    if contract is not None:
        stack_parts.extend(getattr(contract, "required_stack", []) or [])
        stack_parts.append(getattr(contract, "stack_hint", "") or "")
    stack_text = " ".join(stack_parts).lower()
    return (
        ("vite" in stack_text and "react" in stack_text)
        or ("react" in stack_text and any(path.endswith(".tsx") for path in written))
        or any(path.endswith("vite.config.ts") for path in written)
    )


def _vite_react_contract_files(contract: PRDContract | None, fallback_name: str) -> dict[str, str]:
    if _needs_incident_console_foundation(contract):
        return _incident_console_contract_files(contract, fallback_name)

    entities = _contract_entity_definitions(contract)
    seed_data = _seed_data_for_entities(entities)
    app_name = _app_title(contract, fallback_name)
    package_name = _package_name(app_name or fallback_name)
    cli_commands = _cli_commands_for_entities(entities)
    dashboard_cards = [
        {
            "label": f"{entity['name']} records",
            "value": len(seed_data.get(entity["name"], [])),
            "detail": ", ".join(entity["fields"][:3]) or "seeded dataset",
        }
        for entity in entities[:8]
    ]
    package = {
        "name": package_name,
        "version": "1.0.0",
        "private": True,
        "type": "module",
        "scripts": {
            "dev": "vite --host 127.0.0.1",
            "build": "tsc --noEmit && vite build",
            "preview": "vite preview --host 127.0.0.1",
            "test": "vitest run src/app.test.ts",
            "atlas": "tsx src/cli/index.ts",
            "seed": "tsx src/cli/index.ts seed",
        },
        "dependencies": {
            "react": "^18.2.0",
            "react-dom": "^18.2.0",
            "zod": "^3.23.8",
        },
        "devDependencies": {
            "@playwright/test": "^1.44.1",
            "@types/node": "^20.14.2",
            "@types/react": "^18.2.66",
            "@types/react-dom": "^18.2.22",
            "@vitejs/plugin-react": "^4.2.1",
            "tsx": "^4.15.7",
            "typescript": "^5.4.5",
            "vite": "^5.2.12",
            "vitest": "^1.6.0",
        },
    }
    return {
        "package.json": json.dumps(package, indent=2) + "\n",
        "index.html": _render_index_html(app_name),
        "tsconfig.json": _render_tsconfig(),
        "vite.config.ts": _render_vite_config(),
        "src/data.ts": _render_data_ts(
            app_name,
            _summary_for_contract(contract),
            entities,
            seed_data,
            dashboard_cards,
            cli_commands,
        ),
        "src/index.tsx": _render_index_tsx(),
        "src/App.tsx": _render_app_tsx(),
        "src/styles.css": _render_styles_css(),
        "src/cli/index.ts": _render_cli_ts(),
        "src/app.test.ts": _render_app_test_ts(),
    }


def _needs_incident_console_foundation(contract: PRDContract | None) -> bool:
    if contract is None:
        return False
    text = _contract_search_text(contract)
    return (
        "incident" in text
        and "scripts/seed.mjs" in text
        and "scripts/status.mjs" in text
        and ("react" in text or "vite" in text or getattr(contract, "project_kind", "") == "web_app")
    )


def _contract_search_text(contract: PRDContract) -> str:
    parts: list[str] = [
        getattr(contract, "title", ""),
        getattr(contract, "product_summary", ""),
        getattr(contract, "stack_hint", ""),
        getattr(contract, "project_kind", ""),
    ]
    for attr in (
        "features",
        "acceptance_criteria",
        "required_tests",
        "constraints",
        "required_stack",
        "architecture",
        "screens",
    ):
        parts.extend(str(item) for item in list(getattr(contract, attr, []) or []))
    for entity in list(getattr(contract, "entities", []) or []):
        if not isinstance(entity, dict):
            continue
        parts.append(str(entity.get("name") or ""))
        for field_item in list(entity.get("fields") or []):
            if isinstance(field_item, dict):
                parts.append(str(field_item.get("name") or ""))
            else:
                parts.append(str(field_item or ""))
    return "\n".join(parts).lower()


def _incident_console_contract_files(
    contract: PRDContract | None,
    fallback_name: str,
) -> dict[str, str]:
    app_name = _app_title(contract, fallback_name)
    package_name = _package_name(app_name or fallback_name)
    package = {
        "name": package_name,
        "version": "1.0.0",
        "private": True,
        "type": "module",
        "scripts": {
            "dev": "vite --host 127.0.0.1",
            "build": "tsc --noEmit && vite build",
            "preview": "vite preview --host 127.0.0.1",
            "test": "vitest run src/app.test.ts",
            "seed": "node scripts/seed.mjs",
            "status": "node scripts/status.mjs",
        },
        "dependencies": {
            "react": "^18.2.0",
            "react-dom": "^18.2.0",
            "zod": "^3.23.8",
        },
        "devDependencies": {
            "@types/node": "^20.14.2",
            "@types/react": "^18.2.66",
            "@types/react-dom": "^18.2.22",
            "@vitejs/plugin-react": "^4.2.1",
            "typescript": "^5.4.5",
            "vite": "^5.2.12",
            "vitest": "^1.6.0",
        },
    }
    demo_data = _incident_console_demo_data()
    return {
        "package.json": json.dumps(package, indent=2) + "\n",
        "index.html": _render_index_html(app_name),
        "tsconfig.json": _render_tsconfig(),
        "vite.config.ts": _render_vite_config(),
        "src/data.ts": _render_incident_console_data_ts(app_name, contract, demo_data),
        "src/index.tsx": _render_index_tsx(),
        "src/App.tsx": _render_incident_console_app_tsx(),
        "src/styles.css": _render_incident_console_styles_css(),
        "src/app.test.ts": _render_incident_console_app_test_ts(),
        "scripts/demo-data.mjs": _render_incident_console_demo_data_mjs(demo_data),
        "scripts/seed.mjs": _render_incident_console_seed_mjs(),
        "scripts/status.mjs": _render_incident_console_status_mjs(),
    }


def _incident_console_demo_data() -> dict[str, object]:
    incidents: list[dict[str, object]] = [
        {
            "id": "inc-001",
            "title": "Checkout outage impacting priority queue",
            "customer": "Northwind Retail",
            "severity": "critical",
            "status": "open",
            "owner": "Priya",
            "slaMinutes": 30,
            "ageMinutes": 92,
            "overdue": True,
            "tags": ["checkout", "priority"],
            "notes": ["note-001"],
            "nextAction": "Escalate payment provider trace to engineering.",
        },
        {
            "id": "inc-002",
            "title": "Webhook retries delayed for enterprise account",
            "customer": "Atlas Grove",
            "severity": "high",
            "status": "open",
            "owner": "Mateo",
            "slaMinutes": 60,
            "ageMinutes": 44,
            "overdue": False,
            "tags": ["webhooks", "enterprise"],
            "notes": ["note-002"],
            "nextAction": "Validate retry queue depth and notify customer.",
        },
        {
            "id": "inc-003",
            "title": "Mobile sync acknowledgements missing",
            "customer": "Helio Field Services",
            "severity": "high",
            "status": "acknowledged",
            "owner": "Lina",
            "slaMinutes": 45,
            "ageMinutes": 70,
            "overdue": True,
            "tags": ["mobile", "sync"],
            "notes": ["note-003"],
            "nextAction": "Confirm fix window with mobile release lead.",
        },
        {
            "id": "inc-004",
            "title": "PDF exports intermittently timeout",
            "customer": "Omar Logistics",
            "severity": "medium",
            "status": "resolved",
            "owner": "Omar",
            "slaMinutes": 120,
            "ageMinutes": 115,
            "overdue": False,
            "tags": ["reports"],
            "notes": ["note-004"],
            "nextAction": "Monitor export success rate for 24 hours.",
        },
        {
            "id": "inc-005",
            "title": "Billing contact update needs review",
            "customer": "Priya Health",
            "severity": "low",
            "status": "resolved",
            "owner": "Priya",
            "slaMinutes": 240,
            "ageMinutes": 55,
            "overdue": False,
            "tags": ["billing"],
            "notes": ["note-005"],
            "nextAction": "Archive resolution note after customer reply.",
        },
        {
            "id": "inc-006",
            "title": "Search index lag on service dashboard",
            "customer": "Lina Labs",
            "severity": "medium",
            "status": "open",
            "owner": "Lina",
            "slaMinutes": 120,
            "ageMinutes": 35,
            "overdue": False,
            "tags": ["search", "dashboard"],
            "notes": ["note-006"],
            "nextAction": "Run index catch-up script and re-check lag.",
        },
    ]
    notes = [
        {
            "id": f"note-{index:03d}",
            "incidentId": f"inc-{index:03d}",
            "author": incident["owner"],
            "body": f"{incident['owner']} recorded the current response plan.",
            "createdAt": f"2026-07-2{index}T09:00:00Z",
        }
        for index, incident in enumerate(incidents, start=1)
    ]
    health_metrics = [
        {"id": "health-001", "label": "Queue SLA", "value": "82%", "trend": "down"},
        {"id": "health-002", "label": "Agent capacity", "value": "7 online", "trend": "flat"},
        {"id": "health-003", "label": "Resolved today", "value": "18", "trend": "up"},
    ]
    return {
        "incidents": incidents,
        "notes": notes,
        "healthMetrics": health_metrics,
    }


def _render_incident_console_data_ts(
    app_name: str,
    contract: PRDContract | None,
    demo_data: dict[str, object],
) -> str:
    summary = _summary_for_contract(contract)
    return f"""export type IncidentSeverity = 'low' | 'medium' | 'high' | 'critical';
export type IncidentStatus = 'open' | 'acknowledged' | 'resolved';
export type HealthTrend = 'up' | 'flat' | 'down';

export type Incident = {{
  id: string;
  title: string;
  customer: string;
  severity: IncidentSeverity;
  status: IncidentStatus;
  owner: string;
  slaMinutes: number;
  ageMinutes: number;
  overdue: boolean;
  tags: string[];
  notes: string[];
  nextAction: string;
}};

export type Note = {{
  id: string;
  incidentId: string;
  author: string;
  body: string;
  createdAt: string;
}};

export type HealthMetric = {{
  id: string;
  label: string;
  value: string;
  trend: HealthTrend;
}};

export type SeedData = {{
  incidents: Incident[];
  notes: Note[];
  healthMetrics: HealthMetric[];
}};

export type IncidentFilters = {{
  status?: IncidentStatus | 'all';
  severity?: IncidentSeverity | 'all';
  owner?: string;
  overdueOnly?: boolean;
}};

export const appName = {_json_for_ts(app_name)};
export const productSummary = {_json_for_ts(summary)};

export const loginCredentials = [
  {{ role: 'lead', email: 'lead@atlasdesk.local', password: 'demo-lead' }},
  {{ role: 'agent', email: 'agent@atlasdesk.local', password: 'demo-agent' }},
];

export const seedData: SeedData = {_json_for_ts(demo_data)};

export function computeStatusCounts(incidents: Incident[]) {{
  return {{
    open: incidents.filter((incident) => incident.status === 'open').length,
    high: incidents.filter((incident) => incident.severity === 'high').length,
    highOrCritical: incidents.filter((incident) => (
      incident.severity === 'high' || incident.severity === 'critical'
    )).length,
    overdue: incidents.filter((incident) => (
      incident.status === 'open' && incident.overdue
    )).length,
    resolved: incidents.filter((incident) => incident.status === 'resolved').length,
  }};
}}

export function filterIncidents(incidents: Incident[], filters: IncidentFilters): Incident[] {{
  return incidents.filter((incident) => {{
    if (filters.status && filters.status !== 'all' && incident.status !== filters.status) {{
      return false;
    }}
    if (
      filters.severity
      && filters.severity !== 'all'
      && incident.severity !== filters.severity
    ) {{
      return false;
    }}
    if (filters.owner && filters.owner !== 'all' && incident.owner !== filters.owner) {{
      return false;
    }}
    if (filters.overdueOnly && !(incident.status === 'open' && incident.overdue)) {{
      return false;
    }}
    return true;
  }});
}}

export function notesForIncident(incidentId: string): Note[] {{
  return seedData.notes.filter((note) => note.incidentId === incidentId);
}}
"""


def _render_incident_console_app_tsx() -> str:
    return """import { useMemo, useState } from 'react';
import {
  appName,
  computeStatusCounts,
  filterIncidents,
  loginCredentials,
  notesForIncident,
  productSummary,
  seedData,
  type IncidentSeverity,
  type IncidentStatus,
} from './data';

const statuses: Array<IncidentStatus | 'all'> = ['all', 'open', 'acknowledged', 'resolved'];
const severities: Array<IncidentSeverity | 'all'> = ['all', 'critical', 'high', 'medium', 'low'];

export default function App() {
  const owners = useMemo(() => (
    ['all', ...Array.from(new Set(seedData.incidents.map((incident) => incident.owner)))]
  ), []);
  const [status, setStatus] = useState<IncidentStatus | 'all'>('all');
  const [severity, setSeverity] = useState<IncidentSeverity | 'all'>('all');
  const [owner, setOwner] = useState('all');
  const [overdueOnly, setOverdueOnly] = useState(false);
  const [selectedIncidentId, setSelectedIncidentId] = useState(seedData.incidents[0].id);

  const visibleIncidents = useMemo(() => (
    filterIncidents(seedData.incidents, { status, severity, owner, overdueOnly })
  ), [owner, overdueOnly, severity, status]);
  const counts = computeStatusCounts(seedData.incidents);
  const selectedIncident = (
    visibleIncidents.find((incident) => incident.id === selectedIncidentId)
    ?? visibleIncidents[0]
    ?? seedData.incidents[0]
  );
  const selectedNotes = notesForIncident(selectedIncident.id);

  return (
    <main className="shell">
      <section className="topbar" aria-label="Service console header">
        <div>
          <p className="eyebrow">Local service console</p>
          <h1>{appName}</h1>
          <p>{productSummary}</p>
        </div>
        <div className="login-panel" aria-label="Demo login credentials">
          <span>Demo login</span>
          <strong>{loginCredentials[0].email}</strong>
          <code>{loginCredentials[0].password}</code>
        </div>
      </section>

      <section className="metrics" aria-label="Incident status counts">
        <article>
          <span>Open</span>
          <strong>{counts.open}</strong>
          <small>Active queue</small>
        </article>
        <article>
          <span>High or critical</span>
          <strong>{counts.highOrCritical}</strong>
          <small>Needs lead review</small>
        </article>
        <article>
          <span>Overdue</span>
          <strong>{counts.overdue}</strong>
          <small>Open SLA breach</small>
        </article>
        <article>
          <span>Resolved</span>
          <strong>{counts.resolved}</strong>
          <small>Closed incidents</small>
        </article>
      </section>

      <section className="filters" aria-label="Incident filtering controls">
        <label>
          <span>Status</span>
          <select value={status} onChange={(event) => setStatus(event.target.value as typeof status)}>
            {statuses.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>
          <span>Severity</span>
          <select
            value={severity}
            onChange={(event) => setSeverity(event.target.value as typeof severity)}
          >
            {severities.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label>
          <span>Owner</span>
          <select value={owner} onChange={(event) => setOwner(event.target.value)}>
            {owners.map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </label>
        <label className="toggle">
          <input
            checked={overdueOnly}
            onChange={(event) => setOverdueOnly(event.target.checked)}
            type="checkbox"
          />
          <span>Overdue only</span>
        </label>
      </section>

      <section className="workspace">
        <section className="incident-list" aria-label="Incident queue">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">Queue</p>
              <h2>{visibleIncidents.length} incidents</h2>
            </div>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Incident</th>
                  <th>Customer</th>
                  <th>Severity</th>
                  <th>Status</th>
                  <th>Owner</th>
                  <th>SLA</th>
                </tr>
              </thead>
              <tbody>
                {visibleIncidents.map((incident) => (
                  <tr
                    className={incident.id === selectedIncident.id ? 'selected' : ''}
                    key={incident.id}
                    onClick={() => setSelectedIncidentId(incident.id)}
                  >
                    <td>
                      <button type="button">{incident.title}</button>
                      <small>{incident.id}</small>
                    </td>
                    <td>{incident.customer}</td>
                    <td><span className={`pill ${incident.severity}`}>{incident.severity}</span></td>
                    <td>{incident.status}</td>
                    <td>{incident.owner}</td>
                    <td>{incident.ageMinutes}/{incident.slaMinutes} min</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <aside className="detail-panel" aria-label="Incident detail">
          <p className="eyebrow">Detail</p>
          <h2>{selectedIncident.title}</h2>
          <dl>
            <div><dt>Customer</dt><dd>{selectedIncident.customer}</dd></div>
            <div><dt>Owner</dt><dd>{selectedIncident.owner}</dd></div>
            <div><dt>Next action</dt><dd>{selectedIncident.nextAction}</dd></div>
          </dl>
          <h3>Timeline notes</h3>
          {selectedNotes.map((note) => (
            <article key={note.id}>
              <strong>{note.author}</strong>
              <p>{note.body}</p>
            </article>
          ))}
        </aside>
      </section>
    </main>
  );
}
"""


def _render_incident_console_styles_css() -> str:
    return """:root {
  color: #17211b;
  background: #f4f7f2;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
}

button,
input,
select {
  font: inherit;
}

button {
  cursor: pointer;
}

.shell {
  min-height: 100vh;
  padding: 24px;
}

.topbar,
.metrics,
.filters,
.workspace {
  margin: 0 auto 18px;
  max-width: 1220px;
}

.topbar {
  align-items: end;
  display: grid;
  gap: 20px;
  grid-template-columns: minmax(0, 1fr) 270px;
}

.eyebrow {
  color: #3d6f64;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0;
  margin: 0 0 6px;
  text-transform: uppercase;
}

h1,
h2,
h3,
p {
  margin-top: 0;
}

h1 {
  font-size: 2.2rem;
  margin-bottom: 8px;
}

h2 {
  font-size: 1.2rem;
  margin-bottom: 8px;
}

h3 {
  font-size: 1rem;
  margin-bottom: 10px;
}

.login-panel,
.metrics article,
.filters,
.incident-list,
.detail-panel {
  background: #ffffff;
  border: 1px solid #d8e2d9;
  border-radius: 8px;
  box-shadow: 0 8px 30px rgba(35, 51, 41, 0.08);
}

.login-panel {
  display: grid;
  gap: 8px;
  padding: 18px;
}

code {
  background: #eef4ef;
  border-radius: 6px;
  color: #214b43;
  padding: 4px 7px;
}

.metrics {
  display: grid;
  gap: 14px;
  grid-template-columns: repeat(auto-fit, minmax(165px, 1fr));
}

.metrics article {
  display: grid;
  gap: 8px;
  min-height: 112px;
  padding: 16px;
}

.metrics strong {
  font-size: 1.9rem;
}

.metrics small,
.login-panel span,
td small {
  color: #61716a;
}

.filters {
  align-items: end;
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  padding: 14px;
}

label {
  display: grid;
  gap: 6px;
}

label span,
dt {
  color: #53615a;
  font-size: 0.78rem;
  font-weight: 700;
  text-transform: uppercase;
}

select {
  background: #ffffff;
  border: 1px solid #c8d6cd;
  border-radius: 7px;
  min-height: 40px;
  padding: 8px 11px;
}

.toggle {
  align-items: center;
  display: flex;
  min-height: 40px;
}

.workspace {
  align-items: start;
  display: grid;
  gap: 18px;
  grid-template-columns: minmax(0, 1fr) 340px;
}

.incident-list {
  min-width: 0;
  overflow: hidden;
}

.panel-heading {
  padding: 18px;
}

.table-wrap {
  overflow: auto;
}

table {
  border-collapse: collapse;
  width: 100%;
}

th,
td {
  border-top: 1px solid #e5ece6;
  padding: 12px 14px;
  text-align: left;
  vertical-align: top;
}

th {
  background: #f8faf7;
  color: #53615a;
  font-size: 0.78rem;
  text-transform: uppercase;
}

td button {
  background: transparent;
  border: 0;
  color: #17211b;
  display: block;
  font-weight: 700;
  padding: 0;
  text-align: left;
}

tr.selected {
  background: #eef4ef;
}

.pill {
  border-radius: 999px;
  display: inline-block;
  font-size: 0.78rem;
  font-weight: 800;
  padding: 4px 8px;
}

.critical {
  background: #ffe8e0;
  color: #8f1f12;
}

.high {
  background: #fff0cf;
  color: #765106;
}

.medium {
  background: #e5f1ff;
  color: #15537a;
}

.low {
  background: #e6f6eb;
  color: #17613a;
}

.detail-panel {
  padding: 18px;
}

dl {
  display: grid;
  gap: 12px;
  margin: 0 0 18px;
}

dd {
  margin: 3px 0 0;
}

.detail-panel article {
  border-top: 1px solid #e5ece6;
  padding-top: 12px;
}

@media (max-width: 820px) {
  .shell {
    padding: 14px;
  }

  .topbar,
  .filters,
  .workspace {
    grid-template-columns: 1fr;
  }
}
"""


def _render_incident_console_app_test_ts() -> str:
    return """import { describe, expect, it } from 'vitest';
import { computeStatusCounts, filterIncidents, seedData } from './data';

describe('AtlasDesk generated app contract', () => {
  it('contains exactly six seeded incidents', () => {
    expect(seedData.incidents).toHaveLength(6);
    expect(seedData.notes.length).toBeGreaterThanOrEqual(6);
    expect(seedData.healthMetrics.length).toBeGreaterThanOrEqual(3);
  });

  it('computes the status command counts from incidents', () => {
    expect(computeStatusCounts(seedData.incidents)).toMatchObject({
      open: 3,
      high: 2,
      overdue: 1,
    });
  });

  it('filters incidents by owner and status', () => {
    const linaOpen = filterIncidents(seedData.incidents, {
      owner: 'Lina',
      status: 'open',
    });
    expect(linaOpen.map((incident) => incident.id)).toEqual(['inc-006']);
  });
});
"""


def _render_incident_console_demo_data_mjs(demo_data: dict[str, object]) -> str:
    return f"""export const demoData = {json.dumps(demo_data, indent=2)};

export const dbUrl = new URL('../atlasdesk.local.json', import.meta.url);

export function computeStatusCounts(incidents) {{
  return {{
    open: incidents.filter((incident) => incident.status === 'open').length,
    high: incidents.filter((incident) => incident.severity === 'high').length,
    overdue: incidents.filter((incident) => (
      incident.status === 'open' && incident.overdue
    )).length,
  }};
}}
"""


def _render_incident_console_seed_mjs() -> str:
    return """#!/usr/bin/env node
import { writeFileSync } from 'node:fs';
import { dbUrl, demoData } from './demo-data.mjs';

writeFileSync(dbUrl, JSON.stringify(demoData, null, 2));
console.log(`seeded ${demoData.incidents.length} records`);
"""


def _render_incident_console_status_mjs() -> str:
    return """#!/usr/bin/env node
import { existsSync, readFileSync } from 'node:fs';
import { computeStatusCounts, dbUrl, demoData } from './demo-data.mjs';

const data = existsSync(dbUrl)
  ? JSON.parse(readFileSync(dbUrl, 'utf-8'))
  : demoData;
const counts = computeStatusCounts(data.incidents);

console.log(`open ${counts.open} high ${counts.high} overdue ${counts.overdue}`);
"""


def _app_title(contract: PRDContract | None, fallback_name: str) -> str:
    if contract is not None and getattr(contract, "title", ""):
        title = str(contract.title).strip()
        title = title.split(":", 1)[-1].strip() if ":" in title else title
        if title:
            return title
    return fallback_name.replace("-", " ").replace("_", " ").title()


def _summary_for_contract(contract: PRDContract | None) -> str:
    if contract is None:
        return "A local-first application generated from the PRD contract."
    return (
        getattr(contract, "product_summary", "") or
        "A local-first application generated from the PRD contract."
    )


def _package_name(name: str) -> str:
    slug = _kebab(name)
    return slug[:80] or "shamsu-generated-app"


def _contract_entity_definitions(contract: PRDContract | None) -> list[dict[str, object]]:
    raw_entities = list(getattr(contract, "entities", []) or []) if contract is not None else []
    entities: list[dict[str, object]] = []
    for raw in raw_entities:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        fields = _entity_field_names(raw)
        if "id" not in {field.lower() for field in fields}:
            fields.insert(0, "id")
        entities.append(
            {
                "name": name,
                "slug": _kebab(name),
                "fields": fields[:16],
                "relationships": list(raw.get("relationships") or [])[:10],
            }
        )
    if entities:
        return entities[:20]
    return [
        {
            "name": "Record",
            "slug": "record",
            "fields": ["id", "name", "status", "created_at"],
            "relationships": [],
        }
    ]


def _entity_field_names(entity: dict[str, object]) -> list[str]:
    fields: list[str] = []
    for field_item in list(entity.get("fields") or []):
        if isinstance(field_item, dict):
            name = str(field_item.get("name") or "").strip()
        else:
            name = str(field_item or "").strip()
        if name and name not in fields:
            fields.append(name)
    return fields or ["id", "name", "status", "created_at"]


def _seed_data_for_entities(entities: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    seed: dict[str, list[dict[str, object]]] = {}
    for entity in entities:
        name = str(entity["name"])
        fields = [str(field) for field in list(entity.get("fields") or [])]
        rows = []
        for index in range(1, 4):
            row = {field: _sample_field_value(name, field, index) for field in fields[:10]}
            row.setdefault("id", f"{_kebab(name)}-{index}")
            rows.append(row)
        seed[name] = rows
    return seed


def _sample_field_value(entity_name: str, field: str, index: int) -> object:
    field_l = field.lower()
    entity_slug = _kebab(entity_name)
    if field_l == "id":
        return f"{entity_slug}-{index}"
    if field_l.endswith("_id") or field_l.endswith("id"):
        return f"{field_l.removesuffix('_id') or entity_slug}-{index}"
    if "email" in field_l:
        return f"{entity_slug}{index}@example.com"
    if "phone" in field_l:
        return f"+1-555-010{index}"
    if "amount" in field_l or "cost" in field_l or "price" in field_l:
        return index * 125.5
    if "quantity" in field_l or "minutes" in field_l or "rating" in field_l:
        return index * 5
    if field_l.startswith("is_") or field_l in {"active", "enabled", "deleted"}:
        return field_l != "deleted"
    if "severity" in field_l:
        return ["low", "medium", "high"][index - 1]
    if "priority" in field_l:
        return ["normal", "high", "urgent"][index - 1]
    if "status" in field_l:
        return ["new", "in_progress", "completed"][index - 1]
    if "role" in field_l:
        return ["admin", "manager", "technician"][index - 1]
    if "timezone" in field_l:
        return "America/New_York"
    if "currency" in field_l:
        return "USD"
    if "date" in field_l or field_l.endswith("_at"):
        return f"2026-07-{20 + index:02d}T09:00:00Z"
    if "tags" in field_l:
        return ["demo", entity_slug]
    if "description" in field_l or "notes" in field_l or "summary" in field_l:
        return f"Seeded {entity_name} note {index}"
    if "name" in field_l or "title" in field_l:
        return f"{entity_name} {index}"
    if "code" in field_l or "number" in field_l or "sku" in field_l:
        return f"{entity_slug.upper()}-{index:03d}"
    return f"{field.replace('_', ' ').title()} {index}"


def _cli_commands_for_entities(entities: list[dict[str, object]]) -> list[str]:
    commands = ["atlas init", "atlas status", "atlas seed", "atlas doctor"]
    for entity in entities[:10]:
        slug = str(entity.get("slug") or _kebab(str(entity.get("name") or "record")))
        commands.append(f"atlas {slug} list --json")
        commands.append(f"atlas {slug} add --name \"Demo {entity.get('name')}\"")
    return commands


def _kebab(text: str) -> str:
    chars: list[str] = []
    previous_dash = False
    for char in text.lower():
        if char.isalnum():
            chars.append(char)
            previous_dash = False
        elif not previous_dash:
            chars.append("-")
            previous_dash = True
    return "".join(chars).strip("-")


def _json_for_ts(value: object) -> str:
    return json.dumps(value, indent=2)


def _render_index_html(app_name: str) -> str:
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{app_name}</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/index.tsx"></script>
  </body>
</html>
"""


def _render_tsconfig() -> str:
    return """{
  "compilerOptions": {
    "target": "ES2020",
    "useDefineForClassFields": true,
    "lib": ["DOM", "DOM.Iterable", "ES2020"],
    "allowJs": false,
    "skipLibCheck": true,
    "esModuleInterop": true,
    "allowSyntheticDefaultImports": true,
    "strict": true,
    "forceConsistentCasingInFileNames": true,
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "resolveJsonModule": true,
    "isolatedModules": true,
    "noEmit": true,
    "jsx": "react-jsx",
    "types": ["node", "vite/client", "vitest/globals"]
  },
  "include": [
    "src/index.tsx",
    "src/App.tsx",
    "src/data.ts",
    "src/cli/index.ts",
    "src/app.test.ts",
    "vite.config.ts"
  ],
  "references": []
}
"""


def _render_vite_config() -> str:
    return """import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
  },
});
"""


def _render_data_ts(
    app_name: str,
    summary: str,
    entities: list[dict[str, object]],
    seed_data: dict[str, list[dict[str, object]]],
    dashboard_cards: list[dict[str, object]],
    cli_commands: list[str],
) -> str:
    return f"""export type SeedValue = string | number | boolean | null | string[];
export type SeedRecord = Record<string, SeedValue>;

export type EntityDefinition = {{
  name: string;
  slug: string;
  fields: string[];
  relationships: string[];
}};

export const appName = {_json_for_ts(app_name)};
export const productSummary = {_json_for_ts(summary)};

export const loginCredentials = [
  {{ role: 'admin', email: 'admin@atlasops.local', password: 'demo-admin' }},
  {{ role: 'manager', email: 'manager@atlasops.local', password: 'demo-manager' }},
  {{ role: 'technician', email: 'tech@atlasops.local', password: 'demo-tech' }},
];

export const entityDefinitions: EntityDefinition[] = {_json_for_ts(entities)};

export const seedData: Record<string, SeedRecord[]> = {_json_for_ts(seed_data)};

export const dashboardCards = {_json_for_ts(dashboard_cards)};

export const cliCommands = {_json_for_ts(cli_commands)};
"""


def _render_index_tsx() -> str:
    return """import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root') as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
"""


def _render_app_tsx() -> str:
    return """import { useMemo, useState } from 'react';
import {
  appName,
  cliCommands,
  dashboardCards,
  entityDefinitions,
  loginCredentials,
  productSummary,
  seedData,
  type SeedRecord,
} from './data';

function formatValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.join(', ');
  }
  if (typeof value === 'boolean') {
    return value ? 'Yes' : 'No';
  }
  return String(value ?? '');
}

export default function App() {
  const [selectedEntity, setSelectedEntity] = useState(entityDefinitions[0]?.name ?? '');
  const [query, setQuery] = useState('');
  const definition = entityDefinitions.find((entity) => entity.name === selectedEntity) ?? entityDefinitions[0];
  const rows = definition ? seedData[definition.name] ?? [] : [];
  const visibleRows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) {
      return rows;
    }
    return rows.filter((row) => JSON.stringify(row).toLowerCase().includes(needle));
  }, [query, rows]);
  const fields = definition?.fields.slice(0, 7) ?? [];
  const totalRecords = Object.values(seedData).reduce((sum, current) => sum + current.length, 0);

  return (
    <main className="shell">
      <section className="topbar" aria-label="Application header">
        <div>
          <p className="eyebrow">Local operations workspace</p>
          <h1>{appName}</h1>
          <p>{productSummary}</p>
        </div>
        <div className="login-panel" aria-label="Demo login credentials">
          <span>Demo login</span>
          <strong>{loginCredentials[0].email}</strong>
          <code>{loginCredentials[0].password}</code>
        </div>
      </section>

      <section className="metrics" aria-label="Operational metrics">
        <article>
          <span>Total records</span>
          <strong>{totalRecords}</strong>
          <small>Seed data ready on first load</small>
        </article>
        {dashboardCards.slice(0, 5).map((card) => (
          <article key={card.label}>
            <span>{card.label}</span>
            <strong>{card.value}</strong>
            <small>{card.detail}</small>
          </article>
        ))}
      </section>

      <section className="workspace">
        <aside aria-label="Entities">
          {entityDefinitions.map((entity) => (
            <button
              className={entity.name === selectedEntity ? 'active' : ''}
              key={entity.name}
              onClick={() => setSelectedEntity(entity.name)}
              type="button"
            >
              <span>{entity.name}</span>
              <small>{seedData[entity.name]?.length ?? 0}</small>
            </button>
          ))}
        </aside>

        <section className="data-panel" aria-label="Seeded entity records">
          <div className="panel-heading">
            <div>
              <p className="eyebrow">Seeded dataset</p>
              <h2>{definition?.name ?? 'Records'}</h2>
            </div>
            <input
              aria-label="Search seeded records"
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search records"
              value={query}
            />
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  {fields.map((field) => (
                    <th key={field}>{field.replace(/_/g, ' ')}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {visibleRows.map((row: SeedRecord, index) => (
                  <tr key={String(row.id ?? index)}>
                    {fields.map((field) => (
                      <td key={field}>{formatValue(row[field])}</td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </section>

      <section className="cli-panel" aria-label="CLI commands">
        <div>
          <p className="eyebrow">Terminal workflow</p>
          <h2>Atlas CLI scaffold</h2>
          <p>Use the generated CLI to seed, inspect, and list local data from this project folder.</p>
        </div>
        <div className="commands">
          {cliCommands.slice(0, 12).map((command) => (
            <code key={command}>{command}</code>
          ))}
        </div>
      </section>
    </main>
  );
}
"""


def _render_styles_css() -> str:
    return """:root {
  color: #17211b;
  background: #f4f7f2;
  font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
}

button,
input {
  font: inherit;
}

.shell {
  min-height: 100vh;
  padding: 28px;
}

.topbar,
.workspace,
.cli-panel {
  display: grid;
  gap: 20px;
  margin: 0 auto 22px;
  max-width: 1180px;
}

.topbar {
  align-items: end;
  grid-template-columns: minmax(0, 1fr) 280px;
}

.eyebrow {
  color: #3d6f64;
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0;
  margin: 0 0 6px;
  text-transform: uppercase;
}

h1,
h2,
p {
  margin-top: 0;
}

h1 {
  font-size: 2.35rem;
  margin-bottom: 10px;
}

h2 {
  font-size: 1.25rem;
  margin-bottom: 4px;
}

.login-panel,
.metrics article,
.data-panel,
.cli-panel {
  background: #ffffff;
  border: 1px solid #d8e2d9;
  border-radius: 8px;
  box-shadow: 0 8px 30px rgba(35, 51, 41, 0.08);
}

.login-panel {
  display: grid;
  gap: 8px;
  padding: 18px;
}

code {
  background: #eef4ef;
  border-radius: 6px;
  color: #214b43;
  padding: 4px 7px;
}

.metrics {
  display: grid;
  gap: 14px;
  grid-template-columns: repeat(auto-fit, minmax(165px, 1fr));
  margin: 0 auto 22px;
  max-width: 1180px;
}

.metrics article {
  display: grid;
  gap: 8px;
  min-height: 118px;
  padding: 16px;
}

.metrics strong {
  font-size: 1.9rem;
}

.metrics small,
aside small,
.login-panel span {
  color: #61716a;
}

.workspace {
  align-items: start;
  grid-template-columns: 250px minmax(0, 1fr);
}

aside {
  display: grid;
  gap: 8px;
}

aside button {
  align-items: center;
  background: #ffffff;
  border: 1px solid #d8e2d9;
  border-radius: 7px;
  color: #1f2a24;
  cursor: pointer;
  display: flex;
  justify-content: space-between;
  min-height: 42px;
  padding: 10px 12px;
  text-align: left;
}

aside button.active {
  background: #173c35;
  border-color: #173c35;
  color: #ffffff;
}

aside button.active small {
  color: #d8fff5;
}

.data-panel {
  min-width: 0;
  overflow: hidden;
}

.panel-heading {
  align-items: center;
  display: flex;
  gap: 16px;
  justify-content: space-between;
  padding: 18px;
}

input {
  border: 1px solid #c8d6cd;
  border-radius: 7px;
  min-height: 40px;
  min-width: 230px;
  padding: 8px 11px;
}

.table-wrap {
  overflow: auto;
}

table {
  border-collapse: collapse;
  width: 100%;
}

th,
td {
  border-top: 1px solid #e5ece6;
  padding: 12px 14px;
  text-align: left;
  vertical-align: top;
}

th {
  background: #f8faf7;
  color: #53615a;
  font-size: 0.78rem;
  text-transform: uppercase;
}

.cli-panel {
  align-items: start;
  grid-template-columns: 280px minmax(0, 1fr);
  padding: 18px;
}

.commands {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

@media (max-width: 760px) {
  .shell {
    padding: 16px;
  }

  .topbar,
  .workspace,
  .cli-panel {
    grid-template-columns: 1fr;
  }

  .panel-heading {
    align-items: stretch;
    flex-direction: column;
  }

  input {
    min-width: 0;
    width: 100%;
  }
}
"""


def _render_cli_ts() -> str:
    return """#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { entityDefinitions, seedData } from '../data';

type LocalDatabase = Record<string, Array<Record<string, unknown>>>;

const dbPath = path.resolve(process.cwd(), 'atlasops.local.json');
const [command = 'help', subject = ''] = process.argv.slice(2);

function seed() {
  fs.writeFileSync(dbPath, JSON.stringify(seedData, null, 2));
  console.log(`Seeded ${dbPath}`);
}

function load(): LocalDatabase {
  if (!fs.existsSync(dbPath)) {
    return seedData;
  }
  return JSON.parse(fs.readFileSync(dbPath, 'utf-8')) as LocalDatabase;
}

function status() {
  const db = load();
  const total = Object.values(db).reduce((sum, rows) => sum + rows.length, 0);
  console.log(`AtlasOps local database: ${total} records across ${Object.keys(db).length} entities`);
}

function list(entitySlug: string) {
  const entity = entityDefinitions.find((item) => item.slug === entitySlug || item.name.toLowerCase() === entitySlug);
  if (!entity) {
    console.error(`Unknown entity: ${entitySlug}`);
    process.exitCode = 1;
    return;
  }
  console.table(load()[entity.name] ?? []);
}

if (command === 'seed' || command === 'init') {
  seed();
} else if (command === 'status' || command === 'doctor') {
  status();
} else if (command === 'list') {
  list(subject);
} else {
  console.log('Usage: npm run atlas -- seed | status | doctor | list <entity-slug>');
  console.log(`Entities: ${entityDefinitions.map((entity) => entity.slug).join(', ')}`);
}
"""


def _render_app_test_ts() -> str:
    return """import { describe, expect, it } from 'vitest';
import { entityDefinitions, seedData } from './data';

describe('generated PRD application', () => {
  it('contains seeded records for every extracted entity', () => {
    expect(entityDefinitions.length).toBeGreaterThan(0);
    for (const entity of entityDefinitions) {
      expect(seedData[entity.name]?.length ?? 0).toBeGreaterThan(0);
    }
  });
});
"""


def _normalize_planned_files(files: list[PlannedFile]) -> list[PlannedFile]:
    """Turn model-planned directory placeholders into safe file paths.

    Small local models often return a mixed plan such as ``src/cli`` and
    ``src/cli/index.ts``. Writing the first as a file makes the second crash on
    Windows, and writing extensionless module names also leaves projects that
    package tools do not recognize. Keep the model's intent, but make the paths
    usable before any filesystem mutation happens.
    """
    safe_files = _dedupe_files([file for file in files if _safe_rel_path(file.path)])
    original_paths = [file.path.rstrip("/") for file in safe_files]
    result: list[PlannedFile] = []
    for file in safe_files:
        normalized = _normalize_planned_path(file.path, original_paths)
        if normalized:
            result.append(PlannedFile(path=normalized, purpose=file.purpose))
    return _dedupe_files(result)


def _normalize_planned_path(path: str, all_paths: list[str]) -> str:
    cleaned = _safe_rel_path(path).rstrip("/")
    if not cleaned:
        return ""
    if _has_file_extension(cleaned) or _is_conventional_extensionless(cleaned):
        return cleaned

    prefix = f"{cleaned}/"
    children = [candidate for candidate in all_paths if candidate != cleaned and candidate.startswith(prefix)]
    basename = cleaned.rsplit("/", 1)[-1].lower()
    if children:
        if any(child.startswith(prefix + "index.") for child in children):
            return ""
        ext = _DIRECTORY_INDEX_EXTENSIONS.get(basename)
        if ext:
            candidate = f"{cleaned}/index{ext}"
            return "" if candidate in all_paths else candidate
        return ""

    if basename in _SKIP_EXTENSIONLESS_DIRS:
        return ""
    ext = _EXTENSIONLESS_FILE_REWRITES.get(basename)
    if ext:
        return f"{cleaned}{ext}"
    ext = _DIRECTORY_INDEX_EXTENSIONS.get(basename)
    if ext:
        return f"{cleaned}/index{ext}"
    if cleaned.startswith("src/"):
        return f"{cleaned}.ts"
    return cleaned


def _has_file_extension(path: str) -> bool:
    basename = path.rsplit("/", 1)[-1]
    return "." in basename.lstrip(".")


def _is_conventional_extensionless(path: str) -> bool:
    return path.rsplit("/", 1)[-1].lower() in _CONVENTIONAL_EXTENSIONLESS


def _first_file_parent(path: Path, root: Path) -> Path | None:
    for parent in path.parents:
        if parent == root.parent:
            break
        if parent == path:
            continue
        try:
            parent.relative_to(root)
        except ValueError:
            continue
        if parent.exists() and parent.is_file():
            return parent
    return None


def _sanitize_generated_content(content: str, path: str) -> str:
    """Remove common wrapper prose the model sometimes puts *inside* JSON.

    The structured response already says which file is being written, so a
    leading ``## package.json`` or fenced code block is always metadata, never
    valid file content. Keep this deliberately narrow so real source comments
    and shebangs survive.
    """
    text = content.strip().lstrip("\ufeff")
    if not path.lower().endswith(".md"):
        text = _extract_fenced_body(text)
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
    normalized_path = path.replace("\\", "/").lower()
    basename = normalized_path.rsplit("/", 1)[-1]
    while lines:
        first = lines[0].strip()
        if not first.startswith("#") or first.startswith("#!"):
            break
        lowered = first.lstrip("#").strip().lower()
        if lowered in {normalized_path, basename} or lowered.startswith(f"file: {basename}"):
            lines = lines[1:]
            continue
        break
    cleaned = "\n".join(lines).strip()
    if normalized_path.endswith(".json"):
        cleaned = _repair_wrapped_json(cleaned)
    if normalized_path.endswith(".py"):
        cleaned = _escape_python_literal_newlines(cleaned)
    return cleaned + "\n"


def _escape_python_literal_newlines(text: str) -> str:
    """Normalize model-produced newlines inside single-line Python strings.

    JSON decoding turns ``"\\n"`` into a real newline. Small models often use
    that when they meant the Python source to contain ``\n`` inside a quoted
    string, which creates an immediate SyntaxError. This keeps triple-quoted
    strings intact and only escapes newlines while inside ordinary quotes.
    """
    out: list[str] = []
    quote = ""
    triple = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if triple:
                if text.startswith(quote * 3, i):
                    out.append(quote * 3)
                    i += 3
                    quote = ""
                    triple = False
                    escaped = False
                    continue
                out.append(ch)
                i += 1
                continue
            if escaped:
                out.append(ch)
                escaped = False
                i += 1
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                i += 1
                continue
            if ch == quote:
                out.append(ch)
                quote = ""
                i += 1
                continue
            if ch == "\r" or ch == "\n":
                out.append("\\n")
                if ch == "\r" and i + 1 < len(text) and text[i + 1] == "\n":
                    i += 2
                else:
                    i += 1
                continue
            out.append(ch)
            i += 1
            continue
        if ch in {"'", '"'}:
            quote = ch
            if text.startswith(ch * 3, i):
                triple = True
                out.append(ch * 3)
                i += 3
            else:
                out.append(ch)
                i += 1
            escaped = False
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _extract_fenced_body(text: str) -> str:
    lines = text.splitlines()
    fence_start = -1
    fence_end = -1
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            if fence_start < 0:
                fence_start = index
            else:
                fence_end = index
    if fence_start >= 0 and fence_end > fence_start:
        before = "\n".join(lines[:fence_start]).strip()
        after = "\n".join(lines[fence_end + 1:]).strip()
        if not before or len(before) < 120:
            if not after or len(after) < 120:
                return "\n".join(lines[fence_start + 1:fence_end]).strip()
    return text


def _repair_wrapped_json(text: str) -> str:
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return text
    candidate = text[start:end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        try:
            parsed = repair_json(candidate, return_objects=True)
        except Exception:
            return text
    if isinstance(parsed, (dict, list)):
        return json.dumps(parsed, indent=2)
    return text


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
