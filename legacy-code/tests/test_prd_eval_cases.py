from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from evals.harness import EvalCase, run_evals
from evals.prd_cases import PRD_BENCHMARK_CASES, make_prd_eval_case
from evals.prd_fixtures import fixture_by_name


async def _driver_that_builds_ledgerlite(workspace: Path, case: EvalCase) -> str:
    fixture = fixture_by_name("ledgerlite_medium_cli")
    assert (workspace / fixture.prd_path.name).is_file()
    target = workspace / fixture.target_dir
    target.mkdir()
    (target / "ledgerlite.py").write_text(_LEDGERLITE_SCRIPT, encoding="utf-8")
    return "built ledgerlite"


async def _driver_that_noops(_workspace: Path, _case: EvalCase) -> str:
    return "done"


@pytest.mark.asyncio
async def test_prd_eval_case_scores_acceptance_commands():
    fixture = fixture_by_name("ledgerlite_medium_cli")
    case = make_prd_eval_case(fixture)

    report = await run_evals([case], driver=_driver_that_builds_ledgerlite)

    assert report.passed == 1
    assert report.results[0].note == "acceptance passed"


@pytest.mark.asyncio
async def test_prd_eval_case_failure_note_names_missing_target():
    fixture = fixture_by_name("ledgerlite_medium_cli")
    case = make_prd_eval_case(fixture)

    report = await run_evals([case], driver=_driver_that_noops)

    result = report.results[0]
    assert result.passed is False
    assert "missing target folder" in result.note
    assert "missing target folder" in report.render()


def test_prd_benchmark_cases_are_exposed_as_long_running_cases():
    names = {case.name for case in PRD_BENCHMARK_CASES}

    assert "prd_ledgerlite_medium_cli" in names
    assert "prd_atlasdesk_long_fullstack" in names
    assert all(case.long_running for case in PRD_BENCHMARK_CASES)
    assert all("prd_benchmark" in case.tags for case in PRD_BENCHMARK_CASES)


_LEDGERLITE_SCRIPT = dedent(
    """
    import argparse
    import csv
    import json
    from pathlib import Path

    SEED = [
        {"category": "supplies", "amount": 18.25, "note": "notebooks"},
        {"category": "meals", "amount": 27.75, "note": "client lunch"},
        {"category": "software", "amount": 99.00, "note": "design tool"},
        {"category": "travel", "amount": 5.00, "note": "metro"},
    ]

    def load(path):
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))

    def save(path, rows):
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    def with_ids(rows):
        return [
            {"id": f"exp-{index:03d}", **row}
            for index, row in enumerate(rows, start=1)
        ]

    def main():
        parser = argparse.ArgumentParser()
        parser.add_argument("command", choices=["seed", "add", "list", "summary", "export"])
        parser.add_argument("--db", default="ledgerlite.json")
        parser.add_argument("--category")
        parser.add_argument("--amount", type=float)
        parser.add_argument("--note")
        parser.add_argument("--out")
        args = parser.parse_args()
        path = Path(args.db)

        if args.command == "seed":
            save(path, with_ids(SEED))
            print("seeded 4 expenses")
        elif args.command == "add":
            rows = load(path)
            row = {
                "id": f"exp-{len(rows) + 1:03d}",
                "category": args.category,
                "amount": args.amount,
                "note": args.note,
            }
            rows.append(row)
            save(path, rows)
            print(f"added expense {args.category} {args.amount:.2f}")
        elif args.command == "summary":
            print(f"total {sum(row['amount'] for row in load(path)):.2f}")
        elif args.command == "export":
            with Path(args.out).open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["id", "category", "amount", "note"])
                writer.writeheader()
                writer.writerows(load(path))
            print(f"exported {args.out}")
        elif args.command == "list":
            for row in load(path):
                print(f"{row['id']} {row['category']} {row['amount']:.2f} {row['note']}")

    if __name__ == "__main__":
        main()
    """
).lstrip()
