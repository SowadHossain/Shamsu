# SHAMSU Eval Benchmark

- **Tier:** light
- **Pass rate:** 9/12 (75%)
- **Samples per case:** 3

| Case | Result | Time | Notes |
|------|--------|------|-------|
| qa_reads_repo_fact | PASS 3/3 | 11.3s |  |
| create_file | PASS 3/3 | 13.2s |  |
| edit_file_targeted | PASS 2/3 | 73.5s | FLAKY - re-run before trusting |
| bugfix_syntax_error | PASS 3/3 | 14.2s |  |
| run_command_verify | PASS 2/3 | 10.4s | FLAKY - re-run before trusting |
| ask_user_clarifies | FAIL 0/3 | 23.6s | check failed |
| rename_file_via_move_tool | FAIL 0/3 | 12.2s | check failed |
| ask_before_choosing_an_approach | PASS 2/3 | 8.8s | FLAKY - re-run before trusting |
| ask_before_destructive_guess | FAIL 0/3 | 12.9s | check failed |
| does_not_ask_when_unambiguous | PASS 3/3 | 11.7s |  |
| plan_references_only_real_files | PASS 3/3 | 28.0s |  |
| chat_plan_references_only_real_files | PASS 3/3 | 8.2s |  |

> **Flaky this run:** edit_file_targeted, run_command_verify, ask_before_choosing_an_approach. These cases passed some attempts and failed others on the SAME code - do not read a delta from them.

## Reading these numbers

Local models are stochastic. A case flagged flaky passed some attempts
and failed others on identical code - treat its row as noise until
re-measured with more samples. Compare baselines only at equal
`--samples`; a delta that lives entirely inside the flaky set is no
delta. Tier-specific findings (root causes of consistent failures)
live in `agent context/SHAMSU_agent_gap_analysis.md` under I3.

## Deterministic release metrics

Harness startup, first-answer/task time, peak memory, log growth, and
Python/Django/Node/React/mixed dogfood results are recorded separately in
`RELEASE_VALIDATION.md` so model variance is not mixed with runtime reliability.

