"""DiagnosticDigest: turn one command's raw stdout/stderr into a compact,
deterministic ErrorPacket.

Parsing order (never LLM-first):
1. native structured output (JSON/JUnit the tool already emitted)
2. SARIF output, if the tool emitted it
3. a tool-specific fallback parser, for tools SHAMSU knows well (tsc,
   pytest, python tracebacks, ...) - the most accurate source of category/
   code/symbol information for those tools
4. the errorformat-style adapter, to catch any remaining `file:line:...`
   shaped lines the tool-specific parser didn't recognize
5. a fully generic fallback, only if nothing else matched anything
6. Drain3-style compaction of the raw log, applied only when the output
   looks like noisy runtime/dev-server spam rather than exact diagnostics

An LLM is never called by this module. `/diagnostics explain` renders the
deterministic root-cause reasoning as text; it does not ask a model.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.abstract.context import _names as _names_from
from shamsu.action_ledger.context import get_current_run
from shamsu.diagnostics import compact, normalize, root_cause
from shamsu.diagnostics.adapters import native_json, reviewdog_errorformat, sarif
from shamsu.diagnostics.parsers import (
    generic_fallback,
    node_runtime_fallback,
    python_fallback,
    pytest_fallback,
    typescript_fallback,
)
from shamsu.diagnostics.types import DiagnosticRecord, ErrorPacket
from shamsu.safety.commands import redact

_TS_TOOLS = {"tsc", "vite", "npm", "npm build", "npm dev", "npm test", "eslint"}


class DiagnosticDigest:
    def __init__(self, workspace_root: Path, memory_adapter=None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.memory_adapter = memory_adapter

    def run(
        self,
        command: str,
        cwd: Path | str,
        exit_code: int,
        stdout: str,
        stderr: str,
        raw_log_path: str = "",
    ) -> ErrorPacket:
        stdout = stdout or ""
        stderr = stderr or ""
        combined_text = "\n".join(part for part in (stdout, stderr) if part)
        tool, language = normalize.detect_tool(command, stdout, stderr)

        parser_chain: list[str] = []
        records = native_json.try_parse(tool, stdout) or native_json.try_parse(tool, stderr)
        if records:
            parser_chain.append("native_json")
        else:
            records = sarif.try_parse(stdout) or sarif.try_parse(stderr)
            if records:
                parser_chain.append("sarif")

        if not records:
            records, chain = self._fallback_parse(tool, combined_text)
            parser_chain.extend(chain)

        for record in records:
            record.tool = record.tool or tool
            record.language = record.language or language

        records = normalize.redact_records(records)
        root_diagnostics, secondary_diagnostics = root_cause.select_root_diagnostics(records)

        compact_log, removed = compact.build_compact_log(redact(combined_text), tool, len(records))
        snippets = compact.recommend_snippets(self.workspace_root, root_diagnostics)
        related_facts = self._codebase_memory_facts(root_diagnostics)
        classification, actionable, next_check = _classify_outcome(
            command, exit_code, combined_text, root_diagnostics
        )
        exception = next(
            (record for record in root_diagnostics if record.category in {"exception", "runtime_exception"}),
            None,
        )
        traceback_path = raw_log_path if exception and raw_log_path else ""

        return ErrorPacket(
            command=command,
            cwd=str(cwd),
            exit_code=exit_code,
            tool=tool,
            parser_chain=parser_chain,
            summary=normalize.build_summary(tool, exit_code, root_diagnostics),
            root_diagnostics=root_diagnostics,
            secondary_diagnostics=secondary_diagnostics,
            repeated_noise_removed=removed,
            target_files=normalize.collect_target_files(root_diagnostics or records),
            target_symbols=normalize.collect_target_symbols(root_diagnostics or records),
            related_code_facts=related_facts,
            recommended_snippets=snippets,
            compact_log=compact_log,
            raw_log_path=raw_log_path,
            classification=classification,
            actionable=actionable,
            exception_class=exception.code if exception else "",
            exception_message=exception.message if exception else "",
            traceback_path=traceback_path,
            suggested_next_check=next_check,
        )

    def _fallback_parse(self, tool: str, text: str) -> tuple[list[DiagnosticRecord], list[str]]:
        records: list[DiagnosticRecord] = []
        chain: list[str] = []

        if tool in _TS_TOOLS:
            records.extend(typescript_fallback.parse(text))
            records.extend(node_runtime_fallback.parse_node_runtime_errors(text))
            if records:
                chain.append("typescript_fallback")
        elif tool == "pytest":
            records.extend(pytest_fallback.parse_pytest_failures(text))
            if records:
                chain.append("pytest_fallback")
        elif tool in {"python", "django test"}:
            records.extend(python_fallback.parse_python_traceback(text))
            if records:
                chain.append("python_fallback")

        matched_excerpts = {record.raw_excerpt for record in records}
        errorformat_records = [
            record for record in reviewdog_errorformat.parse(text) if record.raw_excerpt not in matched_excerpts
        ]
        if errorformat_records:
            records.extend(errorformat_records)
            chain.append("reviewdog_errorformat")

        if not records:
            generic_records = generic_fallback.parse_generic_locations(text)
            if generic_records:
                records.extend(generic_records)
                chain.append("generic_fallback")

        return records, chain

    def _codebase_memory_facts(self, root_diagnostics: list[DiagnosticRecord]) -> list[str]:
        import_export_records = [
            record
            for record in root_diagnostics
            if record.category in {"missing_export", "import_export_mismatch", "runtime_missing_export"}
        ]
        if not import_export_records:
            return []
        if self.memory_adapter is None:
            return []

        health = self.memory_adapter.healthcheck(self.workspace_root)
        if not health.ok:
            return [f"Codebase-Memory MCP unavailable: {health.message}"]

        facts: list[str] = []
        for record in import_export_records:
            exporter_path = (
                compact.resolve_module_path(self.workspace_root, record.file, record.module)
                if record.module
                else ""
            )
            if exporter_path:
                exports = self.memory_adapter.get_exports(self.workspace_root, exporter_path)
                names = _names_from(exports)
                ledger = get_current_run()
                if ledger:
                    ledger.log_code_memory_queried("get_exports", exporter_path, len(names))
                if names:
                    facts.append(f"{exporter_path} exports: {', '.join(names[:12])}")
                    if record.symbol and record.symbol not in names:
                        similar = [name for name in names if name.lower() == record.symbol.lower()]
                        if similar:
                            facts.append(
                                f"Suggested fix: '{record.symbol}' not found, but '{similar[0]}' is exported "
                                f"(case mismatch) - alias or import '{similar[0]}' instead of renaming the export."
                            )
            if record.file:
                imports = self.memory_adapter.get_imports(self.workspace_root, record.file)
                names = _names_from(imports)
                ledger = get_current_run()
                if ledger:
                    ledger.log_code_memory_queried("get_imports", record.file, len(names))
                if names:
                    facts.append(f"{record.file} imports: {', '.join(names[:12])}")
        return facts


def _classify_outcome(
    command: str,
    exit_code: int,
    output: str,
    root_diagnostics: list[DiagnosticRecord],
) -> tuple[str, bool, str]:
    if exit_code == 0:
        return "success", False, ""
    lowered_command = " ".join(command.lower().split())
    lowered_output = output.lower()
    if exit_code in {125, 126}:
        return "policy_decision", False, "Review the approval or command safety policy."
    if exit_code == 127:
        return "environment_condition", False, "Check the workspace and working directory."
    git_probe = lowered_command.startswith(
        ("git status", "git diff", "git log", "git rev-parse", "git branch")
    )
    if git_probe and "not a git repository" in lowered_output:
        return (
            "expected_condition",
            False,
            "Run `git init` only if this workspace should be version controlled.",
        )
    if root_diagnostics:
        first = root_diagnostics[0]
        location = f"{first.file}:{first.line}" if first.file else "the first root diagnostic"
        return "command_failure", True, f"Inspect {location}, then rerun `{command}`."
    return "command_failure", True, f"Inspect the raw command log, then rerun `{command}`."
