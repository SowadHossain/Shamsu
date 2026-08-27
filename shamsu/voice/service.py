"""High-level Whisper voice service."""
from __future__ import annotations

import tempfile
from pathlib import Path

from shamsu.voice.audio import normalize_for_whisper
from shamsu.voice.models import Transcript, VoiceError
from shamsu.voice.whisper import WhisperTranscriber


class VoiceService:
    def __init__(self, transcriber: WhisperTranscriber | None = None) -> None:
        self.transcriber = transcriber or WhisperTranscriber()

    def transcribe_file(self, audio_path: Path) -> Transcript:
        source = Path(audio_path)
        if not source.exists():
            raise VoiceError(f"Voice input file does not exist: {source}")
        if source.stat().st_size <= 0:
            raise VoiceError("Voice input was empty.")
        with tempfile.TemporaryDirectory(prefix="shamsu-voice-") as directory:
            wav_path = Path(directory) / "input.wav"
            normalize_for_whisper(source, wav_path)
            transcript = self.transcriber.transcribe(wav_path)
        if not transcript.text.strip():
            raise VoiceError("Whisper did not hear any English speech.")
        return transcript
