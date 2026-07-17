# SHAMSU Eval Benchmark

- **Tier:** default
- **Pass rate:** 11/12 (92%)
- **Samples per case:** 3

| Case | Result | Time | Notes |
|------|--------|------|-------|
| qa_reads_repo_fact | PASS 3/3 | 44.6s |  |
| create_file | PASS 3/3 | 70.7s |  |
| edit_file_targeted | PASS 3/3 | 84.1s |  |
| bugfix_syntax_error | PASS 3/3 | 87.7s |  |
| run_command_verify | PASS 3/3 | 80.2s |  |
| ask_user_clarifies | PASS 3/3 | 33.6s |  |
| rename_file_via_move_tool | FAIL 1/3 | 82.4s | check failed FLAKY - re-run before trusting |
| ask_before_choosing_an_approach | PASS 3/3 | 56.0s |  |
| ask_before_destructive_guess | PASS 2/3 | 83.2s | FLAKY - re-run before trusting |
| does_not_ask_when_unambiguous | PASS 3/3 | 83.0s |  |
| plan_references_only_real_files | PASS 3/3 | 33.3s |  |
| chat_plan_references_only_real_files | PASS 3/3 | 39.1s |  |

> **Flaky this run:** rename_file_via_move_tool, ask_before_destructive_guess. These cases passed some attempts and failed others on the SAME code - do not read a delta from them.

## Reading these numbers

Local models are stochastic. A case flagged flaky passed some attempts
and failed others on identical code - treat its row as noise until
re-measured with more samples. Compare baselines only at equal
`--samples`; a delta that lives entirely inside the flaky set is no
delta. Tier-specific findings (root causes of consistent failures)
live in `agent context/SHAMSU_agent_gap_analysis.md` under I3.

