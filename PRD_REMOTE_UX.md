# PRD — SHAMSU Remote UX

**Telegram transcript parity · install-bound bot · multi-workspace remote · local web chat**

Status: DRAFT — for review. No code has been changed.
Author: Claude (planning pass, 2026-08-18)
Scope: `shamsu/agents/simple_chat.py`, `shamsu/integrations/telegram/*`,
`shamsu/cli/repl.py`, plus one new package `shamsu/webui/`.

> **Companion document: [`PRD_REMOTE_UX_DATA.md`](PRD_REMOTE_UX_DATA.md)** —
> storage, central catalog and cross-process control plane. It **supersedes §10
> and §11 below**, and revises §8.2 (the web server's process model). Read it
> before implementing anything past P2: the measured finding is that
> `SessionLogger.log()` rewrites a 122 KB index under a global lock on every
> event, and that run state lives in a module-level dict no other process can
> see.

---

## 1. Why

Three complaints, one root cause.

1. **Telegram shows a summary; the CLI shows the work.** On the desktop you see
   every line — `model responded in 136s`, `read_file frontend/game.js`,
   `write_file DEVELOPMENT_PLAN.md`. On the phone you get a handful of
   `Working: …` messages and then a final answer. You cannot tell a 10-minute
   model call from a hang.
2. **The remote is welded to one project.** The bot token, the pairing database
   and the session list all live under `<workspace>/.shamsu/`. Change project,
   lose the bot; re-pair from scratch.
3. **There is no way to browse threads.** Sessions, transcripts and runs are all
   on disk in readable form, and nothing reads them back except `/sessions show`.

The root cause of (1) is that SHAMSU has **one activity stream with three
different sinks that were written independently**. The CLI sink prints
everything. The Telegram sink throttles to one message per 8 seconds and drops
duplicates. There is no third sink at all. Fixing this properly — one typed
event stream, many renderers — is what makes (1) and the whole web UI cheap
instead of expensive, so it is the spine of this document.

---

## 2. What is true today

Everything below was read out of the tree, not assumed.

### 2.1 The turn already emits exactly the lines you want

`SimpleChatLoop` takes three callbacks
([simple_chat.py:496-530](shamsu/agents/simple_chat.py#L496-L530)):

| Callback | Emits | Cardinality |
|---|---|---|
| `on_activity` | `model responded in 136s` ([:818](shamsu/agents/simple_chat.py#L818)), `read_file frontend/game.js` ([:1033](shamsu/agents/simple_chat.py#L1033)), compaction, nudges, refusals | append-only, one per event |
| `on_status` | `running read_file... 12s` heartbeat ([:1344-1352](shamsu/agents/simple_chat.py#L1344-L1352)) | **replaces** the previous line |
| `on_trace` | structured `("simple.tool", msg, {"tool": name})` | debug channel |

The exact strings you quoted come from `_activity()` at
[:1330](shamsu/agents/simple_chat.py#L1330), and the `read_file <path>` form is
`_argument_summary()` at [:1403](shamsu/agents/simple_chat.py#L1403). **Parity is
not a formatting problem — the strings are already identical.** It is a
delivery problem.

### 2.2 The CLI keeps all of it; Telegram throws most of it away

CLI ([repl.py:4627-4633](shamsu/cli/repl.py#L4627-L4633)):

```python
on_activity=lambda message: console.print(f"[dim]{message}[/dim]"),
on_status=_status_updater(thinking_status),
```

Telegram ([sessions.py:302-327](shamsu/integrations/telegram/sessions.py#L302-L327)):

```python
loop = SimpleChatLoop(..., on_activity=lambda message: progress.step(str(message)))
```

Then `TelegramProgressReporter._should_notify`
([sessions.py:397-408](shamsu/integrations/telegram/sessions.py#L397-L408)):

```python
if kind == "progress.step" and message != self._last_sent_message:
    return now - self._last_sent_at >= self.min_interval_seconds   # 8.0
return False
```

So on the phone:

- `on_status` is **never passed** → the 136-second model call is total silence.
- `on_activity` lines are dropped unless ≥8 s since the last one **and** the text
  differs from the last one sent.
- Every surviving line becomes a **separate chat message** prefixed `Working: `
  ([sessions.py:410-421](shamsu/integrations/telegram/sessions.py#L410-L421)),
  so a 12-tool turn would be 12 notifications if it were not throttled. The
  throttle exists because the delivery shape is wrong, not because the
  information is unwanted.

This is the entire bug. It is small.

### 2.3 Token and remote state are workspace-scoped

- `load_telegram_bot_token()` reads `$SHAMSU_TELEGRAM_BOT_TOKEN`, then
  `<workspace>/.shamsu/telegram.env`, then `<workspace>/.env`
  ([service.py:328-342](shamsu/integrations/telegram/service.py#L328-L342)).
- `configure_telegram_bot_token()` **writes into the workspace sandbox**
  ([service.py:345-353](shamsu/integrations/telegram/service.py#L345-L353)).
- Pairings, authorizations, callback tokens, audit and the `getUpdates` offset
  live in `<workspace>/.shamsu/telegram/telegram-state.db`.

Consequence, already documented in `TELEGRAM_BOT.md` §4.2: switching project
means re-configuring the token **and re-pairing**. That is the opposite of
"bound to the install until I change it".

### 2.4 One bot token = one long-poll consumer

`TELEGRAM_BOT.md` §4.3 is right and this PRD does not try to repeal it: if two
processes call `getUpdates` with the same token, Telegram splits updates
non-deterministically between them. **Multi-workspace remote therefore cannot be
"run the bot in every REPL".** It has to be "one poller, many workspaces" — see
§7.

### 2.5 The transport can already do live editing

`OutboundMessage.edit_message_id` → `editMessageText`
([transport.py:74-88](shamsu/integrations/telegram/transport.py#L74-L88)). The
primitive the live turn card needs is present and used by the approval flow.
Missing: a `parse_mode` field on `OutboundMessage`
([models.py:135-141](shamsu/integrations/telegram/models.py#L135-L141)).

### 2.6 A verbosity setting already exists and is unused

`TelegramSettings.tool_by_tool_updates: bool = False`
([models.py:172-177](shamsu/integrations/telegram/models.py#L172-L177)) and
`NotificationFilter.should_send("tool", …)` exist. Nothing on the run path calls
them.

### 2.7 There is no web surface and no web dependency

No `fastapi`, `flask`, `uvicorn`, `starlette` or `websockets` in `pyproject.toml`.
`httpx` is a client only. Sessions are already durable and readable:
`<workspace>/.shamsu/sessions/<id>/messages.jsonl`, lossless and redacted
([manager.py:703-732](shamsu/session/manager.py#L703-L732)).

---

## 3. Goals / non-goals

### Goals

- **G1** Every line the CLI prints during a turn reaches Telegram, in order,
  with no information dropped.
- **G2** A prompt sent from Telegram is echoed as `shamsu (remote-telegram)> …`
  on both surfaces, so both read like a terminal transcript.
- **G3** The bot token binds to the **installation**, survives project switches,
  and changes only when explicitly reconfigured.
- **G4** With `/remote` enabled, every known workspace and every session in them
  is listable and switchable **from Telegram**.
- **G5** A local web chat at `http://127.0.0.1:<port>` showing all workspaces,
  all threads, live streaming turns, and an input box — feeling like the Ollama
  web chat / Claude chat, not like a log viewer.
- **G6** Zero new runtime dependencies (matching the precedent in
  `TELEGRAM_BOT.md` §1.1). Non-negotiable for the Telegram work; see §8.1 for
  the web trade-off.

### Non-goals

- No cloud hosting, no webhook, no public port. Loopback and outbound only.
- No multi-user/tenant model. `PermissionLevel` stays as-is (`TELEGRAM_BOT.md`
  §2.8 gap is out of scope, but noted below as a follow-up).
- Not replacing the CLI. The CLI stays the reference surface; the others mirror.
- No rewrite of the legacy orchestrator. All of this targets SIMPLE mode, which
  is the default; the legacy path keeps its current (worse) reporting.

---

## 4. The spine: one turn stream, three renderers

Everything else is a renderer over this.

### 4.1 The event

New module `shamsu/runtime/turn_stream.py`:

```python
Kind = Literal[
    "turn.start",     # the prompt, plus who sent it and from where
    "status",         # transient; REPLACES the previous status
    "activity",       # append-only line; the CLI's dim lines, verbatim
    "tool.call",      # name + argument summary + full arguments
    "tool.result",    # ok / message / changed paths / diffstat
    "approval",       # an approval is pending (id, description, risk)
    "assistant",      # the final markdown answer
    "turn.end",       # status, elapsed, files changed
    "error",
]

@dataclass(frozen=True)
class TurnEvent:
    seq: int              # monotonic per turn — renderers dedupe/reorder on this
    kind: Kind
    text: str             # the exact string the CLI would print
    data: dict[str, Any]  # structured payload; may be {}
    ts: float             # time.time()
    turn_id: str
    session_id: str
    workspace: str
    source: str           # "cli" | "telegram" | "web"
```

`text` is deliberately the CLI's own string. Renderers that want a richer view
use `data`; renderers that want parity print `text`. That is how G1 becomes
provably true instead of aspirationally true.

### 4.2 Emitting

`SimpleChatLoop` gains **one** optional `emit: Callable[[TurnEvent], None]`.
`on_activity` / `on_status` / `on_trace` stay as thin shims over it, so nothing
that constructs the loop today breaks (`chat_loop.py`, tests, evals). Internally:

- `_activity(msg)` → `emit(kind="activity", text=msg)`
- `_status(msg)` → `emit(kind="status", text=msg)`
- `_run_tools` → `emit("tool.call", text=f"{name} {summary}", data={...})`
  before the call and `emit("tool.result", …)` after, at
  [:1033-1080](shamsu/agents/simple_chat.py#L1033-L1080). Today only the
  pre-call line is emitted; adding the result event is what makes the web UI's
  collapsible tool cards and the diff previews possible.
- `run()` brackets with `turn.start` / `turn.end`.

**Estimated diff: ~80 lines in `simple_chat.py`, all additive.**

### 4.3 The bus

`TurnStream` (same module) is a per-session fan-out:

- `publish(event)` → appends to `<workspace>/.shamsu/sessions/<id>/activity.jsonl`
  **and** pushes to every live subscriber queue.
- `subscribe(since_seq)` → replays from the file, then tails live. This is what
  lets a phone that was locked, or a browser tab opened mid-turn, catch up with
  no special case.
- Bounded per-subscriber queue (say 1000); on overflow, drop `status` events
  first, then coalesce `activity` — never drop `tool.*`, `approval`,
  `assistant`, `turn.end`.
- One writer per session enforced by the existing claim machinery
  (`SessionLogger.claim()` / `claimed_by_other_live_process()`,
  [manager.py:464-512](shamsu/session/manager.py#L464-L512)).

`activity.jsonl` is a **separate file from `messages.jsonl` on purpose**:
messages are the model's context and must stay lossless and clean;
activity is high-frequency UI telemetry. Mixing them would put status ticks into
the model's memory.

All `text` passes `redact()` before it is written — the same guarantee the
transport gives today ([transport.py:78](shamsu/integrations/telegram/transport.py#L78)).

### 4.4 The three renderers

| Renderer | `status` | `activity` / `tool.*` | `assistant` |
|---|---|---|---|
| CLI (`repl.py`) | updates the live spinner | `console.print(dim)` | `Markdown()` |
| Telegram | footer line of the live card | appended to the card body | new message(s), paged |
| Web | footer of the streaming bubble | a live log block in the bubble | rendered markdown |

The CLI renderer becomes a 10-line adapter; today's two lambdas are exactly it.

---

## 5. F1 — Telegram transcript parity

### 5.1 The turn card

When a prompt arrives, the bot sends **one** message and then edits it as the
turn runs. This is the whole idea: a terminal pane, not a notification feed.

```
shamsu (remote-telegram)> add a pause menu to the game

model responded in 136s
read_file frontend/game.js
model responded in 111s
write_file DEVELOPMENT_PLAN.md
write_file frontend/game.js
running python -m compileall frontend  ✓

⏳ running write_file... 14s
```

- Header = `shamsu (<label>)> <prompt>` — see §5.5.
- Body = the `activity` / `tool.*` lines **verbatim, all of them, in order**.
- Footer = the current `status`, replaced in place, cleared at `turn.end`.
- Sent as HTML with the body in `<pre>` so it renders monospace and aligns like
  a terminal. Requires adding `parse_mode` to `OutboundMessage` and escaping
  `& < >` in `text`.
- Inline keyboard under the card: `Stop` · `Changes` · `Full log`.

When the turn ends the footer becomes a one-line verdict
(`done in 6m12s · 2 files changed`) and the keyboard becomes `Changes` · `Diff`
· `Full log`.

### 5.2 Coalescing and rate limits

Telegram allows roughly 1 message/second per chat and rate-limits
`editMessageText` similarly; a 20-tool turn firing an edit per line will get
`429`d.

**Rule: a render thread edits the card at most once every `N` seconds
(default `1.5`), and only if the rendered text changed.** Events are never
dropped — they accumulate in the card body between flushes. This inverts
today's behaviour: today events are dropped to protect the API; here the API
call rate is bounded and events are preserved.

- Immediate flush (bypassing the interval) for `approval`, `error`, `turn.end`.
- On `429`, honour `retry_after`, back off, keep accumulating.
- `sendChatAction("typing")` every ~4 s while a model call is in flight — free,
  and it is the idiom every Telegram user already reads as "it's working".

### 5.3 Overflow

Telegram caps a message at 4096 chars (`MAX_MESSAGE_CHARS = 3900` already in
[formatter.py:13](shamsu/integrations/telegram/formatter.py#L13)).

When the card would exceed the budget: **seal it** (final edit, no footer), and
start a **continuation card** whose header is `… continued`. Long turns become a
readable sequence of terminal panes rather than one truncated blob. The full,
untruncated log is always available via the `Full log` button, which uploads
`activity.jsonl` (rendered to text) as a document — `sendDocument` is already
wired ([transport.py:90-96](shamsu/integrations/telegram/transport.py#L90-L96)).

### 5.4 The final answer

Sent as its own message(s) after the card is sealed, so it is quotable and
forwardable:

- Markdown → Telegram HTML (bold/italic/inline code/links); fenced code blocks →
  `<pre><code class="language-x">`.
- Paged with the existing `Page` machinery at `MAX_MESSAGE_CHARS`.
- Code blocks over ~2500 chars are sent as a document instead, named after the
  file when we know it.

### 5.5 Prompt echo, both directions

You asked for `shamsu (remote-telegram)> [prompt]`. Two readings; we do both,
because they cost the same:

- **On Telegram**, the card header is `shamsu (<session-title>)> <prompt>` —
  matching `_session_prompt_label()`
  ([repl.py:18224-18239](shamsu/cli/repl.py#L18224-L18239)) so the phone and the
  desktop use one vocabulary.
- **On the desktop**, when a prompt arrives from Telegram the CLI mirror prints

  ```
  shamsu (remote-telegram)> add a pause menu to the game
  ```

  followed by the same dim activity lines the local turn would produce, instead
  of today's cyan `Panel` ([local.py:14-24](shamsu/integrations/telegram/local.py#L14-L24)).
  The `remote-telegram` label is the **source**, configurable, and the web UI
  will use `remote-web` by the same rule.

The desktop must never *silently* interleave a remote turn with a local one:
if the local REPL is mid-turn, the mirror buffers and prints after the local
turn completes, with a `(from telegram, 14:02)` timestamp.

### 5.6 Verbosity, since not everyone wants 40 lines

Wire the setting that already exists. `/settings` gains a **Verbosity** row:

| Level | Card body contains |
|---|---|
| `quiet` | tool names only, no timings; final answer |
| `normal` *(default)* | **exact CLI parity** — every `activity` and `tool.call` line |
| `verbose` | parity + `tool.result` summaries + diffstats + `status` history |

`normal` is the default because it is what you asked for; `quiet` exists so the
feature is not a spam machine for someone else.

### 5.7 Acceptance criteria

1. Run the same prompt on the CLI and from Telegram in the same workspace.
   Concatenate the Telegram card bodies; the ordered list of lines is **equal**
   to the ordered list of CLI dim lines. Asserted by a test using
   `FakeTelegramTransport`, not by eye.
2. A 136-second model call produces a visibly ticking footer and a `typing`
   action, never >5 s of dead air.
3. A 40-tool turn produces ≤ `ceil(duration/1.5)` API calls and loses no line.
4. Killing the phone's network mid-turn and reopening the chat shows the
   complete card (because state is rebuilt from `activity.jsonl`, not from
   what was sent).

---

## 6. F2 — Install-bound bot token

**Change the search order and the write target.**

New resolution order in `load_telegram_bot_token()`:

1. `$SHAMSU_TELEGRAM_BOT_TOKEN` — unchanged, still wins (CI/ops override).
2. `~/.shamsu/telegram.env` — **new, and where `configure` now writes.**
3. `<workspace>/.shamsu/telegram.env` — kept, now a per-project *override*.
4. `<workspace>/.env` — kept for compatibility.

`configure_telegram_bot_token(token)` writes `~/.shamsu/telegram.env` with mode
`0600` (best-effort on Windows: ACL to the current user), unless
`--workspace` is passed. Command: `/remote_control configure <token>` keeps
working and now means "bind to this installation".

Migration: on first `/remote_control` after upgrade, if a workspace token exists
and no install token does, copy it up and print
`Bot token promoted to this installation (~/.shamsu/telegram.env). It now
applies to every project.` Never delete the old file; never print the token
(existing rule, `TELEGRAM_BOT.md` §2.6).

**Pairings move with it.** The state DB moves to
`~/.shamsu/telegram/telegram-state.db`, because a pairing that does not survive
a project switch defeats the point. Workspace-scoped rows (`sessions`, `audit`)
gain a `workspace` column; `getUpdates` offset becomes install-global (correct —
there is one bot). Existing workspace DBs are imported once, then left alone.

> **Security note.** This widens the blast radius: one pairing now reaches every
> registered workspace. That is exactly what G4 asks for, and it is the same
> trust boundary as "someone has your laptop's shell". Mitigations: the
> workspace registry is explicit opt-in (§7.2), `Sandbox` still confines every
> file operation to the *active* workspace, approvals still gate mutations, and
> `/remote_control disconnect` still revokes every pairing.

---

## 7. F3 — Workspaces and sessions from Telegram

### 7.1 Install-scoped remote state

Per §6, remote identity lives at `~/.shamsu/telegram/`. `TelegramService` stops
being "the service for workspace W" and becomes "the service for this install,
currently focused on workspace W".

### 7.2 The workspace registry

New `shamsu/runtime/workspaces.py`, storing `~/.shamsu/workspaces.json`:

```json
{"workspaces": [
  {"path": "F:/Work/PROJECTS/game", "label": "game",
   "added_at": "...", "last_used": "...", "pinned": true}
]}
```

- Every REPL start registers its workspace (one line in `main()`).
- Entries whose path no longer contains `.shamsu/` are pruned lazily on read.
- Explicit management: `/workspaces list|add <path>|remove <path>|label <path> <name>`
  on the CLI; the bot can only *select* from the registry, never add an
  arbitrary path from a chat message. That keeps a compromised chat from
  pointing SHAMSU at `C:\`.

### 7.3 One poller, many workspaces

`LocalTelegramBridgeManager` ([local.py:27](shamsu/integrations/telegram/local.py#L27))
stops tearing down the service on workspace change. Instead:

- A `FileLock` at `~/.shamsu/runtime/telegram-poller.lock` (filelock is already a
  dependency, used at [manager.py:403](shamsu/session/manager.py#L403)) elects
  **one** polling process. First REPL to enable `/remote` wins.
- A non-owner REPL prints `Telegram remote is served by PID 12345 (project
  "game"). This project is reachable from the bot.` and does **not** poll. This
  is the only correct answer to `TELEGRAM_BOT.md` §4.3 without a daemon.
- `SessionGateway` becomes a **map** `workspace → LocalShamsuSessionGateway`,
  built lazily. Each already only needs a `Path`
  ([sessions.py:89-99](shamsu/integrations/telegram/sessions.py#L89-L99)), so
  this is a dict and a factory, not a redesign.
- The poller owns a per-chat **active workspace + active session** pair in the
  state DB; every inbound message resolves through it.

If the owning REPL exits, it releases the lock and any other live REPL claims it
on its next tick (10 s). If no REPL is running, the bot is offline — same as
today, and honest about it (`/remote_control status` says so).

> **Optional follow-up, not in scope:** a headless `shamsu remote serve` daemon
> so the bot survives with no REPL open. The lock design above makes that a
> drop-in later.

### 7.4 Commands and UI

Add to `COMMANDS` ([commands.py:7-22](shamsu/integrations/telegram/commands.py#L7-L22)):

| Command | Behaviour |
|---|---|
| `/projects` | list registered workspaces, inline keyboard to switch |
| `/use <name>` | switch active workspace by label or index |
| `/sessions` | *(exists)* now lists sessions **in the active workspace**, newest first, with title, status, last activity |
| `/new [title]` | *(exists)* create a session in the active workspace |
| `/switch <n>` | *(exists)* now accepts an index or a title fragment |
| `/where` | one line: active project, session, branch, dirty-file count |
| `/verbosity <quiet\|normal\|verbose>` | §5.6 |
| `/stop` | alias for `/cancel`, because that is what people type |

`/start` and the home card show **Project** and **Session** as the top two rows,
each a button. Switching either is two taps.

**Concurrency rule:** switching workspace or session never interrupts a running
turn; it is refused with `A run is active in "game". Send /stop first, or switch
after it finishes.` Sessions are single-writer by design
([manager.py:481](shamsu/session/manager.py#L481)) and this respects that.

### 7.5 Contention with a live REPL

Today Telegram calls `resume_session` on the *latest* session in the workspace —
which is usually the one the desktop REPL is sitting in. That is how legacy tool
names leaked into a simple-mode transcript (the comment at
[sessions.py:273-282](shamsu/integrations/telegram/sessions.py#L273-L282)).

New rule: **Telegram gets its own session by default.** On first use in a
workspace it creates/uses a session titled `remote`. `/switch` can attach it to
the desktop's session deliberately; when it does, the claim check refuses if the
desktop is mid-turn. Sharing a thread should be a choice, not an accident.

### 7.6 Acceptance criteria

1. Configure the token once. Open a REPL in project A, `/remote_control connect`,
   pair. Close it. Open a REPL in project B, `/remote_control connect`. The
   phone is still paired, and `/projects` lists A and B.
2. `/use A` then a prompt writes files in A; `/use B` then a prompt writes files
   in B. `Sandbox` rejects any path outside the active workspace (existing
   guarantee, re-asserted by test).
3. Two REPLs open at once → exactly one polls; `getUpdates` offset never
   regresses; no message is processed twice
   (`store.mark_update_processed` already idempotent,
   [controller.py:49-51](shamsu/integrations/telegram/controller.py#L49-L51)).

---

## 8. F4 — Local web chat

> "like we have for ollama … where I can see all the chat threads … all the
> sessions and workspaces viewable"

### 8.1 Technology decision

**Recommendation: stdlib `ThreadingHTTPServer` + Server-Sent Events + one
hand-written HTML/CSS/JS file. Zero new dependencies.**

Rationale:

- The Telegram integration set the precedent — raw `httpx` over the Bot API, no
  library, zero new deps (`TELEGRAM_BOT.md` §1.1) — and it worked out.
- The API surface is ~10 endpoints. FastAPI would buy validation we do not need
  and cost ~15 MB plus an ASGI server on a tool that advertises low RAM.
- **SSE, not WebSockets**: the stream is one-directional (server → browser);
  prompts go over plain `POST`. SSE is ~30 lines on `ThreadingHTTPServer`,
  reconnects automatically in the browser, and carries a `Last-Event-ID` that
  maps exactly onto `TurnEvent.seq` for gap-free resume.
- No build step, no npm, no bundler. One `app.html`, one `app.css`, one `app.js`
  shipped as package data.

Rejected alternatives, briefly: FastAPI+uvicorn (dep weight, no benefit at this
size); Textual/TUI (does not meet "run it in the web"); Gradio/Streamlit
(opinionated UI, heavy, cannot look like a chat client).

### 8.2 Process model

New package `shamsu/webui/` (`server.py`, `api.py`, `sse.py`, `static/`).

- `/web-ui start [--port 8765]` from the REPL, or `python -m shamsu.webui`.
- Binds **`127.0.0.1` only**. Never `0.0.0.0`.
- Runs in a daemon thread inside the REPL process (same trick as the Telegram
  bridge, [local.py:58-76](shamsu/integrations/telegram/local.py#L58-L76)), so
  it shares the `TurnStream` and the session claim in-process.

  > **Revised by [`PRD_REMOTE_UX_DATA.md`](PRD_REMOTE_UX_DATA.md) §2.3.** This
  > in-process design exists only to work around `run_control._RUNS` being a
  > module-level dict. It costs: no portal without an open REPL, and two REPLs
  > give two portals blind to each other. With the control plane (PD-b) the
  > portal can run in its own process. Recommend that; keep the in-thread mode
  > as a fallback.
- Prints one line: `Web chat: http://127.0.0.1:8765/?t=<token>`.

### 8.3 HTTP surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | app shell |
| `GET` | `/api/workspaces` | registry (§7.2) + per-workspace session counts |
| `GET` | `/api/workspaces/{id}/sessions` | list; title, status, last activity, message count |
| `POST` | `/api/workspaces/{id}/sessions` | new thread |
| `GET` | `/api/sessions/{id}/messages?after=N` | from `messages.jsonl` |
| `GET` | `/api/sessions/{id}/activity?turn=X` | past turn's `activity.jsonl` |
| `GET` | `/api/sessions/{id}/stream` | **SSE** of `TurnEvent`, honours `Last-Event-ID` |
| `POST` | `/api/sessions/{id}/prompt` | `{text}` → starts a turn, `202` + `turn_id` |
| `POST` | `/api/sessions/{id}/stop` | cancel |
| `POST` | `/api/approvals/{id}` | `{decision: allow\|deny}` |
| `GET` | `/api/sessions/{id}/changes` | `git diff` for the workspace |
| `GET` | `/api/health` | model, Ollama reachability, VRAM, active runs |

`PATCH /api/sessions/{id}` for rename/close maps onto existing
`rename_session` / `close_session`.

### 8.4 Screens

**One screen, three panes** — the shape every chat client converged on:

```
┌────────────┬───────────────────────────────────┬───────────────┐
│ WORKSPACES │  shamsu (remote-web)> add pause    │  CONTEXT      │
│ ▸ game  ●3 │                                    │               │
│   shamsu   │  ▸ read_file frontend/game.js 0.4s │ Model         │
│   openbzr  │  ▸ write_file game.js  +42 −3      │ qwen3:8b      │
│            │    ⤷ [diff]                        │ Context       │
│ THREADS    │                                    │ ████░░ 18k/32k│
│ ● remote   │  Added a pause menu…               │               │
│   pause-ui │                                    │ Changed       │
│   day-2    │  ⏳ running write_file... 14s       │ 2 files       │
│            │ ┌────────────────────────────────┐ │ [Review diff] │
│ + New      │ │ Message SHAMSU…           [↵]  │ │               │
└────────────┴─┴────────────────────────────────┴─┴───────────────┘
```

- **Left**: workspaces (collapsible groups) → threads. Active run gets a pulsing
  dot. Search box filters via the existing `search_sessions()`
  ([manager.py:339](shamsu/session/manager.py#L339)).
- **Centre**: the conversation. User bubbles plain; assistant bubbles rendered
  markdown with syntax highlighting. **Tool calls are inline cards**, collapsed
  to one line (`read_file frontend/game.js · 0.4s`), expanding to arguments,
  result and diff. During a live turn the newest bubble carries the
  terminal-style log block and the status footer — the same `TurnEvent` stream
  as the card in §5.1.
- **Right**: run context — model, context-window gauge, changed files, active
  approvals, cancel button. Collapsible; hidden on mobile.
- **Approvals** render as an inline card with `Allow` / `Deny`, mirroring
  `TelegramApprovalBroker` so a decision from either surface resolves the same
  `approval_id`.

### 8.5 Look and feel

Target: Ollama's web chat / Claude — quiet, roomy, monospace only where it means
something.

- System font stack for prose; `ui-monospace` for tool lines, code and diffs.
- Dark by default with `prefers-color-scheme` light; one accent colour.
- Message max-width ~72ch. Generous line-height. No avatars, no gradients.
- Streaming: the assistant bubble grows token-by-token if the client supports
  it; otherwise it appears at `assistant`.
- Keyboard: `↵` send, `⇧↵` newline, `⌘/Ctrl+K` thread switcher, `Esc` stop.
- Responsive down to phone width — which incidentally gives you a second,
  richer remote when you are on the same LAN via an SSH tunnel. (Trade-off worth
  naming: this partly overlaps Telegram. Telegram wins on push notifications and
  zero setup from anywhere; the web UI wins on fidelity. Keep both.)

### 8.6 Security

- Loopback bind, plus a random 32-byte token minted per server start, required
  as `?t=` on first load and stored in `sessionStorage`. Rejects everything else
  with `401`.
- `Origin`/`Host` check on every mutating request (DNS-rebinding defence).
- No CORS headers at all.
- Everything renders through the same `redact()` used by the transport.
- The server never accepts a filesystem path as input — only workspace ids from
  the registry and session ids from the manager.

### 8.7 Acceptance criteria

1. `/web-ui start`, open the URL: every registered workspace and every session
   in each is listed with a correct message count.
2. Send a prompt in the browser; the CLI shows `shamsu (remote-web)> …` and the
   turn appears live in the browser at the same fidelity as the CLI.
3. Hard-refresh mid-turn → the in-flight turn is fully restored from
   `activity.jsonl` + `Last-Event-ID` with no duplicated or missing lines.
4. Approve a `run_command` from the browser while the phone is watching; the
   phone's card updates and the approval card disappears from both.

---

## 9. Suggestions — ideas worth taking, ranked

**Take now (cheap, high impact):**

1. **`typing` chat action** while a model call is in flight. Two lines of code;
   removes most of the "is it dead?" feeling on its own.
2. **Reply-to-message = mid-run feedback.** `add_feedback()` already exists and
   is already what a message during a run does
   ([sessions.py:208-217](shamsu/integrations/telegram/sessions.py#L208-L217)) —
   but the user is not told. Reply to the live card → "Added to the running
   turn" as a quoted confirmation.
3. **Deep-link pairing.** `t.me/<bot>?start=<code>` puts the pairing code in the
   link; `_handle_start` already parses `/start` and could take the argument.
   Pairing becomes one tap from a QR code the REPL prints.
4. **`/where`** — one line telling you project, session, branch, dirty files.
   People get lost constantly.
5. **Auto-titled threads.** `maybe_auto_title()` exists
   ([manager.py:312](shamsu/session/manager.py#L312)); surface the titles in
   `/sessions` and the web sidebar instead of ids.

**Take soon:**

6. **Diff as a document.** When a turn changes files, offer `Diff` on the sealed
   card; `sendDocument` with a `.diff` file renders with syntax colour in
   Telegram's viewer.
7. **Context-pressure line.** SHAMSU already knows `num_ctx` and the VRAM cliff.
   Showing `18k/32k` in the web right pane and on `/status` predicts the
   compaction that is about to happen.
8. **Session fork.** `create_session(parent_session_id=…)` already takes a
   parent ([manager.py:77](shamsu/session/manager.py#L77)) and nothing uses it.
   "Fork from here" is the single most-missed feature in chat agents.
9. **Quiet hours / notification policy.** `NotificationFilter` exists and is
   unused; wire `/settings` to it so a long overnight build does not buzz.
10. **Pin the live card** (`pinChatMessage`) at turn start, unpin at end. The
    card stays at the top of the chat while you scroll history.

**Consider:**

11. **Approval risk colouring** — `PendingApproval.risk_level` is already
    carried and never shown differently. High-risk approvals should look
    different, and should be the only ones that push a notification when
    `quiet` is set.
12. **`/run <cmd>`** shortcut that goes straight to the executor with the normal
    approval gate — faster than asking the model to run a command.
13. **Screenshot-in.** File staging already accepts documents
    ([files.py](shamsu/integrations/telegram/files.py)); accepting photos and
    passing them as context is a small extension — but note SHAMSU's models are
    text-only today, so this only pays off with a vision model.
14. **Voice notes** — would need local STT (faster-whisper). Real dependency
    weight, and it breaks the zero-new-deps rule. Recommend **no** for now.
15. **Wire `PermissionLevel`.** `TELEGRAM_BOT.md` §2.8: OWNER/OPERATOR/VIEWER is
    persisted and never read. Now that one pairing reaches every workspace (§6),
    a VIEWER role stops being decorative. Worth doing before sharing a bot.

**Fix on the way past:**

16. **The `§3.8` cross-loop defect.** `cancel_run` / `pause_run` are called from
    the poll thread into `asyncio` primitives owned by another loop, so `/stop`
    is unreliable. The live card has a `Stop` button, which makes this
    user-visible instead of theoretical. Fix by draining control requests on the
    owning loop (a `threading.Event` the turn polls, or
    `loop.call_soon_threadsafe`). **This is a prerequisite for §5.1's Stop
    button, not an optional extra.**

---

## 10. File and data layout changes

> **Superseded by [`PRD_REMOTE_UX_DATA.md`](PRD_REMOTE_UX_DATA.md) §5 and §10.**
> The table below is still correct for the files, but it is incomplete: it omits
> the central catalog (`~/.shamsu/shamsu.db`) and the control-plane tables that
> a separate-process web portal requires.

| Path | Change |
|---|---|
| `~/.shamsu/telegram.env` | **new** — install-bound token (§6) |
| `~/.shamsu/telegram/telegram-state.db` | **moved** from workspace; pairings, offset, callbacks, audit |
| `~/.shamsu/workspaces.json` | **new** — workspace registry (§7.2) |
| `~/.shamsu/runtime/telegram-poller.lock` | **new** — single-poller election (§7.3) |
| `<ws>/.shamsu/sessions/<id>/activity.jsonl` | **new** — the turn event log (§4.3) |
| `<ws>/.shamsu/telegram.env` | kept, demoted to per-project override |
| `<ws>/.shamsu/sessions/<id>/messages.jsonl` | unchanged, still the lossless transcript |

New modules: `shamsu/runtime/turn_stream.py`, `shamsu/runtime/workspaces.py`,
`shamsu/webui/{__init__,server,api,sse}.py` + `shamsu/webui/static/`.

---

## 11. Delivery plan

> **Superseded by [`PRD_REMOTE_UX_DATA.md`](PRD_REMOTE_UX_DATA.md) §10**, which
> inserts the catalog/control-plane phases (PD-a, PD-b) before P4 and deletes P7
> (absorbed by the control plane). P0→P2 below are unchanged.

Each phase is independently shippable and independently useful.

| Phase | Content | Est. |
|---|---|---|
| **P0** | `TurnEvent` + `TurnStream` + `activity.jsonl`; `SimpleChatLoop.emit`; CLI renderer reimplemented on it (behaviour identical — this is the safety proof) | 1 d |
| **P1** | Telegram live turn card: `parse_mode`, card renderer, coalescing, overflow, prompt echo both ways, `typing`. **Delivers G1+G2.** | 1.5 d |
| **P2** | Install-bound token + state migration. **Delivers G3.** | 0.5 d |
| **P3** | Workspace registry, poller lock, gateway map, `/projects` `/use` `/where`, session rules. **Delivers G4.** | 1.5 d |
| **P4** | `webui` server, SSE, JSON API, static shell; read-only first (browse workspaces/threads/history) | 1.5 d |
| **P5** | Web live turns + prompt input + approvals + diffs. **Delivers G5.** | 1.5 d |
| **P6** | Polish from §9 items 1–5, verbosity settings, `/settings` wiring | 1 d |
| **P7** | Control-plane fix (§9.16), Stop button made reliable | 0.5 d |

P0→P1 is the smallest slice that answers your actual complaint. **If only two
days exist, ship P0+P1.**

P2 and P3 have a hard ordering dependency (a registry is meaningless while the
token is workspace-bound). P4 depends only on P0.

---

## 12. Test plan

Existing: `tests/test_telegram_remote_control.py`, 30 tests, driven entirely by
`FakeTelegramTransport` — no network. Extend, do not replace.

- **Parity test (the important one).** Drive `SimpleChatLoop` with a scripted
  fake Ollama client and both renderers attached. Assert
  `cli_lines == telegram_card_lines`. This is the executable form of G1 and
  should fail loudly if anyone adds a throttle back.
- **Coalescing.** 200 events in 3 s → assert edit count ≤ 3 and zero lines lost.
- **Overflow.** 500 activity lines → assert N sealed cards, concatenation equals
  the input, no truncation marker in the middle of a line.
- **Resume.** Subscribe with `since_seq=K` → exactly the events after K.
- **Token migration.** Workspace token + no install token → promoted, workspace
  file untouched, token never appears in any captured output.
- **Sandbox across workspaces.** Active workspace B, tool call naming a path in
  A → rejected.
- **Poller election.** Two managers, one lock → one polls; kill the owner →
  the other claims within one tick.
- **Web.** API contract tests against the handler (no socket); one SSE
  reconnect test with `Last-Event-ID`; one auth test asserting `401` without the
  token and on a foreign `Origin`.
- **Redaction.** A fake secret in a tool result must not appear in
  `activity.jsonl`, the Telegram card, or the SSE stream.

Regression guard: the suite was last recorded green at 2576 tests. Nothing here
should move that number down.

---

## 13. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Telegram `429` flood from live editing | Med | Fixed-interval flush + `retry_after` backoff; events buffered, never dropped |
| One pairing now reaches every workspace | High (by design) | Explicit registry opt-in, `Sandbox` per active workspace, approvals unchanged, §9.15 roles |
| Two REPLs both try to poll | Med | `FileLock` election; loser is explicit about it |
| Telegram turn hijacks the desktop's session | **Already happening** ([sessions.py:273](shamsu/integrations/telegram/sessions.py#L273)) | §7.5: Telegram gets its own `remote` session by default |
| `activity.jsonl` grows without bound | Low | Rotate per session at 5 MB; it is telemetry, not context |
| `ThreadingHTTPServer` too primitive under load | Low | Single local user; if it bites, swapping in uvicorn is a `server.py` change behind the same API |
| Scope creep into a general web IDE | **High** | Explicit non-goal: the web UI is a *chat client over sessions*, not an editor |

---

## 14. Decisions taken, and the ambiguity behind each

1. **`shamsu (remote-telegram)>` appears on both surfaces.** Your example was
   ambiguous about which one; doing both is consistent and costs nothing (§5.5).
2. **Default verbosity is full CLI parity.** You asked for every line; `quiet`
   exists as an opt-out rather than parity being opt-in.
3. **One live-edited card, not one message per line.** Literal per-line messages
   would be unusable (rate limits, notification storm) while being technically
   "every line". The card preserves every line *and* reads like the terminal.
4. **Web UI on stdlib, no new deps.** Reversible; the API boundary makes swapping
   the server a contained change.
5. **Telegram gets its own session by default.** Silent thread-sharing with the
   desktop already caused a real corruption bug; opt-in is the safe default.
6. **No daemon in v1.** The poller lives in a REPL, elected by lock. A headless
   `shamsu remote serve` is a clean follow-up the lock design already permits.

---

## 15. Open questions

1. **Should the web UI be reachable from your phone on the LAN?** Default is
   loopback-only. Binding `0.0.0.0` behind the token is a one-flag change, but
   it puts an agent that can write files on your network. Recommend: keep
   loopback, document `ssh -L 8765:127.0.0.1:8765`.
2. **When both the phone and the browser are open on the same session, should
   both be able to send prompts?** Recommend: yes, serialized — the second
   prompt queues and is announced on both surfaces.
3. **Retention for `activity.jsonl`** — rotate at 5 MB/session, or keep forever?
   Recommend rotate; it is UI telemetry, and `messages.jsonl` remains the
   lossless record.
4. **Port** — fixed `8765`, or first free port from `8765`? Recommend
   first-free, printed on start.
