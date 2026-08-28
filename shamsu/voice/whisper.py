"""Whisper transcription via faster-whisper.

Only Whisper is supported for now. Keep this module narrow until SHAMSU has a
second speech-to-text backend worth paying the abstraction cost for.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from shamsu.voice.models import Transcript, VoiceError

DEFAULT_WHISPER_MODEL = "base.en"
DEFAULT_WHISPER_DEVICE = "cpu"
DEFAULT_WHISPER_COMPUTE_TYPE = "int8"


class WhisperTranscriber:
    def __init__(
        self,
        *,
        model_name: str | None = None,
        device: str | None = None,
        compute_type: str | None = None,
    ) -> None:
        self.model_name = model_name or os.environ.get("SHAMSU_WHISPER_MODEL", "").strip()
        self.model_name = self.model_name or DEFAULT_WHISPER_MODEL
        self.device = (
            device
            or os.environ.get("SHAMSU_WHISPER_DEVICE", "").strip()
            or DEFAULT_WHISPER_DEVICE
        )
        self.compute_type = (
            compute_type
            or os.environ.get("SHAMSU_WHISPER_COMPUTE_TYPE", "").strip()
            or DEFAULT_WHISPER_COMPUTE_TYPE
        )
        self._model: Any = None

    def transcribe(self, audio_path: Path, *, retry_without_vad: bool = False) -> Transcript:
        model = self._load_model()
        try:
            transcript = self._transcribe_once(model, audio_path, vad_filter=True)
            if not transcript.text.strip() and retry_without_vad:
                transcript = self._transcribe_once(model, audio_path, vad_filter=False)
        except Exception as exc:
            raise VoiceError(f"Whisper transcription failed: {exc}") from exc
        return transcript

    def _transcribe_once(self, model: Any, audio_path: Path, *, vad_filter: bool) -> Transcript:
        segments, info = model.transcribe(
            str(audio_path),
            language="en",
            vad_filter=vad_filter,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        language = str(getattr(info, "language", "en") or "en")
        return Transcript(text=text, language=language, duration_seconds=duration)

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise VoiceError(
                "Voice input needs faster-whisper. Install SHAMSU with the voice extra."
            ) from exc
        self._model = WhisperModel(
            self.model_name,
            device=self.device,
            compute_type=self.compute_type,
        )
        return self._model
