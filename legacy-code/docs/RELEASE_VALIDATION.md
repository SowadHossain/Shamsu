# SHAMSU Beta Release Validation

- Release gate: PASS
- Startup: 1.266s
- First answer: 1.071s
- Five-task total: 2.193s
- Peak RSS: 90.9 MB
- Run-log growth: 52128 bytes
- Slowest warm answer: 0.307s

| Budget | Result |
|---|---|
| startup_under_3s | PASS |
| cold_first_answer_under_1_5s | PASS |
| warm_answer_under_1s | PASS |
| peak_rss_under_1gb | PASS |
| per_run_logs_under_1mb | PASS |

| Stack | Route | Time | Artifacts | Result |
|---|---|---:|---|---|
| python | workspace.files | 1.071s | complete | PASS |
| django | workspace.files | 0.261s | complete | PASS |
| node | workspace.files | 0.279s | complete | PASS |
| react | workspace.files | 0.275s | complete | PASS |
| mixed | workspace.files | 0.307s | complete | PASS |

The dogfood prompts use deterministic workspace inspection so this gate measures the harness, routing, persistence, and stack-shaped file handling without model variance.
Model-quality evals remain a separate tier-specific gate in `BENCHMARK.md` and `BENCHMARK-light.md`.
