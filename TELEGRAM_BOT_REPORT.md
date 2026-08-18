# Telegram Remote Control for SHAMSU — Project Report

## What we set out to do

SHAMSU is a local-first coding agent. It runs on your machine, uses a local
model, and touches your real files. That is exactly what makes it useful, and
also what makes it inconvenient: to see how a long build was going, or to answer
a single approval question, you had to be sitting at the machine.

The goal of this piece of work was to put SHAMSU in your pocket. You should be
able to kick off a task from your phone, watch it progress, answer its questions,
steer it when it goes the wrong way, and pick the same conversation back up on
your laptop as if you had never left — while SHAMSU keeps working on whatever
else you have running locally.

There were four problems to solve, and this report walks through each in turn:

1. Connecting a bot to a SHAMSU installation, once, so it stays connected.
2. Proving that the person messaging the bot is the person who owns the machine.
3. Letting a phone and a laptop drive the *same* conversation without fighting.
4. Letting several SHAMSU instances run at once without starving each other.

---

## 1. Linking a bot to your SHAMSU installation

### How it works from a user's point of view

Setting up remote control is a one-time job that takes about a minute.

**Step one — create a bot.** You do this in Telegram itself, not in SHAMSU. You
message `@BotFather`, say `/newbot`, pick a name, and it hands you a token — a
long string that looks like `8123456789:AAF...`. That token is the credential
that lets a program act as your bot. It is yours; nobody else gets it.

**Step two — give the token to SHAMSU.** Open SHAMSU and run:

```
/remote_control configure 8123456789:AAF...
```

SHAMSU checks the token looks structurally right, saves it, and — importantly —
never prints it again. From that point on `/remote_control status` will tell you
the token is configured and where it came from, but it will not show you the
token itself. It is also stripped out of the session log, so the command you just
typed is stored as `/remote_control configure [REDACTED]`.

**Step three — connect and pair.** Run `/remote_control` on its own. SHAMSU
starts the bot and prints a six-digit code in your terminal. Open your bot in
Telegram, press Start, and type those six digits. The bot replies that you are
paired, and shows you a home screen with buttons.

That is it. From then on, messaging the bot means talking to SHAMSU.

### "Once, for the whole installation" — and what that actually means

The intent is that you link once per *machine*, and the bot works across
everything you do with SHAMSU on it — not once per project folder.

That distinction matters more than it sounds. SHAMSU organises work into
**workspaces**: a workspace is simply a project directory, and each one carries
its own `.shamsu/` folder holding that project's sessions, run history and
settings. If the bot link lived inside a workspace, you would have to repeat the
whole setup — new token, new pairing — for every project you ever opened. Ten
projects, ten bots, ten chats on your phone. Nobody would use it.

So the link is deliberately stored at **installation level**, in your home
directory alongside SHAMSU's other machine-wide state, rather than inside any one
project. The token lives there, and so does the record of which Telegram account
is allowed in. Both are read by whichever part of SHAMSU needs them, regardless
of which project you happen to be working on.

Unlinking is the mirror image: `/remote_control disconnect` revokes the paired
account and stops the bot. The link is durable until you say otherwise — it does
not quietly expire, and it does not depend on you leaving a particular terminal
window open.

> **Technical aside — why the bot polls instead of using webhooks.**
> The Telegram Bot API offers two ways to receive messages. A *webhook* means
> Telegram makes an HTTPS request to you whenever something happens; *polling*
> means you repeatedly ask Telegram "anything new?".
>
> Webhooks are the better choice for a server. They are the wrong choice here.
> A webhook needs a publicly reachable URL, a real domain, a valid TLS
> certificate, and an open inbound port. SHAMSU runs on a developer laptop, quite
> possibly behind a corporate NAT and a firewall, on a connection whose IP
> changes whenever you move between home and a café. Making that work would mean
> a tunnelling service or a hosted relay — a cloud dependency, in a tool whose
> whole premise is that nothing leaves your machine.
>
> Polling has none of those problems. SHAMSU is a pure outbound HTTP client, the
> same as a browser, so it works anywhere the laptop has internet with zero
> network configuration. We use *long* polling, where the request stays open for
> up to 30 seconds waiting for something to happen, so it is not a busy loop —
> in practice it is one idle connection and near-zero traffic, and messages
> arrive in well under a second.
>
> We also remember our position in the update stream on disk rather than in
> memory. Telegram hands out updates with increasing ids and expects you to
> acknowledge how far you have read. Storing that on disk means restarting
> SHAMSU resumes cleanly instead of replaying every message you ever sent.

> **Technical aside — no Telegram library.**
> There are good Python libraries for this (`python-telegram-bot`, `aiogram`) and
> we used none of them. We call the Bot API directly over HTTP with `httpx`,
> which the project already depended on. We use exactly four API methods, so a
> full framework would have added a large dependency, its own opinions about
> event loops and threading, and an upgrade treadmill, in exchange for
> convenience we did not need. The integration adds **zero new dependencies**.

---

## 2. Authentication — the six-digit code

### The problem

Here is the uncomfortable fact about Telegram bots: **anyone can message yours.**
Bot usernames are public and searchable. If somebody guesses or stumbles onto
your bot's name, they can open a chat and start typing. Telegram will happily
deliver their messages.

That is fine for a bot that tells you the weather. It is not fine for a bot
wired to an agent that can edit files and run shell commands on your laptop.

Telegram has no concept of "this account owns that computer", so it cannot
answer the question for us. We had to answer it ourselves, and the question is
really: *how does a chat prove it belongs to the person sitting at the machine?*

### The answer: prove you have local access

Our answer is the six-digit code, and the reasoning behind it is simple. The one
thing a legitimate user can do that an attacker on the internet cannot is **read
the terminal on your laptop**. So we mint a secret there, and require it to come
back through the chat.

When you run `/remote_control`, SHAMSU generates a random six-digit code and
prints it in your terminal — and nowhere else. It is never sent over the network,
never shown in the chat, never written into a log. It is a short-lived password
that only somebody with eyes on your screen can know.

You type it into the bot. The bot checks it, and if it matches, that Telegram
account is now trusted.

Several deliberate details sit under that simple story:

- **The code is generated with a cryptographic random source**, not an ordinary
  random number generator, so it cannot be predicted from previous codes.
- **We never store the code itself** — only a SHA-256 hash of it. Anyone who
  gets at SHAMSU's internal database still cannot read out a working code.
- **It expires after five minutes.** A code left on a screen overnight is dead.
- **It works exactly once.** Once used, it is marked consumed and rejected
  forever after, so a code someone glimpses cannot be reused.
- **Five wrong guesses and it is dead.** Six digits is a million possibilities,
  which sounds like a lot, but an automated attacker would burn through them
  quickly. A hard attempt limit means brute force never gets started.
- **Pairing only works in a private chat.** You cannot pair from a group.

### What happens on every message afterwards

Pairing establishes *who you are* once. But we check *whether you are still
allowed* on every single message — every text, every button press, every file.
There is no "logged in" state that stops being verified.

Three things have to be true for a message to be acted on:

1. It came from a **one-to-one private chat**, not a group or channel. Groups are
   a multi-party context where you cannot cleanly attribute an instruction to one
   person, so we disable them entirely.
2. The sender holds an **active authorization**.
3. The message arrived in the **same chat the pairing was made in**.

That third check is worth explaining. We bind the authorization to a specific
account *and* a specific conversation. So even a paired user cannot drive SHAMSU
from some other chat window — the credential is not portable.

One more small but important choice: we identify people by their **numeric
Telegram user id**, never their `@username`. Usernames can be changed, and
released usernames can be claimed by someone else. Numeric ids are permanent.
Binding to the username would have created a real hijacking route.

### Buttons need protecting too

Much of the bot is buttons — *Switch session*, *Pause*, *Approve*. Buttons look
harmless but they are a genuine attack surface, because the data a button sends
back is supplied by the client and can be replayed or forged.

So our buttons carry **no meaning at all**. Each one sends back a random opaque
token. What that token *means* — which action, which session, which run, on whose
behalf — is recorded on our side, and is never exposed to the client. When a
button comes back we check that the token exists, that the person pressing it is
the person it was created for, that it is in the right chat, that it belongs to
the run it claims to, that it has not already been used, and that it is less than
ten minutes old.

This kills a whole family of problems at once. A button cannot be forwarded to a
friend and pressed. A stale *Cancel* from an old task cannot cancel today's task.
Double-tapping *Approve* cannot approve twice.

### Approvals: remote answers, not reduced safety

SHAMSU already asks before doing anything risky — deleting files, running
destructive commands. A real concern with remote control is that it becomes a way
to *bypass* those questions.

It does not. The local safety policy still decides **what** requires approval;
Telegram only carries **your answer** to a question that policy already raised.
The rules do not loosen because you happen to be answering from a phone.

And the failure mode is safe by design. If SHAMSU cannot reach you, the answer is
no. If a question goes unanswered for fifteen minutes, it times out — as a
rejection, not an approval.

### Everything is visible locally, and everything is logged

Remote control you cannot see is a security problem on its own. So every message
your phone sends and every reply SHAMSU sends back is **mirrored into the local
terminal** in a panel. If someone is driving your SHAMSU remotely, anyone at the
laptop sees it happening, live.

Underneath, every meaningful action writes an audit record — who, which chat,
which session, which run, what they asked for, what the result was — with secrets
stripped out before anything is written down.

---

## 3. Working on one session from both the bot and your laptop

This was the part that had to feel seamless, and it is the part we are happiest
with.

### What it feels like to use

You are on the train. You message the bot: *"add rate limiting to the login
endpoint."* SHAMSU replies that it has started, and updates trickle in as it
works — which file it is reading, which command it is running.

You get home and open your laptop. The same conversation is there, in the same
session, with the full history of what happened while you were out. You type a
follow-up locally, in the terminal, and it lands in the same thread.

Halfway through, SHAMSU heads in a direction you do not like. You type *"no, use
the middleware pattern instead"* — from either your terminal or your phone,
whichever is closer — and it changes course. It does not start over, it does not
run two conflicting attempts. It adjusts.

Meanwhile your phone stays up to date. Approvals show up wherever you are.

### How we made that work

**Both channels talk to the same conversation on disk.**
A SHAMSU session is not something held in memory by one program — it is a folder
of files: the transcript, the event log, the current state. The bot and the local
REPL both read and append to that same folder. Neither owns the conversation;
the conversation owns itself, and both are just clients of it. That is why the
history is simply *there* when you open your laptop, with no syncing step and
nothing to reconnect.

**The bot runs inside the SHAMSU process, on purpose.**
It would have been more conventional to run the bot as a separate program. We
deliberately did not, and the reason is that SHAMSU's ability to control a
*running* task — pause it, cancel it, feed it new instructions — lives in the
memory of the process executing it. A separate program could have watched from
outside and read the files, but it could never have reached into a task and
steered it. By running the bot as a background thread inside SHAMSU, it shares
that control state directly, which is what makes "change course from your phone,
mid-task" possible at all.

**The important design decision: we merge, we do not race.**
Two input channels pointed at one conversation is an obvious recipe for disaster
— two tasks editing the same files, each undoing the other.

The rule that prevents it: **if SHAMSU is already working, your new message
becomes guidance for the work in progress, not a second competing job.**

When a message arrives, we first check whether that session already has something
running. If it does — whether it was started from your phone or your keyboard —
the message is injected into the running task as feedback. There is one task,
with two ways to talk to it. Only when nothing is running does a message start
something new.

We also interrupt the model mid-thought when feedback arrives, rather than
waiting politely for it to finish. If you have said "stop, do it differently,"
you should not have to watch it spend another minute finishing the wrong idea.
The instruction is picked up at the next safe point — between steps, never in the
middle of writing a file.

**The bot never goes deaf while it is working.**
An early version blocked: send a task, and the bot stopped answering until the
task finished, which felt broken. Now, when you send work, the bot acknowledges
it immediately and hands the actual job to a separate worker. The part of the bot
that listens to Telegram stays free, so `/status` answers instantly even while a
long build is running. That behaviour is important enough that we wrote a test
that deliberately blocks a task and asserts `/status` still replies.

**Progress updates are throttled, not firehosed.**
An agent emits a lot of chatter, and forwarding all of it would be unusable on a
phone. Routine progress is rate-limited to roughly one update every eight
seconds, while the things you actually want immediately — a shell command
starting, a tool failing, a warning, the final result — always go out at once.

> **Technical aside — waiting for a human takes time, and the timeouts know it.**
> A remote task might sit waiting for you to tap *Approve* while you are in a
> meeting. If the task timeout were shorter than the approval window, tasks would
> get killed for doing exactly what they were designed to do. So the task timeout
> is *derived* from the approval timeout — always comfortably longer — rather
> than configured independently and left to drift out of sync.

---

## 4. Running several SHAMSU instances at once

The last requirement was the hardest: your phone drives a task in one project
while you work locally on a completely different project, and neither gets in the
other's way.

### The easy half: keeping projects apart

Isolation between projects was largely solved already, because SHAMSU treats a
workspace as a hard boundary. Each project keeps its own sessions, its own run
history, its own settings, in its own folder.

More importantly, every file operation is checked against a **sandbox** tied to
the project it belongs to. Any path that resolves outside that project's
directory is rejected outright. So a task running in project A physically cannot
read or write files in project B, however it is instructed to. Two SHAMSU
instances working side by side cannot contaminate each other's code — that is
enforced, not just conventional.

### The hard half: they share one machine

Isolation of *files* is not isolation of *resources*, and this is where it got
interesting. Two SHAMSU instances on one laptop share one CPU, one disk, and —
critically — **one local model**.

That last one is the real constraint. SHAMSU runs its model through Ollama on
your own GPU. On a typical 8 GB card there is room for one 7-billion-parameter
model and its context window, and not much else. Ollama also reserves memory for
the *entire* context window up front, not just what is used, so two instances
each asking for a large window do not gracefully share — one spills into system
RAM and slows down by an order of magnitude, dragging the other down with it.

Nothing in SHAMSU limited this. Two instances would both fire requests at the
model, and the second would simply block, waiting.

Worse, it would then **lie about it**. SHAMSU watches for a model that has gone
silent, and reports a stall if nothing comes back for a few minutes — a sensible
deadlock detector. But a request queued behind another instance is *also* silent.
So a perfectly healthy second instance, patiently waiting its turn, would be
reported as a crashed model.

### How we handled it

**One queue for the model, everything else in parallel.**
We put a single machine-wide slot in front of the model. Only one instance
generates at a time; the others queue. Everything that is *not* the model —
reading files, running tests, git operations, indexing, applying edits — carries
on fully in parallel. In practice a lot of an agent's wall-clock time is spent on
those, so this costs much less throughput than it sounds, and it completely
removes the thrashing.

We chose one slot rather than two because of the hardware reality above. On an
8 GB card, two concurrent generations do not run twice as fast — they run several
times slower, or fall over. One at a time, quickly, beats two at once, badly.
It is a configurable number for people with more VRAM.

**Waiting is not a timeout.**
This is the fix for the false stall, and it is a change in *where the clock
starts*. All of SHAMSU's model timeouts now begin only once a request has been
granted a slot. Time spent queueing does not count against anything — an instance
can wait as long as it needs to. A queue is not a fault, and it should never be
reported as one.

**Say what is actually happening.**
Rather than showing a spinner and hoping, a queued instance reports that it is
waiting, and what it is waiting for. When you see one project pause, you can tell
at a glance that the other one has the model, and that yours will proceed.

**One writer per conversation.**
Separately from the model, we needed to stop two processes writing the *same*
conversation at once — that corrupts the transcript, because the session files
were written on the assumption that one program owned them. Each active session
now has a single owner. If a message arrives for a session another process owns,
it is handed to that owner to execute rather than run twice. This is the same
merge-don't-race rule from section 3, extended across processes.

**Crashes clean up after themselves.**
Every instance records that it is alive and periodically checks in. If one is
killed or crashes, whatever it was holding — its model slot, its session
ownership — is noticed as abandoned and reclaimed by the next instance that needs
it. A hard kill does not leave the machine wedged.

> **Technical aside — one bot, many projects.**
> Getting a single bot to serve several projects at once needed one specific
> thing: exactly one component on the machine talking to Telegram.
>
> Telegram hands each incoming message to **only one** listener. If two SHAMSU
> instances both polled with the same token, they would split the conversation
> between them at random — half your messages going to one project, half to the
> other, with no pattern. It would look like messages were vanishing.
>
> So the bot became a single machine-wide component rather than something each
> instance runs, and it routes each message to the right project. That is also
> what makes "link once for the whole installation" true rather than aspirational
> — one link, one listener, every project reachable.

---

## What we would do differently

**We should have separated the bot from the terminal earlier.** Running it inside
the interactive session was the fast way to get something working, and it bought
us the shared control state that makes section 3 work. But it also meant the bot
was tied to the lifetime of a terminal window, and unpicking that later was more
work than building it that way from the start.

**Resource contention was invisible until we ran two instances for real.** Every
test passed. The model queueing problem — and the false "stalled" report hiding
it — only appeared under genuine concurrent load. Worth remembering: the bugs
that matter in concurrent systems tend not to be the ones unit tests find.

---

## Where this stands

Honest status, so the report is not read as more finished than it is.

**Working and tested today:**

- Bot setup, token handling and pairing, with the full secret-handling story.
- The six-digit authentication flow and per-message authorization, including the
  opaque button tokens and the fail-closed approval bridge.
- Driving one session from both Telegram and the local terminal, including the
  merge-don't-race feedback rule, the immediate acknowledgement, and progress
  streaming.
- Per-project file isolation via the sandbox.

There are 30 automated tests covering this, all running against a fake Telegram
transport — no network, no real bot account, no live model needed.

**Designed and specified, not yet landed:**

- Installation-level linking. Today the token and pairing live inside a project
  folder, so the link is per-project and stops when you close the terminal.
  Moving both to machine level is what makes "link once, use everywhere" real.
- One bot serving several projects at once. Today it is one project at a time.
- The model slot queue, the corrected timeout behaviour, and session ownership
  across processes.

The last two both depend on the same change — moving the bot out of the terminal
into a single machine-wide component — which is why they are grouped together as
one piece of work rather than three.

**Known rough edges we found along the way:** pause and cancel are not reliable
in all cases, file uploads currently arrive empty, and replies longer than about
four thousand characters get cut off. All three are tracked with specific causes
identified.
