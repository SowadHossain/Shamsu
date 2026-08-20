# Implementation prompt — SHAMSU Remote UX

Paste everything below the line into a fresh Claude Code session opened at
`F:\Work\PROJECTS\shamsu`.

To change scope, edit the **SCOPE** section only — the rest stays valid.

---

Implement the SHAMSU Remote UX plan. Three PRDs describe it; read all three
before writing code, in this order:

1. `Shamsu/PRD_REMOTE_UX.md` — the core: one `TurnEvent` stream, three
   renderers; Telegram live turn card; install-bound token; multi-workspace;
   web portal.
2. `Shamsu/PRD_REMOTE_UX_DATA.md` — Addendum 1: central SQLite catalog and
   cross-process control plane. **Supersedes §10 and §11 of the first doc.**
3. `Shamsu/PRD_REMOTE_UX_TRANSPORT_CLI.md` — Addendum 2: webhook transport over
   a zero-config Cloudflare tunnel, the `shamsu serve` daemon, and the CLI.
   **Supersedes the delivery plan again (its §5 is the current one)** and
   replaces long polling throughout.

Where they conflict, the later document wins. Addendum 2 §0 is a re-baseline —
read it first if you read nothing else, because it records what already shipped.

## SCOPE for this session

**Phase P0 + P1 only.** They are independently shippable and they fix the
complaint that started the project: the CLI prints every line of a run, and
Telegram throws most of it away.

- **P0** — `TurnEvent` + `TurnStream` + `activity.jsonl`; add a single `emit`
  seam to `SimpleChatLoop`; reimplement the CLI renderer on top of it with
  **identical observable behaviour** (that identity is the safety proof).
- **P1** — the Telegram live turn card: `parse_mode` on `OutboundMessage`, the
  card renderer, 1.5s coalescing, overflow into continuation cards, the
  `shamsu (remote-telegram)>` prompt echo on both surfaces, and
  `sendChatAction("typing")`.

Do **not** start the catalog, control plane, webhook, daemon or web portal in
this session. They are planned in the addenda and have their own phases.

## Repo orientation — traps that will cost you an hour each

- **The real package is `Shamsu/shamsu/`.** `Shamsu/src/shamsu/` exists and is
  **empty** — an abandoned v2 scaffold. Do not put code there.
- **`Shamsu/CLAUDE.md` is partly stale.** It describes a Linux checkout at
  `/home/shamsu/Shamsu` and a layout (`shamsu/abstract/cli`, `src/` package)
  that does not match this Windows tree. Trust the filesystem over that file.
- **Another agent (Codex) edits this repo concurrently.** During planning,
  `agents/simple_chat.py` went from 1,698 to 4,091 lines in one day. **Re-read
  every file immediately before you edit it**, and expect line numbers in the
  PRDs to have drifted. The PRDs cite anchors by symbol name as well — prefer
  those.
- Python venv is at `Shamsu/.venv`. Run from `Shamsu/`.

## Already done — do not rebuild

- **Mid-run feedback ships already**: `agents/simple_feedback.py`
  (`FeedbackQueue`) and `_LiveFeedbackReader` in `cli/repl.py`. It steers the
  *running* turn at a round boundary, which is better than the reference
  implementation it was modelled on. Addendum 2 §0.1 has the detail.
- The Telegram **transport already supports `editMessageText`** via
  `OutboundMessage.edit_message_id` — the live card needs no new transport
  primitive, only `parse_mode`.

## The actual bug P1 fixes

`shamsu/integrations/telegram/sessions.py`:

- `SimpleChatLoop` is constructed with `on_activity=lambda m: progress.step(m)`
  and **no `on_status`** — so a 136-second model call is total silence on the
  phone.
- `TelegramProgressReporter._should_notify` drops every `progress.step` unless
  ≥8s have passed **and** the text differs from the last one sent.
- Each surviving line becomes a separate chat message prefixed `Working: `.

The strings are already correct and already identical to the CLI's
(`_activity()` and `_argument_summary()` in `agents/simple_chat.py`). This is a
delivery problem, not a formatting one. Do not rewrite the strings.

## Acceptance criteria

P0 is done when the CLI behaves exactly as before, now driven by `TurnStream`.

P1 is done when:

1. A **test** drives `SimpleChatLoop` with a scripted fake client and both
   renderers attached, and asserts the ordered CLI lines equal the ordered
   Telegram card lines. This is the executable form of the goal — write it
   first if you can.
2. A long model call shows a ticking footer and a `typing` action; no gap over
   ~5s of visible silence.
3. A 40-tool turn makes at most `ceil(duration / 1.5)` Telegram API calls and
   loses no line.
4. Card overflow past ~3900 chars seals the card and starts a continuation —
   concatenating card bodies reproduces the input exactly, with no truncation
   marker mid-line.

## Constraints — these are load-bearing, not preferences

- **Zero new runtime dependencies.** The Telegram integration is raw `httpx`
  against the Bot API by deliberate choice; keep it that way.
- **Never drop an event to protect the API.** Bound the *edit rate* and let
  lines accumulate between flushes. Today's code does the opposite, and that is
  the bug.
- `activity.jsonl` is UI telemetry and must stay **separate from**
  `messages.jsonl`, which is the model's context and is lossless on purpose.
  Do not put status ticks into the transcript.
- Everything rendered passes `redact()` — Telegram, the activity log, all of it.
- Additive changes to `SimpleChatLoop`: `on_activity` / `on_status` / `on_trace`
  stay as shims over `emit`, so existing callers and tests keep working.

## Verification

- Run the test suite before you start, to get today's baseline, and again at the
  end. It was last recorded at ~2576 passing. **Do not regress it**; if the
  baseline is already red when you start, say so rather than absorbing it.
- Report the real numbers. If something is untested or you could not verify it
  live, say which part and why.

## Working agreement

- Show me the plan for P0 before you write it — specifically the `TurnEvent`
  shape and where `emit` is called in `SimpleChatLoop`.
- Small commits per phase; do not mix P0 and P1 in one change.
- If a PRD decision looks wrong once you are in the code, say so and argue it.
  The plans were written from a read of the tree, not from running it.
