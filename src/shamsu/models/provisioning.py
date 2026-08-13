"""Getting Ollama ready: start the server, fetch the model.

`OllamaClient` deliberately never contacts a server when it is constructed, and
reports an unreachable one honestly when it is used. That is the right
behaviour for a *client*, and it leaves the user holding two chores the machine
can do itself: `ollama serve`, and `ollama pull <model>`.

This module does them, under three rules.

**Nothing happens silently.** Starting a background server and downloading
several gigabytes are both things a person would want to know about while they
are happening, not afterwards. Every step reports through `on_progress`, and
the caller decides how to show it.

**Everything is bounded.** Server startup polls to a deadline. The pull uses a
per-chunk read timeout rather than a total one, so a large download is allowed
to take as long as it takes while a *stalled* one still fails — the same
distinction `OllamaClient` makes between a slow generation and a dead socket.

**A missing Ollama is not fixable here.** If the executable is not installed,
this says so and stops. Downloading and running an inference server that the
user never installed is not a chore, it is a decision, and it is theirs.

Provisioning is only ever invoked from the CLI entry point. Nothing in the
runtime, the tests, or the library path reaches for the network on its own.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass

import httpx

from shamsu.models.ollama import DEFAULT_HOST, list_models

#: How long to wait for a freshly started server to answer.
START_TIMEOUT_SECONDS = 30.0

#: Gap between readiness checks while waiting for startup.
POLL_SECONDS = 0.4

#: How long a pull may go without producing *any* output before it is treated
#: as stalled. Not a total budget: a multi-gigabyte download over a slow link
#: is legitimate, and a total cap would fail exactly the case that needs
#: patience most.
PULL_STALL_SECONDS = 180.0


@dataclass(frozen=True)
class Progress:
    """One observable step of getting ready."""

    stage: str
    message: str

    #: 0.0–1.0 when the step can report completion, else None.
    fraction: float | None = None

    def render(self) -> str:
        if self.fraction is None:
            return f"{self.stage}: {self.message}"
        return f"{self.stage}: {self.message} {self.fraction * 100:.0f}%"


#: Told about each step. Returning nothing; this is display, not control.
ProgressCallback = Callable[[Progress], None]


class NotProvisionable(Exception):
    """The situation cannot be fixed automatically, and why."""


def _noop(progress: Progress) -> None:
    return None


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


def server_is_up(host: str = DEFAULT_HOST, *, timeout: float = 3.0) -> bool:
    """Whether something is answering the Ollama API at `host`."""
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{host.rstrip('/')}/api/tags")
    except httpx.HTTPError:
        return False
    return response.status_code == httpx.codes.OK


def ollama_executable() -> str | None:
    """The `ollama` binary, or None when it is not installed."""
    return shutil.which("ollama")


def start_server(
    host: str = DEFAULT_HOST,
    *,
    timeout: float = START_TIMEOUT_SECONDS,
    on_progress: ProgressCallback = _noop,
) -> None:
    """Start `ollama serve` in the background and wait for it to answer.

    The child is fully detached: it must outlive this process, because the
    whole point is that the *next* command finds a server already running. A
    server that died with the run that started it would make every invocation
    pay the startup cost.

    Raises:
        NotProvisionable: Ollama is not installed, or did not come up in time.
    """
    if server_is_up(host):
        return

    executable = ollama_executable()
    if executable is None:
        raise NotProvisionable(
            "Ollama is not installed, or not on PATH. Install it from "
            "https://ollama.com/download, then run this again."
        )

    on_progress(Progress("ollama", "starting the server"))

    try:
        _spawn_detached([executable, "serve"])
    except OSError as exc:
        raise NotProvisionable(f"could not start Ollama: {exc}") from exc

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if server_is_up(host):
            on_progress(Progress("ollama", "server is up"))
            return
        time.sleep(POLL_SECONDS)

    raise NotProvisionable(
        f"started Ollama but it did not answer at {host} within {timeout:.0f}s. "
        "Try running `ollama serve` yourself to see why."
    )


#: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP. The first keeps the server off
#: this console, the second stops our Ctrl-C from reaching it — otherwise
#: interrupting a run would also kill the server it just started.
_WINDOWS_DETACHED = 0x00000008 | 0x00000200


def _spawn_detached(argv: list[str]) -> None:
    """Start a process that outlives this one, with its streams discarded.

    Branching on `os.name` rather than `sys.platform` is deliberate: mypy
    narrows `sys.platform` to the checked platform, so with `warn_unreachable`
    one of the two branches becomes an error on every machine. `os.name` is
    opaque to it and both branches stay checked.
    """
    if os.name == "nt":
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=_WINDOWS_DETACHED,
        )
        return

    subprocess.Popen(  # noqa: S603 - fixed argv, no shell, no user input
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


def has_model(model: str, host: str = DEFAULT_HOST) -> bool:
    """Whether `model` is already pulled.

    An untagged name means `:latest` to Ollama, so `qwen2.5-coder` matches a
    pulled `qwen2.5-coder:latest`. Comparing raw strings would report a model
    as missing and pull a second copy of it.
    """
    pulled = set(list_models(host))
    if model in pulled:
        return True
    return f"{model}:latest" in pulled if ":" not in model else False


def pull_model(
    model: str,
    host: str = DEFAULT_HOST,
    *,
    on_progress: ProgressCallback = _noop,
    stall_timeout: float = PULL_STALL_SECONDS,
) -> None:
    """Download `model`, reporting progress as it goes.

    Raises:
        NotProvisionable: the server refused the pull, or it stalled.
    """
    on_progress(Progress("pull", f"downloading {model} (this can take a while)"))

    try:
        for update in _pull_events(model, host, stall_timeout):
            # Checked first, and before the `status` guard. Ollama reports a bad
            # model name as a bare `{"error": ...}` with no `status` field, so
            # skipping statusless events first swallowed the only explanation
            # there was — and the pull then failed with the vague fallback
            # below instead of "file does not exist".
            if error := update.get("error"):
                raise NotProvisionable(f"could not pull {model}: {error}")

            status = str(update.get("status", "")).strip()
            if not status:
                continue

            total = update.get("total")
            done = update.get("completed")
            fraction = (
                done / total
                if isinstance(total, int) and isinstance(done, int) and total > 0
                else None
            )
            on_progress(Progress("pull", status, fraction))
    except httpx.HTTPError as exc:
        raise NotProvisionable(f"could not pull {model}: {exc}") from exc

    if not has_model(model, host):
        raise NotProvisionable(
            f"the pull of {model} reported no error but the model is still not "
            "available; check `ollama list`"
        )
    on_progress(Progress("pull", f"{model} is ready"))


def _pull_events(model: str, host: str, stall_timeout: float) -> Iterator[Mapping[str, object]]:
    """Stream `/api/pull`, one decoded JSON object per line."""
    timeout = httpx.Timeout(connect=10.0, read=stall_timeout, write=10.0, pool=10.0)
    with (
        httpx.Client(timeout=timeout) as client,
        client.stream(
            "POST",
            f"{host.rstrip('/')}/api/pull",
            json={"model": model, "stream": True},
        ) as response,
    ):
        if response.status_code != httpx.codes.OK:
            response.read()
            raise NotProvisionable(
                f"could not pull {model}: Ollama returned HTTP {response.status_code}"
            )
        for line in response.iter_lines():
            if not line.strip():
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:  # pragma: no cover - defensive
                continue
            if isinstance(decoded, dict):
                yield decoded


# ---------------------------------------------------------------------------
# Both, in order
# ---------------------------------------------------------------------------


def ensure_ready(
    model: str,
    host: str = DEFAULT_HOST,
    *,
    start: bool = True,
    pull: bool = True,
    on_progress: ProgressCallback = _noop,
) -> None:
    """Make `model` usable at `host`, doing as little as possible.

    A no-op when the server is already up and the model already pulled, which
    is the common case and must stay free — this runs before every invocation.

    `start` and `pull` are separate because refusing them has different
    meanings: a CI job may have a server it did not start and must not download
    into, and both refusals should fail with the actual situation named rather
    than with a generic unavailability later on.

    Raises:
        NotProvisionable: something is missing that this cannot or may not fix.
    """
    if not server_is_up(host):
        if not start:
            raise NotProvisionable(
                f"nothing is answering at {host} and starting it is disabled. "
                "Run `ollama serve`, or drop --offline."
            )
        start_server(host, on_progress=on_progress)

    if has_model(model, host):
        return

    if not pull:
        available = ", ".join(list_models(host)) or "none"
        raise NotProvisionable(
            f"{model} is not pulled and downloading it is disabled. "
            f"Available: {available}. Run `ollama pull {model}`, or drop --offline."
        )

    pull_model(model, host, on_progress=on_progress)


__all__ = [
    "POLL_SECONDS",
    "PULL_STALL_SECONDS",
    "START_TIMEOUT_SECONDS",
    "NotProvisionable",
    "Progress",
    "ProgressCallback",
    "ensure_ready",
    "has_model",
    "ollama_executable",
    "pull_model",
    "server_is_up",
    "start_server",
]
