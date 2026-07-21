from __future__ import annotations

from pathlib import Path

from shamsu.diagnostics.digest import DiagnosticDigest


class FakeMemoryAdapter:
    """Fake Codebase-Memory MCP adapter - never touches the real upstream
    binary. Mirrors tests/test_abstract_service.py's FakeCodebaseMemoryAdapter."""

    def __init__(self, available: bool = True, exports=None, imports=None) -> None:
        self.available = available
        self._exports = exports or {}
        self._imports = imports or {}
        self.export_calls: list[str] = []
        self.import_calls: list[str] = []

    def healthcheck(self, workspace: Path):
        class _Health:
            def __init__(self, ok: bool) -> None:
                self.ok = ok
                self.message = "ready" if ok else "Codebase-Memory MCP unavailable in tests."

        return _Health(self.available)

    def get_exports(self, workspace: Path, path: str) -> dict:
        self.export_calls.append(path)
        names = self._exports.get(path, [])
        return {"ok": True, "results": [{"name": name} for name in names]}

    def get_imports(self, workspace: Path, path: str) -> dict:
        self.import_calls.append(path)
        names = self._imports.get(path, [])
        return {"ok": True, "results": [{"name": name} for name in names]}


def test_digest_command_output_converts_to_compact_error_packet(tmp_path: Path):
    digest = DiagnosticDigest(tmp_path)

    packet = digest.run(
        "npm run build",
        tmp_path,
        1,
        "",
        "src/game/rules.ts(71,17): error TS1005: ')' expected.",
        raw_log_path="/fake/session/events.jsonl",
    )

    assert packet.command == "npm run build"
    assert packet.exit_code == 1
    assert packet.root_diagnostics
    assert packet.root_diagnostics[0].code == "TS1005"
    assert packet.raw_log_path == "/fake/session/events.jsonl"


def test_digest_packet_includes_parser_chain(tmp_path: Path):
    digest = DiagnosticDigest(tmp_path)

    packet = digest.run("tsc", tmp_path, 1, "", "src/x.ts(1,1): error TS1005: ')' expected.")

    assert packet.parser_chain == ["typescript_fallback"]


def test_digest_classifies_non_repository_git_probe_as_expected_condition(tmp_path: Path):
    packet = DiagnosticDigest(tmp_path).run(
        "git status --short",
        tmp_path,
        128,
        "",
        "fatal: not a git repository (or any of the parent directories): .git",
    )

    assert packet.classification == "expected_condition"
    assert packet.actionable is False
    assert "git init" in packet.suggested_next_check


def test_digest_preserves_exception_identity_and_traceback_artifact_path(tmp_path: Path):
    stderr = (
        "Traceback (most recent call last):\n"
        f'  File "{tmp_path / "app.py"}", line 4, in main\n'
        "ValueError: broken value\n"
    )

    packet = DiagnosticDigest(tmp_path).run(
        "python app.py", tmp_path, 1, "", stderr, raw_log_path="commands/cmd_000.stderr.log"
    )

    assert packet.classification == "command_failure"
    assert packet.actionable is True
    assert packet.exception_class == "ValueError"
    assert packet.exception_message == "broken value"
    assert packet.traceback_path == "commands/cmd_000.stderr.log"
    assert packet.target_files


def test_digest_compact_log_removes_repeated_npm_lifecycle_noise(tmp_path: Path):
    digest = DiagnosticDigest(tmp_path)
    stderr = (
        "npm notice New minor version of npm available\n"
        "> myapp@1.0.0 build\n"
        "> tsc\n"
        "src/x.ts(1,1): error TS1005: ')' expected.\n"
        "npm ERR! code ELIFECYCLE\n"
    )

    packet = digest.run("npm run build", tmp_path, 1, "", stderr)

    assert "npm notice" not in packet.compact_log
    assert "npm ERR!" not in packet.compact_log
    assert "TS1005" in packet.compact_log
    assert packet.repeated_noise_removed >= 4


def test_digest_succeeds_quietly_on_zero_exit_with_no_diagnostics(tmp_path: Path):
    digest = DiagnosticDigest(tmp_path)

    packet = digest.run("npm test", tmp_path, 0, "All tests passed", "")

    assert packet.ok is True
    assert packet.root_diagnostics == []


def test_digest_recommends_target_files_and_snippets_from_file_and_line(tmp_path: Path):
    project_file = tmp_path / "rules.ts"
    project_file.write_text("\n".join(f"line {i}" for i in range(1, 120)), encoding="utf-8")
    digest = DiagnosticDigest(tmp_path)

    packet = digest.run("tsc", tmp_path, 1, "", "rules.ts(71,17): error TS1005: ')' expected.")

    assert packet.target_files == ["rules.ts"]
    assert packet.recommended_snippets
    snippet = packet.recommended_snippets[0]
    assert snippet.file == "rules.ts"
    assert snippet.line_start == 41
    assert snippet.line_end == 101


def test_digest_redacts_secrets_in_records_and_compact_log(tmp_path: Path):
    digest = DiagnosticDigest(tmp_path)
    stderr = "app.py:1: error: SECRET_KEY = \"django-insecure-secret\"\n"

    packet = digest.run("python app.py", tmp_path, 1, "", stderr)

    assert "django-insecure-secret" not in packet.compact_log
    assert all("django-insecure-secret" not in record.message for record in packet.root_diagnostics)


def test_digest_queries_codebase_memory_for_import_export_errors(tmp_path: Path):
    (tmp_path / "session.ts").write_text("import { GameLoop } from './loop';\n", encoding="utf-8")
    (tmp_path / "loop.ts").write_text("export function gameLoop() {}\n", encoding="utf-8")
    adapter = FakeMemoryAdapter(available=True, exports={"loop.ts": ["gameLoop"]})
    digest = DiagnosticDigest(tmp_path, memory_adapter=adapter)
    stderr = "session.ts(1,10): error TS2305: Module '\"./loop\"' has no exported member named 'GameLoop'. Did you mean 'gameLoop'?"

    packet = digest.run("npm run build", tmp_path, 1, "", stderr)

    assert adapter.export_calls == ["loop.ts"]
    assert any("gameLoop" in fact for fact in packet.related_code_facts)
    assert any("case mismatch" in fact for fact in packet.related_code_facts)


def test_digest_reports_codebase_memory_unavailable_honestly_without_faking_facts(tmp_path: Path):
    adapter = FakeMemoryAdapter(available=False)
    digest = DiagnosticDigest(tmp_path, memory_adapter=adapter)
    stderr = "Module '\"./loop\"' has no exported member 'GameLoop'."

    packet = digest.run("npm run build", tmp_path, 1, "", stderr)

    assert packet.related_code_facts
    assert "unavailable" in packet.related_code_facts[0].lower()
    assert adapter.export_calls == []


def test_digest_skips_codebase_memory_lookup_when_no_import_export_error(tmp_path: Path):
    adapter = FakeMemoryAdapter(available=True)
    digest = DiagnosticDigest(tmp_path, memory_adapter=adapter)

    packet = digest.run("tsc", tmp_path, 1, "", "a.ts(1,1): error TS1005: ')' expected.")

    assert packet.related_code_facts == []
    assert adapter.export_calls == []
