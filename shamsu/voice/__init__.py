"""Voice input and local CLI speech output for SHAMSU."""
from __future__ import annotations

from shamsu.voice.models import Transcript, VoiceError
from shamsu.voice.service import VoiceService
from shamsu.voice.speech import SpeechPlayer, SpeechSettings, prepare_spoken_text

__all__ = [
    "SpeechPlayer",
    "SpeechSettings",
    "Transcript",
    "VoiceError",
    "VoiceService",
    "prepare_spoken_text",
]
