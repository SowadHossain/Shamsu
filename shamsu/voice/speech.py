"""Speaking a reply: the settings, the text cleanup, and the player.

The player owns no voice of its own. It holds the settings, turns markdown
into something worth reading aloud, and hands the result to whichever engine
`shamsu.voice.engines` selected - Kokoro, Piper, or the operating system's.
The system engine lives here because it is the floor: it needs no model, no
download and no optional package, so it is the one that must always exist.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

from shamsu.voice.models import VoiceError

VOICE_OUTPUT_ENV_VAR = "SHAMSU_VOICE_OUTPUT"
VOICE_OUTPUT_RATE_ENV_VAR = "SHAMSU_VOICE_RATE"
VOICE_OUTPUT_VOLUME_ENV_VAR = "SHAMSU_VOICE_VOLUME"
VOICE_OUTPUT_VOICE_ENV_VAR = "SHAMSU_VOICE_NAME"
VOICE_OUTPUT_MAX_CHARS_ENV_VAR = "SHAMSU_VOICE_MAX_CHARS"
VOICE_ENGINE_ENV_VAR = "SHAMSU_VOICE_ENGINE"
VOICE_THREADS_ENV_VAR = "SHAMSU_VOICE_THREADS"

#: When a reply is spoken aloud. "voice" - the default - speaks only the
#: replies to something you SAID, at the microphone, on this machine. Typed
#: prompts get a typed answer, and so does every turn that began somewhere
#: else: a Telegram voice note is transcribed and answered in text, and the
#: terminal that happens to be watching that turn stays silent. "always"
#: restores the old speak-everything behaviour; "off" is silence.
SPEAK_WHEN_VOICE = "voice"
SPEAK_WHEN_ALWAYS = "always"
_SPEAK_WHEN_VALUES = {SPEAK_WHEN_VOICE, SPEAK_WHEN_ALWAYS}
_DISABLED_VALUES = {"0", "false", "no", "off", "disabled", "never", "none"}

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
    speak_when: str = SPEAK_WHEN_VOICE
    #: "auto" walks the engine order and takes the first that can run here.
    #: A named engine is honoured or it fails loudly - see `build_engine`.
    engine: str = "auto"
    #: Measured, not guessed: 8 beat both 4 and all-20 on a 14-core hybrid
    #: CPU, where oversubscription pushed synthesis behind real time.
    threads: int = 8


class SystemSpeechEngine:
    """Windows SAPI, macOS `say`, Linux `spd-say` - via one subprocess each.

    The floor of the engine order. It sounds like 2004 and it starts a whole
    PowerShell to say one sentence, but it is on every machine, needs nothing
    downloaded, and so is what a session falls back to rather than falling
    silent.
    """

    name = "system"

    def __init__(self, settings: SpeechSettings | None = None) -> None:
        self.settings = settings or load_speech_settings()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    def speak(self, text: str) -> None:
        spoken = str(text or "").strip()
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


class SpeechPlayer:
    """Speak a reply through whichever engine this machine can run.

    Engine choice is deferred to the first spoken word, not made here: loading
    Kokoro costs ~0.8s and ~400MB, and a session that never speaks must not
    pay it. That is also why `stop()` on an unbuilt player is a no-op rather
    than a reason to build one.
    """

    def __init__(self, settings: SpeechSettings | None = None, engine: Any = None) -> None:
        self.settings = settings or load_speech_settings()
        self._engine = engine

    @property
    def engine_name(self) -> str:
        return str(getattr(self._engine, "name", "")) if self._engine is not None else ""

    def speak(self, text: str) -> None:
        if not self.settings.enabled:
            return
        spoken = prepare_spoken_text(text, max_chars=self.settings.max_chars)
        if not spoken:
            return
        self._resolve_engine().speak(spoken)

    def stop(self) -> None:
        engine = self._engine
        if engine is None:
            return
        engine.stop()

    def _resolve_engine(self) -> Any:
        if self._engine is None:
            # Imported here: `engines` imports this module for the system
            # engine, and at module scope that is a cycle.
            from shamsu.voice.engines import build_engine

            self._engine = build_engine(self.settings)
        return self._engine


def load_speech_settings() -> SpeechSettings:
    return SpeechSettings(
        enabled=_env_enabled(VOICE_OUTPUT_ENV_VAR, default=True),
        rate=_env_int(VOICE_OUTPUT_RATE_ENV_VAR, default=0, minimum=-10, maximum=10),
        volume=_env_int(VOICE_OUTPUT_VOLUME_ENV_VAR, default=100, minimum=0, maximum=100),
        voice_name=os.environ.get(VOICE_OUTPUT_VOICE_ENV_VAR, "").strip(),
        max_chars=_env_int(VOICE_OUTPUT_MAX_CHARS_ENV_VAR, default=900, minimum=80, maximum=5000),
        speak_when=_env_speak_when(VOICE_OUTPUT_ENV_VAR, default=SPEAK_WHEN_VOICE),
        engine=(os.environ.get(VOICE_ENGINE_ENV_VAR, "").strip().lower() or "auto"),
        threads=_env_int(VOICE_THREADS_ENV_VAR, default=8, minimum=1, maximum=64),
    )


def reply_should_be_spoken(*, voice_input: bool, settings: SpeechSettings | None = None) -> bool:
    """Whether THIS reply is spoken, given how its prompt arrived.

    The player itself stays dumb - it speaks what it is handed - because the
    only surface that knows a prompt came from the microphone is the one that
    recorded it. Asking here keeps that single answer in one place instead of
    letting each caller invent its own rule.
    """
    resolved = settings or load_speech_settings()
    if not resolved.enabled:
        return False
    if resolved.speak_when == SPEAK_WHEN_ALWAYS:
        return True
    return bool(voice_input)


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
    return raw not in _DISABLED_VALUES


def _env_speak_when(name: str, *, default: str) -> str:
    """One variable carries both the switch and the policy.

    `SHAMSU_VOICE_OUTPUT` was already the on/off knob, and a second variable
    for "on, but only for what I said out loud" would be a knob whose two
    halves can contradict each other.
    """
    raw = os.environ.get(name, "").strip().lower()
    if raw in _SPEAK_WHEN_VALUES:
        return raw
    return default


def _env_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, min(maximum, value))
