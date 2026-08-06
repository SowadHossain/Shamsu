"""Optional LLMLingua-style prose compression - disabled by default.

Per the diagnostics prompt: LLMLingua may only compress long natural-language
prose (old session summaries, noisy non-code narration), never exact code,
diffs, error codes, file paths, line numbers, or command exit codes. Those
exact fields must never be routed through this module.

`llmlingua` itself depends on `transformers`/`torch`, which conflicts with
SHAMSU's offline/low-RAM footprint, so it is never auto-installed and stays
off unless a user opts in.
"""
from __future__ import annotations

import os


def is_enabled(config: dict | None = None) -> bool:
    if config and config.get("enable_llmlingua"):
        return True
    return os.environ.get("SHAMSU_DIAGNOSTICS_LLMLINGUA", "").strip() == "1"


def is_available() -> bool:
    try:
        import llmlingua  # noqa: F401
    except ImportError:
        return False
    return True


def maybe_compress_prose(text: str, config: dict | None = None) -> tuple[str, bool]:
    """Returns (possibly-compressed text, whether compression was applied).

    Never call this with exact diagnostics/code/error text - callers are
    responsible for only routing free-form prose through it.
    """
    if not is_enabled(config) or not is_available():
        return text, False
    from llmlingua import PromptCompressor  # type: ignore

    compressor = PromptCompressor()
    result = compressor.compress_prompt(text)
    return result.get("compressed_prompt", text), True
