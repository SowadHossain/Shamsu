"""Kokoro-82M: the natural voice, synthesized on the processor.

Two measurements decided everything in this file, both taken on the machine
this was written on (14 physical cores) and both saved under
`logs/test-runs/2026-08-31-tts-bakeoff.log`:

  * fp32 is 3.4x FASTER than the int8 build - 0.24 real-time factor against
    0.83. The 92MB "small" model is a trap: dynamic quantization has no fast
    kernel here, so it costs 233MB of disk savings and most of the speed. The
    default is the 325MB fp32 model for that reason and no other.
  * intra_op_num_threads=8 beats both 4 and "all of them". At 20 threads the
    RTF passes 1.2 and synthesis falls behind the speaker; oversubscribing a
    hybrid CPU is slower than using half of it.

A reply is spoken one sentence at a time, synthesized on a producer thread
that runs ahead of playback. At RTF 0.25 the producer stays roughly four
sentences ahead, so the voice starts about half a second after the answer
lands and never pauses mid-paragraph waiting for the model.

The session is built with `providers=["CPUExecutionProvider"]`, explicitly,
rather than by letting kokoro-onnx choose: its own resolver takes every
available provider once an accelerated onnxruntime is installed, and the GPU
in this machine belongs to Ollama. `test_kokoro_never_asks_for_the_gpu` is
what actually holds that line.
"""
from __future__ import annotations

import os
import queue
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from shamsu.voice.models import VoiceError
from shamsu.voice.playback import play_pcm_chunks

VOICE_MODEL_ENV_VAR = "SHAMSU_VOICE_MODEL"
VOICE_MODEL_DIR_ENV_VAR = "SHAMSU_VOICE_MODEL_DIR"

DEFAULT_MODEL_NAME = "kokoro-v1.0.onnx"
DEFAULT_VOICES_NAME = "voices-v1.0.bin"
#: SHAMSU's voice. Overridable per machine with SHAMSU_VOICE_NAME; run
#: `python -m shamsu.voice voices` for the other 53.
DEFAULT_VOICE = "am_adam"
DEFAULT_THREADS = 8

#: Shared between engines and between projects, so a reinstall never
#: re-downloads and two workspaces never keep two copies.
DEFAULT_MODEL_DIR = Path.home() / ".shamsu" / "voices"

MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)

#: Split on sentence ends, keeping the punctuation - the model needs the mark
#: to get the intonation right, and "?" and "." do not sound the same.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def model_search_dirs() -> list[Path]:
    dirs: list[Path] = []
    configured = os.environ.get(VOICE_MODEL_DIR_ENV_VAR, "").strip()
    if configured:
        dirs.append(Path(configured).expanduser())
    dirs.append(DEFAULT_MODEL_DIR)
    return dirs


def resolve_model_files() -> tuple[Path, Path] | None:
    """The (model, voices) pair, or None when this machine has not got them."""
    override = os.environ.get(VOICE_MODEL_ENV_VAR, "").strip()
    for directory in model_search_dirs():
        model = Path(override).expanduser() if override else directory / DEFAULT_MODEL_NAME
        voices = directory / DEFAULT_VOICES_NAME
        if model.is_file() and voices.is_file():
            return model, voices
    return None


def kokoro_available() -> bool:
    """Package installed AND models on disk. Both, or this engine cannot run."""
    if resolve_model_files() is None:
        return False
    try:
        import kokoro_onnx  # noqa: F401
    except Exception:  # noqa: BLE001 - an optional extra, not an error
        return False
    return True


def download_model(directory: Path | None = None) -> tuple[Path, Path]:
    """Fetch the model and the voice bank. Explicit, never implicit.

    353MB is not something a spoken reply gets to start on its own, which is
    why `auto` falls back to a lesser engine in silence instead. This is the
    call that says yes.
    """
    import urllib.request

    target = directory or DEFAULT_MODEL_DIR
    target.mkdir(parents=True, exist_ok=True)
    model, voices = target / DEFAULT_MODEL_NAME, target / DEFAULT_VOICES_NAME
    for path, url in ((model, MODEL_URL), (voices, VOICES_URL)):
        if path.is_file():
            continue
        try:
            urllib.request.urlretrieve(url, path)
        except Exception as exc:
            raise VoiceError(f"Could not download {path.name}: {exc}") from exc
    return model, voices


def rate_to_speed(rate: int) -> float:
    """SHAMSU's -10..10 rate onto Kokoro's speed multiplier."""
    return max(0.5, min(2.0, 1.0 + (rate * 0.05)))


class KokoroSpeechEngine:
    name = "kokoro"

    def __init__(
        self,
        *,
        model_path: Path | None = None,
        voices_path: Path | None = None,
        voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        volume: float = 1.0,
        threads: int = DEFAULT_THREADS,
        kokoro: Any = None,
        player: Any = None,
    ) -> None:
        if kokoro is None:
            found = (model_path, voices_path)
            if not all(found):
                resolved = resolve_model_files()
                if resolved is None:
                    raise VoiceError(
                        "No Kokoro model found. Download it with SHAMSU's voice "
                        f"setup, or point {VOICE_MODEL_DIR_ENV_VAR} at the folder "
                        f"holding {DEFAULT_MODEL_NAME} and {DEFAULT_VOICES_NAME}."
                    )
                model_path, voices_path = resolved
        self._model_path = model_path
        self._voices_path = voices_path
        self._voice = voice or DEFAULT_VOICE
        self._speed = speed
        self._volume = volume
        self._threads = threads
        self._kokoro = kokoro
        self._player = player or play_pcm_chunks
        self._lock = threading.Lock()
        self._stop_flag = threading.Event()

    @classmethod
    def from_settings(cls, settings: Any) -> KokoroSpeechEngine:
        return cls(
            voice=str(getattr(settings, "voice_name", "") or DEFAULT_VOICE),
            speed=rate_to_speed(int(getattr(settings, "rate", 0) or 0)),
            volume=max(0.0, min(1.0, int(getattr(settings, "volume", 100) or 0) / 100)),
            threads=int(getattr(settings, "threads", DEFAULT_THREADS) or DEFAULT_THREADS),
        )

    # -- model -------------------------------------------------------------

    def _load(self) -> Any:
        """Loaded on the first thing spoken, once. Roughly 0.8s and ~400MB of
        RAM, which a session that never speaks should not be charged."""
        with self._lock:
            if self._kokoro is not None:
                return self._kokoro
            try:
                import onnxruntime as rt
                from kokoro_onnx import Kokoro
            except Exception as exc:
                raise VoiceError(
                    "Kokoro is not installed. Install the voice extra: "
                    "pip install kokoro-onnx"
                ) from exc
            try:
                options = rt.SessionOptions()
                options.intra_op_num_threads = self._threads
                session = rt.InferenceSession(
                    str(self._model_path),
                    sess_options=options,
                    # The GPU belongs to the coder model. Named here rather
                    # than left to kokoro-onnx, which would take the GPU the
                    # day someone installs an accelerated onnxruntime.
                    providers=["CPUExecutionProvider"],
                )
                self._kokoro = Kokoro.from_session(session, str(self._voices_path))
            except Exception as exc:
                raise VoiceError(f"Could not load the Kokoro model: {exc}") from exc
            return self._kokoro

    def voices(self) -> list[str]:
        return list(self._load().get_voices())

    # -- speaking ----------------------------------------------------------

    def speak(self, text: str) -> None:
        body = str(text or "").strip()
        if not body:
            return
        self._stop_flag.clear()
        kokoro = self._load()
        sentences = split_sentences(body)
        self._player(self._synthesize_ahead(kokoro, sentences), self._stop_flag)

    def _synthesize_ahead(self, kokoro: Any, sentences: list[str]) -> Iterator[Any]:
        """Synthesis on its own thread, playback on this one.

        A queue of two is deliberate: enough that the speakers never wait on
        the model, small enough that pressing stop discards a sentence of
        audio rather than a paragraph of it.
        """
        import numpy as np

        pending: queue.Queue[Any] = queue.Queue(maxsize=2)
        done = object()

        def produce() -> None:
            try:
                for sentence in sentences:
                    if self._stop_flag.is_set():
                        break
                    samples, sample_rate = kokoro.create(
                        sentence, voice=self._voice, speed=self._speed, lang="en-us"
                    )
                    if self._volume != 1.0:
                        samples = samples * self._volume
                    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
                    pending.put((pcm, int(sample_rate), 1))
            except Exception as exc:  # noqa: BLE001
                pending.put(exc)
            finally:
                pending.put(done)

        worker = threading.Thread(target=produce, daemon=True)
        worker.start()
        while True:
            item = pending.get()
            if item is done:
                return
            # Checked on the consuming side too: the producer stops making
            # audio, but up to two sentences are already queued behind it, and
            # an engine that was told to stop must not hand those out either.
            if self._stop_flag.is_set():
                return
            if isinstance(item, BaseException):
                raise VoiceError(f"Kokoro synthesis failed: {item}") from item
            yield item

    def stop(self) -> None:
        self._stop_flag.set()


def split_sentences(text: str) -> list[str]:
    """One sentence per chunk, so the voice can start before the rest exists."""
    parts = [part.strip() for part in _SENTENCE_END.split(text) if part.strip()]
    return parts or [text]
