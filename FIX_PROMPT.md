Read `SMALLCODE_GAP_ANALYSIS.md` in this repo and implement **§6.8's checklist**
(items 1–7). That section is the spec — follow it rather than re-deriving the design.

Context you need before touching anything:

- The live package is `Shamsu/shamsu/`. `Shamsu/src/shamsu/` is an empty stale tree — ignore it.
- The default path is **simple mode**: `shamsu/agents/simple_chat.py`.
  `chat_loop.py` is legacy (`SHAMSU_LEGACY_ROUTING=1`) — read it for the
  continue-from-the-tail recovery at line 4465, but ship the fix in simple mode.
- Goal: restore smallcode's **4x headroom ratio** so writes can never exhaust the
  reply budget. Bound the write size; let the tool-call count grow. That trade is
  already decided — more calls is fine, truncation is not.

Non-negotiables:

- **Do not shrink `MAX_REPLY_TOKENS`.** Bound the unit of work, not the budget.
- Items 4 (chunk verification reports open blocks as *progress*) and 5 (pre-write
  gate tests for *truncation signatures*, not validity) must land in the **same
  change** as items 1–3. Without them, chunking creates false "unclosed brace"
  failures on half-built files — see §6.6 and §6.7.
- The cap is the **minimum of two walls**: the reply budget AND llama.cpp's ~13KB
  tool-argument limit. A budget-only cap still breaks on a large window.

Work in order, smallest shippable step first. After each item, run the relevant
tests. Live-test on a small model (`qwen2.5:3b-instruct`), one run at a time —
VRAM won't take two.

Acceptance (§6.8): ask it to write a 1,500-line file in one prompt. Expect 6–18
successful calls, zero truncation refusals, and a file that parses.
