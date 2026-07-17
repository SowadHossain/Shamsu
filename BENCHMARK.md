# SHAMSU Eval Benchmark

- **Tier:** default
- **Pass rate:** 11/11 (100%)
- **Samples per case:** 3

| Case | Result | Time | Notes |
|------|--------|------|-------|
| qa_reads_repo_fact | PASS 3/3 | 41.7s |  |
| create_file | PASS 3/3 | 80.4s |  |
| edit_file_targeted | PASS 3/3 | 91.2s |  |
| bugfix_syntax_error | PASS 2/3 | 95.3s | FLAKY - re-run before trusting |
| run_command_verify | PASS 3/3 | 86.1s |  |
| ask_user_clarifies | PASS 3/3 | 35.6s |  |
| ask_before_choosing_an_approach | PASS 3/3 | 56.9s |  |
| ask_before_destructive_guess | PASS 3/3 | 78.9s |  |
| does_not_ask_when_unambiguous | PASS 3/3 | 78.7s |  |
| plan_references_only_real_files | PASS 3/3 | 29.3s |  |
| chat_plan_references_only_real_files | PASS 3/3 | 35.4s |  |

> **Flaky this run:** bugfix_syntax_error. These cases passed some attempts and failed others on the SAME code - do not read a delta from them.

