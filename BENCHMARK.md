# SHAMSU Eval Benchmark

- **Tier:** default
- **Pass rate:** 9/10 (90%)
- **Samples per case:** 3

| Case | Result | Time | Notes |
|------|--------|------|-------|
| qa_reads_repo_fact | PASS 3/3 | 32.3s |  |
| create_file | FAIL 1/3 | 53.3s | check failed FLAKY - re-run before trusting |
| edit_file_targeted | PASS 3/3 | 54.9s |  |
| bugfix_syntax_error | PASS 2/3 | 59.8s | FLAKY - re-run before trusting |
| run_command_verify | PASS 2/3 | 44.7s | FLAKY - re-run before trusting |
| ask_user_clarifies | PASS 3/3 | 32.2s |  |
| ask_before_choosing_an_approach | PASS 3/3 | 19.8s |  |
| ask_before_destructive_guess | PASS 2/3 | 47.6s | FLAKY - re-run before trusting |
| does_not_ask_when_unambiguous | PASS 3/3 | 56.5s |  |
| plan_references_only_real_files | PASS 3/3 | 20.8s |  |

> **Flaky this run:** create_file, bugfix_syntax_error, run_command_verify, ask_before_destructive_guess. These cases passed some attempts and failed others on the SAME code - do not read a delta from them.

