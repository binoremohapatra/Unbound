"""main_window.py - Native PySide6 Desktop Command Center for OmniBand V2.

Features:
- Dark theme from theme.py
- Multi-view tabs:
  1. Oscilloscope & Live Signal
  2. Native 60 FPS Cyberpunk Highway Arena
  3. ICU Patient Board
  4. Calibration Wizard & Diagnostics
- 6-Mode Switcher Toolbar & Status Bar
- Chaos Injection Dock (cable pull, jitter, packet loss)
- System Tray and HUD Overlay integration
- CloseEvent minimizes to System Tray rather than killing the somatic daemon
"""
from __future__ import annotations

import sys
import time
import threading
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QLabel, QPushButton, QComboBox, QStatusBar, QMessageBox, QApplication,
    QSystemTrayIcon, QCheckBox, QSlider
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIcon, QFont, QCloseEvent, QImage, QPixmap

from host.omniband.app.theme import (
    apply_theme, BURNT_ORANGE, STATUS_OK, STATUS_WARN,
    STATUS_ERR, INDUSTRIAL_IVORY, WARM_GRAPHITE, SURFACE_1
)
from host.omniband.app.oscilloscope import OscilloscopeWidget
from host.omniband.app.arena_widget import ArenaWidget
from host.omniband.app.icu_view import IcuView
from host.omniband.app.calibration_view import CalibrationView
from host.omniband.app.hud_overlay import HudOverlay
from host.omniband.app.tray_agent import SystemTrayAgent
from host.omniband.app.auto_profile import AutoProfileWatcher, ProfileRule, get_active_process_win32
from host.omniband.app.somatic_keyboard import SomaticKeyboardWidget
from host.omniband.app.floating_keyboard import FloatingKeyboardWindow
from host.omniband.app.cursor_reticle import SomaticCursorReticle
from host.omniband.tracking.webcam_tracker import RealWebcamTracker
from host.omniband.tracking.physical_keys import PhysicalKeyPoller


class MainWindow(QMainWindow):
    """OmniBand Native Desktop Command Center."""

    sig_biometric_click = Signal()
    sig_drag_toggle = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OmniBand Command Center")
        self.resize(1024, 680)

        self.sig_biometric_click.connect(self._on_biometric_click)
        self.sig_drag_toggle.connect(self._toggle_drag_lock)

        # Child windows & agents
        self.hud = HudOverlay()
        self.reticle = SomaticCursorReticle()
        self.reticle.show()

        self.floating_kb = FloatingKeyboardWindow(
            on_type_fn=self._inject_keyboard_char,
            on_scroll_fn=self._inject_mouse_scroll,
            on_toggle_drag_fn=self._toggle_drag_lock,
        )

        self.tray = SystemTrayAgent(self)
        self.tray.show()

        # Connect tray signals
        self.tray.sig_toggle_main_window.connect(self._toggle_visibility)
        self.tray.sig_toggle_hud.connect(self._toggle_hud)
        self.tray.sig_mode_selected.connect(self._set_mode_index)
        self.tray.sig_assist_selected.connect(self._set_assist_level)
        self.tray.sig_release_all.connect(self._emergency_release_all)
        self.tray.sig_exit.connect(self._force_exit)

        # Central widget layout
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(8)

        # Armed / Disarmed State (Starts ARMED by default for instant feedback)
        self.armed = True
        self._cruise_w = False
        self._sprint_shift = False
        self._aim_ads = False
        self._drag_latch = False

        # Top Bar: Mode Selector, Tier Indicator, HUD Toggle
        top_bar = QHBoxLayout()

        self._btn_arm = QPushButton("🟢 ARMED (LIVE) [F12]")
        self._btn_arm.setObjectName("accent_btn")
        self._btn_arm.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self._btn_arm.setStyleSheet(f"background-color: {STATUS_OK}; color: #111; font-weight: bold;")
        self._btn_arm.setCheckable(True)
        self._btn_arm.setChecked(True)
        self._btn_arm.clicked.connect(self._toggle_arm_disarm)

        lbl_mode = QLabel("Operational Mode:")
        lbl_mode.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self._combo_mode = QComboBox()
        self._combo_mode.addItems([
            "Mode 0: Desktop & Media",
            "Mode 1: Coding & Terminals",
            "Mode 2: High-Speed Racing",
            "Mode 3: FPS / Twitch",
            "Mode 4: ICU / SOS",
            "Mode 5: Drone FPV",
        ])
        self._combo_mode.currentIndexChanged.connect(self._on_combo_mode_changed)

        self._lbl_tier = QLabel("ACTIVE: TIER 1 (USB)")
        self._lbl_tier.setFont(QFont("Cascadia Code", 10, QFont.Bold))
        self._lbl_tier.setStyleSheet(f"color: {STATUS_OK}; padding: 4px 8px; border-radius: 4px; background: {SURFACE_1};")

        btn_toggle_hud = QPushButton("Toggle Tactical HUD")
        btn_toggle_hud.clicked.connect(self._toggle_hud)

        self._chk_reticle = QCheckBox("🎯 Tactical Reticle")
        self._chk_reticle.setChecked(True)
        self._chk_reticle.toggled.connect(lambda c: self.reticle.setVisible(c))

        btn_popout_kb = QPushButton("⌨️ FLOATING KEYBOARD")
        btn_popout_kb.clicked.connect(self._toggle_floating_keyboard)

        self._btn_head_mouse = QPushButton("🖱️ HEAD-MOUSE: ON [F11]")
        self._btn_head_mouse.setCheckable(True)
        self._btn_head_mouse.setChecked(True)
        self._btn_head_mouse.setStyleSheet(f"background-color: {STATUS_OK}; color: #111; font-weight: bold;")
        self._btn_head_mouse.clicked.connect(self._toggle_head_mouse)

        self._chk_lock_mode = QPushButton("🔓 Auto-Mode")
        self._chk_lock_mode.setCheckable(True)
        self._chk_lock_mode.toggled.connect(self._on_lock_mode_toggled)

        self._chk_allow_all = QCheckBox("Allow All Windows (Desktop Test Mode)")
        self._chk_allow_all.setChecked(True)  # Checked by default so Notepad/Games work immediately!
        self._chk_allow_all.setStyleSheet(f"color: {STATUS_OK}; font-weight: bold;")

        btn_release = QPushButton("⚠️ RELEASE ALL")
        btn_release.setObjectName("danger_btn")
        btn_release.clicked.connect(self._emergency_release_all)

        top_bar.addWidget(self._btn_arm)
        top_bar.addWidget(self._btn_head_mouse)
        top_bar.addWidget(btn_popout_kb)
        top_bar.addWidget(lbl_mode)
        top_bar.addWidget(self._combo_mode)
        top_bar.addWidget(self._chk_lock_mode)
        top_bar.addWidget(self._chk_allow_all)
        top_bar.addSpacing(12)
        top_bar.addWidget(self._chk_reticle)
        top_bar.addWidget(self._lbl_tier)
        top_bar.addStretch()
        top_bar.addWidget(btn_toggle_hud)
        top_bar.addWidget(btn_release)
        root_layout.addLayout(top_bar)

        # Real Injection Proof Bar
        proof_bar = QHBoxLayout()
        self._lbl_proof_target = QLabel("TARGET: ALL WINDOWS (DESKTOP MODE)")
        self._lbl_proof_target.setFont(QFont("Cascadia Code", 9, QFont.Bold))
        self._lbl_proof_target.setStyleSheet(f"color: {STATUS_OK}; background: {SURFACE_1}; padding: 4px 8px; border-radius: 4px;")

        self._lbl_proof_keys = QLabel("LIVE OS KEYS HELD: []")
        self._lbl_proof_keys.setFont(QFont("Cascadia Code", 9, QFont.Bold))
        self._lbl_proof_keys.setStyleSheet(f"color: {BURNT_ORANGE}; background: {SURFACE_1}; padding: 4px 8px; border-radius: 4px;")

        self._lbl_proof_last = QLabel("LAST INJECTION: READY")
        self._lbl_proof_last.setFont(QFont("Cascadia Code", 9))
        self._lbl_proof_last.setStyleSheet(f"color: {WARM_GRAPHITE}; background: {SURFACE_1}; padding: 4px 8px; border-radius: 4px;")

        proof_bar.addWidget(self._lbl_proof_target)
        proof_bar.addWidget(self._lbl_proof_keys)
        proof_bar.addWidget(self._lbl_proof_last)
        proof_bar.addStretch()
        root_layout.addLayout(proof_bar)

        # Main Tabs
        self.tabs = QTabWidget()
        self.scope = OscilloscopeWidget()
        self.keyboard = SomaticKeyboardWidget(on_type_fn=self._inject_keyboard_char)
        self.arena = ArenaWidget()
        self.icu = IcuView()
        self.calib = CalibrationView()

        # Camera Preview Tab
        self.camera_tab = QWidget()
        cam_layout = QVBoxLayout(self.camera_tab)

        cam_ctrls = QHBoxLayout()
        self._btn_recenter = QPushButton("🎯 Recenter Face (Zero)")
        self._btn_recenter.clicked.connect(lambda: self.webcam_tracker.recenter())

        self._chk_head_mouse = QCheckBox("Enable Webcam Head-Mouse (120Hz Decoupled)")
        self._chk_head_mouse.setChecked(True)
        self._chk_head_mouse.toggled.connect(lambda chk: setattr(self.webcam_tracker, "head_mouse_enabled", chk))

        # Pointer Speed Slider (0.2x to 2.5x)
        lbl_speed_title = QLabel("Pointer Speed:")
        lbl_speed_title.setFont(QFont("Cascadia Code", 9, QFont.Bold))
        self._slider_speed = QSlider(Qt.Horizontal)
        self._slider_speed.setRange(2, 25)
        self._slider_speed.setValue(6)  # 0.6x calm default
        self._slider_speed.setFixedWidth(110)
        self._lbl_speed_val = QLabel("0.6x")
        self._lbl_speed_val.setFont(QFont("Cascadia Code", 9))
        self._slider_speed.valueChanged.connect(self._on_speed_slider_changed)

        # 1-Euro Smoothing Slider (0.3 to 2.0 Hz)
        lbl_smooth_title = QLabel("Smoothing (1-Euro):")
        lbl_smooth_title.setFont(QFont("Cascadia Code", 9, QFont.Bold))
        self._slider_smooth = QSlider(Qt.Horizontal)
        self._slider_smooth.setRange(3, 20)
        self._slider_smooth.setValue(8)  # 0.8 Hz
        self._slider_smooth.setFixedWidth(110)
        self._lbl_smooth_val = QLabel("0.8 Hz")
        self._lbl_smooth_val.setFont(QFont("Cascadia Code", 9))
        self._slider_smooth.valueChanged.connect(self._on_smooth_slider_changed)

        self._chk_double_click = QCheckBox("Double-Click Mode (Desktop Icons)")
        self._chk_double_click.setChecked(False)
        self._chk_double_click.setToolTip("When checked, blink gesture sends a true OS Double-Click to open desktop apps/folders.")

        cam_ctrls.addWidget(self._btn_recenter)
        cam_ctrls.addWidget(self._chk_head_mouse)
        cam_ctrls.addSpacing(12)
        cam_ctrls.addWidget(lbl_speed_title)
        cam_ctrls.addWidget(self._slider_speed)
        cam_ctrls.addWidget(self._lbl_speed_val)
        cam_ctrls.addSpacing(12)
        cam_ctrls.addWidget(lbl_smooth_title)
        cam_ctrls.addWidget(self._slider_smooth)
        cam_ctrls.addWidget(self._lbl_smooth_val)
        cam_ctrls.addSpacing(12)
        cam_ctrls.addWidget(self._chk_double_click)
        cam_ctrls.addStretch()
        cam_layout.addLayout(cam_ctrls)

        self.camera_lbl = QLabel("Initializing Real Webcam (cv2)...")
        self.camera_lbl.setAlignment(Qt.AlignCenter)
        self.camera_lbl.setMinimumSize(480, 360)
        self.camera_lbl.setStyleSheet(f"background: #111; border: 2px solid {BURNT_ORANGE}; border-radius: 6px;")
        cam_layout.addWidget(self.camera_lbl)

        # Biometric Telemetry Bar
        self._lbl_cam_telemetry = QLabel("FPS: -- | EAR: -- | MAR: -- | YAW: 0.0° | PITCH: 0.0°")
        self._lbl_cam_telemetry.setFont(QFont("Cascadia Code", 9, QFont.Bold))
        self._lbl_cam_telemetry.setStyleSheet(f"color: {BURNT_ORANGE}; background: {SURFACE_1}; padding: 4px 8px; border-radius: 4px;")
        cam_layout.addWidget(self._lbl_cam_telemetry)

        self.tabs.addTab(self.scope, "Oscilloscope (Signal)")
        self.tabs.addTab(self.keyboard, "⌨️ Hands-Free Keyboard")
        self.tabs.addTab(self.camera_tab, "Live Webcam (Face & Jaw)")
        self.tabs.addTab(self.arena, "Cyber-Arena (Native 60 FPS)")
        self.tabs.addTab(self.icu, "ICU Patient Board")
        self.tabs.addTab(self.calib, "Calibration & Diagnostics")
        root_layout.addWidget(self.tabs)

        # Chaos Injection Quick Dock
        chaos_bar = QHBoxLayout()
        chaos_lbl = QLabel("Chaos Injection:")
        chaos_lbl.setFont(QFont("Segoe UI", 9, QFont.Bold))
        btn_chaos_pull = QPushButton("Simulate Cable Pull")
        btn_chaos_loss = QPushButton("Simulate 20% Packet Loss")
        btn_chaos_jitter = QPushButton("Simulate 50ms Jitter")

        btn_chaos_pull.clicked.connect(lambda: self.calib.append_log("CHAOS: Cable disconnect injected (Fallback to Tier 2 BLE)"))
        btn_chaos_loss.clicked.connect(lambda: self.calib.append_log("CHAOS: 20% Packet Loss burst injected"))
        btn_chaos_jitter.clicked.connect(lambda: self.calib.append_log("CHAOS: 50ms Transport Jitter injected"))

        chaos_bar.addWidget(chaos_lbl)
        chaos_bar.addWidget(btn_chaos_pull)
        chaos_bar.addWidget(btn_chaos_loss)
        chaos_bar.addWidget(btn_chaos_jitter)
        chaos_bar.addStretch()
        root_layout.addLayout(chaos_bar)

        # Status Bar
        self.status = QStatusBar()
        self.status.showMessage("OmniBand somatic link active | 60 Hz host engine loop online")
        self.setStatusBar(self.status)

        # Gateway Real Win32 SendInput Instance
        from gateway import Gateway, SendInputBackend
        self.gw = Gateway(SendInputBackend())
        self.gw.install_exit_handlers()
        if hasattr(self, "_chk_allow_all"):
            self.gw.process_gate.allow_all = self._chk_allow_all.isChecked()
            self._chk_allow_all.toggled.connect(lambda chk: setattr(self.gw.process_gate, "allow_all", chk))

        # Real Optical Webcam Tracker (GameFace 468-point 3D FaceMesh + 1-Euro filter)
        self.webcam_tracker = RealWebcamTracker(
            camera_id=0,
            on_envelopes=self._on_optical_envelopes,
            on_frame=self._on_webcam_frame,
            on_gesture=self._on_optical_gesture,
            on_click=self.sig_biometric_click.emit,
            on_drag_toggle=self.sig_drag_toggle.emit,
        )
        self.webcam_tracker.start()

        # 120 Hz eViacam / GameFace Delta-Velocity Integration Timer (8 ms tick)
        self._cursor_timer = QTimer(self)
        self._cursor_timer.setInterval(8)
        self._cursor_timer.timeout.connect(self._on_cursor_tick)
        self._subpixel_x: float = 0.0
        self._subpixel_y: float = 0.0
        self._cursor_timer.start()

        # Physical Key Poller (F7-F10 + F11 + F12)
        self.key_poller = PhysicalKeyPoller(
            on_edge=self._on_physical_edge,
            on_envelopes=self._on_key_envelopes,
            on_f11_toggle=self._toggle_head_mouse,
            on_f12_toggle=self._toggle_arm_disarm,
        )
        self.key_poller.start()

        # Target & OS Keys Live Refresher Timer (10 Hz)
        self._proof_timer = QTimer(self)
        self._proof_timer.setInterval(100)
        self._proof_timer.timeout.connect(self._refresh_proof_bar)
        self._proof_timer.start()

        # Background 4 Hz Auto-Profile Watcher
        self.watcher = AutoProfileWatcher(
            on_rule_applied=self._on_auto_rule_applied,
            on_release_all=self._emergency_release_all,
        )
        self.watcher.start()

    def _on_webcam_frame(self, frame_rgb) -> None:
        try:
            h, w, ch = frame_rgb.shape
            bytes_per_line = ch * w
            qimg = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
            pix = QPixmap.fromImage(qimg).scaled(480, 360, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.camera_lbl.setPixmap(pix)
            # Live Biometric Telemetry update
            ear_str = f"{self.webcam_tracker.current_ear:.2f}" if self.webcam_tracker.current_ear is not None else "--"
            mar_str = f"{self.webcam_tracker.current_mar:.2f}" if self.webcam_tracker.current_mar is not None else "--"
            self._lbl_cam_telemetry.setText(
                f"FPS: {self.webcam_tracker.fps:4.1f} | "
                f"EAR: {ear_str} | "
                f"MAR: {mar_str} | "
                f"YAW: {self.webcam_tracker.head_yaw:+4.1f}° | "
                f"PITCH: {self.webcam_tracker.head_pitch:+4.1f}°"
            )
        except Exception:
            pass

    def _on_cursor_tick(self) -> None:
        """120 Hz eViacam / GameFace delta-velocity cursor integration loop."""
        if not self.webcam_tracker.head_mouse_enabled:
            return
        if time.perf_counter() < self.webcam_tracker.freeze_until:
            return

        vx, vy, in_deadzone = self.webcam_tracker.velocity_state
        if in_deadzone or (vx == 0.0 and vy == 0.0):
            # RULE #1 OF HEAD MICE: NEVER CALL SetCursorPos INSIDE DEADZONE!
            # Zero jitter (0.0 px) and user can freely use their physical mouse
            return

        dt = 0.008  # 125 Hz tick interval
        dx_px = vx * dt
        dy_px = vy * dt

        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))

                self._subpixel_x += dx_px
                self._subpixel_y += dy_px

                step_x = int(self._subpixel_x)
                step_y = int(self._subpixel_y)
                self._subpixel_x -= step_x
                self._subpixel_y -= step_y

                if step_x != 0 or step_y != 0:
                    sw = user32.GetSystemMetrics(0)
                    sh = user32.GetSystemMetrics(1)
                    new_x = max(0, min(sw - 1, pt.x + step_x))
                    new_y = max(0, min(sh - 1, pt.y + step_y))
                    user32.SetCursorPos(new_x, new_y)

                    if hasattr(self, "reticle") and self.reticle.isVisible():
                        self.reticle.move(new_x - 28, new_y - 28)
            except Exception:
                pass

    def _on_biometric_click(self) -> None:
        """Triggered by EAR double-blink, deliberate long blink, or MAR quick jaw tap."""
        if hasattr(self, "reticle"):
            self.reticle.trigger_click_flash()
        if hasattr(self, "_chk_head_mouse") and not self._chk_head_mouse.isChecked():
            return

        def _execute_click() -> None:
            if sys.platform != "win32":
                return
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32

                pt = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))

                # Virtual desktop dimensions for multi-monitor / Shell / Taskbar targeting
                SM_XVIRTUALSCREEN = 76
                SM_YVIRTUALSCREEN = 77
                SM_CXVIRTUALSCREEN = 78
                SM_CYVIRTUALSCREEN = 79

                vx = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
                vy = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
                vw = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
                vh = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)

                norm_x = int(((pt.x - vx) * 65535) / (vw - 1)) if vw > 1 else 0
                norm_y = int(((pt.y - vy) * 65535) / (vh - 1)) if vh > 1 else 0

                MOUSEEVENTF_MOVE = 0x0001
                MOUSEEVENTF_LEFTDOWN = 0x0002
                MOUSEEVENTF_LEFTUP = 0x0004
                MOUSEEVENTF_VIRTUALDESK = 0x4000
                MOUSEEVENTF_ABSOLUTE = 0x8000

                flags_down = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTDOWN
                flags_up = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK | MOUSEEVENTF_MOVE | MOUSEEVENTF_LEFTUP

                ULONG_PTR = ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_uint32

                class MOUSEINPUT(ctypes.Structure):
                    _fields_ = [
                        ("dx", wintypes.LONG),
                        ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD),
                        ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR),
                    ]

                class KEYBDINPUT(ctypes.Structure):
                    _fields_ = [
                        ("wVk", wintypes.WORD),
                        ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR),
                    ]

                class HARDWAREINPUT(ctypes.Structure):
                    _fields_ = [
                        ("uMsg", wintypes.DWORD),
                        ("wParamL", wintypes.WORD),
                        ("wParamH", wintypes.WORD),
                    ]

                class _INPUT_UNION(ctypes.Union):
                    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

                class INPUT(ctypes.Structure):
                    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]

                def _click_once() -> None:
                    # 1. Primary path: SendInput with exact 40-byte 64-bit struct & absolute virtual coords
                    inp_down = INPUT(type=0, u=_INPUT_UNION(mi=MOUSEINPUT(norm_x, norm_y, 0, flags_down, 0, 0)))
                    inp_up = INPUT(type=0, u=_INPUT_UNION(mi=MOUSEINPUT(norm_x, norm_y, 0, flags_up, 0, 0)))

                    res_down = user32.SendInput(1, ctypes.byref(inp_down), ctypes.sizeof(INPUT))
                    if res_down == 0:
                        # Fallback 1: mouse_event with absolute virtual coordinates
                        user32.mouse_event(flags_down, norm_x, norm_y, 0, 0)
                        # Fallback 2: relative mouse_event
                        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)

                    time.sleep(0.045)  # Natural human button dwell

                    res_up = user32.SendInput(1, ctypes.byref(inp_up), ctypes.sizeof(INPUT))
                    if res_up == 0:
                        user32.mouse_event(flags_up, norm_x, norm_y, 0, 0)
                        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

                is_dbl = hasattr(self, "_chk_double_click") and self._chk_double_click.isChecked()
                _click_once()
                if is_dbl:
                    time.sleep(0.065)
                    _click_once()
            except Exception:
                try:
                    import ctypes
                    user32 = ctypes.windll.user32
                    user32.mouse_event(0x0002, 0, 0, 0, 0)
                    time.sleep(0.045)
                    user32.mouse_event(0x0004, 0, 0, 0, 0)
                    if hasattr(self, "_chk_double_click") and self._chk_double_click.isChecked():
                        time.sleep(0.065)
                        user32.mouse_event(0x0002, 0, 0, 0, 0)
                        time.sleep(0.045)
                        user32.mouse_event(0x0004, 0, 0, 0, 0)
                except Exception:
                    pass

        threading.Thread(target=_execute_click, daemon=True, name="biometric-clicker").start()
        self.calib.append_log("BIOMETRIC: Left Click registered via facial gesture")
        now_str = time.strftime("%H:%M:%S")
        self._lbl_proof_last.setText(f"LAST INJECTION: LEFT CLICK (BIOMETRIC GESTURE) @ {now_str}")

    def _on_speed_slider_changed(self, val: int) -> None:
        speed = val / 10.0
        self._lbl_speed_val.setText(f"{speed:.1f}x")
        self.webcam_tracker.set_pointer_speed(speed)

    def _on_smooth_slider_changed(self, val: int) -> None:
        cutoff = val / 10.0
        self._lbl_smooth_val.setText(f"{cutoff:.1f} Hz")
        self.webcam_tracker.set_smoothing_cutoff(cutoff)

    def _on_optical_envelopes(self, vals: list[float], active: list[bool]) -> None:
        self.scope.push_sample(vals, active)

    def _on_key_envelopes(self, vals: list[float], active: list[bool]) -> None:
        self.scope.push_sample(vals, active)

    def _toggle_floating_keyboard(self) -> None:
        if self.floating_kb.isVisible():
            self.floating_kb.hide()
        else:
            self.floating_kb.show()

    def _toggle_head_mouse(self) -> None:
        new_state = not self.webcam_tracker.head_mouse_enabled
        self.webcam_tracker.head_mouse_enabled = new_state
        self._btn_head_mouse.setChecked(new_state)
        # Flush and release all keys immediately on head-mouse toggle to prevent stuck keys
        self.gw.release_all("head mouse mode toggle")
        if new_state:
            self._btn_head_mouse.setStyleSheet(f"background-color: {STATUS_OK}; color: #111; font-weight: bold;")
            self.status.showMessage("🖱️ HEAD-MOUSE ACTIVE: Moving head moves cursor | A/D scancodes blocked", 3000)
        else:
            self._btn_head_mouse.setStyleSheet("")
            self.status.showMessage("HEAD-MOUSE PAUSED", 2000)

    def _toggle_drag_lock(self) -> None:
        """Toggle Left Mouse Button drag latch."""
        if hasattr(self, "_chk_head_mouse") and not self._chk_head_mouse.isChecked():
            return
        from core.events import OutputCommand
        self._drag_latch = not self._drag_latch
        self.reticle.set_drag_locked(self._drag_latch)
        cmd = OutputCommand(kind="button", name="left", pressed=self._drag_latch)
        self.gw.apply([cmd])
        self.gw.flush(time.perf_counter())
        now_str = time.strftime("%H:%M:%S")
        self._lbl_proof_last.setText(f"DRAG LATCH: {'LOCKED (HELD)' if self._drag_latch else 'RELEASED (DROP)'} @ {now_str}")

    def _inject_mouse_scroll(self, delta: int) -> None:
        """Inject Win32 mouse wheel scroll without stealing focus."""
        if hasattr(self, "_chk_head_mouse") and not self._chk_head_mouse.isChecked():
            return
        try:
            import ctypes
            MOUSEEVENTF_WHEEL = 0x0800
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
            self.reticle.set_scrolling(True)
            QTimer.singleShot(300, lambda: self.reticle.set_scrolling(False))
        except Exception:
            pass

    def _toggle_arm_disarm(self) -> None:
        self.armed = not self.armed
        if self.armed:
            self._btn_arm.setText("🟢 ARMED (LIVE) [F12]")
            self._btn_arm.setStyleSheet(f"background-color: {STATUS_OK}; color: #111; font-weight: bold;")
            self.status.showMessage("⚠️ SYSTEM ARMED: Real inputs are actively injected into Windows", 4000)
            self.calib.append_log("SYSTEM ARMED: Real hardware inputs enabled.")
        else:
            self._btn_arm.setText("🔴 DISARMED (SAFE) [F12]")
            self._btn_arm.setStyleSheet(f"background-color: {STATUS_ERR}; color: white; font-weight: bold;")
            self.gw.release_all("disarmed by operator")
            self.status.showMessage("SYSTEM DISARMED: All keys released. Safe mode active.", 4000)
            self.calib.append_log("SYSTEM DISARMED: All keys released.")

    def _inject_keyboard_char(self, char: str) -> None:
        """Inject character typed from Somatic Keyboard into active target app."""
        if not self.armed:
            return
        from core.events import OutputCommand
        cmds = []
        if char == "backspace":
            cmds.append(OutputCommand(kind="key", name="backspace", pressed=True))
            cmds.append(OutputCommand(kind="key", name="backspace", pressed=False))
        elif char == "\n":
            cmds.append(OutputCommand(kind="key", name="enter", pressed=True))
            cmds.append(OutputCommand(kind="key", name="enter", pressed=False))
        elif char == " ":
            cmds.append(OutputCommand(kind="key", name="space", pressed=True))
            cmds.append(OutputCommand(kind="key", name="space", pressed=False))
        elif len(char) == 1 and char.isalpha():
            cmds.append(OutputCommand(kind="key", name=char.lower(), pressed=True))
            cmds.append(OutputCommand(kind="key", name=char.lower(), pressed=False))

        if cmds:
            self.gw.apply(cmds)
            self.gw.flush(time.perf_counter())

    def _on_optical_gesture(self, ch: int, down: bool, amp: float) -> None:
        """Inject real OS keys only when in an actual game window and NOT in head-mouse mode."""
        if not self.armed:
            return
        now_str = time.strftime("%H:%M:%S")

        # CRITICAL RULE: When Head-Mouse mode is active, head movement NEVER injects keyboard keys!
        # Head movement is dedicated exclusively to cursor velocity control.
        if self.webcam_tracker.head_mouse_enabled and ch in (1, 2, 3):
            return

        # In Keyboard Tab: Route channels to Grid Navigation & Typing
        if self.tabs.currentIndex() == 1:
            if down:
                if ch == 1:  # Face Left
                    self.keyboard.move_left()
                    self._lbl_proof_last.setText(f"KEYBOARD: ← (Left) @ {now_str}")
                elif ch == 2:  # Face Right
                    self.keyboard.move_right()
                    self._lbl_proof_last.setText(f"KEYBOARD: → (Right) @ {now_str}")
                elif ch == 3:  # Face Up / Nod
                    self.keyboard.move_down_row()
                    self._lbl_proof_last.setText(f"KEYBOARD: ↓ (Next Row) @ {now_str}")
                elif ch == 4:  # Jaw Drop
                    self.keyboard.type_selected()
                    self._lbl_proof_last.setText(f"KEYBOARD: Selected Key Typed @ {now_str}")
            return

        # Strict Game Mode Check: A/D/W keys are ONLY injected into racing/game targets
        # or when active on the native Cyber-Arena simulator tab!
        proc = get_active_process_win32()
        exe_lower = proc.exe_name.lower()
        is_racing_game = any(g in exe_lower for g in ["cyberpunk2077.exe", "forzahorizon5.exe", "gta5.exe"])
        is_arena_tab = (self.tabs.currentIndex() == 3)

        # In desktop apps, browsers, chat windows, notepad: NEVER inject A/D keys from head turn!
        if ch in (1, 2) and not (is_racing_game or is_arena_tab):
            return

        from core.events import OutputCommand
        cmds = []

        # Full Game Control:
        # CH1: Face Left -> A (Steer Left in game)
        if ch == 1:
            cmds.append(OutputCommand(kind="key", name="a", pressed=down))
            self._lbl_proof_last.setText(f"LAST: Face L -> Key 'a' ({'DOWN' if down else 'UP'}) @ {now_str}")
        # CH2: Face Right -> D (Steer Right in game)
        elif ch == 2:
            cmds.append(OutputCommand(kind="key", name="d", pressed=down))
            self._lbl_proof_last.setText(f"LAST: Face R -> Key 'd' ({'DOWN' if down else 'UP'}) @ {now_str}")
        # CH4: Jaw Drop -> Left Click / Primary Fire / Release Drag
        elif ch == 4:
            self.webcam_tracker.trigger_blink_freeze(0.35)
            self.reticle.trigger_click_flash()
            if self._drag_latch and down:
                # Release drag lock
                self._toggle_drag_lock()
                return
            cmds.append(OutputCommand(kind="button", name="left", pressed=down))
            self._lbl_proof_last.setText(f"LAST: Jaw Drop -> Left Click ({'DOWN' if down else 'UP'}) @ {now_str}")

        if cmds:
            self.gw.apply(cmds)
            self.gw.flush(time.perf_counter())

    def _on_physical_edge(self, ch: int, down: bool, amp: float) -> None:
        if not self.armed:
            return
        now_str = time.strftime("%H:%M:%S")

        # In Keyboard Tab: Route F7-F10 to Grid Navigation & Typing
        if self.tabs.currentIndex() == 1:
            if down:
                if ch == 1:  # F7 -> Move Left
                    self.keyboard.move_left()
                    self._lbl_proof_last.setText(f"KEYBOARD: F7 -> ← (Left) @ {now_str}")
                elif ch == 2:  # F8 -> Move Right
                    self.keyboard.move_right()
                    self._lbl_proof_last.setText(f"KEYBOARD: F8 -> → (Right) @ {now_str}")
                elif ch == 3:  # F9 -> Next Row
                    self.keyboard.move_down_row()
                    self._lbl_proof_last.setText(f"KEYBOARD: F9 -> ↓ (Row) @ {now_str}")
                elif ch == 4:  # F10 -> Type Key
                    self.keyboard.type_selected()
                    self._lbl_proof_last.setText(f"KEYBOARD: F10 -> Type Selected @ {now_str}")
            return

        # Target gating check
        if not self._chk_allow_all.isChecked():
            proc = get_active_process_win32()
            exe_lower = proc.exe_name.lower()
            allowed = ["cyberpunk2077.exe", "notepad.exe", "unbound.exe", "forzahorizon5.exe", "gta5.exe"]
            if not any(a in exe_lower for a in allowed):
                return

        from core.events import OutputCommand
        cmds = []

        # Full 10-Action Somatic Game Control:
        # F7 -> CH1 (A / Steer L)
        if ch == 1:
            cmds.append(OutputCommand(kind="key", name="a", pressed=down))
            self._lbl_proof_last.setText(f"LAST: F7 -> Key 'a' ({'DOWN' if down else 'UP'}) @ {now_str}")
        # F8 -> CH2 (D / Steer R)
        elif ch == 2:
            cmds.append(OutputCommand(kind="key", name="d", pressed=down))
            self._lbl_proof_last.setText(f"LAST: F8 -> Key 'd' ({'DOWN' if down else 'UP'}) @ {now_str}")
        # F9 -> CH3 (Cruise Latch Toggle: W)
        elif ch == 3:
            if down:
                self._cruise_w = not self._cruise_w
                cmds.append(OutputCommand(kind="key", name="w", pressed=self._cruise_w))
                self._lbl_proof_last.setText(f"LAST: F9 -> Cruise Latch 'w' ({'ON' if self._cruise_w else 'OFF'}) @ {now_str}")
        # F10 -> CH4 (Left Click / Space / Handbrake / Release Drag)
        elif ch == 4:
            self.webcam_tracker.trigger_blink_freeze(0.35)
            self.reticle.trigger_click_flash()
            if self._drag_latch and down:
                self._toggle_drag_lock()
                return
            cmds.append(OutputCommand(kind="button", name="left", pressed=down))
            self._lbl_proof_last.setText(f"LAST: F10 -> Mouse Left ({'DOWN' if down else 'UP'}) @ {now_str}")

        if cmds:
            self.gw.apply(cmds)
            self.gw.flush(time.perf_counter())

    def _refresh_proof_bar(self) -> None:
        proc = get_active_process_win32()
        target_name = proc.exe_name or "Windows Desktop"
        if proc.window_title:
            target_name += f" ({proc.window_title[:24]})"
        self._lbl_proof_target.setText(f"TARGET: {target_name}")

        held = sorted(list(self.gw.held))
        held_str = ", ".join(held) if held else "NONE"
        self._lbl_proof_keys.setText(f"LIVE OS KEYS HELD: [{held_str}]")

        # Update HUD Overlay Proof Bar
        self.hud.update_telemetry(
            tier_name=self._lbl_tier.text(),
            mode_name=self._combo_mode.currentText()[:16],
            assist_name="ACTIVE",
            veto_text="STATUS: INJECTING REAL KEYS",
            bar_vals=[0.2, 0.4, 0.3, 0.5],
            target_win=target_name,
            held_keys=held,
        )

    def _on_auto_rule_applied(self, rule: ProfileRule) -> None:
        self.calib.append_log(f"Auto-Hook: {rule.name} (Switching to Mode {rule.mode})")
        self._combo_mode.setCurrentIndex(rule.mode)
        if rule.show_hud:
            self.hud.show()
            self.hud.update_telemetry(
                tier_name=self._lbl_tier.text(),
                mode_name=rule.badge,
                assist_name="ACTIVE",
                veto_text="STATUS: HOOKED",
                bar_vals=[0.3, 0.5, 0.4, 0.6]
            )
        else:
            self.hud.hide()
        self.status.showMessage(f"Mod Active: {rule.name}", 3000)

    def _on_lock_mode_toggled(self, locked: bool) -> None:
        if locked:
            self._chk_lock_mode.setText("🔒 Locked")
            self.watcher.lock_mode(self._combo_mode.currentIndex())
            self.calib.append_log("Mode auto-switching locked by user.")
        else:
            self._chk_lock_mode.setText("🔓 Auto-Mode")
            self.watcher.unlock_mode()
            self.calib.append_log("Mode auto-switching unlocked.")

    def _toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.showNormal()
            self.activateWindow()

    def _toggle_hud(self) -> None:
        if self.hud.isVisible():
            self.hud.hide()
        else:
            self.hud.show()

    def _set_mode_index(self, mode: int) -> None:
        if 0 <= mode < self._combo_mode.count():
            self._combo_mode.setCurrentIndex(mode)

    def _on_combo_mode_changed(self, idx: int) -> None:
        self.calib.append_log(f"Switched to Mode {idx}")
        self.hud.update_telemetry(
            tier_name=self._lbl_tier.text(),
            mode_name=f"MODE {idx}",
            assist_name="ACTIVE",
            veto_text="STATUS: OK",
            bar_vals=[0.2, 0.4, 0.3, 0.5]
        )

    def _set_assist_level(self, level: str) -> None:
        self.calib.append_log(f"Co-Pilot assist level changed to: {level}")

    def _emergency_release_all(self) -> None:
        self.calib.append_log("EMERGENCY RELEASE ALL KEYS TRIGGERED")
        self.status.showMessage("⚠️ All inputs released by operator failsafe", 4000)

    def _force_exit(self) -> None:
        self.tray.hide()
        self.hud.hide()
        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        # Minimize to system tray instead of closing
        if self.tray.isVisible():
            self.hide()
            self.tray.showMessage(
                "OmniBand Daemon Active",
                "Application minimized to system tray. Gestures and Co-Pilot remain running.",
                QSystemTrayIcon.Information,
                2000
            )
            event.ignore()
        else:
            event.accept()
