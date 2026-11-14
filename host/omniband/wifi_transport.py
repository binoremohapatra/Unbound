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
from typing import TYPE_CHECKING, Callable, List, Optional, Tuple

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

                now = time.perf_counter()
                self._last_packet_t = now
                self._packet_count += 1
                self._remote_addr = addr[0]

                # Update connection status edge
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
                pkt = parse_wifi_packet(data)
                if pkt:
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
                pass
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
            dx = float(pkt.get("dx", 0.0))
            dy = float(pkt.get("dy", 0.0))
            if (abs(dx) > 0.001 or abs(dy) > 0.001) and self.on_imu_motion:
                self.on_imu_motion(dx, dy)

            # Route inline gesture/click/scroll if present
            click = pkt.get("click", "none")
            if click and click not in ("none", ""):
                if self.on_hardware_gesture:
                    self.on_hardware_gesture(click.upper())

            scroll = pkt.get("scroll", 0)
            if scroll != 0 and self.on_hardware_gesture:
                self.on_hardware_gesture(f"SCROLL_{'UP' if scroll > 0 else 'DOWN'}")

            gesture = pkt.get("gesture", "")
            if gesture and self.on_hardware_gesture:
                self.on_hardware_gesture(gesture.upper())

        elif kind == "gesture":
            g = pkt.get("gesture", "")
            if g and self.on_hardware_gesture:
                self.on_hardware_gesture(g.upper())

        elif kind == "scroll":
            delta_y = pkt.get("delta_y", 0)
            g = "SCROLL_UP" if delta_y > 0 else "SCROLL_DOWN"
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
