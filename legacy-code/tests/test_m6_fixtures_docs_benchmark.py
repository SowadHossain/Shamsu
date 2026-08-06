from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.benchmark_mvp import render_report
from shamsu.agents.full_pipeline import FullDjangoPipeline
from shamsu.tools.django import DjangoSetupResult
from shamsu.types import TestRunResult as ShamsuTestRunResult


FIXTURES = Path("tests/fixtures/prds")


class EmptySearch:
    def search(self, query: str, top_k: int = 5, boost_paths=None):
        return []

    def symbol_lookup(self, name: str):
        return []

    def fts_search(self, query: str, top_k: int = 5):
        return []


class FakeSetupRunner:
    def run(self, project_cwd: Path | str = ".") -> DjangoSetupResult:
        return DjangoSetupResult(project_cwd=Path(project_cwd))


class FakeTestRunner:
    def run(self, project_cwd: Path | str = ".") -> ShamsuTestRunResult:
        return ShamsuTestRunResult(passed=1, failed=0, raw_output="System check identified no issues\nOK")


@pytest.mark.parametrize(
    ("fixture_name", "expected"),
    [
        ("todo.md", ["Task"]),
        ("expense_tracker.md", ["Budget", "Expense", "DecimalField", "ForeignKey"]),
        ("blog.md", ["Post", "Tag", "ManyToManyField"]),
    ],
)
@pytest.mark.asyncio
async def test_prd_fixtures_generate_projects_with_docs_and_summary(
    tmp_path: Path,
    fixture_name: str,
    expected: list[str],
):
    fixture = tmp_path / fixture_name
    shutil.copyfile((FIXTURES / fixture_name).resolve(), fixture)
    target = tmp_path / fixture.stem

    result = await FullDjangoPipeline(
        tmp_path,
        search=EmptySearch(),
        approval_func=lambda _request: True,
        setup_runner=FakeSetupRunner(),
        test_runner=FakeTestRunner(),
    ).run(fixture, target)

    assert result.success is True
    assert result.diagnostics == []
    assert (target / "README.md").exists()
    assert (target / "SHAMSU_SUMMARY.md").exists()
    assert "pip install -r requirements.txt" in (target / "README.md").read_text(encoding="utf-8")
    assert "Generated Files" in (target / "SHAMSU_SUMMARY.md").read_text(encoding="utf-8")
    generated_text = "\n".join(path.read_text(encoding="utf-8") for path in target.rglob("*.py"))
    for marker in expected:
        assert marker in generated_text


def test_benchmark_report_records_peak_memory_and_status():
    report = render_report([("todo.md", 0.1, 120.0, True)], peak_mb=120.0)

    assert "Peak RSS: 120.0 MB" in report
    assert "Target peak: under 7168 MB" in report
    assert "| todo.md | 0.100 | 120.0 | True |" in report
    assert "Status: PASS" in report
