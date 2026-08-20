# PRD Addendum 2 — Webhook Transport, Self-Hosting, and the CLI

**Companion to `PRD_REMOTE_UX.md` and `PRD_REMOTE_UX_DATA.md`.**
Replaces the long-polling assumption throughout, and adds the CLI section that
neither earlier document covered.

Status: DRAFT — for review. No code has been changed.
Author: Claude (planning pass, 2026-08-19)

---

## 0. Re-baseline — the repo moved while these PRDs were being written

Before planning anything I re-read the tree. It has changed substantially since
`PRD_REMOTE_UX.md` was drafted, and **one of the features requested in this round
is already built**.

| Module | State |
|---|---|
| `agents/simple_chat.py` | **1,698 → 4,091 lines** |
| `agents/simple_feedback.py` | **new** — `FeedbackQueue`, mid-run steering |
| `agents/simple_router.py`, `simple_graph.py`, `simple_memory.py`, `simple_verify.py`, `simple_log.py` | **new** |
| `session/history.py`, `paths.py`, `retriever/semantic.py`, `tools/hybrid_search.py` | **new** |
| `cli/repl.py` | `_LiveFeedbackReader` added (line 4687) |
| `integrations/telegram/sessions.py` | touched, but **the 8-second throttle is unchanged** |

### 0.1 Mid-run feedback is DONE, and it already improves on smallcode

The feature described as "give the model text as feedback while the agent is
already working" shipped today. `agents/simple_feedback.py` is explicit about
where the idea came from, and about where it deliberately diverges:

> *"Steering the RUNNING turn is the difference from smallcode, whose TUI queues
> the input as the next turn once the current one finishes. Mid-flight is when
> the correction is worth something."*

That divergence checks out against the reference. smallcode's key handler
submits with `await this.onSubmit(input)`
(`reference/smallcode/src/tui/fullscreen.js:828`) — the handler is blocked for
the whole duration of the turn, so anything typed meanwhile is buffered and
lands as the *next* turn. Useful, but it cannot change the run you are watching
go wrong.

SHAMSU now injects free text into the **running** turn at a round boundary, and
records it as an ordinary user message so it survives in the transcript.

**Planning consequence: there is no CLI feedback work item.** It exists. What
remains is making it *visible* (§6.3) and reaching it from the phone and browser.

### 0.2 What is still exactly as the earlier PRDs described

`integrations/telegram/sessions.py` still reads:

```python
on_activity=lambda message: progress.step(str(message))   # line 326
min_interval_seconds: float = 8.0                          # line 380
```

No `on_status`. Still one chat message per surviving line. **The parity bug that
started all of this is untouched**, and P0→P1 remain the highest-value work.

---

## 1. The transport correction

> "use ngnix tunneling to achieve the webhook part"

**nginx cannot do this.** It is a reverse proxy: it forwards traffic that already
reaches your machine. With no domain and no public IP, nothing reaches your
machine in the first place, so there is nothing for nginx to proxy. What is
actually needed is a **tunnel** — an outbound connection to a public relay that
hands back a public HTTPS URL.

That distinction is the whole reason `TELEGRAM_BOT.md` §1.1 rejected webhooks
originally ("a webhook would require an inbound port, a public hostname and TLS").
A tunnel supplies all three without you owning any of them.

### 1.1 Tunnel options

Telegram's `setWebhook` requires **HTTPS with a CA-valid certificate**, on port
443, 80, 88 or 8443.

| Option | Account needed | Domain needed | Verdict |
|---|---|---|---|
| **Cloudflare Quick Tunnel** (`cloudflared tunnel --url`) | **none** | **none** | **Recommended** |
| Cloudflare Named Tunnel | yes (free) | yes (or a CF-provided one) | Stable upgrade path |
| ngrok | **yes** — authtoken required | no | Fails "don't want to mess with it" |
| Tailscale Funnel | yes | no | Account + node setup |
| localtunnel / serveo / bore | no | no | Unreliable; not for an agent that writes files |

**Cloudflare Quick Tunnel wins on your stated constraint.** One binary, no signup,
no config file, no domain, real TLS:

```
cloudflared tunnel --url http://127.0.0.1:8787
# → https://random-three-words-1234.trycloudflare.com
```

**The honest caveat:** Cloudflare documents quick tunnels as for testing, with no
uptime guarantee and no support. They can be slow or drop. This is why §2.4
makes automatic fallback to long-polling mandatory rather than optional, and why
a named tunnel is offered as the stable upgrade for anyone who wants it.

### 1.2 What webhooks actually buy — and what they do not

Being precise here, because one of the stated motivations does not hold.

**Real gains:**

1. **Instant delivery.** Long polling waits up to 30 s
   (`transport.py:42`, `timeout_seconds: int = 30`). A webhook arrives immediately.
2. **It structurally solves the multi-consumer problem.** This is the big one.
   `TELEGRAM_BOT.md` §4.3 documents that two processes calling `getUpdates` with
   one token get updates split non-deterministically between them — the reason
   `PRD_REMOTE_UX.md` §7.3 needed a `FileLock` election. **A webhook has exactly
   one registered URL, so there is exactly one consumer by construction.** The
   election becomes a fallback concern rather than the core design.
3. No idle HTTP traffic when nothing is happening.

**What it does not buy — the correction:**

> "so the bot can run by its own"

A webhook does not make the bot autonomous. Something still has to be *listening*
on that URL. If the listener lives inside the REPL, closing the REPL kills the
bot exactly as it does today.

**Autonomy comes from the daemon, not the transport.** The two together are what
you actually want:

```
shamsu serve  ──┬── owns the webhook listener + tunnel
                ├── owns the Telegram bot
                ├── owns the web portal
                └── starts runs itself, via the control plane (Addendum 1, PD-b)
```

`shamsu serve` was listed as an out-of-scope follow-up in `PRD_REMOTE_UX.md`
§7.3. **This request promotes it to required.** The control plane from Addendum 1
§7.2 is what makes it a small addition rather than a redesign — without shared
run state, a daemon and a REPL cannot see each other's runs.

---

## 2. F5 — Zero-config webhook transport

### 2.1 One command, no configuration

Everything below happens inside `/remote_control connect`. Nothing is asked of
you beyond the bot token, once, ever.

```
1. resolve token           ~/.shamsu/telegram.env  (Addendum 0 §6)
2. ensure cloudflared      ~/.shamsu/tools/cloudflared/  — download + checksum
3. bind listener           127.0.0.1:<free port>, loopback only
4. start tunnel            cloudflared tunnel --url http://127.0.0.1:<port>
5. parse public URL        from cloudflared's stderr, with a 30s timeout
6. mint secrets            path segment (16B) + secret_token (32B)
7. setWebhook              url + secret_token + allowed_updates + drop_pending
8. verify                  getWebhookInfo → assert url matches, last_error empty
9. persist                 ~/.shamsu/telegram/webhook.json
```

Step 2 follows the pattern already used for `codebase-memory-mcp`, `graphiti` and
`taskmaster`, all of which live under `~/.shamsu/tools/`
(`memory/graphiti_adapter.py:58`, `taskmaster/adapter.py:56`,
`tools/codebase_memory.py:37`). It is an established convention here, not a new one.

On shutdown: `deleteWebhook`, then terminate the tunnel. On crash, the next start
re-registers — `setWebhook` is idempotent and overwrites.

### 2.2 The listener

New `shamsu/integrations/telegram/webhook.py`, on the same stdlib
`ThreadingHTTPServer` the web portal uses (`PRD_REMOTE_UX.md` §8.1). One server,
two mounts, one dependency-free HTTP stack for the whole product.

```
POST /tg/<random-path>   → the only route that exists
*                        → 404, no body, no hints
```

Every request must satisfy **all** of:

| Check | Failure |
|---|---|
| `X-Telegram-Bot-Api-Secret-Token` equals our minted token | `403` |
| Method is `POST`, path matches exactly | `404` |
| `Content-Length` ≤ 1 MB | `413` |
| Body parses as a Telegram `Update` | `400` |

The secret-token header is Telegram's own anti-spoofing mechanism and it is the
real control — the tunnel hides the source IP, so IP allowlisting is not
available. The random path segment is defence in depth: the URL is unguessable
and rotates on every start, because quick-tunnel hostnames are ephemeral.

The listener returns `200` immediately and processes the update on a worker.
Telegram retries on non-2xx, and a slow agent turn must never look like a failure.

### 2.3 Reusing what already exists

This is a new `TelegramTransport` implementation, not a rewrite. The ABC has
exactly two methods (`transport.py:23-33`) and the entire test suite drives
`FakeTelegramTransport`, so:

- `TelegramWebhookTransport.updates()` yields from an internal queue that the
  HTTP handler feeds, instead of from `getUpdates`.
- `send()` is **unchanged** — outbound is still `httpx` to the Bot API.
- `normalize_update()` is **unchanged** — a webhook body is the same `Update`
  object the poll loop already parses.
- Offset tracking (`_offset_getter`/`_offset_setter`) becomes dead on this path;
  webhooks have no offset. Kept for the polling fallback.

`store.mark_update_processed()` (`controller.py:49-51`) already gives idempotency,
which matters more now: Telegram **will** redeliver on timeout.

### 2.4 Fallback is mandatory, not optional

Given §1.1's caveat, the bot must never be dead because a free tunnel had a bad day.

```
tunnel healthy    → webhook mode
tunnel fails to start, or
getWebhookInfo reports last_error, or
no update in N minutes while the tunnel is down
                  → deleteWebhook, fall back to long polling, tell the user once
```

Health is checked by watching the `cloudflared` process and polling
`getWebhookInfo` every 60 s. `/remote_control status` shows which mode is live,
the public URL host, and the last error Telegram reported.

### 2.5 Security posture — stated plainly

This is the first time SHAMSU accepts **inbound** traffic. That is a real change
and it deserves a straight accounting.

| | Before (polling) | After (webhook) |
|---|---|---|
| Inbound path to the machine | none | one loopback port behind a tunnel |
| Reachable by | nobody | anyone who knows URL **and** secret token |
| Third-party in the path | Telegram only | Telegram + Cloudflare |
| Cloudflare can see | nothing | the ciphertext-terminated request bodies |

That last row is the one worth thinking about: **a Cloudflare quick tunnel
terminates TLS at Cloudflare.** They can, in principle, see webhook payloads —
your Telegram messages to the bot. For a personal dev tool this is likely
acceptable, and it is the same trust you extend by using Telegram at all. It is
not acceptable for anything confidential, and it is why polling remains supported
rather than being ripped out.

Unchanged by all of this: `Sandbox` still confines file writes, approvals still
gate mutations, `redact()` still scrubs outbound text.

---

## 3. F6 — `shamsu serve`

The daemon that makes "runs by itself" true.

```
shamsu serve [--port 8765] [--no-web] [--no-telegram]
```

Owns, in one process: the webhook listener + tunnel, the Telegram bot, the web
portal, and a run executor. Registers itself in the control plane
(Addendum 1 §5, `runs.owner_pid` / `owner_surface = "serve"`), so a REPL opened
later can see its runs and vice versa.

- Single instance enforced by the same `FileLock` idea already planned for the
  poller (`~/.shamsu/runtime/serve.lock`).
- Works across every registered workspace, because the gateway is a map
  (`PRD_REMOTE_UX.md` §7.3) and the catalog is install-wide.
- A REPL started while `serve` is running does **not** start its own bot; it
  attaches to the shared control plane and says so.

**Hard dependency: Addendum 1 PD-b.** Without shared run state, a daemon and a
REPL are two blind processes fighting over the same sessions.

---

## 4. The CLI — what to take from smallcode, and the one thing not to

Reference read: `reference/smallcode/` — a Node agent for 8B–35B models, so its
constraints are genuinely SHAMSU's. The TUI is `src/tui/fullscreen.js`
(1,524 lines, zero-dependency raw ANSI) plus `src/tui/terminal.js`.

### 4.1 Why you cannot scroll in smallcode — the precise cause

There are two layers here, and only the second one is the real complaint.

**Layer 1 — the alternate screen.** `fullscreen.js:78` enters `\x1b[?1049h`. The
alternate screen has no scrollback buffer by design, so your terminal's own
scroll, wheel and search are dead. smallcode compensates with an in-app
implementation: `chatScroll`, PgUp/PgDn, Shift+Up/Down, SGR mouse wheel
(`fullscreen.js:971-995`), a `↑ scrolled` status indicator, and a 5,000-line
retained buffer (`MAX_CHAT_LINES`, `fullscreen.js:1131`). On paper that is a
complete scrollback feature.

**Layer 2 — and this is the actual bug — every append snaps you back down:**

```js
this.chatScroll = 0; // snap to bottom
```

That line runs at **six** separate append sites — `fullscreen.js:1125, 1161,
1182, 1254, 1303, 1321`. So you *can* press PgUp, but the next line the agent
emits yanks you to the bottom. During a run, output is near-continuous, which
means scrollback is unusable at exactly the moment you want it: while watching a
long turn go wrong.

That is why it feels like you cannot scroll up even though the bindings exist.

smallcode itself knows the alternate screen is a tradeoff — `bin/smallcode.js:230`
offers `--classic  Use classic readline TUI (no alternate screen)`.

### 4.1a The rule that fixes it: sticky-bottom

The standard fix, and the one every good chat client uses:

> **Auto-scroll only when the view is already at the bottom.** If the user has
> scrolled up, hold the position absolutely and show a `↓ 12 new lines`
> affordance. Jump to bottom on an explicit key (`End`) or by clicking it.

This is a small rule with a large effect, and it applies to **all three
surfaces** — the CLI, the web portal's conversation pane, and the Telegram card's
overflow behaviour. It is added to the web spec (`PRD_REMOTE_UX.md` §8.4) as a
requirement, not a nicety.

### 4.2 The decision: stay line-oriented

**SHAMSU's REPL is not full-screen.** It is `prompt_toolkit.PromptSession` plus
607 `console.print()` calls (`cli/repl.py`). It never enters the alternate screen,
so **native terminal scrollback already works** — mouse wheel, `Shift+PgUp`,
your terminal's own search, and selecting text to copy.

> **Therefore: do not rebuild SHAMSU as a full-screen TUI.** That single decision
> is what preserves the property you asked for. Everything else SmallCTL does
> well can be had without it.

This is a live risk, not a hypothetical. A full-screen TUI *was* built here
before — `ui/interactive.py`, `ui/session_frame.py`, `ui/terminal.py`, alternate
screen and all — in the `src/` tree that was later abandoned (`src/shamsu/ui/` is
now empty). Resurrecting it would reintroduce exactly the flaw you named. Left in
the plan as an explicit **non-goal**.

Note what this buys for free, which smallcode had to build and still got wrong:
staying line-oriented means scrollback, mouse wheel, terminal search, and
copy-paste all keep working with **zero code**, and there is no snap-to-bottom
bug to have, because the terminal owns the viewport.

### 4.3 Take from smallcode

| Take | Where | Cost |
|---|---|---|
| **Sticky-bottom rule** | §4.1a — the fix for its own bug | small, and it is the headline |
| **Persistent status line** — model, elapsed, context meter, `↑ scrolled` state | `fullscreen.js:651-733` renders a genuinely good one | small — reuses `on_status` |
| **Command palette with live filtering** | `fullscreen.js:540-596`, `/` opens it and filters as you type | medium |
| **Bounded retained buffer with an explicit trim counter** (`_chatTrim`) | `fullscreen.js:1131-1134` — honest about what it dropped | small |
| **Clean suspend** — Ctrl+Z restores the terminal before stopping | `fullscreen.js:791-795` | small |
| **A dedicated cancel key** | smallcode does *not* have one — see §4.4 | small |

### 4.4 Reject from smallcode

- **The full-screen model** — §4.2. This is the one thing not to take.
- **Snap-to-bottom on append** — §4.1. The bug itself.
- **Ctrl+C exits the whole app** (`fullscreen.js:776-780` → `leave()` + `onExit()`).
  There is no cancel-the-run key in the TUI at all. "Stopping a run is easier" is
  true only because the stop is *blunt*: it kills the process. **SHAMSU should
  beat this, not copy it** — one keystroke that cancels the turn and keeps the
  session, transcript and REPL alive.
- **`!` shell prefix** — previously rejected here on architectural grounds and
  still right: SHAMSU has no shell-string execution anywhere by design.

### 4.5 The remaining CLI work

Given §0.1, this is smaller than expected:

1. **Make feedback discoverable.** `_LiveFeedbackReader` works, but nothing tells
   you it exists. A dim hint on the first long turn — `type to steer · ctrl+k to
   stop` — and an echo when a line is accepted (`↳ queued: use game.js not
   main.js`), plus a status-line counter while it waits for the round boundary.
2. **`ctrl+k` cancel** — cancels the *turn*, not the process, and keeps the
   session alive (§4.4). Routed through the control plane so it works whether the
   run is local or owned by `serve`.
3. **Status line** carrying `on_status` plus the context gauge.
4. **Sticky-bottom** (§4.1a) wherever SHAMSU controls a viewport — which in the
   CLI means: do not fight the terminal. The only rule the REPL needs is to never
   force-scroll or repaint over scrolled-back output; the live status line must
   be the *only* thing that rewrites in place, and it must sit on the last line.
5. **Surface feedback on the other two surfaces** — a Telegram reply to the live
   card and a web composer entry both call the same `FeedbackQueue`. This is
   where the phone finally gets a feature the desktop already has.

---

## 5. Revised delivery plan

Supersedes Addendum 1 §10. Changes: CLI feedback removed (built), webhook and
daemon phases added, `shamsu serve` promoted from follow-up to deliverable.

| Phase | Content | Est. |
|---|---|---|
| **P0** | `TurnEvent` + `TurnStream` + `activity.jsonl`; `SimpleChatLoop.emit`; CLI renderer on it | 1 d |
| **P1** | Telegram live turn card — **still the fix for the original complaint** | 1.5 d |
| **P2** | Install-bound token + state migration | 0.5 d |
| **PW** | **Webhook transport**: listener, secret token, `cloudflared` bootstrap, `setWebhook`/`getWebhookInfo`, health watch, polling fallback | 2 d |
| **PD-a** | Catalog schema, dual-write, `shamsu reindex`, reconciliation | 1.5 d |
| **PD-b** | Control plane: `runs` / `run_commands` / `approvals`, heartbeat, orphan detection | 1.5 d |
| **P3** | Workspace registry, gateway map, `/projects` `/use` `/where` | 1.5 d |
| **PS** | **`shamsu serve`** — daemon owning tunnel + bot + portal | 1 d |
| **P4** | Web portal: server, SSE, JSON API, shell | 1.5 d |
| **P5** | Web live turns, prompt input, approvals, diffs | 1.5 d |
| **P6** | CLI polish (§4.5): `ctrl+k`, status line, feedback affordances, view toggles | 1 d |

**Ordering that matters:**

- **P0→P1 is unchanged and still the answer if time is short.** Neither the
  webhook nor the database is needed to fix Telegram's transcript.
- **PW can ship independently** of everything after P2 — it is a transport swap
  behind an existing ABC.
- **PS hard-depends on PD-b.** A daemon without shared run state is worse than no
  daemon: two processes silently fighting over the same sessions.
- P6 is small now, because the hard part landed on its own.

---

## 6. Test plan additions

- **Webhook auth.** Correct secret token → `200`. Wrong, absent, or
  right-token-wrong-path → `403`/`404`. No route leaks its existence via timing
  or body.
- **Redelivery.** Same `update_id` posted three times → processed once
  (`mark_update_processed`).
- **Slow turn.** A 10-minute turn still returns `200` within a second; Telegram
  never retries the trigger.
- **Tunnel death mid-run.** Kill `cloudflared`; assert fallback to polling, one
  user-visible notice, and that the in-flight run is untouched.
- **Registration idempotence.** `connect` twice → one webhook, no duplicate
  delivery, old URL revoked.
- **Token never printed.** Assert across `connect`, `status`, and every log path.
- **No full-screen regression.** Assert the REPL never emits the alternate-screen
  escape (`\x1b[?1049h`) — the executable form of §4.2, so scrollback cannot be
  broken by accident later.
- **Sticky-bottom, in the web pane.** Scroll up 200 lines, append 50 more, assert
  the scroll offset is byte-identical and the `↓ N new` counter reads 50. This is
  the executable form of §4.1a and the guard against reproducing
  `fullscreen.js:1125`.
- **Feedback reaches simple mode.** Push through `FeedbackQueue` mid-turn; assert
  injection at the round boundary and presence in `messages.jsonl`.

---

## 7. Decisions

1. **Cloudflare Quick Tunnel**, not ngrok — no account is the requirement that
   decides it. Named tunnels are the documented upgrade for anyone who wants
   stability.
2. **Polling is kept, not deleted.** It is the fallback and the private-by-default
   option, given §2.5's TLS-termination note.
3. **Webhook + daemon, not webhook alone.** The transport was never what made the
   bot need a REPL.
4. **The CLI stays line-oriented.** Non-negotiable if scrollback matters, and
   asserted by a test. smallcode's scrollback bug is unreachable from here.
5. **No CLI feedback work.** It exists and beats the thing it was modelled on.
6. **Cancel keeps the session.** smallcode's Ctrl+C kills the process; SHAMSU's
   `ctrl+k` must cancel only the turn.

## 8. Open questions

1. **Is Cloudflare terminating TLS acceptable to you?** (§2.5) If not, the answer
   is a named tunnel on your own account, or staying on polling. This is the only
   question here I would not decide for you.
2. **Should `serve` auto-start on login?** A background agent that can write files
   and run commands, starting unattended, is a meaningful step up in exposure.
   Recommend: manual start only, at least until it has run for a while.
3. **`ctrl+k` or `ctrl+c` twice for cancel?** `ctrl+k` matches SmallCTL and is
   unambiguous; `ctrl+c` twice needs no learning. Recommend both.
