"""Append-only transcript build route.

The PRD orchestrator rebuilds a fresh two-message state frame on every model call
(``shamsu/context/compiler.py``), so the model never sees a word it previously
wrote and Ollama can never reuse a cached prefix. This package is the opposite
bet: keep the conversation, append to it, and let the model continue its own text
the way it does in a plain chat session.

``session.py`` holds the cache-safe transcript, ``build.py`` the loop that drives
it, ``run.py`` a standalone entry point.
"""
from shamsu.transcript.build import BuildReport, SliceOutcome, TranscriptBuilder
from shamsu.transcript.session import Message, Transcript

__all__ = [
    "BuildReport",
    "Message",
    "SliceOutcome",
    "Transcript",
    "TranscriptBuilder",
]
