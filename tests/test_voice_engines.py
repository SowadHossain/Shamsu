"""The engine layer: selection, fallback, and the two promises Kokoro makes.

The promises, both of which a refactor could quietly break and neither of
which shows up in ordinary use:

  * it never asks onnxruntime for the GPU, because the GPU is Ollama's
  * it speaks sentence by sentence, so a long answer starts talking at once

Nothing here loads a real model or opens an audio device. The engines take a
synthesizer and a player as constructor arguments for exactly that reason.
"""
from __future__ import annotations

import threading

import pytest

from shamsu.voice.engines import (
    AUTO_ORDER,
    build_engine,
    register_engine,
    registered_engines,
    reset_registry_for_tests,
)
from shamsu.voice.kokoro_engine import (
    DEFAULT_VOICE,
    KokoroSpeechEngine,
    rate_to_speed,
    split_sentences,
)
from shamsu.voice.models import VoiceError
from shamsu.voice.speech import SpeechPlayer, SpeechSettings


class FakeEngine:
    def __init__(self, name: str) -> None:
        self.name = name
        self.spoken: list[str] = []
        self.stops = 0

    def speak(self, text: str) -> None:
        self.spoken.append(text)

    def stop(self) -> None:
        self.stops += 1


class FakeKokoro:
    """Stands in for kokoro_onnx.Kokoro: one array per call, and a record of
    what it was asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, float]] = []

    def create(self, text, voice, speed=1.0, lang="en-us"):
        import numpy as np

        self.calls.append((text, voice, speed))
        return np.zeros(120, dtype="float32"), 24000

    def get_voices(self):
        return ["am_adam", "bm_george"]


def collecting_player(collected):
    def play(chunks, stop_flag):
        for chunk in chunks:
            collected.append(chunk)

    return play


@pytest.fixture
def no_model_needed(tmp_path, monkeypatch):
    """Let `from_settings` build an engine on a machine that has downloaded
    nothing. Without this the test passes only where the 353MB model happens
    to be on disk, which is not a test - it is a coincidence."""
    model, voices = tmp_path / "m.onnx", tmp_path / "v.bin"
    model.write_bytes(b"x")
    voices.write_bytes(b"x")
    monkeypatch.setattr(
        "shamsu.voice.kokoro_engine.resolve_model_files", lambda: (model, voices)
    )
    return model, voices


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# -- selection -------------------------------------------------------------


def test_auto_takes_the_first_engine_that_can_run_here() -> None:
    kokoro, system = FakeEngine("kokoro"), FakeEngine("system")

    def missing_model(_settings):
        raise VoiceError("no model on disk")

    register_engine("kokoro", lambda _s: kokoro)
    register_engine("piper", missing_model)
    register_engine("system", lambda _s: system)

    assert build_engine(SpeechSettings(engine="auto")) is kokoro


def test_auto_falls_past_engines_that_cannot_run() -> None:
    """A machine with nothing downloaded still speaks - through the floor."""
    system = FakeEngine("system")

    def missing_model(_settings):
        raise VoiceError("no model on disk")

    register_engine("kokoro", missing_model)
    register_engine("piper", missing_model)
    register_engine("system", lambda _s: system)

    assert build_engine(SpeechSettings(engine="auto")) is system


def test_a_named_engine_fails_loudly_rather_than_falling_back() -> None:
    """Someone who ASKED for Kokoro wants to hear that the model is missing.

    Silently handing them SAPI is how you get a bug report saying the new
    voice "did nothing".
    """
    register_engine("kokoro", lambda _s: (_ for _ in ()).throw(VoiceError("no model on disk")))
    register_engine("system", lambda _s: FakeEngine("system"))

    with pytest.raises(VoiceError, match="no model on disk"):
        build_engine(SpeechSettings(engine="kokoro"))


def test_an_unknown_engine_name_names_the_ones_that_exist() -> None:
    register_engine("system", lambda _s: FakeEngine("system"))

    with pytest.raises(VoiceError, match="Unknown voice engine 'kokoru'"):
        build_engine(SpeechSettings(engine="kokoru"))


def test_auto_order_is_best_first_and_ends_at_the_engine_that_always_works() -> None:
    assert AUTO_ORDER[0] == "kokoro"
    assert AUTO_ORDER[-1] == "system"


def test_the_three_engines_register_themselves_on_first_use() -> None:
    build_engine(SpeechSettings(engine="system"))

    assert set(registered_engines()) == {"kokoro", "piper", "system"}


# -- the player delegates --------------------------------------------------


def test_the_player_hands_cleaned_text_to_the_engine() -> None:
    engine = FakeEngine("fake")
    player = SpeechPlayer(SpeechSettings(), engine=engine)

    player.speak("Done.\n\n```python\nprint('x')\n```\nSee `pytest`.")

    assert engine.spoken == ["Done. code block omitted. See pytest."]
    assert player.engine_name == "fake"


def test_stopping_a_player_that_never_spoke_builds_no_engine() -> None:
    """Loading Kokoro costs ~0.8s and ~400MB. Pressing stop must not spend it."""
    player = SpeechPlayer(SpeechSettings())

    player.stop()

    assert player.engine_name == ""


def test_a_disabled_player_never_reaches_its_engine() -> None:
    engine = FakeEngine("fake")

    SpeechPlayer(SpeechSettings(enabled=False), engine=engine).speak("anything")

    assert engine.spoken == []


# -- kokoro ----------------------------------------------------------------


def test_kokoro_speaks_one_sentence_at_a_time() -> None:
    """The whole point of streaming: sentence one is audio before sentence
    three has been synthesized."""
    fake, chunks = FakeKokoro(), []
    engine = KokoroSpeechEngine(kokoro=fake, player=collecting_player(chunks))

    engine.speak("First one. Second one! Third one?")

    assert [call[0] for call in fake.calls] == ["First one.", "Second one!", "Third one?"]
    assert len(chunks) == 3
    assert chunks[0][1] == 24000


def test_kokoro_never_asks_onnxruntime_for_the_gpu(tmp_path, monkeypatch) -> None:
    """kokoro-onnx's own resolver takes EVERY provider once an accelerated
    onnxruntime is installed. The session is built here instead, so the day
    someone pip-installs onnxruntime-gpu for something else, a spoken reply
    still cannot evict the coder model from VRAM.
    """
    import kokoro_onnx
    import onnxruntime as rt

    seen: dict[str, object] = {}

    class FakeSession:
        def __init__(self, path, sess_options=None, providers=None):
            seen["providers"] = providers
            seen["threads"] = getattr(sess_options, "intra_op_num_threads", None)

    monkeypatch.setattr(rt, "InferenceSession", FakeSession)
    monkeypatch.setattr(kokoro_onnx.Kokoro, "from_session", staticmethod(lambda s, v: FakeKokoro()))

    model, voices = tmp_path / "m.onnx", tmp_path / "v.bin"
    model.write_bytes(b"x")
    voices.write_bytes(b"x")
    engine = KokoroSpeechEngine(
        model_path=model, voices_path=voices, threads=8, player=collecting_player([])
    )
    engine.speak("Anything at all.")

    assert seen["providers"] == ["CPUExecutionProvider"]
    assert seen["threads"] == 8


def test_kokoro_carries_the_voice_and_the_rate_through() -> None:
    fake = FakeKokoro()
    engine = KokoroSpeechEngine(
        kokoro=fake, voice="bm_george", speed=rate_to_speed(4), player=collecting_player([])
    )

    engine.speak("Say this.")

    _text, voice, speed = fake.calls[0]
    assert voice == "bm_george"
    assert speed == pytest.approx(1.2)


def test_the_default_voice_is_the_one_shamsu_speaks_with(no_model_needed) -> None:
    """Changing this changes what SHAMSU sounds like on every machine that has
    not set SHAMSU_VOICE_NAME, which is most of them."""
    fake = FakeKokoro()
    engine = KokoroSpeechEngine.from_settings(SpeechSettings())
    engine._kokoro = fake

    engine.speak("Who is speaking.")

    assert fake.calls[0][1] == DEFAULT_VOICE == "am_adam"


def test_an_explicit_voice_name_beats_the_default(no_model_needed) -> None:
    fake = FakeKokoro()
    engine = KokoroSpeechEngine.from_settings(SpeechSettings(voice_name="bf_emma"))
    engine._kokoro = fake

    engine.speak("Who is speaking.")

    assert fake.calls[0][1] == "bf_emma"


def test_a_missing_model_is_a_voice_error_not_a_crash(monkeypatch) -> None:
    monkeypatch.setattr("shamsu.voice.kokoro_engine.resolve_model_files", lambda: None)

    with pytest.raises(VoiceError, match="No Kokoro model found"):
        KokoroSpeechEngine()


def test_stopping_kokoro_cuts_synthesis_rather_than_finishing_the_paragraph() -> None:
    fake, chunks = FakeKokoro(), []

    def stop_after_first(stream, stop_flag):
        for chunk in stream:
            chunks.append(chunk)
            stop_flag.set()

    engine = KokoroSpeechEngine(kokoro=fake, player=stop_after_first)
    engine.speak("One. Two. Three. Four. Five. Six.")

    assert len(chunks) == 1
    # The producer runs one sentence ahead, so it may have started the second
    # before the flag landed - but it must not have synthesized all six.
    assert len(fake.calls) < 6


def test_rate_maps_onto_speed_and_stays_inside_what_kokoro_accepts() -> None:
    assert rate_to_speed(0) == 1.0
    assert rate_to_speed(10) == pytest.approx(1.5)
    assert rate_to_speed(-10) == pytest.approx(0.5)
    assert rate_to_speed(999) <= 2.0


def test_sentence_splitting_keeps_the_punctuation() -> None:
    """The mark is not noise - Kokoro reads "?" with a rising intonation and
    "." without, so stripping it flattens every question."""
    assert split_sentences("Is it? It is. Good!") == ["Is it?", "It is.", "Good!"]
    assert split_sentences("no terminator here") == ["no terminator here"]


def test_playback_aborts_on_stop_and_drains_when_finished() -> None:
    import numpy as np

    from shamsu.voice.playback import play_pcm_chunks

    events: list[str] = []

    class FakeStream:
        def start(self):
            events.append("start")

        def write(self, _data):
            events.append("write")

        def abort(self):
            events.append("abort")

        def stop(self):
            events.append("stop")

        def close(self):
            events.append("close")

    class FakeSd:
        RawOutputStream = staticmethod(lambda **_kw: FakeStream())

    import sys

    sys.modules["sounddevice"] = FakeSd  # type: ignore[assignment]
    try:
        chunk = (np.zeros(4, dtype="int16"), 24000, 1)
        flag = threading.Event()
        play_pcm_chunks(iter([chunk]), flag)
        assert events == ["start", "write", "stop", "close"]

        events.clear()
        flag.set()
        play_pcm_chunks(iter([chunk]), flag)
        assert events == []
    finally:
        del sys.modules["sounddevice"]


def test_a_long_chunk_is_written_in_blocks_so_stopping_is_noticed() -> None:
    """`write` blocks until the device takes the data. One sentence per call
    meant the stop flag went unread for the length of that sentence - measured
    at 2.1s between pressing skip and the sound stopping, against 0.45s once
    the writes were split."""
    import sys

    import numpy as np

    from shamsu.voice.playback import BLOCK_SECONDS, play_pcm_chunks

    writes: list[int] = []

    class FakeStream:
        def start(self):
            return None

        def write(self, data):
            writes.append(len(data))

        def abort(self):
            return None

        def stop(self):
            return None

        def close(self):
            return None

    class FakeSd:
        RawOutputStream = staticmethod(lambda **_kw: FakeStream())

    sample_rate = 24000
    seconds = 4
    chunk = (np.zeros(sample_rate * seconds, dtype="int16"), sample_rate, 1)

    sys.modules["sounddevice"] = FakeSd  # type: ignore[assignment]
    try:
        play_pcm_chunks(iter([chunk]), threading.Event())
    finally:
        del sys.modules["sounddevice"]

    assert len(writes) == pytest.approx(seconds / BLOCK_SECONDS, abs=1)


def test_stopping_part_way_through_a_chunk_drops_the_rest() -> None:
    import sys

    import numpy as np

    from shamsu.voice.playback import play_pcm_chunks

    flag = threading.Event()
    writes: list[int] = []

    class FakeStream:
        def start(self):
            return None

        def write(self, data):
            writes.append(len(data))
            if len(writes) == 3:
                # The user presses F6 while the third block is playing.
                flag.set()

        def abort(self):
            writes.append(-1)

        def stop(self):
            return None

        def close(self):
            return None

    class FakeSd:
        RawOutputStream = staticmethod(lambda **_kw: FakeStream())

    chunk = (np.zeros(24000 * 10, dtype="int16"), 24000, 1)

    sys.modules["sounddevice"] = FakeSd  # type: ignore[assignment]
    try:
        play_pcm_chunks(iter([chunk]), flag)
    finally:
        del sys.modules["sounddevice"]

    # Three blocks played, then it stopped - not a hundred. A block is
    # BLOCK_SECONDS of 24kHz mono at two bytes a sample: 0.1 * 24000 * 2.
    assert writes == [4800, 4800, 4800, -1]
