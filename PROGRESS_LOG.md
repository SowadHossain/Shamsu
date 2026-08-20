# SHAMSU Progress Log

One entry per completed task, newest at the top. Raw model/test output lives in
`logs/test-runs/<date>-<task>.log`.

### 2026-08-20 — Phase 0: verification stops reporting a patch-broken file as "still being built"
Files edited: `shamsu/agents/simple_chat.py`, `shamsu/agents/simple_verify.py`,
`shamsu/agents/chat_state.py`, `tests/test_simple_chat.py`
What changed: `_verify` suppressed a real syntax error whenever the file's only
complaint was open blocks - which is what a patch that eats a `}` leaves behind,
and also what the first section of a chunked write looks like. Nothing told them
apart, so `node --check: SyntaxError` became `{"ok": true, "continue with
append_file"}` and a model asked to fix the file had just been told it was fine.
The exemption is now gated on the last write having ADDED to the file. Also:
unclosed blocks point at the innermost opener rather than line 1; the repair
counter that gates thinking is a streak reset by any successful write, not a
turn-wide tally; and the two write-refusal stops plus the OOM stop no longer
replay into history as assistant turns.
Tests: 5 new behavioural tests + 2 extended; full suite 3319 passed / 0 failed;
ruff at baseline (208, unchanged). Live on `qwen2.5-coder:3b-instruct`: a
non-growing write got the real node error while the append before it correctly
stayed "still being built". | Log: logs/test-runs/2026-08-20-phase0-verify-truth.log
