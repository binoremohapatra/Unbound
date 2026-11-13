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
from host.omniband.tracking.optical_calibration import OpticalCalibrator, OpticalCalibrationProfile


class CalibrationView(QWidget):
    """Wizard for sensor noise calibration, cross-talk suppression, and audit log."""

    sig_start_guided_calib = Signal()
    sig_reset_calib = Signal()
    sig_apply_thresholds = Signal(list)  # 4 floats for oscilloscope sync
    sig_optical_calib_complete = Signal(object)  # OpticalCalibrationProfile

    def __init__(
        self,
        calibrator: Optional[HardwareCalibrator] = None,
        optical_calibrator: Optional[OpticalCalibrator] = None,
        parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.calibrator = calibrator or HardwareCalibrator()
        self.optical_calibrator = optical_calibrator or OpticalCalibrator()

        self._calib_step = 0  # 0: Idle, 1..5: Hardware steps
        self._optical_step = 0  # 0: Idle, 1..4: Optical steps
        self._step_timer = QTimer(self)
        self._step_timer.timeout.connect(self._on_step_tick)
        self._samples_collector: List[List[float]] = []
        self._step_duration_s = 3.0
        self._step_start_t = 0.0

        # Optical collector buffers
        self._opt_bl_left: list[float] = []
        self._opt_bl_right: list[float] = []
        self._opt_bl_nose: list[tuple[float, float]] = []
        self._opt_bl_dist: list[float] = []
        self._opt_wink_l: list[float] = []
        self._opt_comp_r: list[float] = []
        self._opt_wink_r: list[float] = []
        self._opt_comp_l: list[float] = []
        self._opt_jaw: list[float] = []

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

        self._btn_start_optical = QPushButton("👁️ Calibrate Wink / Scroll / Zoom (15s Wizard)")
        self._btn_start_optical.setStyleSheet(f"background-color: #00E5FF; color: #111; font-weight: bold; padding: 6px 12px;")
        self._btn_start_optical.clicked.connect(self.start_optical_calibration)
        btn_row.addWidget(self._btn_start_optical)

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

        self._lbl_optical_metrics = QLabel("Optical Thresholds: L-Wink: 0.18 (R-Floor: 0.22) | R-Wink: 0.18 (L-Floor: 0.22) | Neutral: (0.50, 0.45)")
        self._lbl_optical_metrics.setFont(QFont("Cascadia Code", 10, QFont.Bold))
        self._lbl_optical_metrics.setStyleSheet(f"color: #00E5FF; background: {SURFACE_2}; padding: 6px 10px; border-radius: 4px;")
        metrics_layout.addWidget(self._lbl_optical_metrics, 3, 0, 1, 2)

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
        self.optical_calibrator.reset_defaults()
        self._refresh_profile_display()
        self.sig_apply_thresholds.emit(self.calibrator.profile.thresholds)
        self.sig_optical_calib_complete.emit(self.optical_calibrator.profile)
        self.append_log("Reset hardware and optical calibration and gains to defaults.")

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
        op = self.optical_calibrator.profile
        self._lbl_optical_metrics.setText(
            f"Optical: L-Wink: {op.left_wink_thresh:.2f} (R-Floor: {op.right_open_floor:.2f}) | "
            f"R-Wink: {op.right_wink_thresh:.2f} (L-Floor: {op.left_open_floor:.2f}) | "
            f"Neutral: ({op.neutral_cx:.2f}, {op.neutral_cy:.2f}) | MAR Thr: {op.jaw_click_thresh:.2f}"
        )

    def feed_sample(self, raw_ch: List[float]) -> None:
        """Called whenever a physical raw sample arrives from BLE/USB during calibration."""
        if self._calib_step > 0:
            self._samples_collector.append(list(raw_ch[:4]))

    def feed_optical_frame(
        self,
        left_ear: Optional[float],
        right_ear: Optional[float],
        mar: Optional[float],
        nose_norm: Optional[tuple[float, float]],
        eye_dist: Optional[float],
    ) -> None:
        """Feed live biometric frame metrics into the 15-second optical wizard."""
        if self._optical_step == 0:
            return

        le = left_ear if left_ear is not None else 0.28
        re = right_ear if right_ear is not None else 0.28
        m = mar if mar is not None else 0.0
        dist = eye_dist if eye_dist is not None else 0.12

        if self._optical_step == 1:
            self._opt_bl_left.append(le)
            self._opt_bl_right.append(re)
            if nose_norm is not None:
                self._opt_bl_nose.append(nose_norm)
            self._opt_bl_dist.append(dist)
        elif self._optical_step == 2:
            self._opt_wink_l.append(le)
            self._opt_comp_r.append(re)
        elif self._optical_step == 3:
            self._opt_wink_r.append(re)
            self._opt_comp_l.append(le)
        elif self._optical_step == 4:
            self._opt_jaw.append(m)

    def start_optical_calibration(self) -> None:
        """Launch the 15-second guided optical calibration wizard."""
        self._calib_step = 0
        self._optical_step = 1
        self._opt_bl_left.clear()
        self._opt_bl_right.clear()
        self._opt_bl_nose.clear()
        self._opt_bl_dist.clear()
        self._opt_wink_l.clear()
        self._opt_comp_r.clear()
        self._opt_wink_r.clear()
        self._opt_comp_l.clear()
        self._opt_jaw.clear()

        self._step_start_t = time.perf_counter()
        self._progress.setRange(0, 4)
        self._progress.setValue(1)
        self._progress.setFormat("Optical Step 1/4: Open Eyes & Neutral Head (3s)")
        self._cal_info.setText("OPTICAL STEP 1: Look directly at the camera with normal open eyes and neutral head pose (3s)...")
        self.append_log("Starting 15s Guided Optical Calibration: Step 1 - Normal open eyes baseline.")
        self._step_timer.start(100)

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

        # -------------------------------------------------------------
        # Optical Calibration Wizard Steps
        # -------------------------------------------------------------
        if self._optical_step > 0:
            if self._optical_step == 1:
                self._cal_info.setText(f"OPTICAL STEP 1: Eyes open, neutral head... ({rem:.1f}s remaining)")
                if rem <= 0.0:
                    self.append_log("Optical Step 1 Complete: Open eyes baseline recorded.")
                    self._optical_step = 2
                    self._step_start_t = time.perf_counter()
                    self._progress.setValue(2)
                    self._progress.setFormat("Optical Step 2/4: Wink Left Eye (Scroll Pose)")
                    self._cal_info.setText("OPTICAL STEP 2: Hold your LEFT eye closed firmly while keeping RIGHT eye open (3s)...")

            elif self._optical_step == 2:
                self._cal_info.setText(f"OPTICAL STEP 2: Wink LEFT eye (Scroll Clutch pose)... ({rem:.1f}s remaining)")
                if rem <= 0.0:
                    self.append_log("Optical Step 2 Complete: Left wink thresholds recorded.")
                    self._optical_step = 3
                    self._step_start_t = time.perf_counter()
                    self._progress.setValue(3)
                    self._progress.setFormat("Optical Step 3/4: Wink Right Eye (Zoom Pose)")
                    self._cal_info.setText("OPTICAL STEP 3: Hold your RIGHT eye closed firmly while keeping LEFT eye open (3s)...")

            elif self._optical_step == 3:
                self._cal_info.setText(f"OPTICAL STEP 3: Wink RIGHT eye (Doctor Zoom pose)... ({rem:.1f}s remaining)")
                if rem <= 0.0:
                    self.append_log("Optical Step 3 Complete: Right wink thresholds recorded.")
                    self._optical_step = 4
                    self._step_start_t = time.perf_counter()
                    self._progress.setValue(4)
                    self._progress.setFormat("Optical Step 4/4: Pitch Head & Jaw Tap")
                    self._cal_info.setText("OPTICAL STEP 4: Tilt head slightly up/down and tap jaw to calibrate pitch and MAR (3s)...")

            elif self._optical_step == 4:
                self._cal_info.setText(f"OPTICAL STEP 4: Pitch head & tap jaw... ({rem:.1f}s remaining)")
                if rem <= 0.0:
                    prof = self.optical_calibrator.compute_from_records(
                        self._opt_bl_left,
                        self._opt_bl_right,
                        self._opt_bl_nose,
                        self._opt_bl_dist,
                        self._opt_wink_l,
                        self._opt_comp_r,
                        self._opt_wink_r,
                        self._opt_comp_l,
                        self._opt_jaw,
                    )
                    self._step_timer.stop()
                    self._optical_step = 0
                    self._progress.setValue(4)
                    self._progress.setFormat("Optical Calibration Complete (4/4)")
                    self._cal_info.setText("✅ OPTICAL CALIBRATION SAVED! Scroll/Zoom wink thresholds and neutral pose updated.")
                    self._refresh_profile_display()
                    self.sig_optical_calib_complete.emit(prof)
                    self.append_log("Optical Calibration Complete: Saved to config/optical_calibration.json.")
            return

        # -------------------------------------------------------------
        # Hardware 5-Step Piezo Steps
        # -------------------------------------------------------------
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
