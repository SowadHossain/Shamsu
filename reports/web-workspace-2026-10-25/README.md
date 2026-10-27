# Web workspace implementation - 2026-10-25

The web service now owns its coding worker; running the interactive CLI is unnecessary. Accepted tasks continue when a browser tab closes. Projects, settings, task queue and history persist on disk. Queued work resumes after startup; interrupted active work is marked interrupted rather than replayed.

## Interface

Hallmark Rim night direction, informed by UI UX Pro Max: project navigation, conversation, fixed task composer, run inspector, approval previews, model readiness/download, project creation and settings. Mobile navigation and run details use explicit toggles. Drafts survive navigation; stale conversation responses are ignored; optimistic prompts reconcile with streamed history. Outputs support safe formatted text, code and file diffs.

[Desktop screenshot](desktop.png) | [Mobile screenshot](mobile.png)

## Deployment

See [DEPLOYMENT.md](../../DEPLOYMENT.md). Docker Compose includes a non-root web container, private Ollama, model setup and persistent volumes. Python, Node.js 22, npm, Git, build tools and Chromium are included. GPU and host-project overrides are provided. Access tokens, host validation and mounted-root project boundaries are enforced.

## Verification

- Agent regressions: **606 passed** (simple chat, context budget, shell hazards).
- Final UI/browser/conversation checks: **78 passed**.
- Service/policy/settings checks: **36 passed**.
- Added partial-stream-record regression: **1 passed**.
- Targeted fatal/static checks and JavaScript syntax: passed.
- Rendered Hallmark checks: **0 FAIL, 0 REFLEX, 3 WARN**. Consistent control recipes are intentional; mobile secondary panels remain accessible through toggles.
- Separate-process browser tests use a scripted Ollama endpoint. They prove task submission, real file creation after closing the tab, restored history, single prompt display, stale-response handling and approval previews. They do not establish real-model coding quality.

## Docker verification limit

The Docker app image built successfully, and the GPU Ollama container reached healthy state. During the final incremental build and initial model download, Docker storage on C: exhausted available space (about 140-180 MB remained). The daemon stopped responding. Its test runtime was stopped to halt writes. The latest source refinements have therefore NOT been verified in a final running container, and the real-model task/restart smoke test remains outstanding.

Free at least 8-10 GB on the Docker storage drive, or relocate Docker storage, then restart Docker Desktop and run the documented GPU Compose command. Models and application state use persistent volumes. Do not use `down --volumes` to recover space unless you intend to delete those data.

## Remaining scope

This delivery addresses web autonomy, deployment, UI and several policy/streaming defects. It does not resolve every item in the earlier [harness audit](../harness-audit-2026-10-25/README.md). Long-term memory quality, token estimation, capability claims and broad real-model task evaluations remain follow-up work. These are targeted passing suites, not a claim that the entire repository suite passes.
