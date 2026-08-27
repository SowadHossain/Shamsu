"""Small data contracts for voice input."""
from __future__ import annotations

from dataclasses import dataclass


class VoiceError(RuntimeError):
    """Raised when SHAMSU cannot record, convert, or transcribe voice input."""


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str = "en"
    duration_seconds: float = 0.0
