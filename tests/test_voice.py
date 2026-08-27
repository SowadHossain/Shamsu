from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.voice.models import Transcript, VoiceError
from shamsu.voice.service import VoiceService
from shamsu.voice.speech import prepare_spoken_text
from shamsu.voice.whisper import WhisperTranscriber


class FakeTranscriber:
    def __init__(self) -> None:
        self.paths: list[Path] = []

    def transcribe(self, audio_path: Path) -> Transcript:
        self.paths.append(Path(audio_path))
        return Transcript("run the focused tests")


def test_voice_service_normalizes_then_transcribes(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "voice.oga"
    source.write_bytes(b"audio")
    transcriber = FakeTranscriber()

    def normalize(source_path: Path, destination: Path) -> Path:
        assert source_path == source
        destination.write_bytes(b"wav")
        return destination

    monkeypatch.setattr("shamsu.voice.service.normalize_for_whisper", normalize)

    transcript = VoiceService(transcriber=transcriber).transcribe_file(source)

    assert transcript.text == "run the focused tests"
    assert transcriber.paths


def test_voice_service_rejects_empty_audio(tmp_path: Path) -> None:
    source = tmp_path / "empty.wav"
    source.write_bytes(b"")

    with pytest.raises(VoiceError, match="empty"):
        VoiceService(transcriber=FakeTranscriber()).transcribe_file(source)


def test_whisper_defaults_to_cpu_int8(monkeypatch) -> None:
    monkeypatch.delenv("SHAMSU_WHISPER_DEVICE", raising=False)
    monkeypatch.delenv("SHAMSU_WHISPER_COMPUTE_TYPE", raising=False)

    transcriber = WhisperTranscriber()

    assert transcriber.device == "cpu"
    assert transcriber.compute_type == "int8"


def test_prepare_spoken_text_removes_markdown_noise() -> None:
    spoken = prepare_spoken_text(
        "Done.\n\n```python\nprint('secretly long')\n```\nSee [the file](app.py) and `pytest`.",
        max_chars=200,
    )

    assert spoken == "Done. code block omitted. See the file and pytest."
