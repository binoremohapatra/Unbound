"""tray_agent.py - Windows System Tray Daemon (QSystemTrayIcon).

Keeps OmniBand somatic engine running seamlessly in the background.
Right-click menu provides:
- Show/Hide Command Center
- Toggle HUD Overlay
- Switch Modes (0..5)
- Co-Pilot Assist Level (OFF / LANE-CENTERING / FULL)
- Emergency Release All Keys (Failsafe)
- Exit
"""
from __future__ import annotations

from PySide6.QtWidgets import QSystemTrayIcon, QMenu, QApplication
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor
from PySide6.QtCore import Signal, Qt

from host.omniband.app.theme import BURNT_ORANGE, SURFACE_0


class SystemTrayAgent(QSystemTrayIcon):
    """Background system tray agent for OmniBand."""

    sig_toggle_main_window = Signal()
    sig_toggle_hud = Signal()
    sig_mode_selected = Signal(int)
    sig_assist_selected = Signal(str)
    sig_release_all = Signal()
    sig_exit = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setIcon(self._create_tray_icon())
        self.setToolTip("OmniBand V2 — Somatic OS Daemon Active")

        menu = QMenu()

        act_show = menu.addAction("Command Center Window")
        act_show.triggered.connect(self.sig_toggle_main_window.emit)

        act_hud = menu.addAction("Toggle Tactical HUD Overlay")
        act_hud.triggered.connect(self.sig_toggle_hud.emit)

        menu.addSeparator()

        # Startup toggle
        from host.omniband.app.startup_registry import is_run_at_startup_enabled, set_run_at_startup
        self._act_startup = menu.addAction("Start Unbound with Windows")
        self._act_startup.setCheckable(True)
        self._act_startup.setChecked(is_run_at_startup_enabled())
        self._act_startup.toggled.connect(self._on_toggle_startup)

        # Auto profile switcher toggle
        self._act_auto_profile = menu.addAction("Auto-Switch Game Profiles")
        self._act_auto_profile.setCheckable(True)
        self._act_auto_profile.setChecked(True)

        menu.addSeparator()

        # Mode submenu
        mode_menu = menu.addMenu("Operational Mode")
        modes = [
            (0, "Mode 0: Desktop & Media"),
            (1, "Mode 1: Coding & Terminals"),
            (2, "Mode 2: High-Speed Racing"),
            (3, "Mode 3: FPS / Twitch"),
            (4, "Mode 4: ICU / SOS"),
            (5, "Mode 5: Drone FPV"),
        ]
        for m_id, name in modes:
            act = mode_menu.addAction(name)
            act.triggered.connect(lambda _, m=m_id: self.sig_mode_selected.emit(m))

        # Co-Pilot submenu
        copilot_menu = menu.addMenu("AI Co-Pilot Assist")
        for level in ("OFF", "LANE-CENTERING", "FULL"):
            act = copilot_menu.addAction(f"Assist: {level}")
            act.triggered.connect(lambda _, lvl=level: self.sig_assist_selected.emit(lvl))

        menu.addSeparator()

        act_rel = menu.addAction("⚠️ Emergency Release All Keys")
        act_rel.triggered.connect(self.sig_release_all.emit)

        menu.addSeparator()

        act_exit = menu.addAction("Exit OmniBand Daemon")
        act_exit.triggered.connect(self.sig_exit.emit)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.Trigger:  # Single click
            self.sig_toggle_main_window.emit()

    def _on_toggle_startup(self, checked: bool) -> None:
        from host.omniband.app.startup_registry import set_run_at_startup
        set_run_at_startup(checked)

    def _create_tray_icon(self) -> QIcon:
        pix = QPixmap(32, 32)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(BURNT_ORANGE))
        p.setPen(Qt.NoPen)
        p.drawEllipse(4, 4, 24, 24)
        p.end()
        return QIcon(pix)

