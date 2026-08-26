# SHAMSU Small Harness

SHAMSU is now a small, local-first coding harness inspired by
`reference/smallcode`.

The primary local surface is the framed TUI. Web and Telegram remain supported
as remote surfaces, but they route into the same small `SimpleChatLoop` instead
of the removed legacy orchestration loop.

## Run

```powershell
.\scripts\run-shamsu.ps1
```

```bash
./scripts/run-shamsu.sh
```

Useful direct commands:

```bash
python -m shamsu.cli.app
python -m shamsu.cli.app run --prompt "inspect this project"
python -m shamsu.cli.app web --port 8765
```

## Current Structure

- `shamsu/cli/app.py` owns the TUI-first application entrypoint.
- `shamsu/cli/repl.py` is only a compatibility shim to the new app.
- `shamsu/agents/simple_chat.py` is the active model/tool loop.
- `shamsu/agents/simple_state.py` owns chat message state.
- `shamsu/tools/` contains the workspace, command, patch, web, and search tools.
- `shamsu/webui/` serves the retained browser surface.
- `shamsu/integrations/telegram/` serves the retained Telegram surface.
- `reference/smallcode/` remains the upstream reference for small-harness ideas.

## Removed From This Branch

The old REPL implementation, agent orchestrator, PRD-to-project generator,
Django templates, taskmaster adapter, diagnostics package, and broad evaluation
fixtures have been removed from the active project surface.

## Development Checks

```bash
python -m compileall -q shamsu
pytest
```
