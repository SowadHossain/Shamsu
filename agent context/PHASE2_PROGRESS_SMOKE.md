# SHAMSU Eval Benchmark

- **Tier:** default
- **Pass rate:** 0/1 (0%)
- **Samples per case:** 1  <- single-sample: a ±1 delta is noise, not signal
- **Artifacts:** `F:\Work\PROJECTS\shamsu\Shamsu\agent context\PHASE2_PRD_EVAL_ARTIFACTS\eval_20260722_062849_be586fe8`

| Case | Result | Time | Notes |
|------|--------|------|-------|
| run_command_verify | FAIL | 2.5s | check failed |

## Reading these numbers

Local models are stochastic. A case flagged flaky passed some attempts
and failed others on identical code - treat its row as noise until
re-measured with more samples. Compare baselines only at equal
`--samples`; a delta that lives entirely inside the flaky set is no
delta. Tier-specific findings (root causes of consistent failures)
live in `agent context/SHAMSU_agent_gap_analysis.md` under I3.

## Deterministic release metrics

Harness startup, first-answer/task time, peak memory, log growth, and Python/Django/Node/React/mixed dogfood results are recorded separately in `RELEASE_VALIDATION.md` so model variance is not mixed with runtime reliability.

