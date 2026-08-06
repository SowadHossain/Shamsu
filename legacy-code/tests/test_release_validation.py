from __future__ import annotations

from scripts.validate_release import STACKS, render_markdown, seed_stack


def test_seed_stack_creates_expected_files(tmp_path):
    for stack in STACKS:
        workspace, expected = seed_stack(tmp_path / stack, stack)
        assert expected
        assert all((workspace / relative).exists() for relative in expected)


def test_release_report_renders_metrics_budgets_and_stacks():
    report = {
        "release_gate_passed": True,
        "metrics": {
            "startup_time_s": 0.2,
            "first_answer_time_s": 0.1,
            "task_time_s": 0.5,
            "peak_rss_mb": 100.0,
            "disk_log_growth_bytes": 1234,
            "warm_answer_max_s": 0.1,
        },
        "budgets": {"startup_under_3s": True},
        "dogfood": [
            {
                "stack": "python",
                "route": "workspace.files",
                "duration_s": 0.1,
                "artifacts_complete": True,
                "passed": True,
            }
        ],
    }

    rendered = render_markdown(report)

    assert "Release gate: PASS" in rendered
    assert "Peak RSS: 100.0 MB" in rendered
    assert "| python | workspace.files |" in rendered
