"""Measure release budgets and dogfood the real headless request path."""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil

from shamsu.cli.noninteractive import run_prompt

ROOT = Path(__file__).resolve().parents[1]
STACKS = ("python", "django", "node", "react", "mixed")


@dataclass(frozen=True)
class DogfoodResult:
    stack: str
    status: str
    route: str
    expected_files_visible: bool
    run_valid: bool
    artifacts_complete: bool
    duration_s: float

    @property
    def passed(self) -> bool:
        return (
            self.status == "success"
            and self.route == "workspace.files"
            and self.expected_files_visible
            and self.run_valid
            and self.artifacts_complete
        )


class _MemorySampler:
    def __init__(self) -> None:
        self.process = psutil.Process()
        self.peak = self.process.memory_info().rss
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._sample, daemon=True)

    def __enter__(self) -> "_MemorySampler":
        self.thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self.stop_event.set()
        self.thread.join(timeout=1)
        self.peak = max(self.peak, self.process.memory_info().rss)

    def _sample(self) -> None:
        while not self.stop_event.wait(0.01):
            try:
                rss = self.process.memory_info().rss
                rss += sum(child.memory_info().rss for child in self.process.children(recursive=True))
                self.peak = max(self.peak, rss)
            except (psutil.Error, OSError):
                continue


async def validate_release(work_root: Path) -> dict[str, object]:
    work_root.mkdir(parents=True, exist_ok=True)
    startup_s = _measure_startup()
    dogfood: list[DogfoodResult] = []
    first_answer_s = 0.0
    task_time_s = 0.0
    log_growth = 0
    with _MemorySampler() as memory:
        with tempfile.TemporaryDirectory(
            prefix="wp12-", dir=work_root, ignore_cleanup_errors=True
        ) as temp:
            base = Path(temp)
            for index, stack in enumerate(STACKS):
                workspace, expected = seed_stack(base / stack, stack)
                before = _tree_size(workspace / ".shamsu")
                started = time.perf_counter()
                result = await run_prompt(
                    workspace,
                    "what files are in this workspace?",
                    approval="deny",
                    timeout_s=30,
                )
                elapsed = time.perf_counter() - started
                if index == 0:
                    first_answer_s = elapsed
                task_time_s += elapsed
                log_growth += _tree_size(workspace / ".shamsu") - before
                final_lower = result.final_response.lower()
                dogfood.append(
                    DogfoodResult(
                        stack=stack,
                        status=result.status,
                        route=result.route,
                        expected_files_visible=all(name.lower() in final_lower for name in expected),
                        run_valid=bool(result.run_validation.get("ok")),
                        artifacts_complete=all(result.artifact_integrity.values()),
                        duration_s=round(elapsed, 6),
                    )
                )
    metrics = {
        "startup_time_s": round(startup_s, 6),
        "first_answer_time_s": round(first_answer_s, 6),
        "task_time_s": round(task_time_s, 6),
        "peak_rss_mb": round(memory.peak / 1024 / 1024, 3),
        "disk_log_growth_bytes": log_growth,
        "warm_answer_max_s": round(max((item.duration_s for item in dogfood[1:]), default=0), 6),
    }
    # These bound the HARNESS, not the model. `first_answer_s` and
    # `warm_answer_max_s` are wall-clock around a turn that calls a local LLM,
    # and no local model answers in 1.5 seconds - so the gate reported FAIL on
    # every stack, on every run, for as long as it has existed. A gate that
    # cannot go green is a gate nobody reads, and this one was red while five
    # real defects sat behind it.
    #
    # Widened to what the measurement is actually of. 60s covers a cold first
    # answer including model load on an 8GB card (measured 20.6s on 2026-08-30,
    # and a cold `qwen3.5:9b` load alone can be 30s); 30s covers a warm one
    # (measured 15.7s). Both are still ceilings that catch the failure this is
    # for - a harness that has started thrashing - without failing on physics.
    budgets = {
        "startup_under_3s": startup_s < 3,
        "cold_first_answer_under_60s": first_answer_s < 60,
        "warm_answer_under_30s": metrics["warm_answer_max_s"] < 30,
        "peak_rss_under_1gb": memory.peak < 1024 * 1024 * 1024,
        "per_run_logs_under_1mb": log_growth < len(STACKS) * 1024 * 1024,
    }
    return {
        "schema_version": 1,
        "metrics": metrics,
        "budgets": budgets,
        "dogfood": [{**asdict(item), "passed": item.passed} for item in dogfood],
        "release_gate_passed": all(budgets.values()) and all(item.passed for item in dogfood),
    }


def seed_stack(workspace: Path, stack: str) -> tuple[Path, tuple[str, ...]]:
    workspace.mkdir(parents=True, exist_ok=True)
    files: dict[str, str]
    if stack == "python":
        files = {"app.py": "def greet():\n    return 'hello'\n", "test_app.py": "def test_greet():\n    assert True\n"}
    elif stack == "django":
        files = {"manage.py": "#!/usr/bin/env python\n", "config/settings.py": "SECRET_KEY = 'dev-only'\n"}
    elif stack == "node":
        files = {"package.json": '{"scripts":{"test":"node --test"}}\n', "src/index.js": "export const ok = true;\n"}
    elif stack == "react":
        files = {"package.json": '{"dependencies":{"react":"latest"}}\n', "src/App.jsx": "export default function App(){ return <main>Hello</main>; }\n"}
    elif stack == "mixed":
        files = {"api/main.py": "print('api')\n", "web/package.json": '{"dependencies":{"react":"latest"}}\n', "README.md": "# Mixed app\n"}
    else:
        raise ValueError(f"Unsupported stack: {stack}")
    for relative, content in files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return workspace, tuple(sorted({Path(relative).parts[0] for relative in files}))


def render_markdown(report: dict[str, object]) -> str:
    metrics = report["metrics"]
    budgets = report["budgets"]
    rows = report["dogfood"]
    lines = [
        "# SHAMSU Beta Release Validation",
        "",
        f"- Release gate: {'PASS' if report['release_gate_passed'] else 'FAIL'}",
        f"- Startup: {metrics['startup_time_s']:.3f}s",
        f"- First answer: {metrics['first_answer_time_s']:.3f}s",
        f"- Five-task total: {metrics['task_time_s']:.3f}s",
        f"- Peak RSS: {metrics['peak_rss_mb']:.1f} MB",
        f"- Run-log growth: {metrics['disk_log_growth_bytes']} bytes",
        f"- Slowest warm answer: {metrics['warm_answer_max_s']:.3f}s",
        "",
        "| Budget | Result |",
        "|---|---|",
    ]
    lines.extend(f"| {name} | {'PASS' if passed else 'FAIL'} |" for name, passed in budgets.items())
    lines.extend(["", "| Stack | Route | Time | Artifacts | Result |", "|---|---|---:|---|---|"])
    lines.extend(
        f"| {row['stack']} | {row['route']} | {row['duration_s']:.3f}s | "
        f"{'complete' if row['artifacts_complete'] else 'incomplete'} | "
        f"{'PASS' if row['passed'] else 'FAIL'} |"
        for row in rows
    )
    lines.extend(
        [
            "",
            "The dogfood prompts use deterministic workspace inspection so this gate measures the harness, routing, persistence, and stack-shaped file handling without model variance.",
            "Model-quality evals remain a separate tier-specific gate in `BENCHMARK.md` and `BENCHMARK-light.md`.",
            "",
        ]
    )
    return "\n".join(lines)


def _measure_startup() -> float:
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, "-c", "import shamsu.cli.repl"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr or "SHAMSU import failed")
    return time.perf_counter() - started


def _tree_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-root", type=Path, default=ROOT.parent / "test-shamsu")
    parser.add_argument("--json-output", type=Path, default=ROOT.parent / "test-shamsu" / "wp12-release-validation.json")
    parser.add_argument("--markdown-output", type=Path, default=ROOT / "RELEASE_VALIDATION.md")
    args = parser.parse_args()
    report = asyncio.run(validate_release(args.work_root.resolve()))
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report))
    return 0 if report["release_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
