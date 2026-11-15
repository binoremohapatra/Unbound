"""wifi_transport.py - Ultra-Low-Latency Wi-Fi UDP Hardware Transport for OmniBand V2.

Provides:
1. Zero-dependency Python socket.AF_INET, socket.SOCK_DGRAM listener on port 4210:
   - SO_REUSEADDR and SO_BROADCAST enabled for instant receipt of broadcast packets.
   - Zero-handshake, zero-pairing: connects in < 100ms as soon as ESP32 powers on.
2. Bidirectional Auto-Ping & HTTP Handshake:
   - Periodically pings 192.168.4.1:4210 and http://192.168.4.1/status
   - ESP32 automatically captures host IP and streams direct unicast UDP datagrams,
     bypassing any Windows firewall broadcast filtering.
3. Universal Hardware Telemetry & Action Parser:
   - Parses streaming JSON: {"ch": [c1, c2, c3, c4], "dx": dx, "dy": dy, "click": "...", "scroll": ..., "seq": ...}
   - Dispatches somatic envelopes, IMU relative mouse movement (dx, dy), Edge DSP clicks, and scrolls.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.request
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from host.omniband.hardware.hardware_calibration import HardwareCalibrator

log = logging.getLogger("omniband.wifi_transport")

DEFAULT_UDP_PORT = 4210
DEFAULT_ESP_IP = "192.168.4.1"


def parse_wifi_packet(data_raw: bytes | str) -> Optional[dict]:
    """Parse incoming Wi-Fi UDP datagram (JSON, CSV, or plain text trigger)."""
    if not data_raw:
        return None

    if isinstance(data_raw, bytes):
        try:
            text = data_raw.decode("utf-8", errors="replace").strip()
        except Exception:
            return None
    else:
        text = str(data_raw).strip()

    if not text:
        return None

    # 1. Native OmniBand Wi-Fi JSON
    if text.startswith("{") and text.endswith("}"):
        try:
            obj = json.loads(text)
            if "ch" in obj:
                ch_list = [float(x) for x in obj["ch"]]
                return {
                    "kind": "somatic",
                    "channels": ch_list,
                    "dx": float(obj.get("dx", 0.0)),
                    "dy": float(obj.get("dy", 0.0)),
                    "pitch": float(obj.get("pitch", 0.0)),
                    "roll": float(obj.get("roll", 0.0)),
                    "click": str(obj.get("click", "none")),
                    "scroll": int(obj.get("scroll", 0)),
                    "gesture": str(obj.get("gesture", "")),
                    "seq": int(obj.get("seq", 0)),
                }
            if "click" in obj:
                return {
                    "kind": "gesture",
                    "gesture": str(obj["click"]).upper(),
                }
            if "scroll" in obj:
                s_val = int(obj["scroll"])
                return {
                    "kind": "scroll",
                    "delta_y": s_val * 40,
                    "gesture": "SCROLL_UP" if s_val > 0 else "SCROLL_DOWN",
                }
            if "gesture" in obj:
                return {
                    "kind": "gesture",
                    "gesture": str(obj["gesture"]).upper(),
                }
            if "drag" in obj:
                return {
                    "kind": "gesture",
                    "gesture": str(obj["drag"]).upper(),
                }
            if "k" in obj:
                # Standard NDJSON format
                if obj.get("k") == "tele":
                    env = [float(x) for x in obj.get("env", [0, 0, 0, 0])]
                    return {
                        "kind": "somatic",
                        "channels": env,
                        "dx": float(obj.get("dx", 0.0)),
                        "dy": float(obj.get("dy", 0.0)),
                        "seq": int(obj.get("seq", 0)),
                    }
                if obj.get("k") == "evt":
                    return {
                        "kind": "gesture",
                        "gesture": str(obj.get("g", "")).upper(),
                    }
        except Exception as e:
            log.debug("JSON parse exception on UDP packet: %s", e)

    # 2. Compact CSV: S,ch1,ch2,ch3,ch4,seq[,dx,dy]
    if text.startswith("S,"):
        parts = text.split(",")
        if len(parts) >= 6:
            try:
                ch = [float(p) for p in parts[1:5]]
                seq = int(parts[5])
                dx = float(parts[6]) if len(parts) > 6 else 0.0
                dy = float(parts[7]) if len(parts) > 7 else 0.0
                return {
                    "kind": "somatic",
                    "channels": ch,
                    "seq": seq,
                    "dx": dx,
                    "dy": dy,
                }
            except (ValueError, IndexError):
                pass

    # 3. Plain text triggers
    upper_txt = text.upper()
    if upper_txt in ("CLICK", "LEFT", "RIGHT", "MIDDLE", "HOLD", "DRAG", "DRAG_ON", "DRAG_OFF",
                     "SCROLL", "SCROLL_UP", "SCROLL_DOWN", "CH1", "CH2", "CH3", "CH4"):
        return {
            "kind": "gesture",
            "gesture": upper_txt,
        }

    return None


class UnifiedGestureEngine:
    """Unified 8-Gesture State Machine for OmniBand V2 (Piezo + IMU + Optical Fusion).

    8 Supported Gestures:
    1. Jaw Clench: Single spike on CH4/CH1 -> gateway.left_click()
    2. Jaw Double-Tap: 2 spikes within < 450ms -> gateway.right_click()
    3. Jaw Hold & Release: Sustained clench > 450ms -> mouse_down('left') on hold start, mouse_up('left') on release
    4. Left Eye Close & Release: Triggers Left Click action
    5. Right Eye Close & Release: Triggers Right Click action
    6. Smile Gesture: Triggers Enter / Confirm action (😊 Smile Trigger)
    7. Eyebrow Raise (CH2+CH3 / Optical): Smooth Scroll UP (gateway.scroll(dy=+3))
    8. Head Nod (MPU Pitch Down / Optical): Smooth Scroll DOWN (gateway.scroll(dy=-3))
    """

    def __init__(
        self,
        on_action: Optional[Callable[[str, Any], None]] = None,
        on_badge: Optional[Callable[[str], None]] = None,
        enable_piezo_clicks: bool = True,
    ) -> None:
        self.on_action = on_action  # ("click", "left"), ("mouse_down", "left"), ("scroll", 3), etc.
        self.on_badge = on_badge    # "⚡ GESTURE: JAW HOLD (DRAGGING...)"
        self.enable_piezo_clicks = enable_piezo_clicks
        self._lock = threading.Lock()

        # Jaw Clench / Tap / Hold state
        self._jaw_active = False
        self._jaw_press_t = 0.0
        self._jaw_hold_fired = False
        self._last_tap_t = 0.0
        self._tap_count = 0
        self._single_tap_pending = False
        self._single_tap_deadline = 0.0

        # Eye states
        self._left_eye_closed = False
        self._left_eye_close_t = 0.0
        self._right_eye_closed = False
        self._right_eye_close_t = 0.0

        # Cooldown timers
        self._last_smile_t = 0.0
        self._last_brow_t = 0.0
        self._last_nod_t = 0.0
        self._prev_mpu_pitch = 0.0

        # Profile & Gaming Modes ("desktop" or "nfs")
        self.profile: str = "desktop"
        self._nfs_gas_held: bool = False
        self._nfs_brake_held: bool = False
        self._nfs_steer_left_held: bool = False
        self._nfs_steer_right_held: bool = False
        self._nfs_handbrake_held: bool = False

    def set_profile(self, profile: str) -> None:
        """Switch active gesture mapping profile ('desktop' or 'nfs')."""
        with self._lock:
            old_profile = self.profile
            self.profile = profile.strip().lower()
            if old_profile == "nfs" and self.profile != "nfs":
                self.reset_nfs_keys()

    def reset_nfs_keys(self) -> None:
        """Release all held keys when leaving NFS gaming mode."""
        if self._nfs_gas_held:
            self._nfs_gas_held = False
            self.trigger_action("key_up", "w", "🏎️ [NFS] GAS: RELEASED")
        if self._nfs_brake_held:
            self._nfs_brake_held = False
            self.trigger_action("key_up", "s", "🏎️ [NFS] BRAKE: RELEASED")
        if self._nfs_steer_left_held:
            self._nfs_steer_left_held = False
            self.trigger_action("key_up", "a", "◀️ [NFS] STEER: STRAIGHT")
        if self._nfs_steer_right_held:
            self._nfs_steer_right_held = False
            self.trigger_action("key_up", "d", "▶️ [NFS] STEER: STRAIGHT")
        if self._nfs_handbrake_held:
            self._nfs_handbrake_held = False
            self.trigger_action("key_up", "space", "🏎️ [NFS] HANDBRAKE: RELEASED")

    def trigger_action(self, action_type: str, param: Any, badge_text: str) -> None:
        if self.on_badge:
            try:
                self.on_badge(badge_text)
            except Exception as e:
                log.debug("on_badge error: %s", e)
        if self.on_action:
            try:
                self.on_action(action_type, param)
            except Exception as e:
                log.debug("on_action error: %s", e)

    def feed_somatic(self, channels: List[float], now: Optional[float] = None) -> None:
        if now is None:
            now = time.perf_counter()

        with self._lock:
            if not self.enable_piezo_clicks:
                # Piezo is VISUAL TELEMETRY ONLY. Disconnected from OS clicks/keys.
                return

            # Check pending single tap timeout
            if self._single_tap_pending and now >= self._single_tap_deadline:
                self._single_tap_pending = False
                self._tap_count = 0
                self.trigger_action("click", "left", "⚡ GESTURE: JAW CLENCH (LEFT CLICK)")

            if len(channels) < 3:
                return

            c = [float(x) / 4095.0 if float(x) > 1.0 else float(x) for x in channels]
            # ─── CHANNEL MAPPING (anatomically correct) ──────────────────────────────
            # CH1 (index 0 / GPIO 32 / ADC1) → LEFT TEMPLE  (Temporalis) → JAW CLENCH
            # CH2 (index 1 / GPIO 34 / ADC1) → RIGHT TEMPLE (Temporalis) → secondary
            # CH3 (index 2 / GPIO 33 / ADC1) → FOREHEAD     (Frontalis)  → BROW RAISE
            # CH4 (index 3)                  → DISABLED (ADC2 / Wi-Fi conflict)
            #
            # Anatomy:
            #   Jaw clench fires TEMPORALIS (temple) — not forehead.
            #   Eyebrow raise fires FRONTALIS (forehead).
            # ─────────────────────────────────────────────────────────────────────────
            ch_temple_l  = c[0]  # CH1 — LEFT temple (primary jaw clench via temporalis)
            ch_temple_r  = c[1]  # CH2 — RIGHT temple (secondary / double-side clench)
            ch_forehead  = c[2]  # CH3 — Forehead (eyebrow raise → scroll up)
            # c[3] = CH4 → not used

            # 1. JAW CLENCH — driven by CH1 (left temple / temporalis)
            # Both temples fire together on jaw clench, so OR with CH2 for redundancy.
            # Single tap  → Left Click
            # Double tap  → Right Click
            # Hold >450ms → Drag Lock (mouse_down) / release → mouse_up
            JAW_THRESH = 0.25   # tune on real hardware: lower = more sensitive
            jaw_is_high = (ch_temple_l > JAW_THRESH) or (ch_temple_r > JAW_THRESH)
            BROW_THRESH = 0.25
            brow_is_high = (ch_forehead > BROW_THRESH)

            # -------------------------------------------------------------
            # PROFILE: NFS MOST WANTED (W/A/S/D/SPACE Driving Mode)
            # -------------------------------------------------------------
            if self.profile == "nfs":
                # Gas / Accelerate ('w'): Clench Jaw (hold)
                if jaw_is_high and not self._nfs_gas_held:
                    self._nfs_gas_held = True
                    self.trigger_action("key_down", "w", "🏎️ [NFS] GAS: W (ACCELERATING)")
                elif not jaw_is_high and self._nfs_gas_held:
                    self._nfs_gas_held = False
                    self.trigger_action("key_up", "w", "🏎️ [NFS] GAS: W (COASTING)")

                # Brake / Reverse ('s'): Forehead / Brow Raise (hold)
                if brow_is_high and not self._nfs_brake_held:
                    self._nfs_brake_held = True
                    self.trigger_action("key_down", "s", "🛑 [NFS] BRAKE: S (BRAKING)")
                elif not brow_is_high and self._nfs_brake_held:
                    self._nfs_brake_held = False
                    self.trigger_action("key_up", "s", "🏎️ [NFS] BRAKE: S (RELEASED)")
                return

            # -------------------------------------------------------------
            # PROFILE: DESKTOP MOUSE NAVIGATION
            # -------------------------------------------------------------
            if jaw_is_high and not self._jaw_active:
                self._jaw_active = True
                self._jaw_press_t = now
                self._jaw_hold_fired = False

            elif jaw_is_high and self._jaw_active:
                dur = now - self._jaw_press_t
                if dur >= 0.450 and not self._jaw_hold_fired:
                    self._jaw_hold_fired = True
                    self._single_tap_pending = False
                    self._tap_count = 0
                    self.trigger_action("mouse_down", "left", "⚡ GESTURE: JAW HOLD (DRAGGING...)")

            elif not jaw_is_high and self._jaw_active:
                self._jaw_active = False
                dur = now - self._jaw_press_t

                if self._jaw_hold_fired:
                    self._jaw_hold_fired = False
                    self.trigger_action("mouse_up", "left", "⚡ GESTURE: JAW RELEASE (DROPPED)")
                    self._tap_count = 0
                else:
                    if self._tap_count == 1 and (now - self._last_tap_t < 0.450):
                        self._single_tap_pending = False
                        self._tap_count = 0
                        self._last_tap_t = 0.0
                        self.trigger_action("click", "right", "⚡ GESTURE: JAW DOUBLE-TAP -> RIGHT CLICK")
                    else:
                        self._tap_count = 1
                        self._last_tap_t = now
                        self._single_tap_pending = True
                        self._single_tap_deadline = now + 0.300

            # 2. EYEBROW RAISE — driven by CH3 (forehead / frontalis) → Scroll UP
            BROW_THRESH = 0.25
            if ch_forehead > BROW_THRESH:
                if now - self._last_brow_t >= 0.180:
                    self._last_brow_t = now
                    self.trigger_action("scroll", 3, "⬆️ BROW RAISE (FOREHEAD) → SCROLL UP")

    def feed_imu(self, dx: float, dy: float, pitch: float = 0.0, roll: float = 0.0, now: Optional[float] = None) -> None:
        if now is None:
            now = time.perf_counter()

        with self._lock:
            if self.profile == "nfs":
                # Completely bypass IMU in NFS mode so head movements never disrupt steering/cursor!
                return

            if self._single_tap_pending and now >= self._single_tap_deadline:
                self._single_tap_pending = False
                self._tap_count = 0
                self.trigger_action("click", "left", "⚡ GESTURE: JAW CLENCH (LEFT CLICK)")

            # Head Nod detection from MPU Pitch Down
            if pitch < -14.0 or (self._prev_mpu_pitch - pitch > 18.0):
                if now - self._last_nod_t >= 0.150:
                    self._last_nod_t = now
                    self.trigger_action("scroll", -3, "⬇️ HEAD NOD")
            self._prev_mpu_pitch = pitch

    def feed_optical(
        self,
        left_ear: Optional[float] = None,
        right_ear: Optional[float] = None,
        smile: float = 0.0,
        brow: float = 0.0,
        pitch: float = 0.0,
        now: Optional[float] = None,
    ) -> None:
        if now is None:
            now = time.perf_counter()

        with self._lock:
            if self.profile == "nfs":
                # ---------------------------------------------------------
                # NFS OPTICAL STEERING (A/D) & HANDBRAKE (SPACE)
                # ---------------------------------------------------------
                if left_ear is not None and right_ear is not None:
                    # Steer Left ('a') via Left Eye Wink/Close (hold)
                    l_closed = (left_ear < 0.22) and (right_ear >= 0.20)
                    if l_closed and not self._nfs_steer_left_held:
                        self._nfs_steer_left_held = True
                        self.trigger_action("key_down", "a", "◀️ [NFS] STEER LEFT: A (HELD)")
                    elif not l_closed and self._nfs_steer_left_held:
                        self._nfs_steer_left_held = False
                        self.trigger_action("key_up", "a", "◀️ [NFS] STEER: STRAIGHT")

                    # Steer Right ('d') via Right Eye Wink/Close (hold)
                    r_closed = (right_ear < 0.22) and (left_ear >= 0.20)
                    if r_closed and not self._nfs_steer_right_held:
                        self._nfs_steer_right_held = True
                        self.trigger_action("key_down", "d", "▶️ [NFS] STEER RIGHT: D (HELD)")
                    elif not r_closed and self._nfs_steer_right_held:
                        self._nfs_steer_right_held = False
                        self.trigger_action("key_up", "d", "▶️ [NFS] STEER: STRAIGHT")

                # Handbrake ('space') via Smile
                smile_active = (smile >= 0.35)
                if smile_active and not self._nfs_handbrake_held:
                    self._nfs_handbrake_held = True
                    self.trigger_action("key_down", "space", "⚡ [NFS] HANDBRAKE: SPACE (HELD)")
                elif not smile_active and self._nfs_handbrake_held:
                    self._nfs_handbrake_held = False
                    self.trigger_action("key_up", "space", "🏎️ [NFS] HANDBRAKE: RELEASED")

                # Fallback optical brow raise for Brake ('s')
                if brow >= 0.40 and not self._nfs_brake_held:
                    self._nfs_brake_held = True
                    self.trigger_action("key_down", "s", "🛑 [NFS] BRAKE: S (HELD)")
                elif brow < 0.25 and self._nfs_brake_held:
                    self._nfs_brake_held = False
                    self.trigger_action("key_up", "s", "🏎️ [NFS] BRAKE: RELEASED")
                return

            if self._single_tap_pending and now >= self._single_tap_deadline:
                self._single_tap_pending = False
                self._tap_count = 0
                self.trigger_action("click", "left", "⚡ GESTURE: JAW CLENCH (LEFT CLICK)")

            # 4. Left Eye Close & Release
            # Lowered close threshold 0.18 → 0.22 (easier to detect wink)
            # Widened wink duration window 0.08–0.65 → 0.06–0.90
            if left_ear is not None and right_ear is not None:
                l_closed = (left_ear < 0.22) and (right_ear >= 0.20)
                if l_closed and not self._left_eye_closed:
                    self._left_eye_closed = True
                    self._left_eye_close_t = now
                elif not l_closed and self._left_eye_closed:
                    self._left_eye_closed = False
                    dur = now - self._left_eye_close_t
                    if 0.06 <= dur <= 0.90:
                        self.trigger_action("click", "left", "👁️ LEFT EYE WINK")

                # 5. Right Eye Close & Release
                # Same improved thresholds
                r_closed = (right_ear < 0.22) and (left_ear >= 0.20)
                if r_closed and not self._right_eye_closed:
                    self._right_eye_closed = True
                    self._right_eye_close_t = now
                elif not r_closed and self._right_eye_closed:
                    self._right_eye_closed = False
                    dur = now - self._right_eye_close_t
                    if 0.06 <= dur <= 0.90:
                        self.trigger_action("click", "right", "👁️ RIGHT EYE WINK")

            # 6. Smile Gesture -> Enter / Confirm
            # Lowered threshold 0.45 → 0.35
            if smile >= 0.35:
                if now - self._last_smile_t >= 0.800:
                    self._last_smile_t = now
                    self.trigger_action("key", "enter", "😊 SMILE")

            # 7. Optical Eyebrow Raise
            if brow >= 0.45:
                if now - self._last_brow_t >= 0.120:
                    self._last_brow_t = now
                    self.trigger_action("scroll", 3, "⬆️ BROW RAISE")

            # 8. Optical Head Nod (Pitch down)
            if pitch < -12.0:
                if now - self._last_nod_t >= 0.150:
                    self._last_nod_t = now
                    self.trigger_action("scroll", -3, "⬇️ HEAD NOD")

    def feed_gesture(self, gesture_name: str, now: Optional[float] = None) -> None:
        if now is None:
            now = time.perf_counter()

        g = (gesture_name or "").strip().upper()
        with self._lock:
            if self.profile == "nfs":
                if g in ("RIGHT", "DOUBLE", "DOUBLE_CLENCH", "JAW_TAP", "TAP"):
                    self.trigger_action("key_down", "space", "⚡ [NFS] HANDBRAKE (SPACE PULSE)")
                    threading.Timer(0.25, lambda: self.trigger_action("key_up", "space", "🏎️ [NFS] READY")).start()
                return

            if g in ("CLICK", "LEFT", "JAW_CLENCH"):
                self.trigger_action("click", "left", "⚡ GESTURE: JAW CLENCH (LEFT CLICK)")
            elif g in ("RIGHT", "DOUBLE", "DOUBLE_CLENCH", "JAW_TAP", "TAP"):
                self.trigger_action("click", "right", "⚡ GESTURE: JAW DOUBLE-TAP -> RIGHT CLICK")
            elif g in ("HOLD", "JAW_HOLD", "DRAG", "DRAG_ON"):
                self.trigger_action("mouse_down", "left", "⚡ GESTURE: JAW HOLD (DRAGGING...)")
            elif g in ("RELEASE", "DRAG_OFF"):
                self.trigger_action("mouse_up", "left", "⚡ GESTURE: JAW RELEASE (DROPPED)")
            elif g in ("SCROLL_UP", "BROW_RAISE"):
                self.trigger_action("scroll", 3, "⬆️ BROW RAISE")
            elif g in ("SCROLL_DOWN", "NOD"):
                self.trigger_action("scroll", -3, "⬇️ HEAD NOD")
            elif g in ("SMILE", "ENTER"):
                self.trigger_action("key", "enter", "😊 SMILE")

    def poll(self, now: Optional[float] = None) -> None:
        if now is None:
            now = time.perf_counter()
        with self._lock:
            if self.profile == "nfs":
                return
            if self._single_tap_pending and now >= self._single_tap_deadline:
                self._single_tap_pending = False
                self._tap_count = 0
                self.trigger_action("click", "left", "⚡ GESTURE: JAW CLENCH (LEFT CLICK)")


class OmniBandWifiClient:
    """Zero-configuration Ultra-Low-Latency Wi-Fi UDP Hardware Receiver for OmniBand."""

    def __init__(
        self,
        port: int = DEFAULT_UDP_PORT,
        esp_ip: str = DEFAULT_ESP_IP,
        calibrator: Optional[HardwareCalibrator] = None,
        on_somatic_frame: Optional[Callable[[List[float], List[bool], int], None]] = None,
        on_imu_motion: Optional[Callable[[float, float], None]] = None,
        on_hardware_gesture: Optional[Callable[[str], None]] = None,
        on_connection_change: Optional[Callable[[bool, str], None]] = None,
        on_metrics_update: Optional[Callable[[float], None]] = None,
        on_status_change: Optional[Callable[[str], None]] = None,
        on_gesture_action: Optional[Callable[[str, Any], None]] = None,
        on_gesture_badge: Optional[Callable[[str], None]] = None,
        on_imu_telemetry: Optional[Callable[[float, float, float, float], None]] = None,
    ) -> None:
        self.port = port
        self.esp_ip = esp_ip
        self.calibrator = calibrator
        self.on_somatic_frame = on_somatic_frame
        self.on_imu_motion = on_imu_motion
        self.on_hardware_gesture = on_hardware_gesture
        self.on_connection_change = on_connection_change
        self.on_metrics_update = on_metrics_update
        self.on_status_change = on_status_change
        self.on_imu_telemetry = on_imu_telemetry

        self.gesture_engine = UnifiedGestureEngine(
            on_action=on_gesture_action,
            on_badge=on_gesture_badge,
            enable_piezo_clicks=False,  # Piezo = Visual Telemetry Only (No OS clicks from piezo)
        )

        self._running = False
        self._connected = False
        self._sock: Optional[socket.socket] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._probe_thread: Optional[threading.Thread] = None

        self._last_packet_t = 0.0
        self._packet_count = 0
        self._pkt_rate = 0.0
        self._last_metrics_calc_t = time.perf_counter()
        self._lock = threading.Lock()
        self._remote_addr: str = ""

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def packet_rate(self) -> float:
        return self._pkt_rate

    @property
    def remote_address(self) -> str:
        return self._remote_addr

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True

        self._listen_thread = threading.Thread(
            target=self._listen_loop,
            daemon=True,
            name="OmniBandWifiListenWorker",
        )
        self._listen_thread.start()

        self._probe_thread = threading.Thread(
            target=self._probe_loop,
            daemon=True,
            name="OmniBandWifiProbeWorker",
        )
        self._probe_thread.start()

        log.info("OmniBand Wi-Fi UDP Receiver started on UDP Port %d", self.port)

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._connected = False

        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        log.info("OmniBand Wi-Fi UDP Receiver stopped.")

    def trigger_ping(self) -> None:
        """Proactively broadcast a ping to prompt ESP32 to send unicast datagrams."""
        try:
            ping_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            ping_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            ping_sock.settimeout(0.2)
            ping_sock.sendto(b"PING\n", (self.esp_ip, self.port))
            ping_sock.sendto(b"PING\n", ("255.255.255.255", self.port))
            ping_sock.close()
        except Exception as e:
            log.debug("UDP Ping error: %s", e)

    def _listen_loop(self) -> None:
        """Dedicated UDP packet reception thread."""
        while self._running:
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                if hasattr(socket, "SO_REUSEADDR"):
                    self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_BROADCAST"):
                    self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                self._sock.settimeout(0.5)
                self._sock.bind(("", self.port))
                log.info("Bound UDP listener to port %d", self.port)
                break
            except Exception as e:
                log.debug("Failed to bind UDP port %d: %s. Retrying in 1s...", self.port, e)
                time.sleep(1.0)

        while self._running:
            try:
                if not self._sock:
                    time.sleep(0.5)
                    continue

                data, addr = self._sock.recvfrom(2048)
                if not data:
                    continue

                # Parse payload first — discard ping/pong/loopback packets
                pkt = parse_wifi_packet(data)
                if not pkt:
                    continue

                now = time.perf_counter()
                self._last_packet_t = now
                self._packet_count += 1
                self._remote_addr = addr[0]

                # Update connection status edge only on genuine telemetry/gesture
                if not self._connected:
                    with self._lock:
                        self._connected = True
                    msg = f"Connected to OmniBand via Wi-Fi UDP ({addr[0]}:{self.port})"
                    log.info("🟢 %s", msg)
                    if self.on_status_change:
                        self.on_status_change(f"🟢 CONNECTED: HARDWARE (Wi-Fi UDP)")
                    if self.on_connection_change:
                        self.on_connection_change(True, msg)

                # Process payload
                self._dispatch_packet(pkt)

                # Rate computation
                dt = now - self._last_metrics_calc_t
                if dt >= 1.0:
                    self._pkt_rate = self._packet_count / dt
                    self._packet_count = 0
                    self._last_metrics_calc_t = now
                    if self.on_metrics_update:
                        self.on_metrics_update(self._pkt_rate)

            except socket.timeout:
                self.gesture_engine.poll(time.perf_counter())
            except Exception as e:
                if self._running:
                    log.debug("UDP receive loop exception: %s", e)
                    time.sleep(0.01)

    def _dispatch_packet(self, pkt: dict) -> None:
        """Route parsed packet contents to UI, cursor, and calibration wizard."""
        kind = pkt.get("kind")

        if kind == "somatic":
            raw_ch = pkt.get("channels", [0.0, 0.0, 0.0, 0.0])
            seq = pkt.get("seq", 0)
            dx = float(pkt.get("dx", 0.0))
            dy = float(pkt.get("dy", 0.0))
            pitch = float(pkt.get("pitch", 0.0))
            roll = float(pkt.get("roll", 0.0))

            # Route to Unified 8-Gesture State Machine
            self.gesture_engine.feed_somatic(raw_ch)
            self.gesture_engine.feed_imu(dx, dy, pitch, roll)

            # Normalization via HardwareCalibrator if registered
            if self.calibrator:
                filtered_ch = self.calibrator.apply_filter(raw_ch)
                active = [val >= self.calibrator.profile.thresholds[i] for i, val in enumerate(filtered_ch)]
            else:
                filtered_ch = [min(1.0, max(0.0, float(c) / 4095.0)) if c > 1.0 else float(c) for c in raw_ch]
                active = [val > 0.45 for val in filtered_ch]

            if self.on_somatic_frame:
                self.on_somatic_frame(filtered_ch, active, seq)

            # Route IMU motion deltas if present
            if (abs(dx) > 0.001 or abs(dy) > 0.001) and self.on_imu_motion:
                self.on_imu_motion(dx, dy)

            # Route complete IMU telemetry (dx, dy, pitch, roll) to subscribers (e.g. NFS racing engine)
            if self.on_imu_telemetry:
                self.on_imu_telemetry(dx, dy, pitch, roll)

            # Route inline gesture/click/scroll if present (Piezo is visual telemetry only: no OS clicks)
            click = pkt.get("click", "none")
            if click and click not in ("none", ""):
                # self.gesture_engine.feed_gesture(click)  # Disabled: Piezo visual telemetry only
                if self.on_hardware_gesture:
                    self.on_hardware_gesture(click.upper())

            scroll = pkt.get("scroll", 0)
            if scroll != 0:
                # self.gesture_engine.feed_gesture("SCROLL_UP" if scroll > 0 else "SCROLL_DOWN")
                if self.on_hardware_gesture:
                    self.on_hardware_gesture(f"SCROLL_{'UP' if scroll > 0 else 'DOWN'}")

            gesture = pkt.get("gesture", "")
            if gesture and gesture not in ("NONE", ""):
                # self.gesture_engine.feed_gesture(gesture)
                if self.on_hardware_gesture:
                    self.on_hardware_gesture(gesture.upper())

        elif kind == "gesture":
            g = pkt.get("gesture", "")
            if g:
                self.gesture_engine.feed_gesture(g)
                if self.on_hardware_gesture:
                    self.on_hardware_gesture(g.upper())

        elif kind == "scroll":
            delta_y = pkt.get("delta_y", 0)
            g = "SCROLL_UP" if delta_y > 0 else "SCROLL_DOWN"
            self.gesture_engine.feed_gesture(g)
            if self.on_hardware_gesture:
                self.on_hardware_gesture(g)

    def _probe_loop(self) -> None:
        """Periodic watchdog and pro-active ping loop."""
        while self._running:
            now = time.perf_counter()

            # Connection timeout check: if no packet for > 1.8s, declare link silent
            if self._connected and (now - self._last_packet_t > 1.8):
                with self._lock:
                    self._connected = False
                log.warning("OmniBand Wi-Fi UDP stream timed out.")
                if self.on_status_change:
                    self.on_status_change("Wi-Fi UDP: Packet silence (Searching...)")
                if self.on_connection_change:
                    self.on_connection_change(False, "Wi-Fi UDP connection timed out")

            # If not connected, trigger proactive ping and HTTP check
            if not self._connected:
                self.trigger_ping()
                self._try_http_probe()

            time.sleep(1.0)

    def _try_http_probe(self) -> None:
        """Lightweight HTTP status query to 192.168.4.1 to handshake through Windows firewall."""
        try:
            req = urllib.request.Request(
                f"http://{self.esp_ip}/status",
                headers={"User-Agent": "OmniBand-Host/2.0"}
            )
            with urllib.request.urlopen(req, timeout=0.3) as resp:
                if resp.status == 200:
                    log.debug("HTTP probe to ESP32 /status successful!")
        except Exception:
            pass
