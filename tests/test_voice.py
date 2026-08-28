from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.voice.audio import WavSignalStats
from shamsu.voice.models import Transcript, VoiceError
from shamsu.voice.service import VoiceService
from shamsu.voice.speech import prepare_spoken_text
from shamsu.voice.whisper import WhisperTranscriber


class FakeTranscriber:
    def __init__(self) -> None:
        self.paths: list[Path] = []
        self.retry_without_vad = False

    def transcribe(self, audio_path: Path, *, retry_without_vad: bool = False) -> Transcript:
        self.paths.append(Path(audio_path))
        self.retry_without_vad = retry_without_vad
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
    monkeypatch.setattr(
        "shamsu.voice.service.wav_signal_stats",
        lambda _path: WavSignalStats(duration_seconds=1.0, peak=2000, rms=300),
    )

    transcript = VoiceService(transcriber=transcriber).transcribe_file(source)

    assert transcript.text == "run the focused tests"
    assert transcriber.paths
    assert transcriber.retry_without_vad


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


def test_whisper_retries_without_vad_when_audio_has_signal(tmp_path: Path) -> None:
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"fake")

    class Segment:
        def __init__(self, text: str) -> None:
            self.text = text

    class Info:
        duration = 1.0
        language = "en"

    class Model:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def transcribe(self, _path: str, *, language: str, vad_filter: bool):
            self.calls.append(vad_filter)
            text = "" if vad_filter else "fallback heard me"
            return [Segment(text)], Info()

    model = Model()
    transcriber = WhisperTranscriber()
    transcriber._model = model

    transcript = transcriber.transcribe(audio, retry_without_vad=True)

    assert transcript.text == "fallback heard me"
    assert model.calls == [True, False]


def test_voice_service_rejects_quiet_empty_transcript(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "quiet.wav"
    source.write_bytes(b"audio")

    class EmptyTranscriber:
        def transcribe(self, _audio_path: Path, *, retry_without_vad: bool = False) -> Transcript:
            assert not retry_without_vad
            return Transcript("")

    def normalize(_source_path: Path, destination: Path) -> Path:
        destination.write_bytes(b"wav")
        return destination

    monkeypatch.setattr("shamsu.voice.service.normalize_for_whisper", normalize)
    monkeypatch.setattr(
        "shamsu.voice.service.wav_signal_stats",
        lambda _path: WavSignalStats(duration_seconds=1.0, peak=100, rms=20),
    )

    with pytest.raises(VoiceError, match="too quiet"):
        VoiceService(transcriber=EmptyTranscriber()).transcribe_file(source)


def test_prepare_spoken_text_removes_markdown_noise() -> None:
    spoken = prepare_spoken_text(
        "Done.\n\n```python\nprint('secretly long')\n```\nSee [the file](app.py) and `pytest`.",
        max_chars=200,
    )

    assert spoken == "Done. code block omitted. See the file and pytest."
