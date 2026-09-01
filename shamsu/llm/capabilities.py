"""What the SERVER says about a model, rather than what a table remembers.

`context/budget.MODEL_CONTEXT_WINDOWS` is thirty hand-written entries, and a
hand-written entry is wrong the moment a model ships a new revision. Measured
2026-08-30 against the local server: `qwen3:8b` was listed at 32,768 and really
holds 40,960, and `gemma3:4b` reports no tool-calling support at all - which the
table has no way to express, so a model that cannot call a tool was being sent
thirty-seven tool schemas.

Ollama already answers both questions on `/api/tags`, for free, in one call.

**Never on the hot path.** `ctx_window_for_model` runs inside `_ceiling()`, which
runs every round; a network call there would be the mistake
`_installed_model_completion_names` documents ("~2.1 s, on the event loop
thread, on a keystroke"). So this is a CACHE that answers instantly and refreshes
itself in the background, and a cache miss answers "I don't know" rather than
waiting. The table stays as the offline answer, which is the right one on a
machine with no server running.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

__all__ = ["ModelFacts", "clear_cache", "model_facts", "refresh_model_facts"]

#: How long a snapshot is trusted. Models do not change under a running server,
#: so this only has to be short enough that pulling one mid-session is noticed.
CACHE_TTL_SECONDS = 300.0

#: Deliberately short. This runs off the hot path, but a hung server must not
#: keep a background thread alive for a minute either.
PROBE_TIMEOUT_SECONDS = 2.0

_LOCK = threading.Lock()
_CACHE: dict[str, ModelFacts] = {}
_STAMP = 0.0
_REFRESHING = False


@dataclass(frozen=True)
class ModelFacts:
    """What the server knows about one installed model."""

    name: str
    #: Tokens the model itself can hold. 0 when the server did not say - some
    #: models report no `context_length`, and inventing one would be worse than
    #: falling back to the table.
    context_length: int = 0
    #: Whether it can be sent a `tools=` array at all. A model without this
    #: needs the prompt-side tool protocol and the output salvager instead.
    supports_tools: bool = True


def _probe(base_url: str) -> dict[str, ModelFacts]:
    """One `/api/tags` call. Returns `{}` for any failure at all."""
    url = base_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return {}
    facts: dict[str, ModelFacts] = {}
    for entry in payload.get("models") or []:
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        details = entry.get("details") or {}
        try:
            window = int(details.get("context_length") or 0)
        except (TypeError, ValueError):
            window = 0
        capabilities = entry.get("capabilities") or []
        facts[name] = ModelFacts(
            name=name,
            context_length=max(0, window),
            # Absent capabilities means an older server that does not report
            # them, not a model that cannot call tools. Assume it can: the
            # salvager backs up a model that turns out not to, while wrongly
            # withholding the schemas costs every tool call in the session.
            supports_tools="tools" in capabilities if capabilities else True,
        )
    return facts


def refresh_model_facts(base_url: str | None = None) -> dict[str, ModelFacts]:
    """Re-probe now, on THIS thread. Returns whatever is then known."""
    global _CACHE, _STAMP, _REFRESHING
    if base_url is None:
        from shamsu.llm.manager import OLLAMA_BASE_URL

        base_url = OLLAMA_BASE_URL
    probed = _probe(base_url)
    with _LOCK:
        # Keep the last good answer when the server is down: a dead server is a
        # reason to stop asking, not a reason to forget what it said.
        if probed:
            _CACHE = probed
        _STAMP = time.monotonic()
        _REFRESHING = False
    return dict(_CACHE)


def _refresh_in_background() -> None:
    global _REFRESHING
    with _LOCK:
        if _REFRESHING:
            return
        _REFRESHING = True
    thread = threading.Thread(
        target=refresh_model_facts, name="shamsu-model-facts", daemon=True
    )
    thread.start()


def model_facts(model_name: str) -> ModelFacts | None:
    """What the server says about *model_name*, or ``None`` if it has not said.

    Answers from cache immediately and schedules a refresh when the snapshot is
    stale. `None` is a real answer - "ask the table" - not an error.
    """
    name = (model_name or "").strip()
    if not name:
        return None
    with _LOCK:
        cached = _CACHE.get(name)
        stale = (time.monotonic() - _STAMP) > CACHE_TTL_SECONDS
        empty = not _CACHE
    if stale or empty:
        _refresh_in_background()
    return cached


#: A model is "spilled" once this much of it is outside VRAM. 256MB rather than
#: any-at-all: a small overhang is normal and costs little, while a gigabyte in
#: system RAM is the difference between a 20-second call and a nine-minute one.
SPILL_BYTES = 256 * 1024 * 1024


def loaded_model_spill(base_url: str | None = None) -> dict[str, int]:
    """`{model: bytes outside VRAM}` for anything Ollama has resident.

    The failure this exists for is SILENT, which is why nothing caught it for
    months. Ollama does not error when a model does not fit - it loads what it
    can onto the card and runs the rest from system RAM, and every token then
    crosses that boundary. `looks_like_out_of_memory` only ever sees the HARD
    refusal; there is no error here to see.

    Measured 2026-08-31 on an 8GB card with the desktop running Chrome, Edge,
    VS Code, OBS, Word and Telegram: 7,632 MiB of 8,188 in use, `qwen3.5:9b`
    resident at 6.8GB with 5.5GB on the GPU, and one model call that took
    **536 seconds**. SHAMSU reported nothing at all; from outside it looked
    like a hang.

    A live call every time, deliberately not cached: what else is on the card
    changes while a session runs, which is the whole point. `/api/ps` is local
    and answers in milliseconds.
    """
    if base_url is None:
        from shamsu.llm.manager import OLLAMA_BASE_URL

        base_url = OLLAMA_BASE_URL
    url = base_url.rstrip("/") + "/api/ps"
    try:
        with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return {}
    spilled: dict[str, int] = {}
    for entry in payload.get("models") or []:
        name = str(entry.get("name") or "").strip()
        try:
            total = int(entry.get("size") or 0)
            on_gpu = int(entry.get("size_vram") or 0)
        except (TypeError, ValueError):
            continue
        # `size_vram` of 0 means a CPU-only load, which is a deliberate choice
        # on a machine with no GPU rather than a spill to report every turn.
        if name and on_gpu and total - on_gpu >= SPILL_BYTES:
            spilled[name] = total - on_gpu
    return spilled


def clear_cache() -> None:
    """Forget the snapshot. For tests, and for `/models` after a pull."""
    global _CACHE, _STAMP
    with _LOCK:
        _CACHE = {}
        _STAMP = 0.0
