# SHAMSU Eval Benchmark

- **Tier:** default
- **Pass rate:** 1/2 (50%)
- **Samples per case:** 3
- **Artifacts:** `F:\Work\PROJECTS\shamsu\Shamsu\agent context\PHASE2_PRD_EVAL_ARTIFACTS\eval_20260722_054128_c3f7c719`

| Case | Result | Time | Notes |
|------|--------|------|-------|
| prd_ledgerlite_medium_cli | PASS 3/3 | 97.3s | acceptance passed |
| prd_atlasdesk_long_fullstack | FAIL 0/3 | 499.2s | missing file `scripts/seed.mjs` |

## Reading these numbers

Local models are stochastic. A case flagged flaky passed some attempts
and failed others on identical code - treat its row as noise until
re-measured with more samples. Compare baselines only at equal
`--samples`; a delta that lives entirely inside the flaky set is no
delta. Tier-specific findings (root causes of consistent failures)
live in `agent context/SHAMSU_agent_gap_analysis.md` under I3.

## Deterministic release metrics

Harness startup, first-answer/task time, peak memory, log growth, and Python/Django/Node/React/mixed dogfood results are recorded separately in `RELEASE_VALIDATION.md` so model variance is not mixed with runtime reliability.

