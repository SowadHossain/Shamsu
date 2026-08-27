"""Audio normalization helpers for Whisper."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from shamsu.voice.models import VoiceError

WHISPER_SAMPLE_RATE = 16_000


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
