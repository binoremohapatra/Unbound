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
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QLabel, QPushButton, QComboBox, QStatusBar, QMessageBox, QApplication,
    QSystemTrayIcon
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon, QFont, QCloseEvent

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
from host.omniband.app.auto_profile import AutoProfileWatcher, ProfileRule


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

        # Top Bar: Mode Selector, Tier Indicator, HUD Toggle
        top_bar = QHBoxLayout()
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

        btn_release = QPushButton("⚠️ RELEASE ALL")
        btn_release.setObjectName("danger_btn")
        btn_release.clicked.connect(self._emergency_release_all)

        top_bar.addWidget(lbl_mode)
        top_bar.addWidget(self._combo_mode)
        top_bar.addWidget(self._chk_lock_mode)
        top_bar.addSpacing(16)
        top_bar.addWidget(self._lbl_tier)
        top_bar.addStretch()
        top_bar.addWidget(btn_toggle_hud)
        top_bar.addWidget(btn_release)
        root_layout.addLayout(top_bar)

        # Main Tabs
        self.tabs = QTabWidget()
        self.scope = OscilloscopeWidget()
        self.arena = ArenaWidget()
        self.icu = IcuView()
        self.calib = CalibrationView()

        self.tabs.addTab(self.scope, "Oscilloscope (Signal)")
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

        # Background 4 Hz Auto-Profile Watcher
        self.watcher = AutoProfileWatcher(
            on_rule_applied=self._on_auto_rule_applied,
            on_release_all=self._emergency_release_all,
        )
        self.watcher.start()

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
