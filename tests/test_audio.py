"""tests/test_audio.py – Tests for adapters/audio.py pop detector (Step 12).

All tests are hardware-free: they inject synthetic PCM bytes via audio_fn.
No real microphone, no sounddevice, no threads.
"""
from __future__ import annotations

import struct
import pytest

from adapters.audio import AudioSource, _is_pop, CHUNK_FRAMES, BYTES_PER_SAMPLE


# ---------------------------------------------------------------------------
# PCM helpers
# ---------------------------------------------------------------------------

def _make_silence(frames: int = CHUNK_FRAMES) -> bytes:
    """Return *frames* zero-amplitude 16-bit samples."""
    return b"\x00" * (frames * BYTES_PER_SAMPLE)


def _make_spike(amplitude: float = 0.9, frames: int = CHUNK_FRAMES) -> bytes:
    """Return a PCM chunk that looks like a real pop / click.

    A short burst of *~10* high-amplitude samples followed by silence produces:
    - RMS well above the 0.08 threshold (burst carries energy)
    - peak/RMS >> 3.5 (impulsive crest factor)
    This models a mouth-pop or switch-click as heard by a microphone.
    """
    BURST = 10
    samples = [0] * frames
    for i in range(min(BURST, frames)):
        samples[i] = int(amplitude * 32767)
    # Short decay tail (keeps crest factor high, adds tiny bit of energy)
    for i in range(BURST, min(BURST + 4, frames)):
        samples[i] = int(amplitude * 32767 * 0.08)
    return struct.pack(f"<{frames}h", *samples)


def _make_loud_sustain(amplitude: float = 0.5, frames: int = CHUNK_FRAMES) -> bytes:
    """Sustained loud tone — high RMS but low crest → NOT a pop."""
    val = int(amplitude * 32767)
    samples = [val] * frames
    return struct.pack(f"<{frames}h", *samples)


# ---------------------------------------------------------------------------
# Unit tests for _is_pop()
# ---------------------------------------------------------------------------

class TestIsPopHelper:
    def test_silence_is_not_pop(self):
        assert _is_pop(_make_silence()) is False

    def test_spike_is_pop(self):
        assert _is_pop(_make_spike(0.9)) is True

    def test_sustained_loud_is_not_pop(self):
        # High RMS but low crest factor → not impulsive
        assert _is_pop(_make_loud_sustain(0.5)) is False

    def test_empty_bytes_is_not_pop(self):
        assert _is_pop(b"") is False

    def test_low_amplitude_spike_is_not_pop(self):
        # Very quiet spike — RMS below threshold
        assert _is_pop(_make_spike(0.001)) is False


# ---------------------------------------------------------------------------
# AudioSource integration tests
# ---------------------------------------------------------------------------

class TestAudioSourcePop:
    """AudioSource.poll() with injected audio_fn — no hardware."""

    def _source(self, chunks: list[bytes], refractory_ms: float = 300.0) -> AudioSource:
        """Create an AudioSource that returns chunks in sequence."""
        it = iter(chunks)

        def audio_fn() -> bytes:
            return next(it, _make_silence())

        return AudioSource(audio_fn=audio_fn, refractory_ms=refractory_ms)

    # 1. spike → one pop event emitted
    def test_spike_emits_pop(self):
        src = self._source([_make_spike()])
        events = src.poll(now=1.0)
        assert len(events) == 1
        ev = events[0]
        assert ev.src == "mic"
        assert ev.name == "pop"
        assert ev.value == 1.0
        assert ev.t == 1.0

    # 2. silence → no events
    def test_silence_emits_nothing(self):
        src = self._source([_make_silence()])
        events = src.poll(now=1.0)
        assert events == []

    # 3. two spikes within refractory → only ONE pop
    def test_refractory_suppresses_second_pop(self):
        src = self._source(
            [_make_spike(), _make_spike()],
            refractory_ms=300.0,
        )
        events1 = src.poll(now=1.000)   # fires
        events2 = src.poll(now=1.100)   # 100 ms later — still in refractory
        assert len(events1) == 1
        assert len(events2) == 0        # suppressed

    # 4. two spikes 400 ms apart → TWO pops
    def test_two_pops_400ms_apart(self):
        src = self._source(
            [_make_spike(), _make_spike()],
            refractory_ms=300.0,
        )
        events1 = src.poll(now=0.0)
        events2 = src.poll(now=0.4)    # 400 ms > 300 ms refractory
        assert len(events1) == 1
        assert len(events2) == 1

    # 5. silence between two spikes — refractory still enforced by wall time
    def test_silence_between_spikes_no_spurious_events(self):
        src = self._source(
            [_make_spike(), _make_silence(), _make_silence(), _make_spike()],
            refractory_ms=300.0,
        )
        e1 = src.poll(now=0.0)
        e2 = src.poll(now=0.1)
        e3 = src.poll(now=0.2)
        e4 = src.poll(now=0.5)   # 500 ms after first → fires again
        assert len(e1) == 1
        assert len(e2) == 0
        assert len(e3) == 0
        assert len(e4) == 1

    # 6. sustained loud → no pop (wrong crest factor)
    def test_sustained_loud_no_pop(self):
        src = self._source([_make_loud_sustain()])
        events = src.poll(now=1.0)
        assert events == []

    # 7. empty bytes from audio_fn → graceful, no crash
    def test_empty_chunk_no_crash(self):
        src = self._source([b""])
        events = src.poll(now=1.0)
        assert events == []

    # 8. vosk flag False → no Vosk-related attributes loaded
    def test_vosk_disabled_by_default(self):
        src = self._source([_make_silence()])
        # _vosk_rec should be None (flag off)
        assert src._vosk_rec is None

    # 9. multiple consecutive pops after refractory all fire
    def test_many_pops_each_after_refractory(self):
        chunks = [_make_spike()] * 5
        src = self._source(chunks, refractory_ms=100.0)
        results = [src.poll(now=i * 0.15) for i in range(5)]  # 150ms gaps
        # Every poll is 150ms apart > 100ms refractory → all 5 should fire
        total = sum(len(r) for r in results)
        assert total == 5

    # 10. refractory = 0 → every spike fires
    def test_zero_refractory_every_spike_fires(self):
        chunks = [_make_spike()] * 3
        src = self._source(chunks, refractory_ms=0.0)
        results = [src.poll(now=i * 0.001) for i in range(3)]
        total = sum(len(r) for r in results)
        assert total == 3

    # 11. close() does not raise
    def test_close_no_error(self):
        src = self._source([_make_silence()])
        src.close()  # should not raise


# ---------------------------------------------------------------------------
# create_source() factory
# ---------------------------------------------------------------------------

class TestCreateSource:
    def test_factory_with_audio_fn_returns_source(self):
        """create_source(audio_fn=...) should always succeed (no hardware)."""
        from adapters.audio import create_source
        src = create_source(audio_fn=lambda: _make_silence())
        assert isinstance(src, AudioSource)

    def test_factory_without_audio_fn_raises_when_no_mic(self):
        """create_source() with no args should raise RuntimeError
        when sounddevice is unavailable or no input device exists."""
        from adapters.audio import create_source
        import unittest.mock as mock
        # Simulate sounddevice not installed
        with mock.patch.dict("sys.modules", {"sounddevice": None}):
            with pytest.raises((RuntimeError, ImportError)):
                create_source()
