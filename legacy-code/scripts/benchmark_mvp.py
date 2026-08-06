from __future__ import annotations

import argparse
import asyncio
import tempfile
import time
from pathlib import Path

import psutil

from shamsu.agents.full_pipeline import FullDjangoPipeline
from shamsu.tools.django import DjangoSetupResult
from shamsu.types import TestRunResult


class EmptySearch:
    def search(self, query: str, top_k: int = 5):
        return []

    def symbol_lookup(self, name: str):
        return []

    def fts_search(self, query: str, top_k: int = 5):
        return []


class BenchmarkSetupRunner:
    def run(self, project_cwd: Path | str = ".") -> DjangoSetupResult:
        return DjangoSetupResult(project_cwd=Path(project_cwd))


class BenchmarkTestRunner:
    def run(self, project_cwd: Path | str = ".") -> TestRunResult:
        return TestRunResult(passed=1, failed=0, raw_output="Benchmark simulated test pass")


async def run_benchmark(prd_paths: list[Path]) -> str:
    process = psutil.Process()
    rows: list[tuple[str, float, float, bool]] = []
    peak_mb = process.memory_info().rss / 1024 / 1024
    workspace_root = Path.cwd().resolve()
    with tempfile.TemporaryDirectory(prefix="shamsu-benchmark-", dir=workspace_root) as tmp:
        workspace = Path(tmp)
        for prd in prd_paths:
            target = workspace / prd.stem
            start = time.perf_counter()
            result = await FullDjangoPipeline(
                workspace_root,
                search=EmptySearch(),
                approval_func=lambda _request: True,
                setup_runner=BenchmarkSetupRunner(),
                test_runner=BenchmarkTestRunner(),
            ).run(prd, target_dir=target)
            elapsed = time.perf_counter() - start
            rss_mb = process.memory_info().rss / 1024 / 1024
            peak_mb = max(peak_mb, rss_mb)
            rows.append((prd.name, elapsed, rss_mb, result.success))
    return render_report(rows, peak_mb)


def render_report(rows: list[tuple[str, float, float, bool]], peak_mb: float) -> str:
    lines = [
        "# SHAMSU MVP Benchmark",
        "",
        "Representative PRD generation benchmark. Setup/test commands are simulated so the benchmark measures SHAMSU orchestration and file generation without installing generated-project dependencies.",
        "",
        f"- Peak RSS: {peak_mb:.1f} MB",
        "- Target peak: under 7168 MB",
        f"- Status: {'PASS' if peak_mb < 7168 else 'FAIL'}",
        "",
        "| PRD | Runtime Seconds | RSS MB | Success |",
        "| --- | ---: | ---: | --- |",
    ]
    for name, elapsed, rss_mb, success in rows:
        lines.append(f"| {name} | {elapsed:.3f} | {rss_mb:.1f} | {success} |")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SHAMSU MVP generation benchmark.")
    parser.add_argument(
        "--fixtures",
        default="tests/fixtures/prds",
        help="Directory containing PRD fixture files.",
    )
    parser.add_argument(
        "--output",
        default="BENCHMARK.md",
        help="Benchmark report output path.",
    )
    args = parser.parse_args()
    fixtures = sorted(Path(args.fixtures).glob("*.md"))
    report = asyncio.run(run_benchmark(fixtures))
    Path(args.output).write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
