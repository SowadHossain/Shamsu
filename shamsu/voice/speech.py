"""Local text-to-speech playback for CLI voice replies."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass

from shamsu.voice.models import VoiceError

VOICE_OUTPUT_ENV_VAR = "SHAMSU_VOICE_OUTPUT"
VOICE_OUTPUT_RATE_ENV_VAR = "SHAMSU_VOICE_RATE"
VOICE_OUTPUT_VOLUME_ENV_VAR = "SHAMSU_VOICE_VOLUME"
VOICE_OUTPUT_VOICE_ENV_VAR = "SHAMSU_VOICE_NAME"
VOICE_OUTPUT_MAX_CHARS_ENV_VAR = "SHAMSU_VOICE_MAX_CHARS"

_FENCED_CODE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MARKDOWN_DECORATION = re.compile(r"[*_#>~]+")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class SpeechSettings:
    enabled: bool = True
    rate: int = 0
    volume: int = 100
    voice_name: str = ""
    max_chars: int = 900


class SpeechPlayer:
    """Speak text through the operating system's local TTS engine."""

    def __init__(self, settings: SpeechSettings | None = None) -> None:
        self.settings = settings or load_speech_settings()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def speak(self, text: str) -> None:
        if not self.settings.enabled:
            return
        spoken = prepare_spoken_text(text, max_chars=self.settings.max_chars)
        if not spoken:
            return
        system = platform.system().lower()
        if system == "windows":
            command, env = _windows_command(spoken, self.settings)
            self._run(command, env=env)
            return
        if system == "darwin" and shutil.which("say"):
            self._run(["say", spoken])
            return
        if shutil.which("spd-say"):
            self._run(["spd-say", spoken])
            return
        raise VoiceError("Voice output needs Windows SAPI, macOS say, or Linux spd-say.")

    def stop(self) -> None:
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    def _run(self, command: list[str], *, env: dict[str, str] | None = None) -> None:
        self.stop()
        try:
            process = subprocess.Popen(
                command,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise VoiceError(f"Voice output command was not found: {command[0]}") from exc
        with self._lock:
            self._process = process
        try:
            _stdout, stderr = process.communicate()
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
        if process.returncode:
            detail = (stderr or "").strip()
            raise VoiceError(f"Voice output failed: {detail[:300] or command[0]}")


def load_speech_settings() -> SpeechSettings:
    return SpeechSettings(
        enabled=_env_enabled(VOICE_OUTPUT_ENV_VAR, default=True),
        rate=_env_int(VOICE_OUTPUT_RATE_ENV_VAR, default=0, minimum=-10, maximum=10),
        volume=_env_int(VOICE_OUTPUT_VOLUME_ENV_VAR, default=100, minimum=0, maximum=100),
        voice_name=os.environ.get(VOICE_OUTPUT_VOICE_ENV_VAR, "").strip(),
        max_chars=_env_int(VOICE_OUTPUT_MAX_CHARS_ENV_VAR, default=900, minimum=80, maximum=5000),
    )


def prepare_spoken_text(text: str, *, max_chars: int = 900) -> str:
    clean = str(text or "")
    clean = _FENCED_CODE.sub(" code block omitted. ", clean)
    clean = _MARKDOWN_LINK.sub(r"\1", clean)
    clean = _INLINE_CODE.sub(r"\1", clean)
    clean = _MARKDOWN_DECORATION.sub("", clean)
    clean = clean.replace("\\", " ")
    clean = _WHITESPACE.sub(" ", clean).strip()
    if len(clean) <= max_chars:
        return clean
    return clean[:max_chars].rsplit(" ", 1)[0].rstrip() + "."


def _windows_command(text: str, settings: SpeechSettings) -> tuple[list[str], dict[str, str]]:
    script = """
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$speaker.Rate = [Math]::Max(-10, [Math]::Min(10, [int]$env:SHAMSU_VOICE_RATE_CURRENT))
$speaker.Volume = [Math]::Max(0, [Math]::Min(100, [int]$env:SHAMSU_VOICE_VOLUME_CURRENT))
if ($env:SHAMSU_VOICE_NAME_CURRENT) {
    $speaker.SelectVoice($env:SHAMSU_VOICE_NAME_CURRENT)
}
$speaker.Speak($env:SHAMSU_VOICE_TEXT_CURRENT)
$speaker.Dispose()
"""
    env = {
        **os.environ,
        "SHAMSU_VOICE_TEXT_CURRENT": text,
        "SHAMSU_VOICE_RATE_CURRENT": str(settings.rate),
        "SHAMSU_VOICE_VOLUME_CURRENT": str(settings.volume),
        "SHAMSU_VOICE_NAME_CURRENT": settings.voice_name,
    }
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], env


def _env_enabled(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off", "disabled"}


def _env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))
