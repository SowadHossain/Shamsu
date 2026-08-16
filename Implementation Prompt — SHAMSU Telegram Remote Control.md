# SHAMSU — Telegram Remote Control Implementation

You are working on the SHAMSU software-engineering agent.

Your task is to design and implement a **first-class Telegram remote-control interface** for SHAMSU.

This is not a separate Telegram AI agent.

Telegram must act as another user interface for SHAMSU's existing runtime, sessions, tasks, tools, approvals, and project workspaces.

The goal is that a user can begin working with SHAMSU locally, enable remote control, walk away from the computer, open Telegram, and continue controlling the exact same SHAMSU session from their phone.

---

# 1. Product Goal

The desired experience is:

```text
User works with SHAMSU locally
        ↓
User enters:

/remote_control

        ↓
SHAMSU starts/enables Telegram bridge
        ↓
SHAMSU displays pairing instructions
        ↓
User opens Telegram bot
        ↓
User pairs securely with this SHAMSU installation
        ↓
Telegram shows current SHAMSU session
        ↓
User sends:

"Implement the login endpoint and run the tests"

        ↓
Message enters the SAME SHAMSU runtime
        ↓
SHAMSU works normally
        ↓
Progress appears in Telegram
        ↓
If approval is needed:

[ Approve ] [ Reject ]

        ↓
User can inspect status, diff, tests, logs, plans, and sessions
        ↓
User can switch to another SHAMSU chat/session
```

The Telegram experience should feel like a proper mobile control panel for SHAMSU rather than a thin chatbot wrapper.

---

# 2. Critical Architectural Rule

DO NOT create another agent loop for Telegram.

Wrong:

```text
Telegram
   ↓
TelegramAgentLoop
   ↓
Tools
```

Correct:

```text
                        ┌─ Local CLI/UI
                        │
User Input ─────────────┤
                        │
                        └─ Telegram
                               ↓
                     Input / Session Gateway
                               ↓
                       SHAMSU Runtime
                               ↓
                   Existing Agent Execution
                               ↓
                      Existing Tool Layer
```

Telegram is an **input/output transport**.

It must use the same:

- session
- task state
- run state
- planner
- executor
- tools
- approvals
- evidence
- verification
- cancellation
- checkpoints
- project workspace

as the local SHAMSU interface.

There must never be:

```text
local SHAMSU state
```

and separately:

```text
Telegram SHAMSU state
```

There is one authoritative SHAMSU state.

Telegram merely views and controls it.

---

# 3. Prerequisite: Run Control

Before implementing the complete Telegram integration, inspect SHAMSU's existing run-control system.

SHAMSU currently contains run-control functionality for:

- run registration
- cancellation
- feedback injection
- active model-task cancellation

but previous architectural review found that much of this was attached to a test-only execution path rather than the production AgentChatLoop.

Telegram must integrate with the actual production run controller.

If necessary, first repair the production run-control integration.

The Telegram subsystem must not implement its own:

- cancellation mechanism
- task state
- approval system
- feedback queue
- execution status

Use or repair SHAMSU's authoritative runtime mechanisms instead.

---

# 4. Security Requirement

The Telegram bot gives remote access to a software-engineering agent capable of:

- modifying files
- running commands
- using Git
- managing containers
- eventually accessing databases
- potentially using credentials

Therefore authentication must be treated as a primary architectural requirement.

Never allow arbitrary Telegram users to control SHAMSU.

---

# 5. Bot Token Handling

The Telegram bot token must NEVER be:

- committed to Git
- placed in source code
- written to normal logs
- included in model context
- stored in SHAMSU project memory
- included in task artifacts
- sent back to Telegram
- displayed after initial configuration

Use an environment variable or secure local credential store.

Recommended environment variable:

```text
SHAMSU_TELEGRAM_BOT_TOKEN
```

If no token is configured, `/remote_control` should explain how to configure one.

Never hardcode a real token in tests.

Use fake credentials in test fixtures.

---

# 6. Authentication and Pairing

Do not authorize users by Telegram username.

Usernames can change.

Use stable Telegram user IDs.

Implement a secure pairing flow.

Suggested flow:

```text
Local SHAMSU:

/remote_control
```

SHAMSU responds locally:

```text
Telegram Remote Control

Status: Not paired

Pairing code:
847291

Expires in 5 minutes.

Open @your_bot and press Start,
then enter the pairing code.
```

Telegram:

```text
Welcome to SHAMSU Remote.

Enter your pairing code:
```

User:

```text
847291
```

Backend verifies:

```text
pairing code
+
expiration
+
unused state
```

Then stores:

```text
telegram_user_id
telegram_chat_id
shamsu_installation_id
paired_at
permission_level
```

Pairing code requirements:

- cryptographically random
- single use
- short expiration
- invalidated after successful pairing
- rate limited

Prefer private Telegram chats for v1.

Reject group/channel control by default.

Group support may be added later behind explicit configuration.

---

# 7. Optional Deep-Link Pairing

If practical, support Telegram deep-link pairing later.

Example user experience:

```text
/remote_control
```

SHAMSU displays:

```text
Open Telegram:
[ Pair SHAMSU ]
```

The link contains a short-lived pairing identifier.

Do not put:

- bot token
- project secret
- permanent authorization token
- database credentials

inside the deep link.

It should contain only a short-lived opaque pairing token.

---

# 8. Remote-Control Lifecycle

Implement these states:

```text
DISABLED
STARTING
WAITING_FOR_PAIR
CONNECTED
DISCONNECTED
ERROR
```

Local command:

```text
/remote_control
```

should open a small control panel:

```text
Telegram Remote Control

Status: Connected
User: <Telegram display name>
Active session: auth-refactor
Bot: @<configured bot>

[ Disconnect ]
[ Re-pair ]
[ Show Sessions ]
```

Possible local commands:

```text
/remote_control
/remote_control status
/remote_control connect
/remote_control disconnect
/remote_control repair
```

Do not require the user to remember all of these if an interactive local menu exists.

---

# 9. Telegram Transport Mode

For the initial local implementation, prefer:

```text
Telegram long polling
```

because SHAMSU is intended to run locally and should not require the user to expose a public HTTP endpoint.

Architecture:

```text
Telegram Bot API
       ↓
long-poll worker
       ↓
Telegram Update Normalizer
       ↓
Authentication
       ↓
Telegram Controller
       ↓
SHAMSU Session Gateway
```

Later support optional webhooks for server-hosted SHAMSU deployments.

Keep transport behind an interface so changing:

```text
LongPollingTransport
```

to:

```text
WebhookTransport
```

does not affect session management or command handling.

---

# 10. Telegram Module Structure

Create a dedicated integration package.

Example:

```text
shamsu/
└── integrations/
    └── telegram/
        ├── service.py
        ├── transport.py
        ├── controller.py
        ├── authentication.py
        ├── pairing.py
        ├── commands.py
        ├── callbacks.py
        ├── sessions.py
        ├── formatter.py
        ├── keyboards.py
        ├── notifications.py
        └── models.py
```

Do not put Telegram-specific code into the main AgentChatLoop.

---

# 11. Session Architecture

Telegram must support multiple SHAMSU sessions.

Introduce or reuse an authoritative session registry.

Each session should contain something like:

```text
session_id
display_name
project_id
workspace
created_at
last_activity
current_task
current_run
status
active_branch
remote_control_enabled
```

Telegram should maintain:

```text
telegram_user_id
        ↓
active_session_id
```

The active Telegram session determines where normal free-text messages are routed.

---

# 12. Session Commands

Implement:

```text
/sessions
```

Example response:

```text
Your SHAMSU Sessions

🟢 auth-refactor
   ~/projects/shop
   Running: Add OAuth login

⚪ api-redesign
   ~/projects/backend
   Idle

🔴 dashboard
   ~/projects/dashboard
   Last run failed
```

Attach inline buttons:

```text
[ auth-refactor ✓ ]
[ api-redesign ]
[ dashboard ]
[ + New Session ]
```

Pressing another session should switch the Telegram user's active SHAMSU session.

Then edit the existing Telegram message instead of sending unnecessary new messages.

---

# 13. `/switch`

Also provide:

```text
/switch
```

which opens the same interactive session selector.

Do not require:

```text
/switch <long-session-id>
```

unless the user explicitly wants CLI-like usage.

Human-friendly buttons should be the normal workflow.

---

# 14. `/new`

Implement:

```text
/new
```

Interactive flow:

```text
Create SHAMSU Session

Select workspace:

[ Current Project ]
[ Recent Projects ]
[ Enter Path ]
[ Cancel ]
```

Then optionally:

```text
Session name:
```

SHAMSU creates the session through the normal session manager.

Do not create an independent Telegram-only session format.

---

# 15. Free-Text Messages

The main experience should not require commands.

Once a session is active, normal messages such as:

```text
Implement the registration endpoint
```

or:

```text
check why the Docker container is crashing
```

or:

```text
continue with the next plan step
```

should be forwarded to that active SHAMSU session as ordinary user input.

The Telegram integration must attach metadata:

```text
source = telegram
telegram_user_id
telegram_chat_id
telegram_message_id
session_id
timestamp
```

but the semantic user request should enter SHAMSU exactly as local user input would.

---

# 16. User-Friendly Home Screen

Implement:

```text
/start
```

Response should be compact and useful.

Example:

```text
🤖 SHAMSU Remote

Connected to your SHAMSU workstation.

Active:
🟢 auth-refactor

Project:
shop-platform

Current task:
Add OAuth authentication

Status:
Working

What would you like SHAMSU to do?
```

Buttons:

```text
[ 💬 Current Session ]
[ 🗂 Sessions ]

[ 📋 Plan ]
[ 📊 Status ]

[ 🧪 Tests ]
[ 📝 Changes ]

[ ⏸ Pause ]
[ ❌ Cancel ]
```

Do not dump a giant command manual on `/start`.

---

# 17. `/help`

`/help` should provide categorized help:

```text
SHAMSU Remote

WORK
Just send a normal message to give SHAMSU a task.

SESSIONS
/sessions — View and switch sessions
/new — Create a session

WORK STATUS
/status — Current task
/plan — Current plan
/changes — Changed files
/tests — Latest tests
/logs — Recent activity

CONTROL
/pause
/resume
/cancel

SETTINGS
/settings
```

Keep descriptions short.

---

# 18. `/status`

Example:

```text
🟢 SHAMSU — Working

Session
auth-refactor

Task
Implement OAuth login

Phase
AUTHOR

Plan
3 / 6 steps complete

Current step
Add callback endpoint

Actions
2 / 4

Last action
Edited auth/routes.py

Updated
12 seconds ago
```

Buttons:

```text
[ 📋 Plan ]
[ 📝 Changes ]

[ ⏸ Pause ]
[ ❌ Cancel ]
```

---

# 19. Progress Notifications

Do not send a Telegram message for every internal tool call.

That would be noisy.

Use meaningful progress events.

Examples:

```text
🔎 Inspecting authentication code...
```

then edit that message:

```text
✏️ Updating authentication routes...
```

then:

```text
🧪 Running authentication tests...
```

then:

```text
✅ Step completed

23 tests passed.
```

Prefer editing one progress/status message over sending many separate messages.

Important events that justify new messages:

- task started
- approval required
- task blocked
- important failure
- task completed
- user action required

---

# 20. `/plan`

Display the active plan compactly.

Example:

```text
📋 Plan — OAuth Login

✅ 1. Inspect current auth flow
✅ 2. Add OAuth dependency
🔄 3. Add callback endpoint
⬜ 4. Add session handling
⬜ 5. Add frontend button
⬜ 6. Run integration tests
```

Buttons:

```text
[ Current Step ]
[ Refresh ]
```

Do not send huge planner JSON.

---

# 21. Approval UX

This is critical.

When SHAMSU requires approval:

```text
⚠️ Approval Required

SHAMSU wants to:

Run database migration

Command:
alembic upgrade head

Project:
shop-platform

Risk:
Database modification
```

Buttons:

```text
[ ✅ Approve ]
[ ❌ Reject ]
[ 🔍 Details ]
```

The callback must map to the real SHAMSU approval request.

Approval actions must contain:

```text
approval_id
run_id
session_id
authorized_user_id
```

Validate all of them server-side.

Never trust callback data by itself.

The Telegram interface must never bypass SHAMSU's normal approval policy.

---

# 22. Dangerous Actions

Telegram should have exactly the same permissions as the authorized user's local SHAMSU interface.

But remote access increases risk.

Require confirmation for sensitive actions such as:

- destructive Git operations
- deleting files
- database writes
- migrations
- privileged container operations
- secret access
- external deployment
- destructive shell commands

For especially dangerous operations, consider a second confirmation:

```text
⚠️ HIGH RISK

Drop development database?

[ Continue ]
[ Cancel ]
```

followed by:

```text
Confirm database deletion:

[ YES, DELETE ]
[ Cancel ]
```

Do not implement:

```text
Telegram user sends shell string
→ shell executes immediately
```

All commands must pass through SHAMSU's normal agent/tool/policy system.

---

# 23. `/cancel`

`/cancel` must cancel the current SHAMSU run using the authoritative run controller.

Response:

```text
Cancel current task?

Implement OAuth login
Step 3 / 6

[ ❌ Cancel Task ]
[ Keep Running ]
```

After cancellation:

```text
🛑 Task cancelled.

Last verified checkpoint:
Step 2 — Add OAuth dependency

Your project remains at the last safe state.
```

---

# 24. `/pause` and `/resume`

Support:

```text
/pause
/resume
```

Pause should prevent the runtime from beginning another agent action.

If a model/tool operation is currently safely interruptible, it may be interrupted.

Otherwise pause at the nearest safe boundary.

Status example:

```text
⏸ SHAMSU paused

Session:
auth-refactor

Safe checkpoint:
STEP-3 / action 2

[ ▶️ Resume ]
```

---

# 25. `/changes`

Show a compact Git-style summary:

```text
📝 Changes

4 files changed

M backend/auth/routes.py
M backend/auth/service.py
A tests/auth/test_oauth.py
M pyproject.toml

+184 / -31
```

Buttons:

```text
[ View Diff ]
[ View Files ]
```

For a large diff:

Do not paste thousands of lines into Telegram.

Options:

```text
[ Summary ]
[ Send Diff File ]
[ Select File ]
```

---

# 26. Files

Support receiving files through Telegram.

Examples:

User sends:

- PRD
- Markdown document
- screenshot
- configuration file
- log file
- code snippet

The Telegram integration should:

1. download it into a safe staging directory
2. validate size/type
3. register it as a SHAMSU user attachment
4. associate it with the current session
5. send a normal user event to SHAMSU

Example confirmation:

```text
📎 Received

PRD.md
42 KB

Attached to:
auth-refactor

What should SHAMSU do with it?
```

Do not automatically execute files.

---

# 27. Sending Files Back

SHAMSU should be able to send useful artifacts back to Telegram.

Examples:

- generated Markdown
- patch file
- diff
- test report
- architecture document
- log bundle

Example:

```text
📄 Generated artifact

AUTH_IMPLEMENTATION_REPORT.md

[ Send File ]
```

Large content should preferably be sent as a document rather than split across dozens of Telegram messages.

---

# 28. `/tests`

Example:

```text
🧪 Tests

Latest run:
Authentication suite

✅ 42 passed
❌ 2 failed

Failures:

1. test_oauth_callback
   Expected 302, got 500

2. test_expired_state
   State validation failed
```

Buttons:

```text
[ View Failure 1 ]
[ View Failure 2 ]
[ Re-run Tests ]
```

---

# 29. `/logs`

Do not expose massive raw logs by default.

Example:

```text
📜 Recent Activity

09:21 Read auth/routes.py
09:22 Updated OAuth callback
09:22 Ran targeted tests
09:23 2 tests failed
09:24 Entered REPAIR phase
```

Buttons:

```text
[ Detailed Logs ]
[ Tool Events ]
[ Errors Only ]
```

---

# 30. Notifications

Allow notification preferences.

Example `/settings`:

```text
⚙️ Telegram Settings

Task progress
[ Important only ✓ ]

Task completion
[ On ✓ ]

Approval requests
[ On ✓ ]

Failures
[ On ✓ ]

Tool-by-tool updates
[ Off ✓ ]
```

Recommended default:

```text
task start       = notify
meaningful phase = update existing status
approval needed  = notify immediately
task blocked     = notify immediately
task completed   = notify
every tool call  = do not notify
```

---

# 31. Session Switching During Active Work

If another session is currently running, switching the active Telegram session must NOT cancel it.

Example:

```text
/sessions
```

shows:

```text
🟢 shop-auth
   Working

🟡 os-kernel
   Paused

⚪ dashboard
   Idle
```

Selecting `dashboard` changes:

```text
telegram_active_session
```

only.

The other run continues according to its own state.

---

# 32. Background Session Notifications

Telegram should notify the owner about important events from sessions that are not currently selected.

Example:

```text
✅ shop-auth completed

OAuth authentication implemented.
42 tests passed.

[ Open Session ]
```

Or:

```text
⚠️ os-kernel requires approval

QEMU image rebuild requires package installation.

[ Open ]
```

Pressing `Open Session` should switch Telegram to that SHAMSU session.

---

# 33. Natural-Language Session References

As an optional convenience, support:

```text
switch to the kernel project
```

or:

```text
show me what the auth session is doing
```

But deterministic `/sessions` plus inline buttons remains the reliable primary interface.

Do not make session management dependent entirely on model interpretation.

---

# 34. Telegram Message Formatting

Create a Telegram-specific formatter.

It should translate SHAMSU events into concise mobile-friendly messages.

Avoid:

- huge markdown tables
- long JSON
- giant stack traces
- complete tool outputs
- verbose internal reasoning
- raw state dumps

Prefer:

```text
Title
short state
important details
next action
buttons
```

Example:

```text
❌ Test Failure

Step:
Add OAuth callback

2 / 42 tests failed

Primary error:
OAuth state token expired unexpectedly.

SHAMSU is attempting repair 1 / 2.

[ View Error ]
[ Pause ]
```

---

# 35. Message Length and Pagination

Implement output chunking/pagination centrally.

For long content:

```text
Page 1 / 4

[ ◀ Previous ]
[ Next ▶ ]
```

Prefer documents for extremely large:

- diffs
- logs
- reports
- generated source listings

Do not scatter one response across many uncontrolled messages.

---

# 36. Inline Keyboard Callback Design

Callback payloads must be compact and opaque.

Do not put sensitive information directly in callback data.

Prefer:

```text
callback_action_id
```

mapped server-side to:

```text
action
session
run
approval
user
expiration
```

Every callback must be checked for:

- authorized Telegram user
- expected chat
- current session/run
- expiration
- already-consumed state
- valid requested transition

---

# 37. Idempotency

Telegram updates can be retried or delivered in ways that result in duplicate handling if the client is incorrect.

Persist processed Telegram update IDs.

For every update:

```text
if update_id already processed:
    ignore safely
```

For callback actions:

```text
approval button clicked twice
```

must not approve twice.

Commands and callbacks should be idempotent wherever practical.

---

# 38. Connection Loss

If Telegram becomes unavailable:

SHAMSU must continue running locally.

Telegram is not authoritative state.

When Telegram reconnects:

- resume receiving updates
- rebuild UI from SHAMSU state
- show current task status
- do not replay already-processed user commands

Example:

```text
🔄 Reconnected

Session:
auth-refactor

Current task:
OAuth login

Phase:
VERIFY

[ Open Status ]
```

---

# 39. Bot Restart

Restarting the Telegram integration must not lose:

- authorized users
- SHAMSU session mappings
- active Telegram session
- processed-update position
- notification settings

Store Telegram integration state locally in SQLite.

Do not use model memory for this.

---

# 40. Multiple Authorized Users

Design the schema so multiple users can be supported later.

But initial implementation may intentionally allow:

```text
one owner
```

or a small explicit allowlist.

Permission levels may eventually include:

```text
OWNER
OPERATOR
VIEWER
```

OWNER:
full approved SHAMSU control

OPERATOR:
normal tasks but restricted dangerous actions

VIEWER:
status/logs only

Do not implement complex RBAC until needed, but do not architect yourself into a single hardcoded Telegram user ID.

---

# 41. Essential Commands

Initial command set:

```text
/start
/help

/status
/plan

/sessions
/switch
/new

/changes
/tests
/logs

/pause
/resume
/cancel

/settings
```

Do NOT create dozens of commands.

Normal work should happen through natural-language messages.

---

# 42. Local SHAMSU Command

Implement:

```text
/remote_control
```

as the primary local entry point.

Ideal behavior:

```text
/remote_control
```

opens:

```text
Remote Control

Telegram
Status: Connected

Owner:
<name>

Active remote sessions:
3

[ Telegram Settings ]
[ Disconnect ]
```

When unconfigured:

```text
Remote Control

Telegram is not configured.

1. Configure SHAMSU_TELEGRAM_BOT_TOKEN
2. Start pairing
3. Open your Telegram bot

[ Check Configuration ]
```

Never display the actual configured token.

---

# 43. Shutdown Behavior

When SHAMSU exits normally:

- stop long polling
- close Telegram client
- flush update state
- mark local connection offline

Do not invalidate the user pairing automatically.

Next startup should reconnect automatically if:

```text
remote_control_enabled = true
```

and valid credentials exist.

Allow configuration:

```text
telegram.auto_start = true/false
```

---

# 44. Audit Trail

Every remote action should record:

```text
source = telegram
telegram_user_id
telegram_chat_id
telegram_message_id
shamsu_session_id
run_id
action
timestamp
result
```

Sensitive message contents should follow SHAMSU's existing privacy/redaction policy.

Approval actions must be especially auditable.

---

# 45. Model Boundary

The Telegram bot itself should not invoke the LLM for:

- command routing
- session selection
- approval handling
- cancellation
- pagination
- status display
- settings
- authentication

Those are deterministic UI/controller operations.

Only normal free-text tasks intended for SHAMSU should enter the agent/model runtime.

Example:

```text
/status
```

must NOT ask the model:

"What is the current status?"

Read runtime state directly.

Likewise:

```text
/tests
```

reads verification state.

```text
/sessions
```

reads the session registry.

This saves tokens and increases reliability.

---

# 46. Do Not Expose Hidden Reasoning

Remote status should show:

- current phase
- plan step
- tool/action description
- verification status
- concise failure explanation

Do not expose private model chain-of-thought.

Example:

Good:

```text
Investigating authentication failure.
Reading the login route and related tests.
```

Not:

```text
Here is the model's internal reasoning...
```

---

# 47. Example Complete Interaction

Telegram:

```text
/start
```

Bot:

```text
🤖 SHAMSU Remote

🟢 Connected

Active session:
shop-auth

Project:
shop-platform

Current status:
Idle

What should SHAMSU do?
```

Buttons:

```text
[ 🗂 Sessions ]
[ 📊 Status ]
```

User:

```text
Add Google OAuth login. Follow the existing architecture and test it.
```

Bot:

```text
🔎 Inspecting project...

Session:
shop-auth
```

Bot edits message:

```text
📋 Plan created

1. Inspect auth architecture
2. Add OAuth configuration
3. Add callback endpoint
4. Add session integration
5. Add tests
6. Verify application

Starting step 1.
```

Later:

```text
⚠️ Approval Required

Add dependency:
authlib 1.x

This modifies project dependencies.

[ ✅ Approve ]
[ ❌ Reject ]
[ 🔍 Details ]
```

User taps Approve.

Later:

```text
🧪 Verifying authentication...

42 tests running
```

Then:

```text
✅ Task Completed

Google OAuth login implemented.

Changed:
4 files

Verification:
✅ 42 tests passed
✅ Type check passed
✅ Git diff reviewed

[ 📝 Changes ]
[ 📄 Report ]
[ 🗂 Sessions ]
```

---

# 48. Testing Requirements

Create unit tests for:

- command parsing
- authorization
- pairing
- expired pairing codes
- unauthorized users
- callback validation
- session switching
- duplicate Telegram updates
- pagination
- message formatting
- settings
- notification filtering

Create integration tests for:

- Telegram message → SHAMSU session
- SHAMSU response → Telegram
- SHAMSU approval → Telegram button
- Telegram approval → SHAMSU approval controller
- Telegram cancel → run cancellation
- pause/resume
- switching sessions
- simultaneous running sessions
- bot restart
- SHAMSU restart
- Telegram disconnect/reconnect
- file upload
- file download
- long output

Use a fake Telegram API adapter in tests.

Do not require the real Telegram service for unit tests.

---

# 49. Security Tests

Explicitly test:

1. Unknown Telegram user cannot control SHAMSU.
2. Username spoofing does not authorize a user.
3. Expired pairing code fails.
4. Pairing code cannot be reused.
5. Callback from wrong user fails.
6. Callback for wrong run fails.
7. Duplicate approval does nothing.
8. Telegram cannot bypass SHAMSU tool policy.
9. Telegram cannot bypass approval requirements.
10. Bot token never appears in logs.
11. Bot token never enters model prompts.
12. Secret values are redacted from Telegram output.
13. Group chats are rejected unless explicitly enabled.
14. Telegram file paths cannot escape the staging directory.
15. Malicious filenames are sanitized.
16. Telegram messages cannot directly invoke internal tools.

---

# 50. Reliability Requirements

Telegram should fail independently.

If Telegram crashes:

```text
SHAMSU continues.
```

If SHAMSU crashes:

```text
Telegram reports unavailable once the integration recovers.
```

If network disappears:

```text
local SHAMSU continues.
```

If Telegram API fails:

```text
retry with bounded backoff.
```

Do not allow Telegram connectivity problems to crash the core agent runtime.

---

# 51. Observability

Track:

```text
telegram_updates_received
telegram_updates_rejected
telegram_commands_processed
telegram_callbacks_processed
telegram_duplicate_updates
telegram_auth_failures
telegram_messages_sent
telegram_send_failures
telegram_reconnects
telegram_active_sessions
telegram_approvals
telegram_cancellations
```

Do not include secrets in telemetry.

---

# 52. Implementation Order

Implement this feature in this order.

## Phase 1 — Core interfaces

- TelegramService
- TelegramTransport
- TelegramController
- TelegramFormatter
- session gateway interface

No bot behavior yet.

## Phase 2 — Authentication

- token configuration
- authorization database
- pairing codes
- private-chat restriction

## Phase 3 — Long polling

- get updates
- offset persistence
- duplicate handling
- clean startup/shutdown

## Phase 4 — Basic commands

- /start
- /help
- /status

## Phase 5 — Session integration

- /sessions
- /switch
- active-session mapping
- session buttons

## Phase 6 — Free-text control

- Telegram message → active SHAMSU session
- SHAMSU result → Telegram

## Phase 7 — Run controls

- /pause
- /resume
- /cancel

## Phase 8 — Approval UX

- approval notifications
- Approve/Reject inline buttons
- callback validation

## Phase 9 — Developer UX

- /plan
- /changes
- /tests
- /logs

## Phase 10 — Files

- inbound files
- outbound artifacts
- diff/report documents

## Phase 11 — Notifications

- progress updates
- background-session events
- settings

## Phase 12 — Hardening

- reconnect
- bot restart
- SHAMSU restart
- security tests
- adversarial tests
- rate limiting

---

# 53. Definition of Done

The Telegram integration is considered complete when the following scenario works reliably:

```text
1. Start SHAMSU locally.

2. Enter:
   /remote_control

3. Pair Telegram securely.

4. Telegram displays available SHAMSU sessions.

5. Select an existing session.

6. Send a normal natural-language development task.

7. The request enters the existing SHAMSU runtime.

8. SHAMSU performs its normal planning/execution.

9. Telegram displays meaningful progress without tool-call spam.

10. SHAMSU requests approval.

11. User approves using an inline Telegram button.

12. SHAMSU continues the same run.

13. User views:
    - status
    - plan
    - tests
    - changes

14. User switches to another session.

15. Previous session continues independently.

16. User returns to the first session.

17. User can pause/resume/cancel its task.

18. Task completes with verified evidence.

19. Telegram provides a concise final report.

20. Restarting the Telegram integration does not lose pairing or session mapping.
```

---

# 54. Non-Negotiable Rules

1. Telegram is a remote interface, not another agent.

2. Telegram never bypasses SHAMSU's runtime.

3. Telegram never bypasses approvals or safety policies.

4. Telegram commands like `/status` and `/sessions` are deterministic and do not call the LLM.

5. Normal free-text requests enter the active SHAMSU session.

6. Session state is authoritative in SHAMSU, not Telegram.

7. Telegram user authorization uses stable IDs, not usernames.

8. The bot token is never committed or exposed.

9. Only a small authorized set of users can control SHAMSU.

10. Long polling is the initial local transport.

11. Webhooks may be added later behind the same transport interface.

12. Important actions use inline buttons.

13. Progress messages should be edited instead of creating excessive chat noise.

14. Large logs/diffs are summarized or sent as files.

15. Every remote action is auditable.

16. Telegram connectivity must never be required for SHAMSU itself to operate.

17. The integration must support multiple SHAMSU sessions.

18. Switching sessions must not terminate other running sessions.

19. Run cancellation must use SHAMSU's actual run controller.

20. The bot must feel like a polished mobile SHAMSU control panel, not a terminal pasted into Telegram.

---

# 55. Final Product Standard

The user should rarely need to memorize commands.

The ideal Telegram interaction consists mostly of:

```text
natural language
+
clear status cards
+
inline buttons
+
session selector
+
approval buttons
```

The bot should make it possible to comfortably manage SHAMSU from a phone while retaining the same project state, safety guarantees, evidence system, plans, and execution semantics as the local interface.