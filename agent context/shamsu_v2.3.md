
# SHAMSU Template Registry + Master Prompt Implementation Plan

Build spec for Claude Code. Target: detect project category, inject the
matching master prompt and core template, and enforce a per-category
Definition of Done so Shamsu stops shipping barebones projects.

Motivating failure: asked for a multiplayer 3D cube runner, Shamsu rendered a
single cube with no menu, no netcode, no lobby, no game loop, no HUD. Root
cause: nothing defined what "done" means for that category, so a small model
saw no error and declared success. This plan fixes that.

---

## 0. The core principle

Three parts must work together. Any one alone fails.

1. Master prompt. Tells the model what this category of project must contain and
   how to work inside the template.
2. Core template. A working, pre-built scaffold that already contains the
   plumbing (menu, connection, lobby, loop, UI), so those features exist by
   default and the model only fills game-specific logic.
3. Definition of Done (DoD). A machine-checked checklist of required features.
   The build is NOT done until every required feature is present and passes its
   check. This is the piece that was missing. It converts "should have a menu"
   into "fails until a menu renders."

The DoD is the enforcement layer. Without it, master prompts and templates are
suggestions a small model can ignore. With it, missing features become failing
gates that drive the fix loop.

---

## 1. High-level flow

```text
user prompt / PRD
  -> detect_category()            two-stage: deterministic router, LLM fallback
  -> load_registry_entry(category)   template dir + master_prompt + manifest + dod
  -> scaffold: copy template into workspace
  -> baseline smoke test           prove the template itself runs BEFORE editing
  -> plan: map PRD features onto manifest holes    [qwen3:8b + master_prompt]
  -> (swap to coder)
  -> for each hole: generate -> self-review 1-2x -> deterministic check   [coder]
  -> full build + run DoD checks
  -> any DoD item failing -> targeted fix loop (bounded)   [coder, stays warm]
  -> review pass                   [qwen3:8b]
  -> serve local preview
```

The DoD checks run after generation and gate completion. If the cube runner
comes back with only a cube, the DoD "two players visible" and "main menu
renders" checks fail, and the fix loop is forced to address them.

---

## 2. Files to create and change

New:

```text
shamsu/registry/__init__.py
shamsu/registry/categories.py        # Category enum + signal tables
shamsu/registry/detector.py          # two-stage category detection
shamsu/registry/loader.py            # load a RegistryEntry from disk
shamsu/registry/schema.py            # dataclasses: RegistryEntry, Manifest, Hole, DoDItem
shamsu/verify/__init__.py
shamsu/verify/dod.py                 # Definition-of-Done runner
shamsu/verify/checks.py              # reusable check primitives (file exists, route exists, headless render, etc.)

shamsu/templates/multiplayer-game/   # worked example category (built first)
  meta.yaml
  master_prompt.md
  manifest.yaml
  dod.yaml
  template/                          # the actual working scaffold
  smoke/                             # baseline + DoD smoke tests

shamsu/templates/portfolio-site/     # same structure, built after
shamsu/templates/multi-tenant-admin/
shamsu/templates/ecommerce/
shamsu/templates/general-web/
```

Changed:

```text
shamsu/types.py                      # ProjectSpec gains category, master_prompt, manifest_path, dod_path
shamsu/prd/extractor.py              # call detector, attach category to spec
shamsu/agents/full_pipeline.py       # dispatch on category, inject master prompt, run DoD gates
shamsu/llm/manager.py                # inject master prompt into system context for planning + coding
shamsu/context/budget.py             # reserve budget for master prompt block
```

---

## 3. Data models

Put these in `shamsu/registry/schema.py` as dataclasses (SHAMSU is Python).

```python
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

class Category(str, Enum):
    MULTIPLAYER_GAME    = "multiplayer-game"
    PORTFOLIO_SITE      = "portfolio-site"
    MULTI_TENANT_ADMIN  = "multi-tenant-admin"
    ECOMMERCE           = "ecommerce"
    GENERAL_WEB         = "general-web"   # fallback

@dataclass
class Hole:
    id: str                 # "entity.player", "rule.win_condition"
    target_file: str        # path inside template, e.g. "src/game/entities.ts"
    marker: str             # the placeholder token in the file, e.g. "// HOLE:entity.player"
    kind: str               # "function" | "component" | "config" | "schema"
    signature: str | None   # expected signature/schema the model must satisfy
    description: str        # one line: what goes here
    depends_on: list[str] = field(default_factory=list)  # other hole ids

@dataclass
class Manifest:
    category: Category
    stack: dict             # {"runtime": "node", "render": "r3f", "net": "colyseus-relay"}
    entry: str              # entry file to build/run
    build_cmd: str
    run_cmd: str
    preview_url: str        # e.g. "http://localhost:5173"
    holes: list[Hole]       # ORDERED. Fill in this order.

@dataclass
class DoDItem:
    id: str                 # "menu.renders", "net.two_players_visible"
    description: str        # human readable requirement
    check: str              # name of a check in verify/checks.py
    args: dict              # args passed to the check
    severity: str = "required"   # "required" | "recommended"

@dataclass
class DefinitionOfDone:
    category: Category
    items: list[DoDItem]

@dataclass
class RegistryEntry:
    category: Category
    root: Path              # shamsu/templates/<category>/
    master_prompt: str      # loaded text of master_prompt.md
    manifest: Manifest
    dod: DefinitionOfDone
```

`ProjectSpec` in `shamsu/types.py` gains:

```python
category: Category
master_prompt: str          # resolved from the registry
manifest_path: Path
dod_path: Path
feature_requests: list[str] # PRD-derived features mapped onto holes during planning
```

---

## 4. Category detection

Two stages. Deterministic first (free, no model), LLM fallback only when the
deterministic result is ambiguous. Put in `shamsu/registry/detector.py`.

### Stage 1: deterministic signal scoring

In `categories.py`, define weighted keyword signals per category:

```python
SIGNALS = {
  Category.MULTIPLAYER_GAME: [
    ("multiplayer", 3), ("game", 2), ("3d", 2), ("player", 2), ("lobby", 3),
    ("real-time", 2), ("pvp", 3), ("co-op", 2), ("runner", 1), ("arena", 2),
  ],
  Category.ECOMMERCE: [
    ("shop", 3), ("cart", 3), ("checkout", 3), ("product", 2), ("catalog", 2),
    ("payment", 2), ("store", 2), ("inventory", 1),
  ],
  Category.MULTI_TENANT_ADMIN: [
    ("tenant", 3), ("multi-tenant", 3), ("rbac", 3), ("role", 2), ("admin", 2),
    ("dashboard", 1), ("organization", 2), ("permission", 2), ("saas", 2),
  ],
  Category.PORTFOLIO_SITE: [
    ("portfolio", 3), ("resume", 2), ("cv", 2), ("gallery", 2), ("showcase", 2),
    ("personal site", 3), ("about me", 2), ("projects page", 2),
  ],
  # GENERAL_WEB has no positive signals; it is the fallback.
}
```

Scoring:

```python
def score_categories(text: str) -> dict[Category, int]:
    text = text.lower()
    scores = {c: 0 for c in Category}
    for cat, signals in SIGNALS.items():
        for kw, w in signals:
            if kw in text:
                scores[cat] += w
    return scores
```

Decision rule:

```python
def deterministic_pick(scores) -> tuple[Category | None, float]:
    top = max(scores, key=scores.get)
    top_score = scores[top]
    second = sorted(scores.values(), reverse=True)[1]
    if top_score == 0:
        return None, 0.0                      # no signal -> go to LLM
    margin = top_score - second
    confidence = min(1.0, (top_score + margin) / 10)
    if top_score >= 5 and margin >= 2:
        return top, confidence                # confident, skip LLM
    return None, confidence                   # ambiguous -> LLM decides
```

### Stage 2: LLM fallback (only if Stage 1 returns None)

Use qwen3:8b with a hard-constrained JSON output. Keep the prompt tiny.

```text
SYSTEM: You classify a software project request into exactly one category.
Categories: multiplayer-game, portfolio-site, multi-tenant-admin, ecommerce, general-web.
Rules: pick the single best fit. If nothing fits well, pick general-web.
Output ONLY JSON: {"category": "<one>", "confidence": <0..1>, "why": "<8 words>"}

USER: <the PRD / prompt text>
```

Parse with the existing JSON-repair path in `manager.py`. Validate the category
is a real enum value; if not, fall back to `general-web`.

### Final resolution

```python
def detect_category(text: str) -> tuple[Category, float]:
    scores = score_categories(text)
    cat, conf = deterministic_pick(scores)
    if cat is not None:
        return cat, conf
    cat, conf = llm_classify(text)   # constrained JSON
    return cat, conf
```

Log the scores, the chosen category, and confidence. Detection mistakes are the
first thing to debug later, so make them visible.

---

## 5. Registry layout (per category)

Every `shamsu/templates/<category>/` directory contains:

```text
meta.yaml           # category, stack, build/run/preview commands
master_prompt.md    # the injected instruction block
manifest.yaml       # ordered holes (maps to Manifest/Hole)
dod.yaml            # required features + checks (maps to DefinitionOfDone/DoDItem)
template/           # the working scaffold, builds and runs as-is with placeholders
smoke/              # baseline smoke test + per-DoD check scripts
```

`loader.py` reads these into a `RegistryEntry`. The registry is just a dict:

```python
def load_registry_entry(category: Category) -> RegistryEntry:
    root = TEMPLATES_DIR / category.value
    return RegistryEntry(
        category=category,
        root=root,
        master_prompt=(root / "master_prompt.md").read_text(),
        manifest=parse_manifest(root / "manifest.yaml"),
        dod=parse_dod(root / "dod.yaml"),
    )
```

---

## 6. Master prompt: format and worked example

The master prompt is injected into the system context for both planning and
coding. It states the non-negotiable feature contract and the template rules.
Keep it under ~500 tokens so it fits the small context budget.

`shamsu/templates/multiplayer-game/master_prompt.md`:

```markdown
You are building a MULTIPLAYER 3D BROWSER GAME from a working template.

NON-NEGOTIABLE: a multiplayer game is not done until ALL of these exist and work:
- A main menu screen (start, join, settings).
- A lobby that lists connected players and a start control.
- A live network connection (relay room) that syncs player state.
- A game loop that updates and renders every frame.
- At least the local player plus remote players rendered as distinct objects.
- A HUD showing score/state.
- A win or lose (or end) condition.

The template ALREADY provides: the render loop, the relay room client/server,
the lobby wiring, and the menu shell. DO NOT rewrite these. DO NOT replace the
netcode. DO NOT collapse the game into a single object.

Your job is ONLY to fill the marked holes: entity definitions, per-entity update
logic, game rules, and the win condition, using the template's existing systems.

If the request is a "cube runner", the cube is ONE entity type. You still need
menu, lobby, connection, remote players, loop, HUD, and an end condition.
```

Note the explicit anti-pattern lines. They exist because the observed failure
was exactly "collapse the game into a single object." State the failure mode
you are preventing, directly.

---

## 7. Manifest: format and worked example

`shamsu/templates/multiplayer-game/manifest.yaml`:

```yaml
category: multiplayer-game
stack:
  runtime: node
  render: r3f            # react-three-fiber + three.js
  net: colyseus-relay    # relay/room sync, NOT authoritative
  physics: rapier        # optional, present in template
entry: src/main.tsx
build_cmd: "npm install && npm run build"
run_cmd: "npm run dev"
preview_url: "http://localhost:5173"
holes:
  - id: entity.player
    target_file: src/game/entities.ts
    marker: "// HOLE:entity.player"
    kind: schema
    signature: "type Player = { id: string; pos: Vec3; ... }"
    description: "Define the player entity fields the game needs."
    depends_on: []
  - id: entity.world
    target_file: src/game/entities.ts
    marker: "// HOLE:entity.world"
    kind: schema
    signature: "define obstacles/pickups for a cube runner"
    description: "Define non-player entities (obstacles, pickups)."
    depends_on: []
  - id: rule.update
    target_file: src/game/rules.ts
    marker: "// HOLE:rule.update"
    kind: function
    signature: "function update(state, dt): state"
    description: "Advance game state each tick: move players, spawn obstacles, collisions."
    depends_on: [entity.player, entity.world]
  - id: rule.win_condition
    target_file: src/game/rules.ts
    marker: "// HOLE:rule.win_condition"
    kind: function
    signature: "function checkEnd(state): 'win' | 'lose' | null"
    description: "Decide when the game ends and who wins."
    depends_on: [rule.update]
  - id: ui.hud_fields
    target_file: src/ui/Hud.tsx
    marker: "// HOLE:ui.hud_fields"
    kind: component
    signature: "render score/lives/time from state"
    description: "Bind the HUD to the game state fields."
    depends_on: [entity.player]
```

The holes are small and ordered by dependency. The model fills entities, rules,
and HUD bindings. It never touches menu, lobby, or netcode files, because those
are complete in the template and not listed as holes.

---

## 8. Definition of Done: format, worked example, enforcement

This is the anti-barebones gate. `shamsu/templates/multiplayer-game/dod.yaml`:

```yaml
category: multiplayer-game
items:
  - id: build.succeeds
    description: "Project builds with no errors."
    check: build_succeeds
    args: {}
  - id: menu.renders
    description: "Main menu screen renders on load."
    check: element_present
    args: { url: "http://localhost:5173", selector: "[data-testid=main-menu]" }
  - id: lobby.renders
    description: "Lobby renders and lists players after joining."
    check: element_present
    args: { url: "http://localhost:5173/lobby", selector: "[data-testid=player-list]" }
  - id: net.connects
    description: "Client opens a relay room connection."
    check: websocket_opens
    args: { url: "http://localhost:5173", timeout_ms: 5000 }
  - id: net.two_players_visible
    description: "With two headless clients joined, each sees two players."
    check: two_clients_see_two_players
    args: { url: "http://localhost:5173", players: 2 }
  - id: loop.runs
    description: "Game loop advances state over time (positions change)."
    check: state_advances
    args: { url: "http://localhost:5173", ticks: 30 }
  - id: hud.visible
    description: "HUD shows score/state during play."
    check: element_present
    args: { url: "http://localhost:5173", selector: "[data-testid=hud]" }
  - id: end.condition
    description: "An end condition can fire (win/lose reachable)."
    check: end_state_reachable
    args: { url: "http://localhost:5173" }
```

`shamsu/verify/checks.py` implements each check name as a function returning
`(passed: bool, detail: str)`. Most game checks use headless Playwright (already
available per the v2 loop). `two_clients_see_two_players` spins up two headless
browser contexts, joins both to a room, and asserts each DOM shows two player
objects. That single check is what would have caught the cube runner.

`shamsu/verify/dod.py` runs all items:

```python
def run_dod(entry: RegistryEntry, workspace: Path) -> list[tuple[DoDItem, bool, str]]:
    results = []
    for item in entry.dod.items:
        fn = CHECKS[item.check]
        passed, detail = fn(workspace, **item.args)
        results.append((item, passed, detail))
    return results

def dod_failures(results):
    return [(i, d) for (i, p, d) in results if not p and i.severity == "required"]
```

Enforcement in the pipeline: after generation and build, run the DoD. Every
required failure becomes a targeted fix task fed to the coder (still warm). Loop
is bounded (e.g. 3 rounds per failing item). If an item still fails after the
bound, stop and surface it clearly ("multiplayer game missing: lobby player
list") rather than declaring success. This is the exact behavior that was
missing before.

---

## 9. Pipeline integration

Edit `shamsu/agents/full_pipeline.py` to this order. Each numbered step is a
function call; keep model usage batched (all planning on qwen3, then swap once
to the coder, then DoD/fix loop stays on the coder).

```text
1. spec = parse_prd(prompt)                          [prd/extractor.py]
2. category, conf = detect_category(spec.text)       [registry/detector.py]
3. entry = load_registry_entry(category)             [registry/loader.py]
4. spec.category, spec.master_prompt = category, entry.master_prompt
5. scaffold_workspace(entry, workspace)              copy template/ in
6. baseline = run_smoke(entry, workspace)            template must pass BEFORE edits
   -> if baseline fails, the TEMPLATE is broken; abort with a clear message
7. plan = plan_holes(spec, entry.manifest)           [qwen3:8b + master_prompt]
   -> maps PRD feature_requests onto holes, sets order
--- swap to coder ---
8. for hole in entry.manifest.holes (in order):
     fill_hole(hole, workspace)                       [coder + master_prompt + hole slice]
       generate -> self_review (<=2) -> deterministic check
9. build(workspace)                                   [manifest.build_cmd]
10. results = run_dod(entry, workspace)               [verify/dod.py]
11. for (item, detail) in dod_failures(results):
      fix_for_dod(item, detail, workspace)            [coder, bounded retries]
      re-run that item's check
12. if unresolved required failures: STOP + report    do not fake success
--- swap back ---
13. review(workspace)                                 [qwen3:8b]
14. serve_preview(entry.manifest.preview_url)
```

`manager.py` change: when building the system context for steps 7, 8, and 11,
prepend `spec.master_prompt`. Keep it in a dedicated context slot so
`context/budget.py` always reserves room for it and never trims it away.

---

## 10. Per-hole generation loop

Inside `fill_hole` (used in steps 8 and 11), reuse the v2 per-hole cycle:

```text
1. build a tiny context: master_prompt + the target file slice around the marker
   + the hole signature/description + any depends_on outputs. Nothing else.
2. generate the hole body           [coder]
3. self-review once, fix own errors [coder]
4. self-review twice only if step 3 changed something; hard cap at 2
5. splice into the marker, run the narrowest deterministic check for that file
   (typecheck / lint / unit) 
6. on fail: feed error back, patch, re-check; bounded retries
7. detect same error twice -> stop, flag hole, move on (do not thrash)
```

Self-review is a filter. The deterministic check is the gate. Never let the
model's opinion end a hole.

---

## 11. Build order for Claude Code

Phase 1: machinery, no templates yet.

- Create `registry/schema.py`, `registry/categories.py`, `registry/detector.py`,
  `registry/loader.py`.
- Add fields to `types.py`.
- Unit-test the detector against a fixture set of prompts (see acceptance below).

Phase 2: verification layer.

- Create `verify/checks.py` with the check primitives (file_exists, route_exists,
  element_present, websocket_opens, state_advances, two_clients_see_two_players,
  end_state_reachable, build_succeeds).
- Create `verify/dod.py`.

Phase 3: first template end to end (multiplayer-game).

- Hand-build `templates/multiplayer-game/template/` as a WORKING R3F + relay game
  with menu, lobby, connection, loop, HUD, and marked holes. It must pass its own
  DoD with placeholder game logic before any generation.
- Write `meta.yaml`, `master_prompt.md`, `manifest.yaml`, `dod.yaml`, `smoke/`.

Phase 4: pipeline wiring.

- Edit `full_pipeline.py` to the step order in section 9.
- Edit `manager.py` to inject the master prompt.
- Edit `context/budget.py` to reserve the master-prompt slot.

Phase 5: the other four templates, same structure, in this order.

- general-web (simplest, proves the fallback), portfolio-site, ecommerce,
  multi-tenant-admin. Each needs its own template/, master_prompt, manifest, dod.

Phase 6: hardening.

- Add logging of category, confidence, DoD pass/fail per run.
- Add the bounded retry counters and the "stop and report" path.

---

## 12. Acceptance criteria

Detector (Phase 1): given a fixture set, classify correctly.

- "build a multiplayer 3d cube runner" -> multiplayer-game
- "my personal portfolio with a project gallery" -> portfolio-site
- "a store with a cart and stripe checkout" -> ecommerce
- "multi-tenant admin with roles and per-org data" -> multi-tenant-admin
- "a simple landing page for my bakery" -> general-web

DoD (Phase 2-3): the multiplayer-game template, with placeholder logic, passes
every required DoD item. Deliberately delete the lobby component and confirm the
DoD FAILS on `lobby.renders`. The gate must catch missing features.

End to end (Phase 4): re-run the original failing test, "build a multiplayer 3D
cube runner." The result must contain and pass: main menu, lobby with player
list, live relay connection, two visible players under two headless clients, a
running game loop, a HUD, and a reachable end condition. If any required DoD item
cannot be satisfied, Shamsu stops and reports exactly which feature is missing
instead of returning a lone cube.

That final test is the whole point: the same prompt that produced one cube must
now either produce a complete game or tell you precisely what it could not
finish.
