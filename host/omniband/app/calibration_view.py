"""calibration_view.py - Calibration Wizard and Diagnostics View.

Provides:
- 5-Step Guided Hardware Auto-Calibration:
    Step 1: Resting Baseline (3s) -> mu_i, sigma_i
    Step 2: CH1 Left Forehead Max Flex (3 flexes) -> peak_1, C[0][j], gain_1
    Step 3: CH2 Right Forehead Max Flex (3 flexes) -> peak_2, C[1][j], gain_2
    Step 4: CH3 Left Temple Max Flex (3 flexes) -> peak_3, C[2][j], gain_3
    Step 5: CH4 Right Temple / Jaw Max Flex (3 flexes) -> peak_4, C[3][j], gain_4
- Auto-Gain & Dynamic Threshold Calculation (gain_i = 0.85 / (peak - mu), thresh = max(mu + 4*sigma, 0.42))
- 1-Click "Reset to Default Gains"
- Live BLE / Link Diagnostics (RSSI, Packet-Rate, Handshake)
- Live Cross-Talk Matrix & Latency Readout
- Real-time gesture and state audit event log
"""
from __future__ import annotations

import time
from typing import Optional, List
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QPlainTextEdit, QGroupBox, QGridLayout
)
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFont

from host.omniband.app.theme import (
    SURFACE_1, SURFACE_2, BURNT_ORANGE, STATUS_OK, STATUS_WARN,
    INDUSTRIAL_IVORY, WARM_GRAPHITE, FONT_MONO
)
from host.omniband.hardware.hardware_calibration import HardwareCalibrator


class CalibrationView(QWidget):
    """Wizard for sensor noise calibration, cross-talk suppression, and audit log."""

    sig_start_guided_calib = Signal()
    sig_reset_calib = Signal()
    sig_apply_thresholds = Signal(list)  # 4 floats for oscilloscope sync

    def __init__(self, calibrator: Optional[HardwareCalibrator] = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.calibrator = calibrator or HardwareCalibrator()

        self._calib_step = 0  # 0: Idle, 1: Baseline, 2: CH1, 3: CH2, 4: CH3, 5: CH4
        self._step_timer = QTimer(self)
        self._step_timer.timeout.connect(self._on_step_tick)
        self._samples_collector: List[List[float]] = []
        self._step_duration_s = 3.0
        self._step_start_t = 0.0

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)

        # Title & BLE Status Row
        header_row = QHBoxLayout()
        title = QLabel("HARDWARE AUTO-CALIBRATION & DIAGNOSTICS")
        title.setFont(QFont("Segoe UI", 13, QFont.Bold))
        title.setStyleSheet(f"color: {BURNT_ORANGE};")
        header_row.addWidget(title)
        header_row.addStretch()

        self._lbl_ble_status = QLabel("BLE: NOT CONNECTED")
        self._lbl_ble_status.setFont(QFont("Cascadia Code", 10, QFont.Bold))
        self._lbl_ble_status.setStyleSheet(f"color: {WARM_GRAPHITE}; background: {SURFACE_1}; padding: 4px 8px; border-radius: 4px;")
        header_row.addWidget(self._lbl_ble_status)

        self._lbl_pkt_rate = QLabel("Rate: 0.0 Hz | RSSI: -- dBm")
        self._lbl_pkt_rate.setFont(QFont("Cascadia Code", 10))
        self._lbl_pkt_rate.setStyleSheet(f"color: {STATUS_OK}; background: {SURFACE_1}; padding: 4px 8px; border-radius: 4px;")
        header_row.addWidget(self._lbl_pkt_rate)

        layout.addLayout(header_row)

        # 5-Step Guided Calibration Box
        cal_group = QGroupBox("Guided 5-Step Piezo Auto-Calibration Wizard")
        cal_layout = QVBoxLayout(cal_group)

        self._cal_info = QLabel("Click 'Start Guided Auto-Calibration' to record resting baseline and individual channel maximum flex peaks.")
        self._cal_info.setStyleSheet(f"color: {WARM_GRAPHITE}; font-size: 11pt;")
        cal_layout.addWidget(self._cal_info)

        btn_row = QHBoxLayout()
        self._btn_start_calib = QPushButton("🚀 Start Guided Auto-Calibration (Steps 1–5)")
        self._btn_start_calib.setStyleSheet(f"background-color: {BURNT_ORANGE}; color: white; font-weight: bold; padding: 6px 12px;")
        self._btn_start_calib.clicked.connect(self.start_guided_calibration)
        btn_row.addWidget(self._btn_start_calib)

        self._btn_reset_calib = QPushButton("🔄 Reset to Default Gains")
        self._btn_reset_calib.clicked.connect(self.reset_to_defaults)
        btn_row.addWidget(self._btn_reset_calib)

        cal_layout.addLayout(btn_row)

        self._progress = QProgressBar()
        self._progress.setRange(0, 5)
        self._progress.setValue(0)
        self._progress.setFormat("Step %v of 5: Idle")
        self._progress.setFixedHeight(22)
        cal_layout.addWidget(self._progress)
        layout.addWidget(cal_group)

        # Calibrated Metrics & Cross-talk Matrix
        metrics_group = QGroupBox("Calibrated Gains & Skull Cross-Talk Suppression Matrix")
        metrics_layout = QGridLayout(metrics_group)

        self._lbl_gains = QLabel("Software Gains: CH1: 0.85 | CH2: 0.85 | CH3: 0.85 | CH4: 0.85")
        self._lbl_gains.setFont(QFont("Cascadia Code", 10, QFont.Bold))
        self._lbl_gains.setStyleSheet(f"color: {STATUS_OK}; background: {SURFACE_2}; padding: 6px 10px; border-radius: 4px;")
        metrics_layout.addWidget(self._lbl_gains, 0, 0, 1, 2)

        self._lbl_thresh = QLabel("Optimal Thresholds: CH1: 0.50 | CH2: 0.50 | CH3: 0.50 | CH4: 0.50")
        self._lbl_thresh.setFont(QFont("Cascadia Code", 10, QFont.Bold))
        self._lbl_thresh.setStyleSheet(f"color: {STATUS_OK}; background: {SURFACE_2}; padding: 6px 10px; border-radius: 4px;")
        metrics_layout.addWidget(self._lbl_thresh, 1, 0, 1, 2)

        self._lbl_crosstalk = QLabel("Cross-talk Matrix C[Jaw CH4 -> CH1..CH3]: CH1: 0.00 | CH2: 0.00 | CH3: 0.00 (Suppression Active)")
        self._lbl_crosstalk.setFont(QFont("Cascadia Code", 10))
        self._lbl_crosstalk.setStyleSheet(f"color: {WARM_GRAPHITE}; background: {SURFACE_2}; padding: 6px 10px; border-radius: 4px;")
        metrics_layout.addWidget(self._lbl_crosstalk, 2, 0, 1, 2)

        layout.addWidget(metrics_group)

        # Live Audit Log
        log_group = QGroupBox("System & Hardware Audit Event Log")
        log_layout = QVBoxLayout(log_group)
        self._log_edit = QPlainTextEdit()
        self._log_edit.setReadOnly(True)
        self._log_edit.setMaximumBlockCount(200)
        log_layout.addWidget(self._log_edit)
        layout.addWidget(log_group)

        self._refresh_profile_display()
        self.append_log("System initialized. 4-Channel Piezo DSP pipeline ready.")

    def set_ble_metrics(self, connected: bool, name: str, rssi: int, rate: float) -> None:
        if connected:
            self._lbl_ble_status.setText(f"BLE: {name}")
            self._lbl_ble_status.setStyleSheet(f"color: {STATUS_OK}; font-weight: bold; background: {SURFACE_1}; padding: 4px 8px; border-radius: 4px;")
            self._lbl_pkt_rate.setText(f"Rate: {rate:.1f} pkt/s | RSSI: {rssi} dBm")
        else:
            self._lbl_ble_status.setText("BLE: DISCONNECTED")
            self._lbl_ble_status.setStyleSheet(f"color: {STATUS_WARN}; font-weight: bold; background: {SURFACE_1}; padding: 4px 8px; border-radius: 4px;")
            self._lbl_pkt_rate.setText("Rate: 0.0 pkt/s | RSSI: -- dBm")

    def append_log(self, text: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self._log_edit.appendPlainText(f"[{ts}] {text}")

    def reset_to_defaults(self) -> None:
        self.calibrator.reset_defaults()
        self._refresh_profile_display()
        self.sig_apply_thresholds.emit(self.calibrator.profile.thresholds)
        self.append_log("Reset hardware calibration and gains to defaults.")

    def _refresh_profile_display(self) -> None:
        p = self.calibrator.profile
        self._lbl_gains.setText(
            f"Software Gains: CH1 (L-Forehead): {p.gains[0]:.2f} | CH2 (R-Forehead): {p.gains[1]:.2f} | "
            f"CH3 (L-Temple): {p.gains[2]:.2f} | CH4 (R-Temple/Jaw): {p.gains[3]:.2f}"
        )
        self._lbl_thresh.setText(
            f"Optimal Thresholds: CH1: {p.thresholds[0]:.2f} | CH2: {p.thresholds[1]:.2f} | "
            f"CH3: {p.thresholds[2]:.2f} | CH4: {p.thresholds[3]:.2f}"
        )
        c4 = p.crosstalk[3]
        self._lbl_crosstalk.setText(
            f"Skull Cross-Talk Leakage C[Jaw CH4 -> Others]: CH1: {c4[0]:.2f} | CH2: {c4[1]:.2f} | CH3: {c4[2]:.2f} (Gate Active)"
        )

    def feed_sample(self, raw_ch: List[float]) -> None:
        """Called whenever a physical raw sample arrives from BLE/USB during calibration."""
        if self._calib_step > 0:
            self._samples_collector.append(list(raw_ch[:4]))

    def start_guided_calibration(self) -> None:
        self._calib_step = 1
        self._samples_collector.clear()
        self._step_start_t = time.perf_counter()
        self._progress.setValue(1)
        self._progress.setFormat("Step 1/5: Resting Baseline (3s)")
        self._cal_info.setText("STEP 1: Relax facial muscles completely. Measuring resting baseline noise (3s)...")
        self.append_log("Starting Guided Calibration: Step 1 - Resting baseline capture.")
        self._step_timer.start(100)

    def _on_step_tick(self) -> None:
        elapsed = time.perf_counter() - self._step_start_t
        rem = max(0.0, self._step_duration_s - elapsed)

        if self._calib_step == 1:
            self._cal_info.setText(f"STEP 1: Relax face completely. Capturing baseline... ({rem:.1f}s remaining)")
            if rem <= 0.0:
                self.calibrator.compute_baseline(self._samples_collector)
                self.append_log("Step 1 Complete: Resting baseline computed.")
                self._advance_to_flex_step(2, "CH1 (Left Forehead - Raise Eyebrow)")

        elif self._calib_step == 2:
            self._cal_info.setText(f"STEP 2: Flex CH1 Left Forehead (3 eyebrow raises)... ({rem:.1f}s remaining)")
            if rem <= 0.0:
                self.calibrator.register_flex_step(0, self._samples_collector)
                self.append_log("Step 2 Complete: CH1 calibrated.")
                self._advance_to_flex_step(3, "CH2 (Right Forehead - Raise Eyebrow)")

        elif self._calib_step == 3:
            self._cal_info.setText(f"STEP 3: Flex CH2 Right Forehead (3 eyebrow raises)... ({rem:.1f}s remaining)")
            if rem <= 0.0:
                self.calibrator.register_flex_step(1, self._samples_collector)
                self.append_log("Step 3 Complete: CH2 calibrated.")
                self._advance_to_flex_step(4, "CH3 (Left Temple - Flex Left)")

        elif self._calib_step == 4:
            self._cal_info.setText(f"STEP 4: Flex CH3 Left Temple (3 firm flexes)... ({rem:.1f}s remaining)")
            if rem <= 0.0:
                self.calibrator.register_flex_step(2, self._samples_collector)
                self.append_log("Step 4 Complete: CH3 calibrated.")
                self._advance_to_flex_step(5, "CH4 (Right Temple / Jaw Clench)")

        elif self._calib_step == 5:
            self._cal_info.setText(f"STEP 5: Clench Jaw firmly on CH4 (3 clenches)... ({rem:.1f}s remaining)")
            if rem <= 0.0:
                self.calibrator.register_flex_step(3, self._samples_collector)
                self.calibrator.save()
                self._step_timer.stop()
                self._calib_step = 0
                self._progress.setValue(5)
                self._progress.setFormat("Calibration Complete (5/5)")
                self._cal_info.setText("✅ CALIBRATION COMPLETE! Gains, dynamic thresholds, and cross-talk matrix saved and synced.")
                self._refresh_profile_display()
                self.sig_apply_thresholds.emit(self.calibrator.profile.thresholds)
                self.append_log("Step 5 Complete: Full hardware calibration saved to config/hardware_calibration.json.")

    def _advance_to_flex_step(self, step: int, name: str) -> None:
        self._calib_step = step
        self._samples_collector.clear()
        self._step_start_t = time.perf_counter()
        self._progress.setValue(step)
        self._progress.setFormat(f"Step {step}/5: {name}")
