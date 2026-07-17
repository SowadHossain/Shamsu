# SHAMSU Eval Benchmark

- **Tier:** default
- **Pass rate:** 8/10 (80%)

| Case | Result | Time | Notes |
|------|--------|------|-------|
| qa_reads_repo_fact | PASS | 17.6s |  |
| create_file | PASS | 18.3s |  |
| edit_file_targeted | PASS | 18.6s |  |
| bugfix_syntax_error | FAIL | 21.2s | check failed |
| run_command_verify | FAIL | 8.7s | check failed |
| ask_user_clarifies | PASS | 11.4s |  |
| ask_before_choosing_an_approach | PASS | 11.0s |  |
| ask_before_destructive_guess | PASS | 4.7s |  |
| does_not_ask_when_unambiguous | PASS | 12.6s |  |
| plan_references_only_real_files | PASS | 12.2s |  |

## Read this before trusting a delta

**These numbers are single-sample against a stochastic local 7B. A 1–2 case
swing is noise, not signal.** Measured directly: re-running the two FAILs above
gave `bugfix_syntax_error` PASS / FAIL / PASS and `run_command_verify` PASS /
PASS. The same commit scores 8/10 or 9/10 depending on the roll.

So the governing rule — *no prompt/loop change ships without an eval delta* —
holds only for deltas this harness can actually resolve:

- **A case flipping consistently** across re-runs is signal.
  `ask_before_choosing_an_approach` went FAIL -> PASS and stayed PASS across
  three runs after the J6 upfront-ask; `plan_references_only_real_files` went
  FAIL -> PASS after the C1 grounding fix. Both are real.
- **A single-run ±1** is not. Re-run the affected case before believing it.

Fixing this properly means N-of-M sampling per case (report 2/3, not PASS/FAIL),
which the harness does not do yet — see gap I3.
