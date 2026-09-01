"""Piper: a small neural voice that speaks from the CPU.

The system engine - Windows SAPI, macOS `say`, Linux `spd-say` - is always
there and always sounds like 2004. Piper is a ~60MB ONNX model that runs on
the processor in real time, which is the whole reason it is the one neural
model SHAMSU is willing to load: the GPU belongs to Ollama, and a spoken
reply must never cost the coder model its VRAM. `PiperVoice.load` is called
with `use_cuda=False` for exactly that reason, and the assertion is kept in a
test rather than in a comment.

Nothing here is required. With no model on disk the player falls back to the
system engine, so voice output keeps working on a machine that has never
downloaded anything.
"""
from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from shamsu.voice.models import VoiceError
from shamsu.voice.playback import play_pcm_chunks

VOICE_MODEL_ENV_VAR = "SHAMSU_VOICE_MODEL"
VOICE_MODEL_DIR_ENV_VAR = "SHAMSU_VOICE_MODEL_DIR"

#: Medium quality, ~63MB, US English. Piper also publishes `low` (faster, more
#: robotic) and `high` (slower than real time on older CPUs); medium is the one
#: that stays ahead of the reader on a laptop core.
DEFAULT_VOICE_NAME = "en_US-lessac-medium"
DEFAULT_THREADS = 8

#: Chosen over the package directory so a reinstall never re-downloads, and
#: over the workspace so two projects share one copy.
DEFAULT_MODEL_DIR = Path.home() / ".shamsu" / "voices"


def model_search_dirs() -> list[Path]:
    dirs: list[Path] = []
    configured = os.environ.get(VOICE_MODEL_DIR_ENV_VAR, "").strip()
    if configured:
        dirs.append(Path(configured).expanduser())
    dirs.append(DEFAULT_MODEL_DIR)
    return dirs


def resolve_voice_model(name: str = "") -> Path | None:
    """The .onnx file for `name`, or None when it is not on this machine.

    A path is taken as given - someone who names a file means that file. A
    bare voice name is looked for in the shared model directory.
    """
    wanted = (name or os.environ.get(VOICE_MODEL_ENV_VAR, "") or DEFAULT_VOICE_NAME).strip()
    if not wanted:
        return None
    candidate = Path(wanted).expanduser()
    if candidate.suffix == ".onnx":
        return candidate if candidate.is_file() else None
    for directory in model_search_dirs():
        found = directory / f"{wanted}.onnx"
        if found.is_file():
            return found
    return None


def rate_to_length_scale(rate: int) -> float:
    """SHAMSU's -10..10 rate onto Piper's length scale, which runs the other
    way: a LONGER sample is a slower voice."""
    return max(0.5, min(2.0, 1.0 - (rate * 0.04)))


def piper_available(name: str = "") -> bool:
    """Whether Piper can speak right now: package installed AND model present."""
    if resolve_voice_model(name) is None:
        return False
    try:
        import piper  # noqa: F401
    except Exception:  # noqa: BLE001 - an optional extra, not an error
        return False
    return True


def download_voice_model(name: str = DEFAULT_VOICE_NAME, *, directory: Path | None = None) -> Path:
    """Fetch a voice into the shared model directory. Explicit, never implicit.

    A spoken reply is not permitted to start a 60MB download on its own, which
    is why the fallback to the system engine is silent. This is the call that
    says yes.
    """
    target_dir = directory or DEFAULT_MODEL_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        from piper.download_voices import download_voice
    except Exception as exc:
        raise VoiceError(
            "Piper is not installed. Install the voice extra: pip install piper-tts"
        ) from exc
    try:
        download_voice(name, target_dir)
    except Exception as exc:
        raise VoiceError(f"Could not download the {name} voice: {exc}") from exc
    model = target_dir / f"{name}.onnx"
    if not model.is_file():
        raise VoiceError(f"The {name} download finished but left no model at {model}.")
    return model


class PiperSpeechEngine:
    """Synthesize on the CPU and play the audio as it arrives.

    Piper yields one chunk per sentence, and each is played the moment it is
    ready - so a four-sentence answer starts speaking after the first one
    instead of after the model has finished the lot.
    """

    name = "piper"

    def __init__(
        self,
        *,
        model_path: Path | None = None,
        length_scale: float = 1.0,
        volume: float = 1.0,
        voice: Any = None,
        player: Any = None,
    ) -> None:
        self._model_path = model_path or resolve_voice_model()
        if self._model_path is None and voice is None:
            raise VoiceError(
                "No Piper voice model found. Run SHAMSU's voice download, or set "
                f"{VOICE_MODEL_ENV_VAR} to an .onnx file."
            )
        self._length_scale = length_scale
        self._volume = volume
        self._voice = voice
        self._player = player or play_pcm_chunks
        self._lock = threading.Lock()
        self._stop_flag = threading.Event()

    @classmethod
    def from_settings(cls, settings: Any) -> PiperSpeechEngine:
        return cls(
            length_scale=rate_to_length_scale(int(getattr(settings, "rate", 0) or 0)),
            volume=max(0.0, min(1.0, int(getattr(settings, "volume", 100) or 0) / 100)),
        )

    # -- model -------------------------------------------------------------

    def _load_voice(self) -> Any:
        """Loaded once, on the first thing spoken - not at import, and not when
        the frame is built, where it would cost a second of startup to a
        session that may never say a word."""
        with self._lock:
            if self._voice is not None:
                return self._voice
            try:
                from piper import PiperVoice
            except Exception as exc:
                raise VoiceError(
                    "Piper is not installed. Install the voice extra: pip install piper-tts"
                ) from exc
            try:
                # use_cuda=False is the point of this engine: the GPU stays
                # free for the model that is writing the code.
                self._voice = PiperVoice.load(str(self._model_path), use_cuda=False)
            except Exception as exc:
                raise VoiceError(f"Could not load the Piper voice model: {exc}") from exc
            return self._voice

    def _synthesis_config(self) -> Any:
        from piper.config import SynthesisConfig

        return SynthesisConfig(length_scale=self._length_scale, volume=self._volume)

    # -- playback ----------------------------------------------------------

    def speak(self, text: str) -> None:
        body = str(text or "").strip()
        if not body:
            return
        self._stop_flag.clear()
        voice = self._load_voice()
        chunks = voice.synthesize(body, self._synthesis_config())
        self._play(chunks)

    def _play(self, chunks: Iterator[Any]) -> None:
        import numpy as np

        def as_pcm() -> Iterator[Any]:
            for chunk in chunks:
                if self._stop_flag.is_set():
                    return
                yield (
                    np.frombuffer(chunk.audio_int16_bytes, dtype=np.int16),
                    int(chunk.sample_rate),
                    int(chunk.sample_channels),
                )

        self._player(as_pcm(), self._stop_flag)

    def stop(self) -> None:
        self._stop_flag.set()
