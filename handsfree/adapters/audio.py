"""Audio adapter: transient pop detector, Vosk speech-to-intent, and speech-activity flag."""

import logging
import math
import struct
import threading
import time
from typing import Callable, Optional
from handsfree.core.events import SignalEvent, SignalType

logger = logging.getLogger("handsfree.adapter.audio")

try:
    import pyaudio
    HAS_PYAUDIO = True
except ImportError:
    HAS_PYAUDIO = False

try:
    import vosk
    HAS_VOSK = True
except ImportError:
    HAS_VOSK = False


class AudioAdapter:
    """Processes microphone audio streams for mouth pops, Vosk speech commands, and voice activity."""

    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_size: int = 512,
        pop_energy_threshold: float = 0.08,
        vosk_model_path: Optional[str] = None,
        on_event: Optional[Callable[[SignalEvent], None]] = None,
        on_speech_activity: Optional[Callable[[bool], None]] = None,
    ):
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.pop_energy_threshold = pop_energy_threshold
        self.vosk_model_path = vosk_model_path
        self.on_event = on_event
        self.on_speech_activity = on_speech_activity

        self.is_running = False
        self.speech_active = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.is_running = True
        self._thread = threading.Thread(target=self._audio_loop, daemon=True, name="AudioThread")
        self._thread.start()
        logger.info("Audio adapter started.")

    def stop(self) -> None:
        self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        logger.info("Audio adapter stopped.")

    def _detect_pop(self, pcm_data: bytes) -> bool:
        count = len(pcm_data) // 2
        if count == 0:
            return False

        shorts = struct.unpack(f"{count}h", pcm_data)
        sum_squares = sum(s * s for s in shorts)
        rms = math.sqrt(sum_squares / count) / 32768.0
        max_val = max(abs(s) for s in shorts) / 32768.0

        if rms > self.pop_energy_threshold and (max_val / (rms + 1e-4) > 3.0):
            return True
        return False

    def _audio_loop(self) -> None:
        if not HAS_PYAUDIO:
            logger.warning("PyAudio not available; running audio adapter in mock mode.")
            while self.is_running:
                time.sleep(0.1)
            return

        pa = pyaudio.PyAudio()
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                input=True,
                frames_per_buffer=self.chunk_size,
            )
        except Exception as e:
            logger.error(f"Failed to open PyAudio input stream: {e}")
            pa.terminate()
            return

        recognizer = None
        if HAS_VOSK and self.vosk_model_path:
            try:
                model = vosk.Model(self.vosk_model_path)
                recognizer = vosk.KaldiRecognizer(model, self.sample_rate)
            except Exception as e:
                logger.warning(f"Could not load Vosk model at '{self.vosk_model_path}': {e}")

        logger.info("Audio input stream opened successfully.")

        while self.is_running:
            try:
                data = stream.read(self.chunk_size, exception_on_overflow=False)
            except Exception:
                continue

            now = time.time()

            # 1. Pop Detection
            if self._detect_pop(data):
                if self.on_event:
                    self.on_event(
                        SignalEvent(
                            signal_type=SignalType.AUDIO_POP,
                            name="pop",
                            value=1.0,
                            timestamp=now,
                        )
                    )

            # 2. Vosk Speech Recognition & Speech Activity Flag
            if recognizer:
                if recognizer.AcceptWaveform(data):
                    import json
                    res = json.loads(recognizer.Result())
                    text = res.get("text", "").strip()
                    if text and self.on_event:
                        self.on_event(
                            SignalEvent(
                                signal_type=SignalType.AUDIO_SPEECH,
                                name="speech_command",
                                value=text,
                                timestamp=now,
                            )
                        )
                        self._set_speech_activity(False)
                else:
                    import json
                    partial = json.loads(recognizer.PartialResult()).get("partial", "").strip()
                    is_speaking = len(partial) > 0
                    if is_speaking != self.speech_active:
                        self._set_speech_activity(is_speaking)

        stream.stop_stream()
        stream.close()
        pa.terminate()

    def _set_speech_activity(self, active: bool) -> None:
        self.speech_active = active
        if self.on_speech_activity:
            self.on_speech_activity(active)

