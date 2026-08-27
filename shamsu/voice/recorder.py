"""Microphone recorder for CLI/TUI push-to-talk."""
from __future__ import annotations

import os
import tempfile
import wave
from pathlib import Path

from shamsu.voice.models import VoiceError


class MicrophoneRecorder:
    def __init__(self, *, sample_rate: int | None = None, device: int | str | None = None) -> None:
        self.sample_rate = int(sample_rate or 0)
        self.device = device
        self._stream = None
        self._chunks: list[bytes] = []
        self._active_sample_rate = 0

    @property
    def recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise VoiceError(
                "TUI voice input needs sounddevice. Install SHAMSU with the voice extra."
            ) from exc
        self._chunks = []
        device = self._resolve_device(sd)
        sample_rate = self.sample_rate or self._default_sample_rate(sd, device)

        def callback(indata, _frames, _time, status) -> None:
            chunk = bytes(indata)
            if chunk:
                self._chunks.append(chunk)

        self._stream = sd.RawInputStream(
            samplerate=sample_rate,
            device=device,
            channels=1,
            dtype="int16",
            callback=callback,
        )
        try:
            self._stream.start()
            self._active_sample_rate = int(sample_rate)
        except Exception as exc:
            self._stream = None
            raise VoiceError(f"Could not start microphone recording: {exc}") from exc

    def stop(self) -> Path:
        stream = self._stream
        if stream is None:
            raise VoiceError("Voice recording was not running.")
        self._stream = None
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            raise VoiceError(f"Could not stop microphone recording: {exc}") from exc
        if not self._chunks:
            raise VoiceError("Voice recording was empty.")
        with tempfile.NamedTemporaryFile(
            prefix="shamsu-recording-", suffix=".wav", delete=False
        ) as handle:
            path = Path(handle.name)
        with wave.open(str(path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self._active_sample_rate or self.sample_rate or 44_100)
            wav.writeframes(b"".join(self._chunks))
        return path

    def _resolve_device(self, sd) -> int | str | None:
        configured = self.device
        if configured is None:
            configured = os.environ.get("SHAMSU_VOICE_INPUT_DEVICE", "").strip()
        if configured in {None, ""}:
            return None
        text = str(configured).strip()
        if text.isdigit():
            return int(text)
        for index, device in enumerate(sd.query_devices()):
            name = str(device.get("name") or "")
            if text.lower() in name.lower() and int(device.get("max_input_channels") or 0) > 0:
                return index
        raise VoiceError(f"No input device matched SHAMSU_VOICE_INPUT_DEVICE={text!r}.")

    def _default_sample_rate(self, sd, device: int | str | None) -> int:
        try:
            info = sd.query_devices(device, "input")
            return int(float(info.get("default_samplerate") or 44_100))
        except (KeyError, TypeError, ValueError):
            return 44_100
