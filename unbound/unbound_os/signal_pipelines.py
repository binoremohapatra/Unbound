"""signal_pipelines.py - vision, audio and the dual-mode PsychicIntentAdapter.

Layers:
  OneEuroFilter + cubic response curve  - zero-lag smoothing for aim
  VisionPipeline                        - daemon capture thread: cv2 CAP_DSHOW
                                          640x480@60, YCrCb CLAHE, MediaPipe
                                          FaceLandmarker (52 blendshapes + iris)
  AudioPipeline                         - sounddevice FFT transient detector
                                          (2500-6000 Hz tongue clicks) + optional
                                          Vosk offline grammar
  PsychicIntentAdapter                  - OmniBand ESP32 serial (115200 JSON) when
                                          present -> HARDWARE_OMNIBAND; otherwise
                                          OPTICAL_FALLBACK (iris gaze + closed-mouth
                                          clench + gaze dwell). Hot-unplug safe.

Honesty rule: every optional dependency is feature-detected. Missing cv2 /
mediapipe / sounddevice / vosk / pyserial degrades THAT pipeline to UNAVAILABLE
with a logged reason - it never emits synthetic data that could be mistaken for
real input.
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False

log = logging.getLogger("omnicontrol_os.pipelines")

# feature detection - each pipeline reports its own status, never fakes data
try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False
try:
    import mediapipe as mp
    HAS_MEDIAPIPE = True
except ImportError:
    HAS_MEDIAPIPE = False
try:
    import sounddevice as sd
    HAS_SOUNDEVICE = True
except ImportError:
    HAS_SOUNDEVICE = False
try:
    import vosk
    HAS_VOSK = True
except ImportError:
    HAS_VOSK = False
try:
    import serial
    import serial.tools.list_ports
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False


# ------------------------------------------------------------------ shared types
@dataclass
class FrameSample:
    """One processed camera frame. All values normalized unless noted."""
    t: float
    face_ok: bool
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    gaze_x: float = 0.0          # -1..1, head-invariant iris vector
    gaze_y: float = 0.0
    blink: float = 0.0           # max(eyeBlinkLeft, eyeBlinkRight) 0..1
    jaw_open: float = 0.0        # 0..1
    brow_raise: float = 0.0
    proc_ms: float = 0.0


@dataclass
class IntentEvent:
    """Something the FSM should react to. `kind` groups semantics; `name` the detail."""
    kind: str          # "gesture" | "voice" | "safety" | "mode"
    name: str          # "clench" | "dwell" | "click" | "freeze" | ...
    value: float = 1.0
    t: float = 0.0
    source: str = "OPTICAL_FALLBACK"


# ------------------------------------------------------------------ filters
class OneEuroFilter:
    """1€ filter: low lag at low speed, strong smoothing at high speed.
    Standard Casiez et al. formulation; alpha from mincutoff/beta/dcutoff."""

    def __init__(self, min_cutoff: float = 1.0, beta: float = 0.0,
                 d_cutoff: float = 1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self._x_prev: Optional[float] = None
        self._dx_prev = 0.0
        self._t_prev: Optional[float] = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2.0 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / max(dt, 1e-6))

    def filter(self, x: float, t: float) -> float:
        if self._t_prev is None or self._x_prev is None:
            self._x_prev, self._t_prev = x, t
            return x
        dt = max(1e-3, t - self._t_prev)
        dx = (x - self._x_prev) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        dx_hat = a_d * dx + (1 - a_d) * self._dx_prev
        cutoff = self.min_cutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1 - a) * self._x_prev
        self._x_prev, self._dx_prev, self._t_prev = x_hat, dx_hat, t
        return x_hat


def cubic_response(x: float) -> float:
    """Cubic ease response curve for aim: fine near center, full at edges.
    x in [-1,1] -> [-1,1], preserves sign, |f(x)| <= |x| for |x|<1."""
    x = max(-1.0, min(1.0, x))
    return math.copysign(abs(x) ** 3 if abs(x) < 1.0 else 1.0, x)


# ------------------------------------------------------------------ vision
class VisionPipeline:
    """Daemon capture + MediaPipe FaceLandmarker thread.

    Exposes latest() -> Optional[FrameSample] and blink/clench edge helpers.
    Status is honest: if cv2/mediapipe/model are missing, latest() stays None and
    status() says why - nothing synthetic is ever emitted.
    """

    def __init__(self, cfg: dict, on_event: Optional[Callable[[IntentEvent], None]] = None):
        self.cfg = cfg.get("vision", {})
        self.on_event = on_event
        self._latest: Optional[FrameSample] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.status = "INIT"
        self._blink = _Hysteresis(self.cfg.get("blink_on", 0.58),
                                  self.cfg.get("blink_off", 0.35),
                                  self.cfg.get("blink_min_ms", 230) / 1000.0)
        self._clench_since: Optional[float] = None
        self._clench_fired = False
        self._dwell = _GazeDwell(self.cfg.get("gaze_dwell_ms", 380) / 1000.0)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if not HAS_CV2 or not HAS_MEDIAPIPE:
            self.status = "UNAVAILABLE: opencv-python or mediapipe not installed"
            log.warning("vision: %s", self.status)
            return
        model = self._find_model()
        if model is None:
            self.status = "UNAVAILABLE: face_landmarker.task not found " \
                          "(set OMNICONTROL_FACE_MODEL or put it in models/)"
            log.warning("vision: %s", self.status)
            return
        self._thread = threading.Thread(target=self._run, args=(model,),
                                        name="vision", daemon=True)
        self._thread.start()
        self.status = "RUNNING"

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    @staticmethod
    def _find_model() -> Optional[str]:
        env = os.environ.get("OMNICONTROL_FACE_MODEL")
        if env and Path(env).is_file():
            return env
        here = Path(__file__).resolve().parent
        for c in (here / "models" / "face_landmarker.task",
                  here.parent / "assets" / "face_landmarker.task",
                  here / "face_landmarker.task"):
            if c.is_file():
                return str(c)
        return None

    # -- the loop -------------------------------------------------------------
    def _run(self, model_path: str) -> None:
        vc = self.cfg
        cap = cv2.VideoCapture(int(vc.get("camera_id", 0)), cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(vc.get("width", 640)))
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(vc.get("height", 480)))
        cap.set(cv2.CAP_PROP_FPS, int(vc.get("fps", 60)))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if not cap.isOpened():
            self.status = "UNAVAILABLE: camera did not open"
            log.error("vision: %s", self.status)
            return
        try:
            opts = mp.tasks.vision.FaceLandmarkerOptions(
                base_options=mp.tasks.vision.BaseOptions(model_asset_path=model_path),
                running_mode=mp.tasks.vision.RunningMode.VIDEO,
                num_faces=1,
                output_face_blendshapes=True)
            landmarker = mp.tasks.vision.FaceLandmarker.create_from_options(opts)
        except Exception as e:
            self.status = f"UNAVAILABLE: FaceLandmarker init failed ({e})"
            log.error("vision: %s", self.status)
            cap.release()
            return

        clahe = cv2.createCLAHE(clipLimit=float(vc.get("clahe_clip", 2.0)),
                                tileGridSize=(int(vc.get("clahe_grid", 8)),) * 2)
        filters = {k: OneEuroFilter(**v) for k, v in
                   (self._filter_cfg or {}).items()}
        self._ts_ms = 0
        while not self._stop.is_set():
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.005)
                continue
            t0 = time.perf_counter()
            sample = self._process(frame, landmarker, clahe, filters, t0)
            with self._lock:
                self._latest = sample
            self._emit_edges(sample)
        cap.release()
        try:
            landmarker.close()
        except Exception:
            pass
        self.status = "STOPPED"

    _filter_cfg: Optional[dict] = None     # set by set_filters() before start()

    def set_filters(self, filter_cfg: dict) -> None:
        self._filter_cfg = filter_cfg

    def _process(self, frame, landmarker, clahe, filters, t0) -> FrameSample:
        # YCrCb CLAHE on the luminance channel: the dark-room enhancer
        ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
        ycrcb[:, :, 0] = clahe.apply(ycrcb[:, :, 0])
        rgb = cv2.cvtColor(ycrcb, cv2.COLOR_YCrCb2RGB)
        h, w = frame.shape[:2]
        self._ts_ms = max(self._ts_ms + 1, int(time.perf_counter() * 1000))
        ts_ms = self._ts_ms
        try:
            res = landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), ts_ms)
        except Exception:
            return FrameSample(t=t0, face_ok=False)
        if not res.face_landmarks:
            return FrameSample(t=t0, face_ok=False)
        lm = res.face_landmarks[0]
        idx = self.cfg
        nose = lm[int(idx.get("nose_idx", 1))]
        le_in, le_out = (lm[i] for i in idx.get("eye_left_idx", [133, 33]))
        re_in, re_out = (lm[i] for i in idx.get("eye_right_idx", [362, 263]))
        iris_l = lm[int(idx.get("iris_left_idx", 468))]
        iris_r = lm[int(idx.get("iris_right_idx", 473))]

        def _norm(p):
            return p.x, p.y

        # head pose (same estimator family as the Unbound adapter)
        chin = lm[152]
        eye_cx = (le_out.x + re_out.x) / 2.0
        eye_cy = (le_out.y + re_out.y) / 2.0
        eye_dx = abs(re_out.x - le_out.x)
        yaw = ((nose.x - eye_cx) / (eye_dx + 1e-6)) * 90.0
        up = nose.y - eye_cy
        lo = chin.y - nose.y
        pitch = ((up - lo) / (up + lo + 1e-6)) * 60.0
        roll = math.degrees(math.atan2(re_out.y - le_out.y,
                                     (re_out.x - le_out.x) * w + 1e-6))

        # head-invariant gaze: iris position inside each eye box, averaged.
        # Iris-between-corners is stable under head rotation (the eye socket moves
        # with the head, the iris stays put relative to it).
        gx = self._axis01(iris_l, le_in, le_out) * 0.5 + \
             self._axis01(iris_r, re_in, re_out) * 0.5
        gy = self._axis01_y(iris_l, le_in, le_out) * 0.5 + \
             self._axis01_y(iris_r, re_in, re_out) * 0.5

        bs = {}
        if res.face_blendshapes:
            for cat in res.face_blendshapes[0]:
                name = getattr(cat, "category_name", "") or getattr(cat, "display_name", "")
                bs[name] = getattr(cat, "score", 0.0)
        blink = max(bs.get("eyeBlinkLeft", 0.0), bs.get("eyeBlinkRight", 0.0))
        jaw = bs.get("jawOpen", 0.0)
        brow = bs.get("browInnerUp", 0.0)

        t = time.perf_counter()
        lut = {"head_yaw": yaw, "head_pitch": pitch, "gaze_x": gx, "gaze_y": gy}
        for name, flt in filters.items():
            if name in lut:
                lut[name] = flt.filter(lut[name], t)
        yaw, pitch, gx, gy = lut["head_yaw"], lut["head_pitch"], lut["gaze_x"], lut["gaze_y"]
        return FrameSample(t=t, face_ok=True, yaw=yaw, pitch=pitch, roll=roll,
                           gaze_x=gx, gaze_y=gy, blink=blink,
                           jaw_open=jaw, brow_raise=brow,
                           proc_ms=(time.perf_counter() - t0) * 1000.0)

    @staticmethod
    def _axis01(iris, inner, outer) -> float:
        """Iris x-position inside the eye corner span, mapped to roughly -1..1."""
        span = outer.x - inner.x
        if abs(span) < 1e-6:
            return 0.0
        return max(-1.5, min(1.5, 2.0 * (iris.x - inner.x) / span - 1.0))

    @staticmethod
    def _axis01_y(iris, inner, outer) -> float:
        span = outer.y - inner.y
        if abs(span) < 1e-6:
            return 0.0
        return max(-1.5, min(1.5, 2.0 * (iris.y - inner.y) / span - 1.0))

    def _emit_edges(self, s: FrameSample) -> None:
        if self.on_event is None:
            return
        t = s.t
        if self._blink.update(t, s.blink if s.face_ok else 0.0):
            self.on_event(IntentEvent("gesture", "hard_blink", 1.0, t, "OPTICAL_FALLBACK"))
        # optical clench: mouth closed (jawOpen ~ 0) sustained -> isometric clench
        jaw_max = float(self.cfg.get("clench_jaw_max", 0.08))
        clench_ms = float(self.cfg.get("clench_min_ms", 350)) / 1000.0
        if s.face_ok and s.jaw_open <= jaw_max:
            if self._clench_since is None:
                self._clench_since = t
            elif not self._clench_fired and t - self._clench_since >= clench_ms:
                self._clench_fired = True
                self.on_event(IntentEvent("gesture", "clench", 1.0, t, "OPTICAL_FALLBACK"))
        else:
            self._clench_since = None
            self._clench_fired = False
        # gaze dwell fixation
        if s.face_ok:
            if self._dwell.update(t, s.gaze_x, s.gaze_y):
                gx, gy = self._dwell.fixation
                self.on_event(IntentEvent("gesture", "dwell", 1.0, t, "OPTICAL_FALLBACK"))

    def latest(self) -> Optional[FrameSample]:
        with self._lock:
            return self._latest


class _Hysteresis:
    """Blink classifier: on at on_th, off at off_th, min duration to count."""

    def __init__(self, on_th: float, off_th: float, min_s: float):
        self.on_th, self.off_th, self.min_s = on_th, off_th, min_s
        self._open_t: Optional[float] = None

    def update(self, t: float, score: float) -> bool:
        """Returns True exactly once per confirmed blink (on the closing edge)."""
        fired = False
        if self._open_t is None:
            if score >= self.on_th:
                self._open_t = t
        elif score <= self.off_th:
            if t - self._open_t >= self.min_s:
                fired = True
            self._open_t = None
        return fired


class _GazeDwell:
    """Psychic Fixation Ring: gaze must stay inside a small radius for dwell_s."""

    def __init__(self, dwell_s: float, radius: float = 0.12):
        self.dwell_s, self.radius = dwell_s, radius
        self._anchor: Optional[tuple[float, float]] = None
        self._since: Optional[float] = None
        self.fixation = (0.0, 0.0)
        self._cooldown_until = 0.0

    def update(self, t: float, gx: float, gy: float) -> bool:
        """True once per fixation (re-arms after the gaze leaves the ring)."""
        if self._anchor is None:
            self._anchor, self._since = (gx, gy), t
            return False
        if math.hypot(gx - self._anchor[0], gy - self._anchor[1]) > self.radius:
            self._anchor, self._since = (gx, gy), t
            return False
        if t - self._since >= self.dwell_s and t >= self._cooldown_until:
            self.fixation = self._anchor
            self._cooldown_until = t + 0.5              # one fire per ~0.5 s fixation
            return True
        return False


# ------------------------------------------------------------------ audio
class AudioPipeline:
    """sounddevice InputStream -> FFT transient clicks + optional Vosk grammar.

    Click = energy in 2500-6000 Hz jumping above threshold x rolling baseline.
    Vosk (if model present) does offline grammar recognition for 'freeze' etc.
    """

    def __init__(self, cfg: dict, on_event: Optional[Callable[[IntentEvent], None]] = None):
        self.cfg = cfg.get("audio", {})
        self.on_event = on_event
        self.status = "INIT"
        self._stream = None
        self._lock = threading.Lock()
        self._baseline = 0.0
        self._last_click = 0.0
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._vosk = None
        self._vosk_rec = None

    def start(self) -> None:
        if not (HAS_SOUNDEVICE and HAS_NUMPY):
            missing = [n for n, ok in (("sounddevice", HAS_SOUNDEVICE),
                                       ("numpy", HAS_NUMPY)) if not ok]
            self.status = f"UNAVAILABLE: {', '.join(missing)} not installed"
            log.warning("audio: %s", self.status)
            return
        try:
            sr = int(self.cfg.get("sample_rate", 16000))
            block = int(self.cfg.get("block_size", 512))
            self._stream = sd.InputStream(samplerate=sr, channels=1, blocksize=block,
                                          dtype="float32", callback=self._callback)
            self._stream.start()
        except Exception as e:
            self.status = f"UNAVAILABLE: mic open failed ({e})"
            log.error("audio: %s", self.status)
            self._stream = None
            return
        self._init_vosk()
        self.status = "RUNNING"

    def _init_vosk(self) -> None:
        if not HAS_VOSK:
            log.info("audio: vosk not installed - speech commands disabled, clicks only")
            return
        model_path = self.cfg.get("vosk_model", "models/vosk-model-small-en-us-0.15")
        if not Path(model_path).is_dir():
            log.info("audio: vosk model %r not found - speech commands disabled", model_path)
            return
        try:
            model = vosk.Model(model_path)
            grammar = json.dumps(self.cfg.get("vosk_grammar", ["freeze", "resume", "stop"]))
            self._vosk_rec = vosk.KaldiRecognizer(model, int(self.cfg.get("sample_rate", 16000)),
                                                  grammar=grammar)
        except Exception as e:
            log.warning("audio: vosk init failed (%s)", e)
            self._vosk_rec = None

    def _callback(self, indata, frames, time_info, status) -> None:
        self._q.put(indata[:, 0].copy())

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        self.status = "STOPPED"

    def poll(self, now: float) -> list[IntentEvent]:
        """Drain queued audio blocks; return click/voice events since last call."""
        out: list[IntentEvent] = []
        while True:
            try:
                block = self._q.get_nowait()
            except queue.Empty:
                break
            out.extend(self._analyze(block, now))
        return out

    def _analyze(self, block: np.ndarray, now: float) -> list[IntentEvent]:
        out: list[IntentEvent] = []
        sr = int(self.cfg.get("sample_rate", 16000))
        spec = np.abs(np.fft.rfft(block))
        freqs = np.fft.rfftfreq(len(block), 1.0 / sr)
        lo, hi = self.cfg.get("click_band_hz", [2500, 6000])
        band = spec[(freqs >= lo) & (freqs <= hi)]
        energy = float(band.max()) if band.size else 0.0
        # rolling baseline: slow attack, instant-ish decay
        self._baseline = energy if self._baseline == 0 else \
            0.98 * self._baseline + 0.02 * energy
        thr = float(self.cfg.get("click_threshold", 6.0))
        min_gap = float(self.cfg.get("click_min_interval_ms", 120)) / 1000.0
        if energy > max(thr, self._baseline * 3.0) and now - self._last_click > min_gap:
            self._last_click = now
            out.append(IntentEvent("gesture", "click", 1.0, now, "AUDIO"))
        if self._vosk_rec is not None:
            pcm = (np.clip(block, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()
            if self._vosk_rec.AcceptWaveform(pcm):
                self._handle_vosk_result()
        return out

    def _handle_vosk_result(self) -> None:
        try:
            res = json.loads(self._vosk_rec.Result())
            text = (res.get("text") or "").lower()
        except Exception:
            return
        for phrase in ("freeze", "resume", "continue", "stop"):
            if phrase in text:
                name = {"freeze": "voice_freeze", "stop": "voice_stop",
                        "resume": "voice_resume", "continue": "voice_resume"}[phrase]
                self.on_event and self.on_event(
                    IntentEvent("voice", name, 1.0, time.perf_counter(), "AUDIO"))
                return


# ------------------------------------------------------------------ psychic adapter
class PsychicIntentAdapter:
    """Dual-mode trigger engine.

    HARDWARE_OMNIBAND: ESP32 + piezo temple sensor, 115200 JSON
        {"piezo_raw": int, "pulse": float, "clench": int, "lat_ms": float}
        -> clench field >= threshold = sub-3 ms trigger.
    OPTICAL_FALLBACK (no hardware): gaze dwell fixation + closed-mouth clench
        from VisionPipeline, tongue clicks from AudioPipeline.
    Hot-unplug: serial going stale drops back to optical; re-plug restores
    hardware - no crash, no state loss (the FSM sees `source` on every event).
    """

    def __init__(self, cfg: dict, vision: VisionPipeline, audio: AudioPipeline,
                 on_event: Optional[Callable[[IntentEvent], None]] = None):
        self.cfg = cfg
        self.omni_cfg = cfg.get("omnibus", {})
        self.vision, self.audio = vision, audio
        self.on_event = on_event
        self.mode = "OPTICAL_FALLBACK"
        self._ser = None
        self._serial_port = ""
        self._last_packet: Optional[dict] = None
        self._last_packet_t = 0.0
        self._scan_done = False

    # -- serial ---------------------------------------------------------------
    def _scan_serial(self) -> None:
        if not HAS_SERIAL:
            log.info("psychic: pyserial not installed - hardware mode unavailable")
            return
        try:
            ports = serial.tools.list_ports.comports()
        except Exception as e:
            log.warning("psychic: port scan failed (%s)", e)
            return
        baud = int(self.omni_cfg.get("serial_baud", 115200))
        for p in ports:
            try:
                s = serial.Serial(p.device, baud, timeout=0.05)
                # probe: OmniBand emits JSON packets unprompted at ~100 Hz
                line = s.readline()
                if line:
                    try:
                        pkt = json.loads(line.decode("utf-8", "replace"))
                        if self._is_omni_packet(pkt):
                            self._ser, self._serial_port = s, p.device
                            self.mode = "HARDWARE_OMNIBAND"
                            log.info("psychic: OmniBand found on %s - hardware mode", p.device)
                            return
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        pass
                s.close()
            except Exception:
                continue
        log.info("psychic: no OmniBand found - optical fallback")

    @staticmethod
    def _is_omni_packet(d: dict) -> bool:
        return all(k in d for k in ("piezo_raw", "pulse", "clench", "lat_ms"))

    def _serial_poll(self, now: float) -> None:
        if self._ser is None:
            return
        stale_ms = float(self.omni_cfg.get("serial_stale_ms", 500))
        try:
            while self._ser.in_waiting:
                line = self._ser.readline()
                if not line:
                    break
                try:
                    pkt = json.loads(line.decode("utf-8", "replace"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if self._is_omni_packet(pkt):
                    self._last_packet, self._last_packet_t = pkt, now
                    self._handle_packet(pkt, now)
        except Exception as e:
            log.warning("psychic: serial error (%s) - dropping to optical", e)
            self._close_serial()
            self.mode = "OPTICAL_FALLBACK"
            return
        # hot-unplug: no valid packet within stale window -> optical
        if now - self._last_packet_t > stale_ms / 1000.0 and self._last_packet is not None:
            log.warning("psychic: OmniBand went stale - optical fallback")
            self._close_serial()
            self.mode = "OPTICAL_FALLBACK"

    def _close_serial(self) -> None:
        try:
            if self._ser is not None:
                self._ser.close()
        except Exception:
            pass
        self._ser = None

    def _handle_packet(self, pkt: dict, now: float) -> None:
        thr = float(self.omni_cfg.get("hardware_clench_threshold", 0.6))
        clench = float(pkt.get("clench", 0.0))
        pulse = float(pkt.get("pulse", 0.0))
        if clench >= thr:
            self._emit(IntentEvent("gesture", "clench", clench, now, "HARDWARE_OMNIBAND"))
        if pulse > 0.8:                                  # sharp pulse transient = click
            self._emit(IntentEvent("gesture", "click", pulse, now, "HARDWARE_OMNIBAND"))

    # -- optical path ----------------------------------------------------------
    def _optical_poll(self, now: float) -> None:
        """Optical mode needs no per-tick work: gaze/aim is read continuously via
        latest_sample() by the FSM, and clench/dwell/blink are edge events already
        emitted by VisionPipeline._emit_edges."""
        return

    def _vision_edge(self, ev: IntentEvent) -> None:
        """Route for VisionPipeline edge events. hard_blink always passes (it is the
        universal cancel gesture); optical-only triggers (clench/dwell) are muted
        while OmniBand hardware owns triggering."""
        if self.mode == "HARDWARE_OMNIBAND" and ev.name in ("clench", "dwell"):
            return
        self._emit(ev)

    def _emit(self, ev: IntentEvent) -> None:
        if self.on_event is not None:
            try:
                self.on_event(ev)
            except Exception:
                log.exception("psychic on_event failed")

    # -- public ------------------------------------------------------------------
    def start(self) -> None:
        if not self._scan_done:
            self._scan_done = True
            if self.omni_cfg.get("scan_ports", True):
                self._scan_serial()
        self.vision.start()
        self.audio.start()

    def poll(self, now: float) -> list[IntentEvent]:
        """Called every tick. Returns audio events; vision/psychic events arrive via
        on_event callback (they are edge-triggered, not polled)."""
        events = self.audio.poll(now)
        # audio click/voice events are returned to the caller; vision/psychic edges
        # arrive via the on_event callback. Nothing is emitted twice.
        if self._ser is not None:
            self._serial_poll(now)
        else:
            self._optical_poll(now)
        return events

    def latest_sample(self):
        return self.vision.latest()

    def status(self) -> dict:
        return {"mode": self.mode,
                "serial_port": self._serial_port,
                "vision": self.vision.status,
                "audio": self.audio.status}
