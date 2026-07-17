# SHAMSU Eval Benchmark

- **Tier:** default
- **Pass rate:** 11/12 (92%)
- **Samples per case:** 3

| Case | Result | Time | Notes |
|------|--------|------|-------|
| qa_reads_repo_fact | PASS 3/3 | 40.6s |  |
| create_file | PASS 2/3 | 68.3s | FLAKY - re-run before trusting |
| edit_file_targeted | PASS 3/3 | 86.5s |  |
| bugfix_syntax_error | PASS 3/3 | 95.8s |  |
| run_command_verify | PASS 3/3 | 77.7s |  |
| ask_user_clarifies | PASS 3/3 | 38.3s |  |
| rename_file_via_move_tool | PASS 2/3 | 69.6s | FLAKY - re-run before trusting |
| ask_before_choosing_an_approach | PASS 2/3 | 50.9s | FLAKY - re-run before trusting |
| ask_before_destructive_guess | FAIL 1/3 | 68.6s | check failed FLAKY - re-run before trusting |
| does_not_ask_when_unambiguous | PASS 3/3 | 85.2s |  |
| plan_references_only_real_files | PASS 3/3 | 30.1s |  |
| chat_plan_references_only_real_files | PASS 3/3 | 36.2s |  |

> **Flaky this run:** create_file, rename_file_via_move_tool, ask_before_choosing_an_approach, ask_before_destructive_guess. These cases passed some attempts and failed others on the SAME code - do not read a delta from them.

## Reading these numbers

Local models are stochastic. A case flagged FLAKY passed some attempts and
failed others **on identical code** - its row is noise, not signal, until
re-measured with more samples. Single-sample deltas of one case are
meaningless; compare baselines only at equal `--samples` and treat any
change that lands entirely inside the flaky set as no change. The previous
baseline (11 cases, before `rename_file_via_move_tool` existed) scored
11/11 with `ask_before_destructive_guess` passing 3/3 on the same code -
that pair of runs together says "passes most of the time, wobbles under
sampling", not "regressed".
