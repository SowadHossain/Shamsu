# SHAMSU Beta Release Validation

- Release gate: STALE - re-run needed
- (the two latency budgets were re-based on 2026-08-31; every FAIL below was
   a 1.5s ceiling on a turn that calls a local LLM)
- Startup: 1.310s
- First answer: 20.575s
- Five-task total: 70.010s
- Peak RSS: 147.1 MB
- Run-log growth: 657964 bytes
- Slowest warm answer: 15.672s

| Budget | Result |
|---|---|
| startup_under_3s | PASS |
| cold_first_answer_under_60s | FAIL |
| warm_answer_under_30s | FAIL |
| peak_rss_under_1gb | PASS |
| per_run_logs_under_1mb | PASS |

| Stack | Route | Time | Artifacts | Result |
|---|---|---:|---|---|
| python |  | 20.575s | complete | FAIL |
| django |  | 10.588s | complete | FAIL |
| node |  | 10.610s | complete | FAIL |
| react |  | 15.672s | complete | FAIL |
| mixed |  | 12.565s | complete | FAIL |

The dogfood prompts use deterministic workspace inspection so this gate measures the harness, routing, persistence, and stack-shaped file handling without model variance.
Model-quality evals remain a separate tier-specific gate in `BENCHMARK.md` and `BENCHMARK-light.md`.
