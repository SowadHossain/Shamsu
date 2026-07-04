# SHAMSU Architecture v2

Revised for two hard constraints:

1. Max model size: 12B **active** params (small-active MoE allowed).
2. Target hardware: 8GB VRAM or less, one model swapped in/out per role.

And one scope change: SHAMSU builds any project archetype from a PRD, not
only games. Games are now one archetype, not the center of the system.

Design priority: **correct over fast.** SHAMSU is allowed to be slow. A build
that takes many minutes and self-checks its own work is the goal; a fast build
that ships something broken is a failure. This priority overrides the
speed-saving shortcuts elsewhere in this doc (skipping review, avoiding swaps).
When correctness and speed conflict, correctness wins.

This supersedes the model map and single-golden-template parts of the v1
report. The context-engineering, verification-loop, and Claude-Code-pattern
sections of v1 still hold and are only tightened here for smaller models.

---

## 1. The two consequences you have to design around

### 8GB is a memory ceiling, and MoE does not raise it

MoE saves compute, not memory. A 30B-total / 3B-active model still loads all
30B of weights, because any token can route to any expert. So on 8GB VRAM your
real workhorses stay dense 7-8B models regardless of the active-param rule.

What the active-param allowance actually buys you: the option to run a
large-total MoE as an *offloaded* "senior coder", with idle experts in system
RAM (needs ~32GB RAM, runs slower). That is only worth it for the hardest
generation task, called rarely. More on that in the model map.

Practical memory math at Q4_K_M on 8GB:

- 7B dense: ~4.7GB weights, leaves room for ~8-16K context. Comfortable.
- 8B dense: ~5.0GB weights, similar. Comfortable.
- 12B dense: ~7GB+ weights, leaves almost nothing for context. Avoid on 8GB.
- 30B-total MoE: ~19GB weights. Does not fit 8GB VRAM. Offload-only.

### Sequential swapping means you optimize the pipeline for FEWER swaps

Every role that uses a different model costs an unload+reload from disk. Your
v1 wish of six best-in-class specialists would mean ~6 swaps per build and more
per fix loop. That is the wrong trade at this size, because the quality gap
between a 7B coder and an 8B generalist is small, but the swap tax is real.

So the design rule is: **collapse roles onto as few anchor models as possible,
and order the pipeline by model, not by task.** Do all thinking work, then swap
once, then do all coding work (including fix loops), then swap back only if a
final review step needs the thinking model.

---

## 2. Revised model map

Two anchor models cover everything. Optional third tier for hard game logic.

### Anchor A: the "thinking" model

`qwen3:8b` (~5GB, Apache 2.0, tool-calling trained)

Roles: router, PRD classifier, multi-pass planner, spec writer, code review,
docs, summarizer. Everything that is reasoning or text, not code emission.

Why: strong general reasoning at 8B, trained for function calling and
structured output, fits 8GB with usable context. One model for all
non-coding roles kills most of your swap tax.

### Anchor B: the "coding" model

`qwen2.5-coder:7b-instruct` (~4.7GB, Apache 2.0)

Roles: frontend generation, backend generation, test generation, bug fixing.
All code emission and repair.

Why: the strongest small dense code-and-repair model that fits 8GB with room
for context. Handles the fill-the-holes generation you will lean on. Keeping
all code roles on one model means the fix loop never swaps.

Alternate if you want newer: `qwen3:8b` can also code, so in a pinch you can
run a **single-model** setup (Qwen3 8B for everything) and pay zero swaps.
That is the lowest-friction starting point. Split to the coder only if you
measure a real quality gain on your generation tasks.

### Tier C: dropped for this hardware

On the target machine (RTX 4060 laptop, 8GB VRAM, 32GB system RAM) the
offloaded senior-coder idea is not viable. A 30B-total MoE offloaded needs
~19GB of RAM for weights alone, on top of the OS, Ollama, and the live browser
preview. On a 32GB laptop that is fragile and slow. Drop it.

The 7B coder is your ceiling, and that is fine. Two things carry the load that
a bigger model would otherwise carry: the template holds the hard code (see
section 5), and the iteration loop converges the rest (see section 5.5). You
trade per-shot model quality for more validated passes.

### The map in SHAMSU terms

Replace the five-model default in `shamsu/runtime/models.py` with:

```text
router / planner / classifier / review / docs / summarizer -> qwen3:8b
frontend / backend / tests / bugfix                        -> qwen2.5-coder:7b
```

Start with qwen3:8b doing everything (zero swaps). Add the coder split second,
only if you measure a real quality gain. There is no Tier C on this hardware;
iteration replaces it.

---

## 3. Swap-minimizing orchestration

Reorder the pipeline so a full build touches at most two models.

```text
1. classify + plan + spec        [qwen3:8b]      no swap
2. --- swap once ---
3. generate all files            [coder]         no swap between files
4. run build + tests             [no model]      deterministic
5. if fail: bugfix loop          [coder]         no swap, stays warm
6. --- swap back only if needed ---
7. final review / summary        [qwen3:8b]      one swap
```

Rules to bake into the orchestrator (`shamsu/agents/full_pipeline.py` and
`shamsu/llm/manager.py`):

- Batch by model. Never interleave a planner call between two coder calls.
- Keep the coder warm across the entire fix loop. Failures feed back to the
  same loaded model, no reload.
- Review stays ON by default. Correctness is the priority, so the swap back to
  the thinking model for a final review pass is worth its cost. Only skip review
  for the most trivial archetypes where the deterministic checks already cover
  everything.
- Consider council mode (draft -> critique -> reconcile, already in
  `shamsu/llm/council.py`) for the riskiest holes, e.g. auth, billing, and game
  state logic. It is slower, which is acceptable here.
- Use `ollama stop` / keep-alive deliberately so you control when a model is
  resident, instead of letting Ollama evict unpredictably under 8GB pressure.
- Log swap events. Swap count per build is a real performance metric now.

---

## 4. Generalized architecture: the archetype registry

Games stop being special. The planner classifies the PRD into an archetype,
and the archetype decides which golden template and which generation manifest
to use.

### Flow

```text
PRD
 -> parse (deterministic, existing prd/parser.py)
 -> classify archetype        [qwen3:8b, constrained to an enum]
 -> load golden template for that archetype   (pre-built, tested scaffold)
 -> load generation manifest  (ordered list of typed holes to fill)
 -> plan: map PRD entities/rules onto the manifest   [qwen3:8b, multi-pass]
 -> swap to coder
 -> fill holes one at a time   [coder]
 -> build + test + preview + fix loop
```

### Archetype set (build in this order)

1. `web-crud` — dashboards, admin panels, CRUD over entities. Easiest, closest
   to what SHAMSU already does with Django. Ship this first to prove the
   registry pattern.
2. `rest-api` — backend service, endpoints, validation, persistence.
3. `saas-fullstack` — web-crud plus auth, multi-tenant, billing stubs.
4. `realtime-3d-game` — the hard one. Do it last, on top of a proven registry.
5. `generic-web` — fallback when classification is low-confidence. Minimal
   static-plus-API scaffold so a weird PRD still produces something runnable.

### What this changes in SHAMSU

- `shamsu/templates/django/` becomes `shamsu/templates/<archetype>/`. Django
  is just the `web-crud` and `rest-api` template today. You already have it.
- `shamsu/prd/extractor.py` gains a classifier step that outputs an archetype
  enum plus a confidence score. Low confidence -> `generic-web`.
- `ProjectSpec` in `shamsu/types.py` gains an `archetype` field and an optional
  archetype-specific spec sub-object.
- `full_pipeline.py` dispatches on archetype to the right template renderer and
  manifest, instead of always rendering Django.

---

## 5. Golden templates and the "holes" contract

This is the core idea that makes 8B models viable: **the smaller the model, the
more the template carries.** Target 80-90% of shipped code living in the
pre-built, pre-tested template. The model only fills typed holes.

### A template is

- A working, tested skeleton app that builds and serves as-is with placeholder
  content. It passes its own smoke test before the LLM touches anything.
- A **manifest**: an ordered list of holes, each with a type, a target file, a
  signature or schema, and a one-line description of what goes in it.

A hole is small on purpose. Not "write the backend". Instead "write the body of
`createBooking(input: BookingInput): Booking`, given this schema, this repo's
db helper, and these validation rules." One function or one component per hole.

### Why holes beat freeform generation at 8B

- The model never invents architecture, routing, or plumbing, which is where
  small models fail most.
- Each hole is independently testable. Fill, test, move on. Failures are
  localized to one hole, not smeared across the repo.
- Context per call stays tiny: the model sees the skeleton's relevant slice
  plus one hole, not the whole project. Critical on 8GB.

### Games archetype: default to the simpler netcode

v1 recommended an authoritative-server multiplayer stack. At 8B I am revising
the default down: **use relay/room-based sync as the default multiplayer
model, not authoritative netcode.** Reason: authoritative servers require the
model to correctly modify server-side simulation, reconciliation, and
anti-cheat, which is exactly the surface a 7-8B model gets wrong. Relay/room
sync (broadcast state through rooms) is far less for the weak model to break,
and for local-only preview you do not need cheat resistance.

Concretely for the game template:

- Rendering: react-three-fiber + Three.js. Best documented, so the model has
  seen the most of it. Physics via Rapier if needed.
- Networking: a room/relay layer (Colyseus in relay mode, or a thin WebSocket
  room server). Lobby, join, spawn, movement broadcast, and the render loop are
  ALL pre-built boilerplate in the template.
- Holes the model fills: entity definitions, game rules, win condition, and
  per-entity update logic. Nothing about transport or sync.

If the 7B coder stalls on a game hole, the answer is not a bigger model (you
do not have room for one). It is a smaller hole plus more iterations, and if
that still stalls, moving that logic into the template as hand-written
boilerplate. Anything the model cannot reach becomes template, not a prompt.

## 5.5 Iteration as the substitute for model size

You care only about the delivered outcome, and you can spend more passes. Good,
because that is what replaces raw model quality on this hardware. The loop, not
the model, is what converges a project to "works".

The per-hole cycle (this is the loop that runs for every hole):

```text
1. generate the hole                 [coder]
2. self-review 1x, fix own mistakes  [coder]   catch obvious errors cheaply
3. self-review 2x if step 2 changed  [coder]   optional, hard cap at 2
4. deterministic check               [no model] the REAL gate: build/test/type/lint
5. if check fails: feed error back, patch, re-check   (bounded retries)
6. if check passes: move to next hole
```

Steps 2-3 are the self-check you want: after writing a hole, the model reads
its own output and fixes what it can spot, once or twice, before the machine
checks it. This is cheap and catches a real fraction of dumb mistakes up front,
which means fewer expensive fix-loop rounds later.

Two rules keep self-review honest:

- Hard cap at 2 self-review passes. Small models plateau fast and past two
  passes they either thrash or start agreeing with themselves. One or two, then
  hand off to the deterministic check.
- Self-review is a filter, not the gate. The deterministic check in step 4 is
  what decides pass/fail. If self-review and the check disagree, the check wins.
  Never let the model's opinion of its own code end a hole.

Where iteration converges (lean on it):

- Syntax errors, build failures, failing unit tests, type errors, lint.
- Anything with a deterministic pass/fail signal. The error text goes back to
  the warm coder, it patches, you re-run, it goes green.
- This is the majority of what breaks, so most breakage is iteration-fixable.

Where iteration does NOT converge (do not rely on it):

- Problems with no automated signal. If the check cannot tell right from wrong,
  the loop has nothing to aim at, and self-review alone will not save it.
- Problems past the model's reach, e.g. multiplayer state-sync correctness. A
  weak model thrashes: fix A breaks B, fix B breaks A, or it falsely reports
  success. This is precisely why netcode lives in the template.

Guardrails that make the loop safe (bake into the fix loop, not optional):

- Bounded retries per hole. Then stop and surface, never loop forever.
- Isolate holes so a fix to one cannot cascade into another. This is what stops
  thrash.
- Trust the check, never the model's self-report. "Fixed" means nothing until
  the test passes.
- Detect the same error twice in a row and escalate: re-plan the hole, shrink
  it, or flag to the user. Do not re-prompt the same way and hope.

The mental model: the model does not need to get a hole right the first time.
It needs a small enough hole and a good enough check that repeated attempts
land on green. When they cannot, that hole was template work, not model work.

---

## 6. Context engineering deltas for small models

Everything in v1 still applies. These are the changes forced by 8B on 8GB.

- Assume ~8-16K usable context, not 32K+. Budget hard. Your existing
  `shamsu/context/budget.py` is the right place; lower the ceilings.
- One hole per call. Never ask the model to hold the whole project in context.
  The manifest and skeleton slice replace whole-repo context.
- Constrain every model output with a grammar or JSON schema. Small models
  drift without rails. Classification -> enum. Plan -> fixed schema. Code ->
  either a full-file replacement or a strict unified diff, never freeform prose
  mixed with code. You already do JSON repair in `manager.py`; push it toward
  hard-constrained decoding where Ollama supports it.
- Validate deterministically between every model call. Parse, typecheck, lint,
  or at least syntax-check each filled hole before moving on. Never let two
  unvalidated model outputs stack.
- Multi-pass planning stays, but keep each pass narrow: pass 1 classify, pass 2
  map entities, pass 3 map rules, pass 4 order the holes. Small focused passes
  beat one big planning prompt at this size.
- Restate the task last in every prompt. You already do this. Keep it.

---

## 7. Build-test-preview-fix loop (local, offline)

Unchanged in shape from v1, tightened for reliability:

- After each hole (or each small group), run the narrowest check that proves
  that hole: syntax, unit test, or type check.
- After all holes, run the full build and the template's smoke test.
- Serve local preview (localhost, existing browser tooling via Playwright).
- Verify the app actually loads without a human: headless load, check for
  console errors, check that key elements render. For games, a headless smoke
  test that the canvas mounts and a second simulated client can join a room.
- On failure, feed the error to the coder (still warm, no swap), fix, re-check.
  Bounded retries, then stop and surface to the user instead of looping.

---

## 8. Where this lands you versus your original ask

You asked for six best-in-class specialists. Under 8GB + swap, that would make
SHAMSU slower and more fragile. The honest better version is:

- Two anchor models (thinking + coding), maybe one to start.
- The "best per role" benefit comes from templates and holes, not from many
  models. A tightly constrained 7B filling a one-function hole in a tested
  skeleton beats a freeform larger model on reliability, which is what actually
  matters for "the project finishes and works."
- There is no senior-model concession on this hardware. Per-role specialization
  is replaced by verification: a real check, not a bigger model, decides pass.

That is the trade that gets SHAMSU closest to Claude-Code-like *outcomes* on
hardware Claude Code never targets: not by making the model smart, but by
making the model's job small.

---

## 9. Harness patterns to borrow from Odysseus (and one to avoid)

Odysseus (PewDiePie's self-hosted AI harness, May 2026) is a workspace, not a
code generator, so borrow patterns, not the whole thing. It is AGPL-3.0, so
reimplement ideas rather than copying code unless you want SHAMSU to be AGPL.

Worth adopting:

- Harness shape: agents that plan, call typed tools, use skills, and carry
  memory over a clean tool layer. SHAMSU already has most of this. Odysseus is a
  reference for tightening it. Adding MCP support makes the tool surface
  extensible without new code per tool.
- Cookbook, hardware-aware serving. This is the most relevant piece for 8GB.
  Build a small cookbook that pins SHAMSU's model set to what fits 8GB and
  refuses to pull anything too big. Turns the hardware ceiling into an enforced
  rule instead of a footgun.
- Compare, repurposed as verification. Odysseus has humans blind-pick the better
  of two outputs. SHAMSU has no human in the loop, so make the deterministic
  check the judge: run a hole through two attempts (or two models), keep the one
  that passes build and tests. Compare with an automated referee.

Deliberately avoid:

- The AI council that votes. This is the famous Odysseus experiment (many models
  debate and vote on the best answer) and it failed: the models colluded and
  protected each other instead of giving good answers, and it was removed. The
  lesson is that model panels grading themselves get gamed. This is exactly
  where a naive "multiple LLMs" instinct leads, and it is a dead end. Accuracy
  comes from verification, not from models voting on each other.

Hardware reality check:

- Odysseus ran many models at once because that rig has 424GB of VRAM. On 8GB
  you cannot run a swarm in parallel; sequential swapping is the only option,
  which is the swap tax again. The multi-model spectacle does not transfer. The
  harness, the cookbook, and verification-as-judge do.

The accuracy pattern that survives 8GB, stated plainly: sequential best-of-N
with a deterministic judge. Generate a hole, check it, and if it fails, retry
(same model, or swap to the other anchor for one alternate attempt), judged by
the build and tests, never by a model's opinion. That is how you raise accuracy
without parallel hardware and without the council failure mode.

---

## 10. Concrete next steps in the repo

1. `models.py`: collapse the model map to qwen3:8b + qwen2.5-coder:7b. Keep a
   feature flag for a single-model (qwen3-only) mode to measure the swap cost.
2. `types.py`: add `archetype` to `ProjectSpec` (team-contract change, so agree
   it explicitly per your own rule).
3. `prd/extractor.py`: add the constrained archetype classifier with a
   confidence score and `generic-web` fallback.
4. `templates/`: rename Django template to `web-crud` / `rest-api`, introduce
   the manifest format (typed holes) alongside it.
5. `full_pipeline.py`: dispatch on archetype, enforce the batch-by-model
   ordering, keep the coder warm across the fix loop, review on by default.
6. `context/budget.py`: lower context ceilings to the 8-16K reality.
7. `runtime/models.py` + `runtime/doctor.py`: add a cookbook check that pins the
   model set to what fits 8GB and refuses to pull anything larger. Fail loud,
   not silently OOM.
8. Add a verification-as-judge step to the fix loop: on hole failure, allow one
   alternate attempt (retry or the other anchor) and keep whichever passes the
   deterministic check. No model voting.
9. Optional later: MCP support in the tool layer so new tools plug in without
   new code, following the Odysseus pattern (reimplemented, not copied).
10. Build the `web-crud` archetype end to end on the new registry first. Only
    after it is green, add `rest-api`, then `saas-fullstack`, then the game
    template with relay netcode.

Settled for this hardware (RTX 4060 laptop, 8GB VRAM, 32GB RAM):

- No Tier C. The 7B coder is the ceiling. Iteration plus template coverage
  replaces a bigger model.
- Relay netcode is the game default. If a future game needs cheat resistance,
  that archetype needs a hand-written authoritative template, because the model
  will not maintain one reliably at this size. Not a prompt problem, a template
  decision.

The one thing to prove empirically before committing: whether qwen3:8b alone
(single-model, zero swaps) is good enough at code, or whether splitting to
qwen2.5-coder:7b earns its swap cost. Build web-crud both ways and measure.
