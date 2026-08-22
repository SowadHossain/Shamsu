# SHAMSU Eval Benchmark

- **Tier:** default
- **Pass rate:** 12/16 (75%)
- **Samples per case:** 7

| Case | Result | Time | Rounds | Calls | Notes |
|------|--------|------|--------|-------|-------|
| qa_reads_repo_fact | PASS 7/7 | 106.5s | 3.1 | 1.0 |  |
| create_file | PASS 7/7 | 109.7s | 4.1 | 2.3 |  |
| edit_file_targeted | PASS 7/7 | 107.8s | 4.0 | 2.0 |  |
| bugfix_syntax_error | PASS 7/7 | 111.8s | 4.0 | 2.3 |  |
| run_command_verify | PASS 7/7 | 107.1s | 4.3 | 2.6 |  |
| ask_user_clarifies | FAIL 3/7 | 213.9s | 6.9 | 4.4 | check failed FLAKY - re-run before trusting |
| rename_file_via_move_tool | PASS 7/7 | 70.7s | 3.0 | 1.0 |  |
| ask_before_choosing_an_approach | FAIL 2/7 | 644.6s | 7.6 | 5.1 | check failed FLAKY - re-run before trusting |
| ask_before_destructive_guess | FAIL 0/7 | 77.1s | 3.0 | 1.0 | check failed |
| does_not_ask_when_unambiguous | PASS 7/7 | 79.6s | 3.1 | 1.1 |  |
| plan_references_only_real_files | PASS 7/7 | 44.3s | 0.0 | 0.0 |  |
| chat_plan_references_only_real_files | PASS 7/7 | 394.6s | 0.0 | 0.0 |  |
| repairs_a_file_it_cannot_pattern_match | PASS 6/7 | 582.3s | 10.4 | 8.4 | FLAKY - re-run before trusting |
| answers_instead_of_reading_forever | PASS 7/7 | 316.5s | 8.3 | 8.9 |  |
| writes_the_steps_down_before_starting | PASS 7/7 | 836.9s | 18.1 | 16.9 |  |
| removes_duplicate_definitions_without_losing_anything | FAIL 3/7 | 1497.0s | 23.9 | 22.6 | check failed FLAKY - re-run before trusting |

> **Flaky this run:** ask_user_clarifies, ask_before_choosing_an_approach, repairs_a_file_it_cannot_pattern_match, removes_duplicate_definitions_without_losing_anything. These cases passed some attempts and failed others on the SAME code - do not read a delta from them.

## Reading these numbers

Local models are stochastic. A case flagged flaky passed some attempts
and failed others on identical code - treat its row as noise until
re-measured with more samples. Compare baselines only at equal
`--samples`; a delta that lives entirely inside the flaky set is no
delta. Tier-specific findings (root causes of consistent failures)
live in `agent context/SHAMSU_agent_gap_analysis.md` under I3.

Do not read a delta out of two of these tables by eye. Run both with
`--json-out` and compare them with `python -m evals.diff <baseline>
<feature>`, which applies every rule in this paragraph mechanically
and exits 0 improved / 1 regressed / 2 noise.

## Deterministic release metrics

Harness startup, first-answer/task time, peak memory, log growth, and Python/Django/Node/React/mixed dogfood results are recorded separately in `RELEASE_VALIDATION.md` so model variance is not mixed with runtime reliability.

