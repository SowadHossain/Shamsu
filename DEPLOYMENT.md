# Standalone Shamsu workspace

The web service owns its coding worker. No interactive CLI or open browser tab
is required for accepted tasks to keep running. The service and model runtime
must remain running; Docker handles their background lifecycle.

## Docker

From this repository:

    docker compose up -d --build
    docker compose logs web

Open the access link printed by the web service. The token is stored in the
persistent state volume and survives restarts. You can also set a stable
SHAMSU_WEB_AUTH_TOKEN in .env and enter it at http://localhost:8765.

The default stack includes:

- The web application and its own queue dispatcher.
- A private Ollama service; its port is not published to the host.
- Initial download of qwen2.5-coder:3b-instruct, configurable in .env.
- Python, Node.js 22, npm, Git, a compiler toolchain, and Chromium for coding
  and browser inspection.
- Separate persistent volumes for projects, conversation/settings state,
  and model weights.

The interface starts while the initial model downloads. Check Run details for
model readiness. Settings can download the configured model as well.

In the browser, use **Add a project**, enter **/workspace/my-project**, and
select **Create this folder**. Then start a conversation and submit a task.

To use existing files on the host, set SHAMSU_PROJECTS_DIR in .env and run:

    docker compose -f compose.yaml -f compose.bind.yaml up -d --build

The folder must exist and be writable by container uid 1000 on Linux. Container
paths are /workspace/...; host paths are not available unless mounted. The
default named project volume avoids host-folder ownership problems.

For supported NVIDIA installations:

    docker compose -f compose.yaml -f compose.gpu.yaml up -d --build

To combine host files and GPU support, pass all three Compose files. The default
CPU configuration works without a GPU; inference speed depends on hardware.

Useful commands:

    docker compose ps
    docker compose logs -f web
    docker compose logs -f model-setup
    docker compose stop
    docker compose start
    docker compose down

Stopping or removing containers retains the volumes. Using down --volumes
deletes persistent projects, state, and models; it is not needed for updates.

To export a project to the host:

    docker compose cp web:/workspace/my-project ./my-project

## Native service

With the repository environment installed:

    python -m shamsu.webui --workspace /path/to/project

Optional service configuration:

- SHAMSU_HOME: persistent settings, registry, control database and access token.
- SHAMSU_WORKSPACE_ROOT: optional boundary for browser project creation/access.
- SHAMSU_OLLAMA_BASE_URL: explicit Ollama service origin, default localhost:11434.
- SHAMSU_MODEL: model used for coding.
- SHAMSU_WEB_HOST / SHAMSU_WEB_PORT: bind address and port.
- SHAMSU_WEB_AUTH_TOKEN: optional stable credential; network binds require one.
- SHAMSU_WEB_ALLOWED_HOSTS: allowed browser hostnames, comma separated. Defaults
  to localhost, 127.0.0.1 and ::1. Add the hostname when using a reverse proxy.

The entry point does not import or start the interactive REPL. For a native
background deployment, run it under your operating system's service manager.

## Task lifecycle

Closing a tab disconnects live updates without cancelling the task. Reopening
the conversation restores persisted history and reconnects to the event stream.
One model task runs at a time; waiting conversations are dispatched automatically.
Queued tasks survive service restarts. Work interrupted during execution is
reported as interrupted instead of automatically replaying possibly applied edits.

Commands requiring approval wait for an answer in the browser. Approval cards
include the command/file preview and project. Ordinary workspace edits can run
without a terminal prompt. Explicit read-only constraints and headless deny/dry-run
policies are enforced separately from this convenience behavior.

The web service runs as a non-root user. Only the loopback host port is published
by default, access is token-gated in Docker, and project creation stays under
the mounted project root. No Docker socket is mounted into the agent.

## Verification

The service tests exercise queue recovery, cross-session serialization, host
validation, project boundaries and endpoint configuration. Browser integration
tests start a separate web process and a scripted Ollama endpoint, submit a task,
close the tab during inference, and verify file creation and restored history.
These tests establish the harness path independently of stochastic model quality.
