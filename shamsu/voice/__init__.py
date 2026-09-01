"""Voice input, and the engines that speak a reply back."""
from __future__ import annotations

from shamsu.voice.engines import (
    AUTO_ORDER,
    SpeechEngine,
    build_engine,
    register_engine,
    registered_engines,
)
from shamsu.voice.models import Transcript, VoiceError
from shamsu.voice.service import VoiceService
from shamsu.voice.speech import (
    SpeechPlayer,
    SpeechSettings,
    SystemSpeechEngine,
    prepare_spoken_text,
    reply_should_be_spoken,
)

__all__ = [
    "AUTO_ORDER",
    "SpeechEngine",
    "SpeechPlayer",
    "SpeechSettings",
    "SystemSpeechEngine",
    "Transcript",
    "VoiceError",
    "VoiceService",
    "build_engine",
    "prepare_spoken_text",
    "register_engine",
    "registered_engines",
    "reply_should_be_spoken",
]
