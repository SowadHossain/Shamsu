# Shamsu PRD Test Feedback: Foodpanda-Like App PRD

## PRD Used

File: `FOODPANDA_LIKE_APP_PRD.md`

Project title: `BiteFleet`

Concept: A Foodpanda-like local-first food delivery marketplace with customer ordering, restaurant portal, rider dispatch, admin dashboard, cart, checkout, promo codes, mock payments, refunds, reviews, support tickets, notifications, persistence, CLI automation, and tests.

## Test Environment

- Shamsu directory: `C:\Users\Mastu\Desktop\Shamsu`
- Workspace shown by Shamsu: `C:\Users\Mastu\Desktop\Shamsu`
- Runtime: local Ollama
- Prompt surface: interactive `shamsu>` REPL

## What Worked

Shamsu parsed the PRD and saved project plan metadata.

The `/plan-prd FOODPANDA_LIKE_APP_PRD.md` output showed:

```text
Project: product_requirements_document_bite_fleet
App: fleet
Theme: corporate
Status: ready
Entities: 24
Endpoints: 120
Pages: 73
Files planned: 2
Extraction confidence: 100%
```

Positive signal:

- Shamsu found the PRD.
- Shamsu extracted 24 entities.
- Shamsu extracted 120 endpoints.
- Shamsu extracted 73 pages.
- Extraction confidence was 100%.

Concern:

- `Files planned: 2` is too shallow for a Foodpanda-like marketplace app. The PRD requires customer, restaurant, rider, and admin experiences plus order lifecycle, dispatch, payments, support, persistence, CLI, and tests.

## Approval Step

Shamsu requested approval to write:

```text
.shamsu/generation-state.json
```

Reason shown:

```text
M3 only stores resume metadata; it does not generate project files.
```

This part looked reasonable. It saved the plan metadata after approval.

## Failure: Entity-Listing Prompt Routed Into Build Flow

After `/plan-prd`, the user asked for a read-only entity list:

```text
Using the last parsed PRD, list only the 24 entities you detected. Do not build, do not generate, do not write files.
```

Observed result:

```text
I found multiple PRD files - which one should I build from?
```

Shamsu listed multiple PRD files:

```text
- agent context/PHASE2_PRD_BENCHMARK_ATLAS_SMOKE.md
- agent context/PHASE2_PRD_BENCHMARK_MULTI.md
- agent context/PHASE2_PRD_BENCHMARK_SMOKE.md
- agent context/REQUIREMENTS.md
- Agent_Arena_PRD.md
- FOODPANDA_LIKE_APP_PRD.md
- SHAMSU_CANVA_LIKE_APP_PRD.md
- SHAMSU_CANVA_PRD_FEEDBACK.md
```

Issue:

The request was explicitly read-only and referred to the last parsed PRD, but Shamsu routed it as a PRD build request and asked which PRD to build from.

The user then clarified:

```text
FOODPANDA_LIKE_APP_PRD.md

Do not build. Do not generate. Do not write files.
Only list the entities from this PRD in plain text.
```

Observed result:

```text
Pipeline Error
```

The same build/pipeline routing problem happened again.

## Main Issue

Shamsu is over-routing PRD follow-up prompts into build/generation mode.

The route classifier appears to prioritize PRD build behavior when a prompt mentions:

- a PRD file
- "last parsed PRD"
- entities
- detected entities
- implementation or milestone wording

even when the prompt includes explicit negative instructions:

- `Do not build`
- `Do not generate`
- `Do not write files`
- `Only list`
- `plain text`

## Expected Behavior

For the prompt:

```text
Using the last parsed PRD, list only the 24 entities you detected. Do not build, do not generate, do not write files.
```

Shamsu should have answered:

```text
1. Customer
2. RestaurantUser
3. Rider
4. AdminUser
5. Address
6. DeliveryZone
7. Restaurant
8. RestaurantHours
9. MenuCategory
10. MenuItem
11. ItemModifierGroup
12. ItemModifierOption
13. Cart
14. CartItem
15. Order
16. OrderItem
17. OrderStatusEvent
18. PromoCode
19. PaymentTransaction
20. Refund
21. Review
22. SupportTicket
23. Notification
24. Asset
```

No build pipeline should run.

## Suggested Fixes

1. Add read-only PRD inspection commands:

```text
/prd summary <file>
/prd entities <file>
/prd pages <file>
/prd endpoints <file>
/prd last
```

2. Treat explicit negative generation instructions as hard blockers.

If the user says:

```text
do not build
do not generate
do not write files
plain text only
only list
```

then Shamsu must not enter build mode.

3. Preserve and reference the last parsed PRD.

If the user says `last parsed PRD`, Shamsu should use the cached metadata from the latest `/parse-prd` or `/plan-prd` command instead of scanning all PRD files and asking which one to build from.

4. Distinguish these intents:

- parse PRD
- plan PRD
- summarize PRD
- inspect entities
- inspect pages
- build/generate from PRD

5. Add a regression test:

```text
After `/plan-prd FOODPANDA_LIKE_APP_PRD.md`, prompt:
"Using the last parsed PRD, list only the 24 entities you detected. Do not build, do not generate, do not write files."

Expected route: read-only PRD inspection
Forbidden route: PRD build pipeline
```

## Reproduction Steps

From PowerShell:

```powershell
cd "C:\Users\Mastu\Desktop\Shamsu"
.\scripts\run-shamsu.ps1
```

Inside Shamsu:

```text
/parse-prd FOODPANDA_LIKE_APP_PRD.md
/plan-prd FOODPANDA_LIKE_APP_PRD.md
```

Approve metadata write.

Then run:

```text
Using the last parsed PRD, list only the 24 entities you detected. Do not build, do not generate, do not write files.
```

Observed:

```text
I found multiple PRD files - which one should I build from?
```

Then run:

```text
FOODPANDA_LIKE_APP_PRD.md

Do not build. Do not generate. Do not write files.
Only list the entities from this PRD in plain text.
```

Observed:

```text
Pipeline Error
```

## Severity

Medium to high for PRD workflows.

Reason:

This makes it difficult to inspect or validate PRD extraction quality without accidentally triggering build flows. It also creates user confusion because the user explicitly requests no file writes, but Shamsu still enters build/pipeline behavior.

