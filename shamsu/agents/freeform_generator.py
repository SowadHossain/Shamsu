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
                operation_reason="deterministic Vite/React foundation from PRD contract",
            ):
                touched.append(path)
        if touched:
            self._log("freeform.vite_react_hardened", {"files": touched})
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
    return cleaned + "\n"


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
