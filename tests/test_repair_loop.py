"""Tests for the strict code-repair feedback loop (shamsu/repair)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import start_run
from shamsu.action_ledger import store
from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.repair.comparator import ErrorComparator, RepairOutcome
from shamsu.repair.import_resolver import suggest_import_fix
from shamsu.repair.kinds import (
    ErrorKind,
    RepairError,
    repair_errors_from_packet,
    select_primary_error,
)
from shamsu.repair.loop import RepairLoop, VerifyRun
from shamsu.repair.proposer_llm import LLMProposer
from shamsu.repair.prompt import (
    contains_unverified_success_claim,
    enforce_final_response,
)
from shamsu.repair.types import DebugContext, RepairPlan
from shamsu.repair.verifiers import CommandVerifier


# --- fakes --------------------------------------------------------------------

class ScriptedVerifier:
    """Returns a queued (exit_code, output) per run; last entry repeats."""

    def __init__(self, command: str, runs: list[tuple[int, str]]) -> None:
        self.command = command
        self._runs = runs
        self.calls = 0

    def run(self) -> VerifyRun:
        idx = min(self.calls, len(self._runs) - 1)
        self.calls += 1
        exit_code, output = self._runs[idx]
        return VerifyRun(command=self.command, exit_code=exit_code, stdout=output)


class ScriptedProposer:
    """Yields a queued RepairPlan per propose() call."""

    def __init__(self, plans: list[RepairPlan | None]) -> None:
        self._plans = plans
        self.calls = 0
        self.seen: list[DebugContext] = []

    def propose(self, context: DebugContext) -> RepairPlan | None:
        self.seen.append(context)
        plan = self._plans[min(self.calls, len(self._plans) - 1)]
        self.calls += 1
        return plan


TSC_ERR = 'src/app.ts(10,4): error TS2305: Module \'"./util"\' has no exported member \'foo\'.'
TSC_ERR_2 = 'src/app.ts(12,4): error TS2304: Cannot find name \'bar\'.'


# --- ErrorPacket / classification --------------------------------------------

def test_tsc_error_classified_as_export_error(tmp_path: Path):
    packet = DiagnosticDigest(tmp_path).run("tsc", tmp_path, 2, TSC_ERR, "")
    errors = repair_errors_from_packet(packet)
    assert errors
    assert errors[0].kind is ErrorKind.EXPORT_ERROR
    assert errors[0].code == "TS2305"
    assert errors[0].file == "src/app.ts"
    assert errors[0].line == 10
    assert errors[0].symbol == "foo"


def test_vite_import_error_parsed_and_classified(tmp_path: Path):
    log = 'Failed to resolve import "./ui/Hud" from "src/ui/index.ts". Does the file exist?'
    packet = DiagnosticDigest(tmp_path).run("npm run dev", tmp_path, 1, log, "")
    errors = repair_errors_from_packet(packet)
    assert errors
    primary = errors[0]
    assert primary.kind is ErrorKind.IMPORT_ERROR
    assert primary.file == "src/ui/index.ts"
    assert primary.module == "./ui/Hud"


def test_jsx_error_classified_and_carries_location(tmp_path: Path):
    log = "src/ui/App.tsx(12,7): error TS17004: Cannot use JSX unless the '--jsx' flag is provided."
    packet = DiagnosticDigest(tmp_path).run("tsc", tmp_path, 2, log, "")
    errors = repair_errors_from_packet(packet)
    assert errors
    assert errors[0].kind is ErrorKind.JSX_ERROR
    assert errors[0].file == "src/ui/App.tsx"
    assert errors[0].line == 12


def test_py_compile_syntax_error_becomes_repairable_root(tmp_path: Path):
    log = (
        '  File "ledgerlite.py", line 94\n'
        "    f.write('id,category,amount,note\n"
        "            ^\n"
        "SyntaxError: unterminated string literal (detected at line 94)\n"
    )

    packet = DiagnosticDigest(tmp_path).run("python -m py_compile ledgerlite.py", tmp_path, 1, "", log)
    errors = repair_errors_from_packet(packet)
    primary = select_primary_error(errors)

    assert packet.parser_chain == ["python_fallback"]
    assert primary is not None
    assert primary.kind is ErrorKind.SYNTAX_ERROR
    assert primary.file == "ledgerlite.py"
    assert primary.line == 94


def test_npm_missing_script_parsed(tmp_path: Path):
    log = 'npm ERR! Missing script: "build"\nnpm ERR! code ELIFECYCLE'
    packet = DiagnosticDigest(tmp_path).run("npm run build", tmp_path, 1, log, "")
    errors = repair_errors_from_packet(packet)
    assert errors
    primary = errors[0]
    assert primary.tool == "npm"
    assert "build" in primary.symbol or "build" in primary.message


def test_npm_missing_package_is_module_not_found(tmp_path: Path):
    log = "npm ERR! code E404\nnpm ERR! 404 Not Found - GET https://registry.npmjs.org/nope - 'nope@1.0.0'"
    packet = DiagnosticDigest(tmp_path).run("npm install", tmp_path, 1, log, "")
    errors = repair_errors_from_packet(packet)
    kinds = {e.kind for e in errors}
    assert ErrorKind.MODULE_NOT_FOUND in kinds


def test_tsc_error_beats_npm_lifecycle_as_root(tmp_path: Path):
    # The real compiler error must be the root cause, not the npm wrapper noise.
    log = (
        "src/app.ts(10,4): error TS2305: Module './util' has no exported member 'foo'.\n"
        "npm ERR! code ELIFECYCLE\nnpm ERR! Exit status 2"
    )
    packet = DiagnosticDigest(tmp_path).run("npm run build", tmp_path, 1, log, "")
    errors = repair_errors_from_packet(packet)
    primary = select_primary_error(errors)
    assert primary is not None
    assert primary.kind is ErrorKind.EXPORT_ERROR
    assert primary.file == "src/app.ts"


def test_primary_selector_prefers_syntax_then_import(tmp_path: Path):
    syntax = RepairError("tsc", 2, "tsc", ErrorKind.SYNTAX_ERROR, "a.ts", 3, 1, "TS1005", "", "", "')' expected", "", "error")
    imp = RepairError("tsc", 2, "tsc", ErrorKind.IMPORT_ERROR, "b.ts", 1, 1, "", "", "./x", "cannot resolve", "", "error")
    typ = RepairError("tsc", 2, "tsc", ErrorKind.TYPE_ERROR, "c.ts", 9, 1, "TS7006", "", "", "implicitly any", "", "error")
    assert select_primary_error([typ, imp, syntax]) is syntax
    assert select_primary_error([typ, imp]) is imp


def test_implicit_any_is_lowest_priority(tmp_path: Path):
    implicit = RepairError("tsc", 2, "tsc", ErrorKind.TYPE_ERROR, "c.ts", 9, 1, "TS7006",
                           "", "", "Parameter 'x' implicitly has an 'any' type", "", "error")
    export = RepairError("tsc", 2, "tsc", ErrorKind.EXPORT_ERROR, "a.ts", 1, 1, "TS2305", "foo", "", "no exported member", "", "error")
    assert implicit.is_implicit_any
    assert select_primary_error([implicit, export]) is export


# --- ErrorComparator ----------------------------------------------------------

def _err(sig_file: str) -> RepairError:
    return RepairError("tsc", 2, "tsc", ErrorKind.TYPE_ERROR, sig_file, 1, 1, "TS1", "", "", "m", "", "error")


def test_comparator_solved_requires_exit_zero_and_no_errors():
    cmp = ErrorComparator()
    assert cmp.compare([_err("a")], [], 0) is RepairOutcome.SOLVED
    # exit 0 but errors still present is NOT solved
    assert cmp.compare([_err("a")], [_err("a")], 0) is RepairOutcome.UNCHANGED


def test_comparator_worse_improved_unchanged_different():
    cmp = ErrorComparator()
    assert cmp.compare([_err("a")], [_err("a"), _err("b")], 1) is RepairOutcome.WORSE
    assert cmp.compare([_err("a"), _err("b")], [_err("a")], 1) is RepairOutcome.IMPROVED
    assert cmp.compare([_err("a")], [_err("a")], 1) is RepairOutcome.UNCHANGED
    assert cmp.compare([_err("a")], [_err("b")], 1) is RepairOutcome.DIFFERENT


# --- deterministic import resolver -------------------------------------------

def test_import_resolver_suggests_corrected_relative_path(tmp_path: Path):
    # Acceptance case: src/ui/index.ts imports "./ui/Hud"; real file is src/ui/Hud.tsx.
    (tmp_path / "src" / "ui").mkdir(parents=True)
    (tmp_path / "src" / "ui" / "index.ts").write_text('import { Hud } from "./ui/Hud";\n')
    (tmp_path / "src" / "ui" / "Hud.tsx").write_text("export const Hud = () => null;\n")

    fix = suggest_import_fix(tmp_path, "src/ui/index.ts", "./ui/Hud")
    assert fix is not None
    assert fix.suggested_specifier == "./Hud"
    assert fix.resolved_file == "src/ui/Hud.tsx"


def test_import_resolver_returns_none_when_already_resolvable(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text("")
    (tmp_path / "src" / "Hud.tsx").write_text("")
    assert suggest_import_fix(tmp_path, "src/index.ts", "./Hud") is None


def test_import_resolver_ignores_bare_imports(tmp_path: Path):
    assert suggest_import_fix(tmp_path, "src/index.ts", "react") is None


# --- final response enforcement ----------------------------------------------

def test_success_words_blocked_when_verifier_failed():
    assert contains_unverified_success_claim("All errors resolved and tests pass", 1)
    assert not contains_unverified_success_claim("All errors resolved", 0)
    neutralized = enforce_final_response("The bug is fixed and verified; tests pass.", 1).lower()
    for forbidden in ("fixed", "resolved", "verified", "pass"):
        assert forbidden not in neutralized


def test_success_words_allowed_when_verifier_passed():
    msg = "The bug is fixed and tests pass."
    assert enforce_final_response(msg, 0) == msg


# --- RepairLoop end to end ----------------------------------------------------

def _write_ts_project(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text("const foo = 1;\nexport { foo };\n" * 5)
    (tmp_path / "src" / "util.ts").write_text("export const other = 1;\n")


def test_loop_solves_and_reports_only_on_exit_zero(tmp_path: Path):
    _write_ts_project(tmp_path)
    verifier = ScriptedVerifier("tsc", [(2, TSC_ERR), (0, "")])  # fail then pass
    plan = RepairPlan(root_cause="missing export", target_file="src/app.ts",
                      full_content="export const foo = 1;\n")
    loop = RepairLoop(tmp_path, verifier, ScriptedProposer([plan]), max_attempts=3)
    result = loop.run()
    assert result.success is True
    assert result.exit_code == 0
    assert "passed" in result.final_message.lower()
    assert result.attempts[-1].outcome is RepairOutcome.SOLVED
    assert result.attempts[-1].kept is True


def test_loop_rolls_back_when_error_gets_worse(tmp_path: Path):
    _write_ts_project(tmp_path)
    original = (tmp_path / "src" / "app.ts").read_text()
    # Verifier: 1 error, then 2 errors (worse), then 2 errors forever.
    verifier = ScriptedVerifier("tsc", [(2, TSC_ERR), (2, TSC_ERR + "\n" + TSC_ERR_2), (2, TSC_ERR + "\n" + TSC_ERR_2)])
    plan = RepairPlan(root_cause="x", target_file="src/app.ts", full_content="BROKEN\n")
    loop = RepairLoop(tmp_path, verifier, ScriptedProposer([plan]), max_attempts=2)
    result = loop.run()
    assert result.success is False
    # The worse patch must have been rolled back: file restored to original.
    assert (tmp_path / "src" / "app.ts").read_text() == original
    assert any(a.outcome is RepairOutcome.WORSE and not a.kept for a in result.attempts)
    assert "not verified" in result.final_message.lower() or "did not resolve" in result.final_message.lower()


def test_loop_blocks_repeated_identical_patch(tmp_path: Path):
    _write_ts_project(tmp_path)
    # Error never changes; model keeps proposing the same unchanged-outcome patch.
    verifier = ScriptedVerifier("tsc", [(2, TSC_ERR)])
    plan = RepairPlan(root_cause="x", target_file="src/app.ts", full_content="export const foo = 2;\n")
    proposer = ScriptedProposer([plan])
    loop = RepairLoop(tmp_path, verifier, proposer, max_attempts=5)
    result = loop.run()
    assert result.success is False
    # First attempt runs (UNCHANGED -> rollback + record failure); the identical
    # second is blocked, so the model is only asked at most twice, not 5 times.
    assert proposer.calls <= 2
    assert "different strategy" in result.stopped_reason or result.stopped_reason


def test_loop_refuses_to_edit_uninspected_file(tmp_path: Path):
    _write_ts_project(tmp_path)
    verifier = ScriptedVerifier("tsc", [(2, TSC_ERR)])
    # Model tries to edit a file that was never shown to it.
    plan = RepairPlan(root_cause="x", target_file="src/secret.ts", full_content="x")
    loop = RepairLoop(tmp_path, verifier, ScriptedProposer([plan]), max_attempts=2)
    result = loop.run()
    assert result.success is False
    assert "uninspected" in result.stopped_reason
    assert not (tmp_path / "src" / "secret.ts").exists()


def test_loop_logs_attempt_signatures(tmp_path: Path):
    _write_ts_project(tmp_path)
    verifier = ScriptedVerifier("tsc", [(2, TSC_ERR), (0, "")])
    plan = RepairPlan(root_cause="x", target_file="src/app.ts", full_content="export const foo = 1;\n")
    loop = RepairLoop(tmp_path, verifier, ScriptedProposer([plan]), max_attempts=2)
    result = loop.run()
    attempt = result.attempts[-1]
    assert attempt.before_signature
    assert attempt.after_signature
    assert attempt.files_changed == ["src/app.ts"]
    assert attempt.outcome is RepairOutcome.SOLVED


def test_loop_writes_repair_attempt_to_action_ledger(tmp_path: Path):
    _write_ts_project(tmp_path)
    ledger = start_run(tmp_path, "repair the project")
    set_current_run(ledger)
    try:
        verifier = ScriptedVerifier("tsc", [(2, TSC_ERR), (0, "")])
        plan = RepairPlan(
            root_cause="missing export",
            target_file="src/app.ts",
            full_content="export const foo = 1;\n",
        )
        result = RepairLoop(tmp_path, verifier, ScriptedProposer([plan]), max_attempts=2).run()
    finally:
        clear_current_run()

    events = store.load_events(tmp_path, ledger.run_id)
    repair_events = [event for event in events if event.get("type") == "repair_attempt_finished"]
    verification_events = [
        event for event in events
        if event.get("type") in {"verification_passed", "verification_failed"}
    ]
    assert result.success is True
    assert repair_events[-1]["outcome"] == "SOLVED"
    assert repair_events[-1]["kept"] is True
    assert repair_events[-1]["verifier_id"].startswith("verifier_")
    assert verification_events
    assert all(event.get("verifier_id") for event in verification_events)


def test_loop_debug_context_carries_import_suggestion(tmp_path: Path):
    (tmp_path / "src" / "ui").mkdir(parents=True)
    (tmp_path / "src" / "ui" / "index.ts").write_text('import { Hud } from "./ui/Hud";\n')
    (tmp_path / "src" / "ui" / "Hud.tsx").write_text("export const Hud = () => null;\n")
    log = 'Failed to resolve import "./ui/Hud" from "src/ui/index.ts".'
    verifier = ScriptedVerifier("npm run dev", [(1, log), (0, "")])
    plan = RepairPlan(root_cause="wrong path", target_file="src/ui/index.ts",
                      search='"./ui/Hud"', replace='"./Hud"')
    proposer = ScriptedProposer([plan])
    loop = RepairLoop(tmp_path, verifier, proposer, max_attempts=2)
    result = loop.run()
    assert proposer.seen  # propose was called
    assert "./Hud" in proposer.seen[0].import_suggestion
    assert result.success is True


# --- Phase 0: CommandVerifier + LLMProposer adapters --------------------------

class FakeRunner:
    """Stands in for CommandRunner: returns queued (exit, out, err) per run."""

    def __init__(self, runs: list[tuple[int, str, str]]) -> None:
        self._runs = runs
        self.calls: list[tuple[str, str]] = []

    def run(self, command: str, cwd) -> tuple[int, str, str]:
        idx = min(len(self.calls), len(self._runs) - 1)
        self.calls.append((command, str(cwd)))
        return self._runs[idx]


def test_command_verifier_maps_exit_and_output(tmp_path: Path):
    runner = FakeRunner([(2, "TS error here", "stderr line")])
    verifier = CommandVerifier("tsc --noEmit", runner, tmp_path)
    run = verifier.run()
    assert verifier.command == "tsc --noEmit"
    assert run.exit_code == 2
    assert run.stdout == "TS error here"
    assert run.stderr == "stderr line"
    assert runner.calls == [("tsc --noEmit", str(tmp_path))]


def test_command_verifier_drives_loop_to_solved(tmp_path: Path):
    _write_ts_project(tmp_path)
    runner = FakeRunner([(2, TSC_ERR, ""), (0, "", "")])  # fail then pass
    verifier = CommandVerifier("tsc --noEmit", runner, tmp_path)
    plan = RepairPlan(root_cause="missing export", target_file="src/app.ts",
                      full_content="export const foo = 1;\n")
    loop = RepairLoop(tmp_path, verifier, ScriptedProposer([plan]), max_attempts=3)
    result = loop.run()
    assert result.success is True
    assert result.exit_code == 0


def test_llm_proposer_parses_json_plan():
    def generate(system: str, user: str, schema: dict) -> str:
        assert "STRICT DEBUG MODE" in system
        return (
            '{"root_cause": "missing export", "target_file": "src/app.ts", '
            '"search": "const foo", "replace": "export const foo"}'
        )

    proposer = LLMProposer(generate)
    context = _debug_context()
    plan = proposer.propose(context)
    assert plan is not None
    assert plan.target_file == "src/app.ts"
    assert plan.search == "const foo"
    assert plan.replace == "export const foo"


def test_llm_proposer_repairs_malformed_json():
    # Trailing prose + missing brace: json_repair should still recover it.
    def generate(system: str, user: str, schema: dict) -> str:
        return 'Here is the fix: {"root_cause": "x", "target_file": "a.ts", "full_content": "y"'

    plan = LLMProposer(generate).propose(_debug_context())
    assert plan is not None
    assert plan.target_file == "a.ts"
    assert plan.full_content == "y"


def test_llm_proposer_returns_none_on_empty_output():
    assert LLMProposer(lambda system, user, schema: "").propose(_debug_context()) is None
    assert LLMProposer(lambda system, user, schema: "   ").propose(_debug_context()) is None


def test_llm_proposer_returns_none_without_edit():
    # A plan with neither search nor full_content is not actionable.
    def generate(system: str, user: str, schema: dict) -> str:
        return '{"root_cause": "x", "target_file": "a.ts"}'

    assert LLMProposer(generate).propose(_debug_context()) is None


def test_llm_proposer_retries_diagnosis_only_plan():
    responses = [
        '{"root_cause": "unterminated string literal", "target_file": "ledgerlite.py"}',
        (
            '{"root_cause": "unterminated string literal", '
            '"target_file": "ledgerlite.py", "search": "bad", "replace": "good"}'
        ),
    ]
    prompts: list[str] = []

    def generate(system: str, user: str, schema: dict) -> str:
        prompts.append(user)
        return responses[min(len(prompts) - 1, len(responses) - 1)]

    plan = LLMProposer(generate).propose(_debug_context())

    assert plan is not None
    assert plan.search == "bad"
    assert plan.replace == "good"
    assert len(prompts) == 2
    assert "Previous invalid repair JSON" in prompts[1]


def test_llm_proposer_survives_generate_exception():
    def generate(system: str, user: str, schema: dict) -> str:
        raise RuntimeError("model transport failed")

    assert LLMProposer(generate).propose(_debug_context()) is None


def _debug_context() -> DebugContext:
    err = RepairError("tsc", 2, "tsc", ErrorKind.EXPORT_ERROR, "src/app.ts", 10, 4,
                      "TS2305", "foo", "./util", "no exported member", "", "error")
    return DebugContext(primary_error=err, verify_command="tsc --noEmit")


# --- Phase 6: DjangoTestVerifier drives the general RepairLoop ----------------

class ScriptedDjangoRunner:
    """Django-style test runner: queued (failed, raw_output) per run."""

    def __init__(self, runs: list[tuple[int, str]]) -> None:
        self._runs = runs
        self.calls = 0

    def run(self, project_cwd="."):
        idx = min(self.calls, len(self._runs) - 1)
        self.calls += 1
        failed, output = self._runs[idx]
        return SimpleNamespace(failed=failed, raw_output=output)


DJANGO_FAIL = (
    "Traceback (most recent call last):\n"
    '  File "app/models.py", line 2, in <module>\n'
    "    x = broken(\n"
    "SyntaxError: invalid syntax"
)


def test_django_test_verifier_maps_failed_to_exit_code(tmp_path: Path):
    from shamsu.repair.verifiers import DjangoTestVerifier

    failing = DjangoTestVerifier("python manage.py test", ScriptedDjangoRunner([(2, "boom")]), tmp_path)
    run = failing.run()
    assert run.exit_code == 1 and run.stdout == "boom"

    passing = DjangoTestVerifier("python manage.py test", ScriptedDjangoRunner([(0, "OK")]), tmp_path)
    assert passing.run().exit_code == 0


def test_django_path_solves_on_repair_loop(tmp_path: Path):
    from shamsu.repair.verifiers import DjangoTestVerifier

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "models.py").write_text("x = broken(\n")
    runner = ScriptedDjangoRunner([(1, DJANGO_FAIL), (0, "OK")])
    verifier = DjangoTestVerifier("python manage.py test --verbosity=2", runner, tmp_path)
    plan = RepairPlan(root_cause="syntax", target_file="app/models.py", full_content="x = 1\n")
    loop = RepairLoop(tmp_path, verifier, ScriptedProposer([plan]), max_attempts=3)
    result = loop.run()
    assert result.success is True
    assert result.exit_code == 0
