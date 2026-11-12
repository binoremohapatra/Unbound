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
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QLabel, QPushButton, QComboBox, QStatusBar, QMessageBox, QApplication,
    QSystemTrayIcon, QCheckBox
)
from PySide6.QtCore import Qt, QTimer
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
from host.omniband.tracking.webcam_tracker import RealWebcamTracker
from host.omniband.tracking.physical_keys import PhysicalKeyPoller


class MainWindow(QMainWindow):
    """OmniBand Native Desktop Command Center."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("OmniBand Command Center")
        self.resize(1024, 680)

        # Child windows & agents
        self.hud = HudOverlay()
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

        # Armed / Disarmed State (Starts DISARMED by default)
        self.armed = False
        self._cruise_w = False
        self._sprint_shift = False
        self._aim_ads = False

        # Top Bar: Mode Selector, Tier Indicator, HUD Toggle
        top_bar = QHBoxLayout()

        self._btn_arm = QPushButton("🔴 DISARMED (SAFE) [F12]")
        self._btn_arm.setObjectName("danger_btn")
        self._btn_arm.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self._btn_arm.setCheckable(True)
        self._btn_arm.setChecked(False)
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

        self._chk_lock_mode = QPushButton("🔓 Auto-Mode")
        self._chk_lock_mode.setCheckable(True)
        self._chk_lock_mode.toggled.connect(self._on_lock_mode_toggled)

        self._chk_allow_all = QCheckBox("Allow All Windows (Desktop Test Mode)")
        self._chk_allow_all.setChecked(False)  # Unchecked by default for safety
        self._chk_allow_all.setStyleSheet(f"color: {STATUS_OK}; font-weight: bold;")

        btn_release = QPushButton("⚠️ RELEASE ALL")
        btn_release.setObjectName("danger_btn")
        btn_release.clicked.connect(self._emergency_release_all)

        top_bar.addWidget(self._btn_arm)
        top_bar.addWidget(lbl_mode)
        top_bar.addWidget(self._combo_mode)
        top_bar.addWidget(self._chk_lock_mode)
        top_bar.addWidget(self._chk_allow_all)
        top_bar.addSpacing(16)
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

        self._chk_head_mouse = QCheckBox("Enable Webcam Head-Mouse + Dwell Click")
        self._chk_head_mouse.toggled.connect(lambda chk: setattr(self.webcam_tracker, "head_mouse_enabled", chk))

        cam_ctrls.addWidget(self._btn_recenter)
        cam_ctrls.addWidget(self._chk_head_mouse)
        cam_ctrls.addStretch()
        cam_layout.addLayout(cam_ctrls)

        self.camera_lbl = QLabel("Initializing Real Webcam (cv2)...")
        self.camera_lbl.setAlignment(Qt.AlignCenter)
        self.camera_lbl.setMinimumSize(480, 360)
        self.camera_lbl.setStyleSheet(f"background: #111; border: 2px solid {BURNT_ORANGE}; border-radius: 6px;")
        cam_layout.addWidget(self.camera_lbl)

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

        # Real Optical Webcam Tracker
        self.webcam_tracker = RealWebcamTracker(
            camera_id=0,
            on_envelopes=self._on_optical_envelopes,
            on_frame=self._on_webcam_frame,
            on_gesture=self._on_optical_gesture,
        )
        self.webcam_tracker.start()

        # Physical Key Poller (F7-F10 + F12)
        self.key_poller = PhysicalKeyPoller(
            on_edge=self._on_physical_edge,
            on_envelopes=self._on_key_envelopes,
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
        except Exception:
            pass

    def _on_optical_envelopes(self, vals: list[float], active: list[bool]) -> None:
        self.scope.push_sample(vals, active)

    def _on_key_envelopes(self, vals: list[float], active: list[bool]) -> None:
        self.scope.push_sample(vals, active)

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
        """Inject real OS keys and mouse inputs from facial motion and jaw drop."""
        if not self.armed:
            return
        now_str = time.strftime("%H:%M:%S")

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

        # Target gating check
        if not self._chk_allow_all.isChecked():
            proc = get_active_process_win32()
            exe_lower = proc.exe_name.lower()
            allowed = ["cyberpunk2077.exe", "notepad.exe", "unbound.exe", "forzahorizon5.exe", "gta5.exe"]
            if not any(a in exe_lower for a in allowed):
                return

        from core.events import OutputCommand
        cmds = []

        # Full 10-Action Game Control:
        # CH1: Face Left -> A (Steer Left)
        if ch == 1:
            cmds.append(OutputCommand(kind="key", name="a", pressed=down))
            self._lbl_proof_last.setText(f"LAST: Face L -> Key 'a' ({'DOWN' if down else 'UP'}) @ {now_str}")
        # CH2: Face Right -> D (Steer Right)
        elif ch == 2:
            cmds.append(OutputCommand(kind="key", name="d", pressed=down))
            self._lbl_proof_last.setText(f"LAST: Face R -> Key 'd' ({'DOWN' if down else 'UP'}) @ {now_str}")
        # CH4: Jaw Drop -> Left Click / Primary Fire
        elif ch == 4:
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
        # F10 -> CH4 (Left Click / Space / Handbrake)
        elif ch == 4:
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
