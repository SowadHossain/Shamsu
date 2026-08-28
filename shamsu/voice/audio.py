"""Audio normalization helpers for Whisper."""
from __future__ import annotations

import shutil
import subprocess
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path

from shamsu.voice.models import VoiceError

WHISPER_SAMPLE_RATE = 16_000
MIN_VOICE_PEAK = 800
MIN_VOICE_RMS = 100


@dataclass(frozen=True)
class WavSignalStats:
    duration_seconds: float
    peak: int
    rms: int

    @property
    def has_voice_level(self) -> bool:
        return self.peak >= MIN_VOICE_PEAK or self.rms >= MIN_VOICE_RMS


def normalize_for_whisper(source: Path, destination: Path) -> Path:
    """Convert an audio file to the mono 16 kHz WAV shape Whisper expects."""
    ffmpeg = _ffmpeg_executable()
    if not ffmpeg:
        raise VoiceError(
            "Voice input needs ffmpeg to convert audio for Whisper. "
            "Install SHAMSU with the voice extra."
        )
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        str(WHISPER_SAMPLE_RATE),
        "-vn",
        str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise VoiceError(f"Could not convert audio for Whisper: {detail[:500]}") from exc
    return destination


def _ffmpeg_executable() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    try:
        import imageio_ffmpeg
    except ImportError:
        return ""
    return str(imageio_ffmpeg.get_ffmpeg_exe() or "")


def wav_signal_stats(path: Path) -> WavSignalStats:
    try:
        with wave.open(str(path), "rb") as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            width = wav.getsampwidth()
            channels = wav.getnchannels()
            data = wav.readframes(frames)
    except (OSError, wave.Error) as exc:
        raise VoiceError(f"Could not inspect normalized audio: {exc}") from exc
    duration = frames / rate if rate else 0.0
    if width != 2:
        return WavSignalStats(duration_seconds=duration, peak=0, rms=0)
    samples = array("h")
    samples.frombytes(data)
    if channels > 1:
        samples = array("h", samples[::channels])
    if not samples:
        return WavSignalStats(duration_seconds=duration, peak=0, rms=0)
    peak = max(abs(sample) for sample in samples)
    rms = int((sum(sample * sample for sample in samples) / len(samples)) ** 0.5)
    return WavSignalStats(duration_seconds=duration, peak=int(peak), rms=rms)
