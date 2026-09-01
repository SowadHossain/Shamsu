"""Which voice speaks, and how one gets swapped for another.

Three engines exist and none of them is special-cased anywhere else in the
codebase: `SpeechPlayer` asks this module for something with `speak()` and
`stop()`, and gets whichever engine the settings and the machine allow. Adding
a fourth is `register_engine("name", factory)` and one line in `AUTO_ORDER` -
the player, the frame and the tests never learn its name.

The order matters more than the list. `auto` walks it and takes the first
engine that can actually run HERE:

  kokoro  - 82M-parameter neural voice, natural, needs a 325MB model on disk
  piper   - small VITS voice, instant, robotic, needs a ~63MB model on disk
  system  - SAPI / say / spd-say. No model, no download, always available

A factory raises `VoiceError` when its engine cannot run, and `auto` reads
that as "try the next one" rather than as a failure. That is what makes a
machine with no models downloaded still speak - badly, through the system
engine, but speak.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from shamsu.voice.models import VoiceError

if TYPE_CHECKING:  # pragma: no cover - the import exists for readers and mypy
    from shamsu.voice.speech import SpeechSettings

ENGINE_AUTO = "auto"
ENGINE_KOKORO = "kokoro"
ENGINE_PIPER = "piper"
ENGINE_SYSTEM = "system"

#: Best first. `auto` stops at the first one that can run on this machine.
AUTO_ORDER = (ENGINE_KOKORO, ENGINE_PIPER, ENGINE_SYSTEM)


@runtime_checkable
class SpeechEngine(Protocol):
    """Everything the player needs from a voice, and nothing more.

    `speak` blocks until the text has been spoken or `stop` interrupts it -
    the player already runs it off the frame's thread, and an engine that
    returned early would take the interrupt away from the person pressing the
    key.
    """

    name: str

    def speak(self, text: str) -> None: ...

    def stop(self) -> None: ...


EngineFactory = Callable[["SpeechSettings"], SpeechEngine]

_REGISTRY: dict[str, EngineFactory] = {}


def register_engine(name: str, factory: EngineFactory) -> None:
    _REGISTRY[name] = factory


def registered_engines() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def build_engine(settings: Any) -> SpeechEngine:
    """The engine this machine will speak with, given these settings.

    A NAMED engine is honoured or it fails loudly: someone who set
    `SHAMSU_VOICE_ENGINE=kokoro` and has no model wants to hear about the
    missing model, not to be quietly handed SAPI and left wondering why the
    voice never improved.
    """
    _ensure_registered()
    choice = str(getattr(settings, "engine", ENGINE_AUTO) or ENGINE_AUTO).strip().lower()
    if choice and choice != ENGINE_AUTO:
        factory = _REGISTRY.get(choice)
        if factory is None:
            raise VoiceError(
                f"Unknown voice engine {choice!r}. Known engines: "
                f"{', '.join(sorted(_REGISTRY))}."
            )
        return factory(settings)

    problems: list[str] = []
    for name in AUTO_ORDER:
        factory = _REGISTRY.get(name)
        if factory is None:
            continue
        try:
            return factory(settings)
        except VoiceError as exc:
            problems.append(f"{name}: {exc}")
    raise VoiceError("No voice engine could start. " + " | ".join(problems))


def _ensure_registered() -> None:
    """Registration is deferred to first use, and each engine is imported in
    its own try: importing `kokoro_engine` pulls in onnxruntime, and a machine
    that has never wanted a spoken reply should not pay for that at startup -
    nor should a missing optional package stop the other engines registering.
    """
    if _REGISTRY:
        return
    from shamsu.voice.speech import SystemSpeechEngine

    def _system(settings: Any) -> SpeechEngine:
        return SystemSpeechEngine(settings)

    def _kokoro(settings: Any) -> SpeechEngine:
        from shamsu.voice.kokoro_engine import KokoroSpeechEngine

        return KokoroSpeechEngine.from_settings(settings)

    def _piper(settings: Any) -> SpeechEngine:
        from shamsu.voice.piper_engine import PiperSpeechEngine

        return PiperSpeechEngine.from_settings(settings)

    register_engine(ENGINE_KOKORO, _kokoro)
    register_engine(ENGINE_PIPER, _piper)
    register_engine(ENGINE_SYSTEM, _system)


def reset_registry_for_tests() -> None:
    _REGISTRY.clear()
