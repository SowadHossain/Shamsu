"""`python -m shamsu.voice` - download a voice, or hear the one you have.

Downloading is a command rather than something a spoken reply does on its own:
353MB is not a thing to start behind someone's back while they are waiting for
an answer. Until it is run, `auto` speaks through whatever lesser engine the
machine already has.

    python -m shamsu.voice download          # fetch the Kokoro model (353MB)
    python -m shamsu.voice voices            # list the 54 voices
    python -m shamsu.voice say "hello there" # hear the current settings
    python -m shamsu.voice status            # engine, models, settings
"""
from __future__ import annotations

import sys

from shamsu.voice.models import VoiceError


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    command = (args[0] if args else "status").lower()

    from shamsu.voice import kokoro_engine
    from shamsu.voice.speech import SpeechPlayer, load_speech_settings

    settings = load_speech_settings()

    if command == "download":
        print("Downloading the Kokoro voice model (about 353MB)...")
        try:
            model, voices = kokoro_engine.download_model()
        except VoiceError as exc:
            print(f"failed: {exc}", file=sys.stderr)
            return 1
        print(f"model:  {model}")
        print(f"voices: {voices}")
        return 0

    if command == "voices":
        try:
            names = kokoro_engine.KokoroSpeechEngine.from_settings(settings).voices()
        except VoiceError as exc:
            print(f"failed: {exc}", file=sys.stderr)
            return 1
        print(f"{len(names)} voices. Set one with SHAMSU_VOICE_NAME.")
        for name in names:
            print(f"  {name}")
        return 0

    if command == "say":
        text = " ".join(args[1:]) or "SHAMSU is speaking through the selected engine."
        player = SpeechPlayer(settings)
        try:
            player.speak(text)
        except VoiceError as exc:
            print(f"failed: {exc}", file=sys.stderr)
            return 1
        print(f"spoke through: {player.engine_name}")
        return 0

    if command == "status":
        from shamsu.voice.engines import AUTO_ORDER, build_engine

        print(f"settings: {settings}")
        found = kokoro_engine.resolve_model_files()
        print(f"kokoro models: {found if found else 'not downloaded'}")
        print(f"engine order: {' -> '.join(AUTO_ORDER)}")
        try:
            print(f"would speak through: {build_engine(settings).name}")
        except VoiceError as exc:
            print(f"would speak through: nothing ({exc})")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":  # pragma: no cover - the entry point itself
    raise SystemExit(main())
