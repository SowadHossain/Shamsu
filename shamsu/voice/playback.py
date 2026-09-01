"""Push PCM at the speakers, and stop the moment someone interrupts.

Both neural engines produce the same thing - little arrays of 16-bit samples,
one per sentence - so they share one player rather than each growing its own
sounddevice handling and its own subtly different idea of what `stop()` means.

The distinction that matters here is `stop()` versus `abort()`. A reply that
finished has already handed over every sample it had, and cutting the buffer
then would clip the last word; an interrupted one must go quiet NOW, because
the user pressed a key to make it shut up. The flag decides which.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

from shamsu.voice.models import VoiceError

#: One (samples, sample_rate, channels) triple per sentence.
PcmChunk = tuple[Any, int, int]

#: How much audio goes to the device per write. A whole sentence in one call
#: blocks until the device has taken all of it, which measured 2.1s between
#: pressing skip and the sound stopping - the flag was set the entire time and
#: nothing was reading it. A tenth of a second is small enough that stopping
#: feels immediate and large enough that the loop is not the bottleneck.
BLOCK_SECONDS = 0.1


def play_pcm_chunks(chunks: Iterator[PcmChunk], stop_flag: threading.Event) -> None:
    try:
        import sounddevice as sd
    except Exception as exc:
        raise VoiceError(
            "Speaking needs the sounddevice package. Install the voice extra."
        ) from exc

    stream = None
    try:
        for samples, sample_rate, channels in chunks:
            if stop_flag.is_set():
                break
            if stream is None:
                stream = sd.RawOutputStream(
                    samplerate=sample_rate, channels=channels, dtype="int16"
                )
                stream.start()
            # Written in blocks, checking between them: `write` blocks until
            # the device accepts the data, so one sentence per call means the
            # stop flag is not looked at for the length of that sentence.
            step = max(1, int(sample_rate * BLOCK_SECONDS)) * max(1, channels)
            for start in range(0, len(samples), step):
                if stop_flag.is_set():
                    break
                stream.write(samples[start : start + step].tobytes())
    except Exception as exc:
        raise VoiceError(f"Voice playback failed: {exc}") from exc
    finally:
        if stream is not None:
            if stop_flag.is_set():
                stream.abort()
            else:
                stream.stop()
            stream.close()
