"""Strict RepairLoop: verify -> select one root error -> propose minimal
patch -> re-verify -> keep or roll back.

This is the general (not Django-specific) code-fixing feedback loop. It never
trusts the model's self-report: only the verifier's exit code and the
diagnostic error set decide whether a patch is kept. Every dependency is
injected so the control flow is unit-testable without Ollama or a real build.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from shamsu.action_ledger.context import get_current_run
from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.diagnostics.types import ErrorPacket
from shamsu.patch.rollback import rollback_transaction
from shamsu.patch.transactions import TransactionWorkspace
from shamsu.repair.action_blocker import RepeatedActionBlocker, action_signature
from shamsu.repair.comparator import ErrorComparator, RepairOutcome
from shamsu.repair.import_resolver import suggest_import_fix
from shamsu.repair.kinds import ErrorKind, RepairError, repair_errors_from_packet, select_primary_error
from shamsu.repair.prompt import build_final_message, enforce_final_response
from shamsu.repair.types import (
    DebugContext,
    InspectedSnippet,
    PreviousAttempt,
    RepairAttemptRecord,
    RepairPlan,
    RepairResult,
)
from shamsu.session.manager import SessionLogger

if TYPE_CHECKING:
    from shamsu.action_ledger.ledger import ActionLedger

_INSPECT_WINDOW = 40
_IMPORT_KINDS = {ErrorKind.IMPORT_ERROR, ErrorKind.MODULE_NOT_FOUND}
_TEST_FILE_RE = re.compile(r"(?:^|/)(?:tests?/|test_[^/]+\.py$)|\.(?:test|spec)\.[cm]?[jt]sx?$", re.I)
_JS_IMPORT_RE = re.compile(
    r"(?:from\s+|import\s*\(\s*|require\s*\(\s*)['\"](?P<module>\.{1,2}/[^'\"]+)['\"]"
)


@dataclass(frozen=True)
class VerifyRun:
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""


class Verifier(Protocol):
    @property
    def command(self) -> str: ...
    def run(self) -> VerifyRun: ...


class Proposer(Protocol):
    """Wraps the strict-debug model call. Returns None when the model has no
    actionable plan (e.g. it needs a file it cannot see)."""
    def propose(self, context: DebugContext) -> RepairPlan | None: ...


class Applier(Protocol):
    """Applies a RepairPlan to disk and returns the changed workspace-relative
    files. The loop wraps this in a transaction, so the applier itself does
    not manage backups/rollback."""
    def apply(self, plan: RepairPlan) -> list[str]: ...


class FileApplier:
    """Default applier: search/replace or full-content overwrite of a single
    file, inside the workspace sandbox."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()

    def apply(self, plan: RepairPlan) -> list[str]:
        target = (self.workspace_root / plan.target_file).resolve()
        target.relative_to(self.workspace_root)  # raises if outside sandbox
        if plan.full_content:
            new_content = plan.full_content
        elif plan.search:
            if not target.is_file():
                return []
            current = target.read_text(encoding="utf-8", errors="replace")
            if plan.search not in current:
                return []
            new_content = current.replace(plan.search, plan.replace, 1)
        else:
            return []
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(new_content, encoding="utf-8")
        return [plan.target_file]


class RepairLoop:
    def __init__(
        self,
        workspace_root: Path,
        verifier: Verifier,
        proposer: Proposer,
        applier: Applier | None = None,
        max_attempts: int = 4,
        session_logger: SessionLogger | None = None,
        digest: DiagnosticDigest | None = None,
        action_ledger: "ActionLedger | None" = None,
        editable_files: list[str] | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.verifier = verifier
        self.proposer = proposer
        self.applier = applier or FileApplier(self.workspace_root)
        self.max_attempts = max_attempts
        self.session_logger = session_logger
        self.digest = digest or DiagnosticDigest(self.workspace_root)
        self.action_ledger = action_ledger if action_ledger is not None else get_current_run()
        self.verifier_id = (
            self.action_ledger.verifier_id_for(self.verifier.command, "repair_loop")
            if self.action_ledger
            else ""
        )
        self.comparator = ErrorComparator()
        self.blocker = RepeatedActionBlocker()
        self.transactions = TransactionWorkspace(self.workspace_root)
        self.editable_files = (
            {path for item in editable_files if (path := self._relative_path(item))}
            if editable_files is not None
            else None
        )

    def run(self) -> RepairResult:
        attempts: list[RepairAttemptRecord] = []
        history: list[PreviousAttempt] = []

        before_packet = self._verify_and_digest()
        if before_packet.exit_code == 0 and not before_packet.root_diagnostics:
            return RepairResult(
                success=True,
                exit_code=0,
                attempts=attempts,
                final_message=build_final_message(0, 0, ""),
            )
        before_errors = repair_errors_from_packet(before_packet)

        stopped_reason = ""
        for index in range(1, self.max_attempts + 1):
            primary = select_primary_error(before_errors)
            if primary is None:
                stopped_reason = "no actionable root error"
                break

            context = self._build_context(primary, history)
            plan = self.proposer.propose(context)
            if plan is None or not plan.has_edit:
                stopped_reason = "model produced no actionable repair plan"
                break

            normalized_target = self._relative_path(plan.target_file)
            if not normalized_target:
                stopped_reason = f"model proposed a path outside the workspace: {plan.target_file}"
                break
            if normalized_target != plan.target_file:
                plan = RepairPlan(
                    root_cause=plan.root_cause,
                    target_file=normalized_target,
                    search=plan.search,
                    replace=plan.replace,
                    full_content=plan.full_content,
                    inspected_files=plan.inspected_files,
                )

            # Enforce: never edit read-only evidence or an uninspected file.
            inspected_files = {snippet.file for snippet in context.inspected}
            if plan.target_file not in inspected_files:
                stopped_reason = (
                    f"model tried to edit an uninspected file: {plan.target_file}"
                )
                break
            if plan.target_file not in set(context.editable_files):
                stopped_reason = f"model tried to edit a read-only evidence file: {plan.target_file}"
                break

            relevance_error = _plan_relevance_error(primary, plan)
            if relevance_error:
                deterministic_plan = _deterministic_unique_fixture_plan(
                    self.workspace_root, primary, plan.target_file
                )
                if deterministic_plan is not None:
                    plan = deterministic_plan
                    self._log(
                        "repair.deterministic_plan",
                        {
                            "target_file": plan.target_file,
                            "reason": plan.root_cause,
                            "rejected_model_plan": relevance_error,
                        },
                    )
                    if self.action_ledger:
                        self.action_ledger.log_event(
                            "deterministic_repair_plan_created",
                            target_file=plan.target_file,
                            reason=plan.root_cause,
                            rejected_model_plan=relevance_error,
                        )
                else:
                    before_signature = primary.signature()
                    history.append(
                        PreviousAttempt(
                            files_changed=[],
                            before_signature=before_signature,
                            after_signature=before_signature,
                            outcome=RepairOutcome.UNCHANGED,
                            note=relevance_error,
                        )
                    )
                    self._record(
                        attempts,
                        index,
                        [],
                        before_signature,
                        before_signature,
                        RepairOutcome.UNCHANGED,
                        kept=False,
                        note=relevance_error,
                    )
                    continue

            signature = action_signature([plan.target_file], plan.payload())
            if self.blocker.is_blocked(signature):
                stopped_reason = "repeated identical patch blocked; needs a different strategy"
                self._log("repair.blocked", {"signature": signature, "file": plan.target_file})
                break

            before_signature = primary.signature()
            transaction_id = self.transactions.begin(
                reason=f"RepairLoop attempt {index}: {plan.root_cause[:120]}",
                operations=[{"op": "edit_file", "path": plan.target_file, "dest_path": "", "reason": ""}],
                destructive=False,
            )
            self.transactions.backup_file(transaction_id, plan.target_file)
            changed = self.applier.apply(plan)
            if not changed:
                rollback_transaction(self.workspace_root, transaction_id)
                self.blocker.record_failure(signature)
                stopped_reason = "patch did not apply (search block not found)"
                self._record(attempts, index, [], before_signature, before_signature,
                             RepairOutcome.UNCHANGED, kept=False, note="apply failed")
                break
            for changed_file in changed:
                self.transactions.record_after(transaction_id, changed_file)

            after_packet = self._verify_and_digest()
            after_errors = repair_errors_from_packet(after_packet)
            outcome = self.comparator.compare(before_errors, after_errors, after_packet.exit_code)
            after_signature = _packet_signature(after_packet)

            if self.comparator.is_progress(outcome):
                self.transactions.save_verification(
                    transaction_id, {"exit_code": after_packet.exit_code, "outcome": outcome.value}
                )
                self.transactions.finalize(transaction_id, "applied")
                self.blocker.reset(signature)
                self._record(attempts, index, changed, before_signature, after_signature,
                             outcome, kept=True)
                if outcome is RepairOutcome.SOLVED:
                    return RepairResult(
                        success=True,
                        exit_code=0,
                        attempts=attempts,
                        final_message=build_final_message(0, index, ""),
                    )
                before_errors = after_errors
                before_packet = after_packet
                continue

            # WORSE / UNCHANGED / DIFFERENT -> roll the patch back.
            rollback_transaction(self.workspace_root, transaction_id)
            self.blocker.record_failure(signature)
            self._record(attempts, index, changed, before_signature, after_signature,
                         outcome, kept=False, note="rolled back")
            history.append(PreviousAttempt(
                files_changed=changed,
                before_signature=before_signature,
                after_signature=after_signature,
                outcome=outcome,
                note=_failed_plan_note(plan),
            ))

        # Exhausted or stopped: report from verifier ground truth only.
        final_packet = self._verify_and_digest()
        remaining = repair_errors_from_packet(final_packet)
        remaining_primary = select_primary_error(remaining)
        remaining_summary = remaining_primary.message if remaining_primary else ""
        final_message = build_final_message(final_packet.exit_code, len(attempts), remaining_summary)
        final_message = enforce_final_response(final_message, final_packet.exit_code)
        success = final_packet.exit_code == 0 and not final_packet.root_diagnostics
        return RepairResult(
            success=success,
            exit_code=final_packet.exit_code,
            attempts=attempts,
            final_message=final_message,
            remaining_errors=remaining,
            stopped_reason=stopped_reason,
        )

    # -- helpers ---------------------------------------------------------------

    def _verify_and_digest(self) -> ErrorPacket:
        if self.action_ledger:
            self.action_ledger.log_verification_started(
                self.verifier.command,
                verifier_id=self.verifier_id,
                source="repair_loop",
                required=True,
            )
        run = self.verifier.run()
        packet = self.digest.run(
            command=run.command,
            cwd=self.workspace_root,
            exit_code=run.exit_code,
            stdout=run.stdout,
            stderr=run.stderr,
        )
        if self.action_ledger:
            self.action_ledger.log_verification_result(
                run.exit_code == 0 and not packet.root_diagnostics,
                packet.summary,
                command=run.command,
                verifier_id=self.verifier_id,
                source="repair_loop",
                required=True,
                exit_code=run.exit_code,
            )
        return packet

    def _build_context(
        self, primary: RepairError, history: list[PreviousAttempt]
    ) -> DebugContext:
        suggestion = ""
        if primary.kind in _IMPORT_KINDS and primary.module and primary.file:
            fix = suggest_import_fix(self.workspace_root, primary.file, primary.module)
            if fix:
                suggestion = fix.describe()
        inspected = self._inspect(primary)
        inspected_files = {snippet.file for snippet in inspected}
        editable = self.editable_files if self.editable_files is not None else inspected_files
        return DebugContext(
            primary_error=primary,
            verify_command=self.verifier.command,
            inspected=inspected,
            editable_files=sorted(editable & inspected_files),
            previous_attempts=list(history),
            import_suggestion=suggestion,
        )

    def _inspect(self, primary: RepairError) -> list[InspectedSnippet]:
        snippets: list[InspectedSnippet] = []
        seen: set[str] = set()
        for file_path, line in self._files_to_inspect(primary):
            snippet = self._read_window(file_path, line)
            if snippet and snippet.file not in seen:
                seen.add(snippet.file)
                snippets.append(snippet)
        return snippets

    def _files_to_inspect(self, primary: RepairError) -> list[tuple[str, int]]:
        targets: list[tuple[str, int]] = []
        if primary.file:
            targets.append((primary.file, primary.line or 1))
        # For export/symbol errors, also inspect the module that should export it.
        if primary.module:
            from shamsu.diagnostics.compact import resolve_module_path

            resolved = resolve_module_path(self.workspace_root, primary.file, primary.module)
            if resolved:
                targets.append((resolved, 1))
        targets.extend(self._related_test_dependencies(primary.file))
        return targets

    def _read_window(self, file_path: str, line: int) -> InspectedSnippet | None:
        raw_target = Path(file_path)
        target = raw_target.resolve() if raw_target.is_absolute() else (self.workspace_root / raw_target).resolve()
        try:
            relative = target.relative_to(self.workspace_root).as_posix()
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError):
            return None
        start = max(line - _INSPECT_WINDOW, 1)
        end = min(line + _INSPECT_WINDOW, len(lines))
        content = "\n".join(lines[start - 1:end])
        return InspectedSnippet(
            file=relative, line_start=start, line_end=end, content=content
        )

    def _relative_path(self, file_path: str) -> str:
        """Return one normalized workspace-relative path, or empty if unsafe."""
        try:
            raw = Path(str(file_path).replace("\\", "/"))
            target = raw.resolve() if raw.is_absolute() else (self.workspace_root / raw).resolve()
            return target.relative_to(self.workspace_root).as_posix()
        except (OSError, ValueError):
            return ""

    def _related_test_dependencies(self, file_path: str) -> list[tuple[str, int]]:
        """Inspect bounded local dependencies when a failure points into a test.

        Tests frequently fail because their fixture no longer matches a model,
        schema, component, or API. Supplying the imported implementation as
        read-only evidence lets a small model repair the test without granting
        permission to rewrite production code.
        """
        relative = self._relative_path(file_path)
        if not relative or not _TEST_FILE_RE.search(relative):
            return []
        source_path = self.workspace_root / relative
        try:
            source = source_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        if source_path.suffix == ".py":
            return self._python_test_dependencies(source_path, source)
        if source_path.suffix.lower() in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
            return self._js_test_dependencies(source_path, source)
        return []

    def _python_test_dependencies(self, source_path: Path, source: str) -> list[tuple[str, int]]:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        targets: list[tuple[str, int]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level <= 0:
                continue
            base = source_path.parent
            for _ in range(node.level - 1):
                base = base.parent
            module_parts = (node.module or "").split(".") if node.module else []
            candidate = base.joinpath(*module_parts)
            resolved = self._resolve_python_module(candidate)
            if not resolved:
                continue
            line = self._python_symbol_line(resolved, [alias.name for alias in node.names])
            targets.append((str(resolved), line))
            if len(targets) >= 3:
                break
        return targets

    @staticmethod
    def _resolve_python_module(candidate: Path) -> Path | None:
        file_candidate = candidate.with_suffix(".py")
        if file_candidate.is_file():
            return file_candidate
        package_candidate = candidate / "__init__.py"
        return package_candidate if package_candidate.is_file() else None

    @staticmethod
    def _python_symbol_line(file_path: Path, symbols: list[str]) -> int:
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            return 1
        wanted = set(symbols)
        lines = [
            node.lineno
            for node in tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in wanted
        ]
        return min(lines, default=1)

    def _js_test_dependencies(self, source_path: Path, source: str) -> list[tuple[str, int]]:
        targets: list[tuple[str, int]] = []
        extensions = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
        for match in _JS_IMPORT_RE.finditer(source):
            candidate = (source_path.parent / match.group("module")).resolve()
            choices = [candidate] if candidate.suffix else [candidate.with_suffix(ext) for ext in extensions]
            choices.extend(candidate / f"index{ext}" for ext in extensions)
            resolved = next((choice for choice in choices if choice.is_file()), None)
            if resolved and self._relative_path(str(resolved)):
                targets.append((str(resolved), 1))
            if len(targets) >= 3:
                break
        return targets

    def _record(
        self,
        attempts: list[RepairAttemptRecord],
        index: int,
        changed: list[str],
        before_signature: str,
        after_signature: str,
        outcome: RepairOutcome,
        kept: bool,
        note: str = "",
    ) -> None:
        record = RepairAttemptRecord(
            index=index,
            files_changed=changed,
            before_signature=before_signature,
            after_signature=after_signature,
            outcome=outcome,
            kept=kept,
            note=note,
        )
        attempts.append(record)
        self._log(
            "repair.attempt",
            {
                "index": index,
                "files_changed": changed,
                "before_signature": before_signature,
                "after_signature": after_signature,
                "outcome": outcome.value,
                "kept": kept,
                "note": note,
            },
        )
        if self.action_ledger:
            self.action_ledger.log_repair_attempt(
                attempt_index=index,
                outcome=outcome.value,
                kept=kept,
                files_changed=changed,
                before_signature=before_signature,
                after_signature=after_signature,
                note=note,
                verifier_id=self.verifier_id,
                command=self.verifier.command,
            )

    def _log(self, event_type: str, payload: dict) -> None:
        if self.session_logger:
            self.session_logger.log(
                event_type, payload, f"RepairLoop: {event_type}", workflow_id="repair-loop"
            )


def _packet_signature(packet: ErrorPacket) -> str:
    errors = repair_errors_from_packet(packet)
    primary = select_primary_error(errors)
    if primary is not None:
        return primary.signature()
    return f"exit={packet.exit_code}"


def _failed_plan_note(plan: RepairPlan, limit: int = 600) -> str:
    """Give the next repair iteration the concrete strategy that was rejected."""
    if plan.search:
        detail = f"rolled back search {plan.search!r} -> {plan.replace!r}"
    else:
        detail = f"rolled back full_content rewrite for {plan.target_file}"
    if len(detail) <= limit:
        return detail
    return detail[:limit].rstrip() + "...<truncated>"


def _plan_relevance_error(primary: RepairError, plan: RepairPlan) -> str:
    """Reject patches that plainly change an unrelated field for a named constraint."""
    match = re.search(
        r"constraint failed:\s*[\w.]+\.(?P<field>[A-Za-z_]\w*)",
        primary.message,
        re.IGNORECASE,
    )
    if not match:
        return ""
    field = match.group("field")
    fixture_field = field.removesuffix("_id")
    replacement = plan.full_content or plan.replace
    if re.search(rf"\b{re.escape(fixture_field)}\b", replacement):
        return ""
    # Reusing/removing a duplicate constructor can resolve a uniqueness error
    # without spelling the constrained field in the replacement.
    if ".objects.create" in plan.search and ".objects.create" not in replacement:
        return ""
    return (
        f"rejected before apply: verifier names constraint field `{field}`, but the patch "
        f"does not modify fixture field `{fixture_field}`. Changing an unrelated field "
        "cannot resolve this diagnostic; use a different strategy."
    )


def _deterministic_unique_fixture_plan(
    workspace_root: Path, primary: RepairError, target_file: str
) -> RepairPlan | None:
    """Add a named UNIQUE text field to the matching constructor in a Python test."""
    match = re.search(
        r"unique constraint failed:\s*(?P<table>[\w.]+)\.(?P<field>[A-Za-z_]\w*)",
        primary.message,
        re.IGNORECASE,
    )
    normalized_target = target_file.replace("\\", "/")
    if not match or not _TEST_FILE_RE.search(normalized_target) or not normalized_target.endswith(".py"):
        return None
    field = match.group("field")
    if field not in {"username", "email", "slug", "code", "name"}:
        return None
    entity = match.group("table").split(".")[-1].split("_")[-1]
    target = (workspace_root / normalized_target).resolve()
    try:
        target.relative_to(workspace_root)
        source = target.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, ValueError, SyntaxError):
        return None

    candidates: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _call_creates_entity(node, entity):
            continue
        end_line = getattr(node, "end_lineno", node.lineno)
        if primary.line and not (node.lineno <= primary.line <= end_line):
            continue
        if any(keyword.arg == field for keyword in node.keywords):
            continue
        candidates.append(node)
    if not candidates:
        return None
    call = min(
        candidates,
        key=lambda node: (
            getattr(node, "end_lineno", node.lineno) - node.lineno,
            getattr(node, "end_col_offset", 0) - node.col_offset,
        ),
    )
    search = ast.get_source_segment(source, call)
    if not search:
        return None
    try:
        replacement_call = ast.parse(search, mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(replacement_call, ast.Call):
        return None
    value = f"test_{field}_{primary.line or call.lineno}"
    if field == "email":
        value = f"test_{primary.line or call.lineno}@example.com"
    replacement_call.keywords.append(ast.keyword(arg=field, value=ast.Constant(value=value)))
    return RepairPlan(
        root_cause=(
            f"Harness fallback: add unique `{field}` fixture value to the failing "
            f"{entity} constructor named by the verifier."
        ),
        target_file=normalized_target,
        search=search,
        replace=ast.unparse(replacement_call),
    )


def _call_creates_entity(node: ast.Call, entity: str) -> bool:
    function = node.func
    if not isinstance(function, ast.Attribute) or function.attr != "create":
        return False
    manager = function.value
    if not isinstance(manager, ast.Attribute) or manager.attr != "objects":
        return False
    owner = manager.value
    return isinstance(owner, ast.Name) and owner.id.lower() == entity.lower()
