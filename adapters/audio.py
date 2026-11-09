"""adapters/audio.py – Pop-click detector (Step 12).

Turns a stream of raw PCM audio into SignalEvents for the main polling loop.
No threads, no timers — Binore's 60 Hz loop calls poll() every tick.

Pop detection algorithm
-----------------------
1. Compute RMS and peak of each chunk.
2. Fire if RMS > *rms_threshold* AND peak/RMS > *crest_threshold*  (impulsive).
3. Enforce a *refractory_ms* (default 300 ms) cooldown after every pop.

Injectable audio source
-----------------------
Pass ``audio_fn`` (a zero-argument callable that returns ``bytes`` of raw
16-bit little-endian mono PCM) to skip real hardware.  When ``audio_fn``
is None we try to import ``sounddevice``; if that is unavailable we fall
back to a silent mock so the module loads everywhere.

Vosk voice commands
-------------------
Off by default (``enable_vosk=False``).  When enabled, Vosk is loaded lazily
and runs in the *same* poll() call — no extra threads, never blocks pop
detection (pop check runs first, Vosk only gets the chunk that wasn't a pop).
"""
from __future__ import annotations

import struct
from typing import Callable, List, Optional

from core.events import SignalEvent

# ---------------------------------------------------------------------------
# Constants / tuneable defaults
# ---------------------------------------------------------------------------
SAMPLE_RATE: int = 16_000          # Hz — standard for voice / pop detection
CHUNK_FRAMES: int = 512            # ~32 ms per chunk at 16 kHz
BYTES_PER_SAMPLE: int = 2          # 16-bit signed int
CHUNK_BYTES: int = CHUNK_FRAMES * BYTES_PER_SAMPLE

RMS_THRESHOLD: float = 0.08        # 0–1 normalised; below this → silence
CREST_THRESHOLD: float = 3.5       # peak/RMS; impulsive events are spiky
REFRACTORY_MS: float = 300.0       # ms cooldown after each pop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rms_peak(raw: bytes) -> tuple[float, float]:
    """Return (rms, peak) in [0, 1] from raw 16-bit LE mono PCM bytes."""
    n = len(raw) // BYTES_PER_SAMPLE
    if n == 0:
        return 0.0, 0.0
    samples = struct.unpack_from(f"<{n}h", raw, 0)
    norm = 32768.0
    sq_sum = sum(s * s for s in samples)
    rms = (sq_sum / n) ** 0.5 / norm
    peak = max(abs(s) for s in samples) / norm
    return rms, peak


def _is_pop(raw: bytes,
            rms_threshold: float = RMS_THRESHOLD,
            crest_threshold: float = CREST_THRESHOLD) -> bool:
    """Return True if *raw* looks like a pop/click."""
    rms, peak = _rms_peak(raw)
    if rms < rms_threshold:
        return False
    crest = peak / rms if rms > 0 else 0.0
    return crest >= crest_threshold


# ---------------------------------------------------------------------------
# AudioSource
# ---------------------------------------------------------------------------

class AudioSource:
    """Poll-based audio pop detector.

    Parameters
    ----------
    audio_fn:
        Zero-argument callable returning ``bytes`` of raw 16-bit LE mono PCM.
        When None, tries ``sounddevice``; falls back to silent stub.
    sample_rate:
        PCM sample rate in Hz (must match ``audio_fn`` output).
    rms_threshold:
        Minimum RMS (0–1) to consider a chunk loud enough.
    crest_threshold:
        Minimum peak/RMS ratio to consider a chunk impulsive.
    refractory_ms:
        Cooldown in milliseconds after each detected pop.
    enable_vosk:
        If True, attempt to load Vosk and emit voice-command events.
        Has no effect when Vosk is not installed.
    """

    def __init__(
        self,
        audio_fn: Optional[Callable[[], bytes]] = None,
        *,
        sample_rate: int = SAMPLE_RATE,
        rms_threshold: float = RMS_THRESHOLD,
        crest_threshold: float = CREST_THRESHOLD,
        refractory_ms: float = REFRACTORY_MS,
        enable_vosk: bool = False,
    ) -> None:
        self._sample_rate = sample_rate
        self._rms_thr = rms_threshold
        self._crest_thr = crest_threshold
        self._refractory = refractory_ms / 1000.0   # convert to seconds
        self._enable_vosk = enable_vosk
        self._last_pop: float = -1e9                 # time of last pop
        self._vosk_rec = None                        # lazy-loaded Vosk recogniser

        # --- set up audio_fn ---------------------------------------------------
        if audio_fn is not None:
            self._audio_fn: Callable[[], bytes] = audio_fn
        else:
            self._audio_fn = self._init_sounddevice(sample_rate)

        # --- Vosk (optional, behind flag) --------------------------------------
        if enable_vosk:
            self._init_vosk(sample_rate)

    # ------------------------------------------------------------------
    # Internal init helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _init_sounddevice(sample_rate: int) -> Callable[[], bytes]:
        """Return a callable that reads one chunk from the default input."""
        try:
            import sounddevice as sd  # type: ignore

            stream = sd.RawInputStream(
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=CHUNK_FRAMES,
            )
            stream.start()

            def _read() -> bytes:
                data, _ = stream.read(CHUNK_FRAMES)
                return bytes(data)

            return _read
        except Exception:
            # No sounddevice or no mic → silent stub so module always imports.
            return lambda: b"\x00" * CHUNK_BYTES

    def _init_vosk(self, sample_rate: int) -> None:
        """Lazily set up Vosk recogniser if Vosk is installed."""
        try:
            import vosk  # type: ignore  # noqa: F401

            # Vosk needs a model directory; skip silently if none found.
            import os
            model_path = os.environ.get("VOSK_MODEL_PATH", "vosk-model")
            if not os.path.isdir(model_path):
                return
            model = vosk.Model(model_path)
            self._vosk_rec = vosk.KaldiRecognizer(model, sample_rate)
        except Exception:
            pass  # Vosk not installed or model missing — no-op

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def poll(self, now: float) -> List[SignalEvent]:
        """Read one PCM chunk and return any events detected."""
        events: List[SignalEvent] = []

        raw = self._audio_fn()
        if not raw:
            return events

        # --- pop check (always runs first) --------------------------------
        if _is_pop(raw, self._rms_thr, self._crest_thr):
            if now - self._last_pop >= self._refractory:
                events.append(
                    SignalEvent(src="mic", name="pop", value=1.0, t=now)
                )
                self._last_pop = now
            # Either way: don't pass a pop chunk to Vosk
            return events

        # --- Vosk voice command (only when enabled & no pop) ---------------
        if self._vosk_rec is not None:
            self._vosk_rec.AcceptWaveform(raw)
            # (In a full implementation we'd parse the result here and emit
            #  SignalEvents for recognised words.  Omitted: Vosk model needed.)

        return events

    def close(self) -> None:
        """Release resources (noop for injected audio_fn)."""
        pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_source(
    audio_fn: Optional[Callable[[], bytes]] = None,
    enable_vosk: bool = False,
) -> AudioSource:
    """Return a ready-to-poll AudioSource.

    Pass ``audio_fn`` for tests or custom sources; omit for real hardware.
    Raises ``RuntimeError`` if no mic is available and ``audio_fn`` is None,
    so dry-run tests are never accidentally stalled waiting for hardware.
    """
    if audio_fn is None:
        try:
            import sounddevice as sd  # type: ignore
            # Quick check: will it actually open a stream?
            sd.query_devices(kind="input")
        except Exception as exc:
            raise RuntimeError(
                f"No audio input available ({exc}). "
                "Pass audio_fn= for tests or set up a microphone."
            ) from exc

    return AudioSource(audio_fn=audio_fn, enable_vosk=enable_vosk)

