# SHAMSU Reliability Report

- Generated: 2026-07-22T01:34:04.525625+00:00
- Runs included: 190
- Tool-pressure threshold: 1000 tokens

## Metric Definitions

- Apply success: mutation transactions that reached `applied`, `verification_failed`, or `rolled_back` divided by observed apply attempts.
- Verification pass: `verification_passed` events divided by all verification result events.
- First-pass verified: a run with mutations whose first verification result passed before repair.
- Repair success: first `repair_attempt_finished` event with `outcome=SOLVED` and `kept=true`.
- False-success candidate: run status `success` with unrecovered failure evidence in the ledger.
- Success without verification: run status `success` with mutations and no `verification_passed` evidence.
- Tool pressure: finished tool results whose original token count exceeded the configured threshold.
- Failure category: deterministic label from recorded ledger evidence; `none` means no failure evidence was observed.

## Aggregate

| Metric | Value |
| --- | --- |
| Statuses | {"denied": 8, "dry_run": 5, "failed": 37, "needs_input": 7, "partial": 9, "success": 109, "success_unverified": 12, "timed_out": 3} |
| Failure categories | {"environment": 11, "none": 104, "patch_application": 2, "planning": 16, "requirement_coverage": 12, "routing": 11, "tool_call": 12, "verification": 22} |
| Apply success | 187/205 (91.2%) |
| Verification pass | 40/80 (50.0%) |
| First-pass verified | 23/67 (34.3%) |
| Repair success | 0/0 (n/a) |
| False-success candidates | 1/190 (0.5%) |
| Success without verification | 5/190 (2.6%) |
| Tool pressure | 0/175 (0.0%) |
| Tool truncation | 0/175 (0.0%) |
| Missing tool-token telemetry | 175/175 (100.0%) |

## Failure Class Ranking

| Class | Rate | Numerator | Denominator |
| --- | --- | --- | --- |
| tool_token_telemetry_missing | 100.0% | 175 | 175 |
| first_pass_not_verified | 65.7% | 44 | 67 |
| verification_failure_or_unavailable | 50.0% | 40 | 80 |
| apply_failure | 8.8% | 18 | 205 |
| success_without_verification | 2.6% | 5 | 190 |
| false_success_candidate | 0.5% | 1 | 190 |
| tool_pressure_over_threshold | 0.0% | 0 | 175 |

## Failure Categories

| Category | Runs |
| --- | --- |
| environment | 11 |
| none | 104 |
| patch_application | 2 |
| planning | 16 |
| requirement_coverage | 12 |
| routing | 11 |
| tool_call | 12 |
| verification | 22 |

## Runs

| Run | Status | Apply | Verify | Repair | Tool | Category | Flags |
| --- | --- | --- | --- | --- | --- | --- | --- |
| run_2026-07-21_22-36-58_8d0e | success | 26/26 | 0/0 | - | 0/0 >1000 | verification | unverified-success |
| run_2026-07-21_22-29-36_05e1 | success_unverified | 39/39 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_22-24-04_d6b2 | failed | 27/27 | 0/0 | - | 0/0 >1000 | routing | command:latest_failed |
| run_2026-07-21_22-06-47_9258 | success_unverified | 18/18 | 0/0 | - | 0/0 >1000 | routing | command:latest_failed |
| run_2026-07-21_21-51-38_f6fd | failed | 11/11 | 0/0 | - | 0/0 >1000 | planning | event:mutation_required_but_missing |
| run_2026-07-21_21-40-34_0b3c | failed | 13/13 | 0/0 | - | 0/0 >1000 | requirement_coverage | event:contract_failed, command:latest_failed |
| run_2026-07-21_21-07-08_5272 | timed_out | 0/0 | 0/0 | - | 0/0 >1000 | environment | - |
| run_2026-07-21_21-03-46_a21c | failed | 0/0 | 0/0 | - | 0/0 >1000 | planning | event:mutation_required_but_missing |
| run_2026-07-21_20-09-08_74b3 | timed_out | 0/0 | 0/0 | - | 0/0 >1000 | environment | command:latest_failed |
| run_2026-07-21_18-03-21_64f9 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_18-02-01_48c7 | failed | 0/0 | 0/0 | - | 0/0 >1000 | routing | - |
| run_2026-07-21_18-01-37_81b6 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_18-00-40_6022 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_18-00-40_45dd | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_17-53-31_3b4f | success | 1/1 | 1/1 | - | 0/0 >1000 | none | - |
| run_2026-07-21_17-52-58_e19c | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_17-52-58_386e | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_17-52-58_0f00 | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_17-52-30_adf8 | dry_run | 2/2 | 0/0 | - | 0/2 >1000 | routing | - |
| run_2026-07-21_17-52-06_fd33 | denied | 0/1 | 0/0 | - | 0/1 >1000 | environment | event:agent_stopped |
| run_2026-07-21_17-52-06_7ad7 | success_unverified | 1/1 | 0/1 | - | 0/1 >1000 | verification | - |
| run_2026-07-21_17-52-06_23e5 | dry_run | 1/1 | 0/1 | - | 0/1 >1000 | requirement_coverage | event:contract_failed |
| run_2026-07-21_17-51-49_a304 | success | 0/0 | 1/1 | - | 0/1 >1000 | none | - |
| run_2026-07-21_17-48-34_cdfa | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_17-48-34_5798 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_17-48-34_551c | failed | 0/0 | 0/0 | - | 0/1 >1000 | tool_call | command:latest_failed |
| run_2026-07-21_17-48-05_e313 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_17-48-05_d48a | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_16-41-24_aa13 | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_16-41-24_8236 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_16-40-09_c046 | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_16-39-39_6b5e | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_16-37-48_00b8 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_16-35-40_0f04 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_16-34-53_6183 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_16-34-37_eb54 | success_unverified | 1/1 | 0/1 | - | 0/1 >1000 | verification | - |
| run_2026-07-21_16-34-18_aeea | denied | 0/1 | 0/0 | - | 0/1 >1000 | environment | event:agent_stopped |
| run_2026-07-21_16-34-18_56c4 | dry_run | 1/1 | 0/1 | - | 0/1 >1000 | verification | - |
| run_2026-07-21_16-33-51_efac | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_16-33-51_412b | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_16-33-38_4366 | failed | 0/0 | 0/0 | - | 0/0 >1000 | routing | - |
| run_2026-07-21_15-40-00_b9e1 | success | 1/1 | 1/1 | - | 0/0 >1000 | none | - |
| run_2026-07-21_15-30-29_7073 | denied | 0/1 | 0/0 | - | 0/1 >1000 | environment | event:agent_stopped |
| run_2026-07-21_15-27-10_815a | failed | 0/0 | 0/0 | - | 0/0 >1000 | requirement_coverage | event:contract_failed |
| run_2026-07-21_15-24-24_54e3 | success | 1/1 | 1/1 | - | 0/0 >1000 | none | - |
| run_2026-07-21_15-17-22_77c7 | success | 1/1 | 1/1 | - | 0/0 >1000 | none | - |
| run_2026-07-21_15-10-12_b210 | success | 1/1 | 1/1 | - | 0/0 >1000 | none | - |
| run_2026-07-21_15-07-20_3314 | failed | 1/1 | 0/1 | - | 0/0 >1000 | verification | verification:unrecovered_failure, command:latest_failed |
| run_2026-07-21_15-06-06_0bea | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_15-03-10_db7a | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_15-02-23_a3ea | success_unverified | 1/1 | 0/1 | - | 0/1 >1000 | verification | - |
| run_2026-07-21_15-01-26_ed97 | needs_input | 0/0 | 0/0 | - | 0/0 >1000 | planning | - |
| run_2026-07-21_15-00-02_f751 | failed | 0/0 | 0/0 | - | 0/1 >1000 | tool_call | event:agent_stopped, event:mutation_required_but_missing |
| run_2026-07-21_14-58-16_352d | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_14-57-55_f315 | dry_run | 1/1 | 0/1 | - | 0/1 >1000 | verification | - |
| run_2026-07-21_14-56-49_e44c | denied | 0/1 | 0/0 | - | 0/1 >1000 | environment | event:agent_stopped |
| run_2026-07-21_14-55-47_c6be | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_14-54-16_fe95 | failed | 0/0 | 0/0 | - | 0/1 >1000 | tool_call | command:latest_failed |
| run_2026-07-21_14-53-02_c0bf | failed | 0/0 | 0/0 | - | 0/1 >1000 | tool_call | - |
| run_2026-07-21_14-52-50_69fd | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_14-26-39_faf1 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_14-25-28_bdcb | failed | 0/0 | 0/0 | - | 0/0 >1000 | planning | event:mutation_required_but_missing |
| run_2026-07-21_14-24-22_fb32 | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_14-22-27_5ea3 | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_14-20-15_0f60 | partial | 0/0 | 0/0 | - | 0/2 >1000 | tool_call | - |
| run_2026-07-21_14-19-38_c08e | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_14-19-07_63f6 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_14-18-00_18e7 | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_14-16-19_6bde | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_14-15-09_d982 | success_unverified | 1/1 | 0/1 | - | 0/1 >1000 | verification | - |
| run_2026-07-21_14-14-10_e1a0 | dry_run | 1/1 | 0/1 | - | 0/1 >1000 | verification | - |
| run_2026-07-21_14-13-22_49d5 | denied | 0/1 | 0/0 | - | 0/1 >1000 | environment | event:contract_failed |
| run_2026-07-21_14-12-38_47e2 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_13-33-48_dbbc | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_13-30-31_33d7 | needs_input | 0/2 | 0/0 | - | 0/3 >1000 | requirement_coverage | event:contract_failed |
| run_2026-07-21_13-27-00_1702 | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_13-26-48_9786 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-21_13-10-43_f28d | failed | 0/0 | 0/0 | - | 0/1 >1000 | requirement_coverage | event:contract_failed |
| run_2026-07-21_13-08-38_0628 | partial | 0/0 | 0/0 | - | 0/1 >1000 | tool_call | - |
| run_2026-07-21_13-06-37_bcfe | partial | 0/0 | 0/0 | - | 0/1 >1000 | planning | - |
| run_2026-07-21_13-04-11_b1f6 | failed | 0/0 | 0/0 | - | 0/0 >1000 | routing | - |
| run_2026-07-21_13-04-11_07c1 | failed | 0/0 | 0/0 | - | 0/0 >1000 | routing | - |
| run_2026-07-21_06-27-37_509a | partial | 0/0 | 0/1 | - | 0/2 >1000 | requirement_coverage | event:contract_failed, verification:unrecovered_failure, +1 evidence |
| run_2026-07-21_06-24-18_bc79 | partial | 1/1 | 1/4 | - | 0/4 >1000 | requirement_coverage | event:contract_failed, verification:unrecovered_failure, +1 evidence |
| run_2026-07-21_06-22-18_3cd6 | needs_input | 0/2 | 0/0 | - | 0/2 >1000 | requirement_coverage | event:contract_failed |
| run_2026-07-21_06-18-23_1213 | needs_input | 1/1 | 0/1 | - | 0/2 >1000 | requirement_coverage | event:contract_failed, event:mutation_required_but_missing, +2 evidence |
| run_2026-07-21_06-08-04_c469 | timed_out | 0/0 | 0/0 | - | 0/1 >1000 | environment | event:mutation_required_but_missing, command:latest_failed |
| run_2026-07-21_06-05-00_0893 | partial | 0/0 | 0/0 | - | 0/1 >1000 | requirement_coverage | event:contract_failed, event:mutation_required_but_missing |
| run_2026-07-21_05-55-50_47ff | success_unverified | 1/1 | 0/1 | - | 0/1 >1000 | verification | - |
| run_2026-07-21_05-55-10_ae5e | success_unverified | 1/1 | 0/1 | - | 0/1 >1000 | verification | - |
| run_2026-07-21_01-04-15_da1b | success | 1/1 | 6/6 | - | 0/6 >1000 | none | - |
| run_2026-07-21_01-03-07_e76c | failed | 1/1 | 6/11 | - | 0/9 >1000 | verification | event:mutation_required_but_missing, verification:unrecovered_failure, +1 evidence |
| run_2026-07-21_00-57-09_b307 | success | 1/1 | 2/2 | - | 0/3 >1000 | none | - |
| run_2026-07-21_00-56-33_f658 | success | 1/1 | 3/3 | - | 0/4 >1000 | none | - |
| run_2026-07-21_00-55-30_730f | failed | 1/1 | 3/3 | - | 0/4 >1000 | tool_call | - |
| run_2026-07-21_00-53-41_e6b9 | failed | 1/1 | 0/5 | - | 0/8 >1000 | verification | verification:unrecovered_failure, command:latest_failed |
| run_2026-07-21_00-51-50_a1d0 | failed | 1/1 | 0/5 | - | 0/8 >1000 | verification | event:mutation_required_but_missing, verification:unrecovered_failure, +1 evidence |
| run_2026-07-21_00-48-31_4491 | failed | 1/1 | 0/3 | - | 0/4 >1000 | verification | verification:unrecovered_failure, command:latest_failed |
| run_2026-07-21_00-46-07_fd4f | failed | 0/0 | 0/2 | - | 0/3 >1000 | requirement_coverage | event:contract_failed, event:mutation_required_but_missing, +2 evidence |
| run_2026-07-21_00-45-34_55b6 | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_00-43-41_9343 | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_00-42-24_27c7 | failed | 0/0 | 0/0 | - | 0/1 >1000 | tool_call | - |
| run_2026-07-21_00-41-44_9404 | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-21_00-38-02_548f | success | 1/1 | 1/1 | - | 0/2 >1000 | none | - |
| run_2026-07-21_00-36-21_fe6e | failed | 1/1 | 1/1 | - | 0/3 >1000 | planning | event:mutation_required_but_missing |
| run_2026-07-21_00-33-42_3f97 | partial | 1/1 | 1/1 | - | 0/2 >1000 | planning | event:mutation_required_but_missing |
| run_2026-07-21_00-30-23_c184 | partial | 0/1 | 0/0 | - | 0/3 >1000 | requirement_coverage | event:contract_failed, event:mutation_required_but_missing |
| run_2026-07-21_00-27-43_2d85 | failed | 0/1 | 0/0 | - | 0/2 >1000 | patch_application | - |
| run_2026-07-21_00-25-13_9bed | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-20_23-52-13_7fae | success | 1/1 | 0/1 | - | 0/1 >1000 | verification | unverified-success |
| run_2026-07-20_23-51-24_7be5 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_23-47-20_104c | failed | 4/4 | 0/0 | - | 0/1 >1000 | routing | event:composite_failed |
| run_2026-07-20_23-46-30_5f7f | failed | 0/1 | 0/0 | - | 0/2 >1000 | patch_application | - |
| run_2026-07-20_23-45-47_f69b | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_23-45-02_5567 | success | 0/2 | 0/0 | - | 0/2 >1000 | verification | unverified-success |
| run_2026-07-20_23-44-16_b29a | success_unverified | 1/1 | 0/1 | - | 0/1 >1000 | verification | - |
| run_2026-07-20_23-43-42_1a65 | success | 0/0 | 0/0 | - | 0/1 >1000 | tool_call | false-success?, command:latest_failed |
| run_2026-07-20_23-43-11_20c7 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_23-42-52_8b38 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_22-49-15_5090 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_22-47-03_7e37 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_22-31-43_210d | failed | 1/1 | 1/1 | - | 0/3 >1000 | planning | event:mutation_required_but_missing |
| run_2026-07-20_22-17-47_8af0 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_22-15-45_ff6f | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_22-15-29_495b | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_21-59-02_846d | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_21-59-01_726e | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_21-56-39_e103 | success | 1/1 | 1/1 | - | 0/1 >1000 | none | - |
| run_2026-07-20_21-55-53_d793 | needs_input | 0/0 | 0/0 | - | 0/0 >1000 | planning | - |
| run_2026-07-20_21-54-04_bbc3 | failed | 0/0 | 0/0 | - | 0/1 >1000 | planning | event:mutation_required_but_missing |
| run_2026-07-20_21-53-42_4072 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_21-51-44_3479 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_21-50-56_0859 | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-20_21-48-18_4b90 | success | 1/1 | 1/1 | - | 0/2 >1000 | none | - |
| run_2026-07-20_21-45-21_5217 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_21-41-20_845a | failed | 0/0 | 0/0 | - | 0/1 >1000 | planning | event:composite_failed, event:mutation_required_but_missing |
| run_2026-07-20_21-40-29_6c5f | failed | 0/0 | 0/0 | - | 0/1 >1000 | planning | event:composite_failed, event:mutation_required_but_missing |
| run_2026-07-20_21-39-12_28ae | success | 1/1 | 1/1 | - | 0/2 >1000 | none | - |
| run_2026-07-20_21-38-20_4029 | success | 1/1 | 1/1 | - | 0/1 >1000 | none | - |
| run_2026-07-20_21-37-36_4a2f | needs_input | 0/0 | 0/0 | - | 0/1 >1000 | planning | - |
| run_2026-07-20_21-36-50_4f96 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_21-36-42_dc33 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_21-36-40_4e7e | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_16-26-46_0c22 | needs_input | 0/0 | 0/0 | - | 0/2 >1000 | tool_call | command:latest_failed |
| run_2026-07-20_16-25-40_7630 | success | 1/1 | 1/1 | - | 0/1 >1000 | none | - |
| run_2026-07-20_16-24-12_3c1c | failed | 0/0 | 0/0 | - | 0/1 >1000 | planning | event:mutation_required_but_missing |
| run_2026-07-20_16-23-10_1602 | failed | 0/0 | 0/0 | - | 0/2 >1000 | tool_call | event:mutation_required_but_missing |
| run_2026-07-20_16-21-13_d83f | success_unverified | 2/2 | 0/1 | - | 0/3 >1000 | verification | - |
| run_2026-07-20_16-20-44_8923 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_16-20-38_9e5d | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_12-54-10_cf9c | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_12-43-32_4afe | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_12-43-32_037a | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_12-43-31_905d | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_12-43-31_3e78 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_12-33-46_f21c | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_12-22-49_1fcc | failed | 0/0 | 0/0 | - | 0/0 >1000 | routing | command:latest_failed |
| run_2026-07-20_12-08-20_7275 | failed | 0/0 | 0/0 | - | 0/2 >1000 | tool_call | event:mutation_required_but_missing |
| run_2026-07-20_12-05-18_18f9 | denied | 0/2 | 0/0 | - | 0/2 >1000 | environment | - |
| run_2026-07-20_12-03-48_c32c | denied | 0/2 | 0/0 | - | 0/2 >1000 | environment | - |
| run_2026-07-20_10-17-16_e751 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_10-15-08_6a8d | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-20_10-12-56_ae74 | success_unverified | 1/1 | 0/1 | - | 0/1 >1000 | verification | - |
| run_2026-07-20_09-56-06_312b | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-19_17-51-06_e993 | denied | 0/0 | 0/0 | - | 0/0 >1000 | environment | - |
| run_2026-07-19_17-15-18_f7d8 | success | 1/1 | 1/1 | - | 0/3 >1000 | none | - |
| run_2026-07-19_17-12-05_304f | partial | 1/1 | 1/1 | - | 0/2 >1000 | planning | - |
| run_2026-07-19_16-35-51_4001 | success | 1/1 | 1/1 | - | 0/2 >1000 | none | - |
| run_2026-07-19_16-31-14_ac4d | success | 1/1 | 1/1 | - | 0/2 >1000 | none | - |
| run_2026-07-19_16-28-51_be69 | success_unverified | 1/1 | 0/0 | - | 0/1 >1000 | planning | event:mutation_required_but_missing |
| run_2026-07-19_16-28-28_235a | failed | 0/0 | 0/0 | - | 0/0 >1000 | routing | command:latest_failed |
| run_2026-07-19_15-57-52_753e | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-19_15-57-33_dbfe | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-19_15-51-03_dab0 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-19_15-30-37_b877 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-19_13-57-51_6df9 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-19_13-53-49_4ae6 | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-19_13-50-39_21e9 | success | 1/1 | 0/0 | - | 0/2 >1000 | verification | unverified-success |
| run_2026-07-19_13-50-04_c0ea | failed | 0/0 | 0/0 | - | 0/0 >1000 | routing | - |
| run_2026-07-19_13-48-07_bbcb | success | 0/0 | 0/0 | - | 0/1 >1000 | none | - |
| run_2026-07-19_13-44-42_cbb6 | success | 1/1 | 0/0 | - | 0/1 >1000 | verification | unverified-success |
| run_2026-07-19_13-44-23_a726 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-19_13-44-22_2902 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-19_13-44-20_083c | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-19_13-44-18_4466 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-19_09-28-10_d484 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-18_21-46-10_95f3 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-18_21-43-02_c715 | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-10_20-19-38_ff2b | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |
| run_2026-07-10_20-17-06_44ed | success | 0/0 | 0/0 | - | 0/0 >1000 | none | - |

## Data Quality

- apply metrics inferred from tool results because mutation records are absent
- mutations have no verification result event
- some tool results lack token telemetry
- some verification events lack verifier_id
