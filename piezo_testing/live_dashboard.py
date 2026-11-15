"""live_dashboard.py - Professional Engineering Desktop Dashboard for Piezo Monitoring.

Built with PySide6 and PyQtGraph for high-FPS, multi-channel independent sensor visualization.
"""
from __future__ import annotations

import collections
import time
from pathlib import Path
from typing import Deque, Dict, List, Optional

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from .config import (
    ACCENT_BLUE,
    ACCENT_GREEN,
    ACCENT_RED,
    ACCENT_WARN,
    BG_DARK,
    BORDER_COLOR,
    CARD_BG,
    CHANNELS,
    DEFAULT_BAUD,
    DEFAULT_BAUDRATES,
    DEFAULT_WINDOW_SEC,
    FIRMWARE_SPEC_ADC_HZ,
    GUI_REFRESH_HZ,
    MAX_BUFFER_POINTS,
    PANEL_BG,
    TARGET_HARDWARE_ADC_HZ,
    TEXT_MUTED,
    TEXT_PRIMARY,
    WINDOW_DURATIONS,
    WIFI_UDP_PORT,
    ChannelInfo,
)
from .data_logger import DataLogger
from .sensor_reader import PiezoSample, SensorReader
from .serial_manager import SerialManager, list_serial_ports
from .signal_statistics import ChannelStats, SignalStatisticsAnalyzer


class ChannelDisplayCard(QFrame):
    """Real-time diagnostic card for one independent piezo sensor."""

    def __init__(self, info: ChannelInfo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.info = info
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            f"ChannelDisplayCard {{"
            f"  background-color: {CARD_BG};"
            f"  border: 1px solid {BORDER_COLOR};"
            f"  border-radius: 6px;"
            f"}}"
        )
        self._init_ui()

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        # Header with channel name and muscle group
        header_row = QHBoxLayout()
        self.lbl_title = QLabel(self.info.name)
        self.lbl_title.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self.lbl_title.setStyleSheet(f"color: {self.info.color_hex};")
        header_row.addWidget(self.lbl_title)

        header_row.addStretch()
        self.lbl_pin = QLabel(self.info.pin.split()[0])
        self.lbl_pin.setFont(QFont("Cascadia Code", 8))
        self.lbl_pin.setStyleSheet(f"color: {TEXT_MUTED}; background: {PANEL_BG}; padding: 2px 5px; border-radius: 3px;")
        header_row.addWidget(self.lbl_pin)
        layout.addLayout(header_row)

        self.lbl_muscle = QLabel(f"Muscle: {self.info.muscle}")
        self.lbl_muscle.setFont(QFont("Segoe UI", 8))
        self.lbl_muscle.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(self.lbl_muscle)

        # Large Live Reading Value
        self.lbl_value = QLabel("0")
        self.lbl_value.setFont(QFont("Cascadia Code", 22, QFont.Bold))
        self.lbl_value.setAlignment(Qt.AlignCenter)
        self.lbl_value.setStyleSheet(f"color: {self.info.color_hex}; padding: 4px 0px;")
        layout.addWidget(self.lbl_value)

        # Stats Sub-Grid
        stats_grid = QGridLayout()
        stats_grid.setSpacing(2)

        self.lbl_min = QLabel("Min: --")
        self.lbl_max = QLabel("Max: --")
        self.lbl_mean = QLabel("Avg: --")
        self.lbl_peak = QLabel("Peak: --")
        self.lbl_std = QLabel("Var: --")
        self.lbl_peaks = QLabel("Peaks: 0")

        lbl_style = f"color: {TEXT_PRIMARY}; font-size: 8pt; font-family: 'Cascadia Code', monospace;"
        for w in (self.lbl_min, self.lbl_max, self.lbl_mean, self.lbl_peak, self.lbl_std, self.lbl_peaks):
            w.setStyleSheet(lbl_style)

        stats_grid.addWidget(self.lbl_min, 0, 0)
        stats_grid.addWidget(self.lbl_max, 0, 1)
        stats_grid.addWidget(self.lbl_mean, 1, 0)
        stats_grid.addWidget(self.lbl_peak, 1, 1)
        stats_grid.addWidget(self.lbl_std, 2, 0)
        stats_grid.addWidget(self.lbl_peaks, 2, 1)

        layout.addLayout(stats_grid)

    def update_stats(self, stats: ChannelStats) -> None:
        self.lbl_value.setText(f"{stats.current:.0f}")
        min_txt = f"{stats.min_val:.0f}" if stats.min_val != float("inf") else "--"
        max_txt = f"{stats.max_val:.0f}" if stats.max_val != float("-inf") else "--"
        self.lbl_min.setText(f"Min: {min_txt}")
        self.lbl_max.setText(f"Max: {max_txt}")
        self.lbl_mean.setText(f"Avg: {stats.mean:.1f}")
        self.lbl_peak.setText(f"Peak: {stats.peak_val:.0f}")
        self.lbl_std.setText(f"Var: ±{stats.std_dev:.1f}")
        self.lbl_peaks.setText(f"Peaks: {stats.peak_count}")


class LivePiezoDashboard(QMainWindow):
    """Main Testing & Diagnostic Window for Independent Piezo Sensor Verification."""

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Unbound Handsfree - Live Piezo Sensor Testing & Monitoring Tool (Phase 2)")
        self.resize(1300, 900)
        self.setMinimumSize(1000, 700)
        self.setStyleSheet(f"background-color: {BG_DARK}; color: {TEXT_PRIMARY};")

        # Core Components
        self.serial_manager = SerialManager(on_status_change=self._on_connection_status_changed)
        self.sensor_reader = SensorReader()
        self.stats_analyzer = SignalStatisticsAnalyzer()
        self.data_logger = DataLogger()

        # Rolling Buffer Data for Plotting (t_relative, values)
        self.time_buffers: Dict[int, Deque[float]] = {i: collections.deque(maxlen=MAX_BUFFER_POINTS) for i in range(4)}
        self.val_buffers: Dict[int, Deque[float]] = {i: collections.deque(maxlen=MAX_BUFFER_POINTS) for i in range(4)}

        self.current_window_sec: int = DEFAULT_WINDOW_SEC
        self.view_mode: str = "ALL"  # "ALL", "CH1", "CH2", "CH3", "CH4"
        self._start_perf_time: float = time.perf_counter()

        self._init_ui()

        # 40 FPS High-Performance Batch Consumer Loop
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(1000 // GUI_REFRESH_HZ)
        self._update_timer.timeout.connect(self._on_gui_refresh_tick)
        self._update_timer.start()

        self._refresh_ports()

    def _init_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(12, 10, 12, 10)
        root_layout.setSpacing(10)

        # 1. Top Connection & Mode Control Panel
        root_layout.addWidget(self._create_top_control_panel())

        # 2. Timing & Frequency Telemetry Banner
        root_layout.addWidget(self._create_timing_banner())

        # 3. 4-Channel Cards Row
        cards_row = QHBoxLayout()
        cards_row.setSpacing(8)
        self.channel_cards: Dict[int, ChannelDisplayCard] = {}
        for idx in range(4):
            card = ChannelDisplayCard(CHANNELS[idx])
            self.channel_cards[idx] = card
            cards_row.addWidget(card)
        root_layout.addLayout(cards_row)

        # 4. PyQtGraph Real-Time Multi-Channel Waveform Area
        self.graph_container = self._create_graph_area()
        root_layout.addWidget(self.graph_container, stretch=1)

        # 5. Bottom Recording & Test Event Tagging Bar
        root_layout.addWidget(self._create_bottom_recording_bar())

    def _create_top_control_panel(self) -> QFrame:
        panel = QFrame()
        panel.setStyleSheet(f"background: {PANEL_BG}; border: 1px solid {BORDER_COLOR}; border-radius: 6px; padding: 4px;")
        layout = QHBoxLayout(panel)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)

        # Port Selection
        lbl_port = QLabel("Serial Port:")
        lbl_port.setFont(QFont("Segoe UI", 9, QFont.Bold))
        layout.addWidget(lbl_port)

        self.cb_ports = QComboBox()
        self.cb_ports.setMinimumWidth(220)
        self.cb_ports.setStyleSheet(f"background: {CARD_BG}; color: {TEXT_PRIMARY}; border: 1px solid {BORDER_COLOR}; padding: 4px;")
        layout.addWidget(self.cb_ports)

        btn_refresh = QPushButton("🔄 Refresh")
        btn_refresh.setStyleSheet(f"background: {CARD_BG}; color: {TEXT_PRIMARY}; border: 1px solid {BORDER_COLOR}; padding: 4px 8px; border-radius: 4px;")
        btn_refresh.clicked.connect(self._refresh_ports)
        layout.addWidget(btn_refresh)

        # Baud Rate
        lbl_baud = QLabel("Baud Rate:")
        lbl_baud.setFont(QFont("Segoe UI", 9, QFont.Bold))
        layout.addWidget(lbl_baud)

        self.cb_baud = QComboBox()
        for b in DEFAULT_BAUDRATES:
            self.cb_baud.addItem(str(b), b)
        self.cb_baud.setCurrentText(str(DEFAULT_BAUD))
        self.cb_baud.setStyleSheet(f"background: {CARD_BG}; color: {TEXT_PRIMARY}; border: 1px solid {BORDER_COLOR}; padding: 4px;")
        layout.addWidget(self.cb_baud)

        # Connect / Disconnect Action Button
        self.btn_connect = QPushButton("⚡ Connect")
        self.btn_connect.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.btn_connect.setStyleSheet(f"background: {ACCENT_BLUE}; color: white; border: none; padding: 5px 14px; border-radius: 4px;")
        self.btn_connect.clicked.connect(self._toggle_connection)
        layout.addWidget(self.btn_connect)

        # Connection Status Pill
        self.lbl_status_pill = QLabel("DISCONNECTED")
        self.lbl_status_pill.setFont(QFont("Cascadia Code", 8, QFont.Bold))
        self.lbl_status_pill.setStyleSheet(f"background: {CARD_BG}; color: {ACCENT_RED}; border: 1px solid {ACCENT_RED}; padding: 4px 8px; border-radius: 4px;")
        layout.addWidget(self.lbl_status_pill)

        layout.addStretch()

        # View Mode Selector
        lbl_view = QLabel("View Channel:")
        lbl_view.setFont(QFont("Segoe UI", 9, QFont.Bold))
        layout.addWidget(lbl_view)

        self.cb_view_mode = QComboBox()
        self.cb_view_mode.addItems(["ALL Channels", "CH1 - Left Temple", "CH2 - Right Temple", "CH3 - Forehead", "CH4 - Jaw Fallback"])
        self.cb_view_mode.setStyleSheet(f"background: {CARD_BG}; color: {TEXT_PRIMARY}; border: 1px solid {BORDER_COLOR}; padding: 4px;")
        self.cb_view_mode.currentIndexChanged.connect(self._on_view_mode_changed)
        layout.addWidget(self.cb_view_mode)

        # Time Window Selector
        lbl_win = QLabel("Time Window:")
        lbl_win.setFont(QFont("Segoe UI", 9, QFont.Bold))
        layout.addWidget(lbl_win)

        self.cb_window = QComboBox()
        for w in WINDOW_DURATIONS:
            self.cb_window.addItem(f"{w} Seconds", w)
        self.cb_window.setCurrentText(f"{DEFAULT_WINDOW_SEC} Seconds")
        self.cb_window.setStyleSheet(f"background: {CARD_BG}; color: {TEXT_PRIMARY}; border: 1px solid {BORDER_COLOR}; padding: 4px;")
        self.cb_window.currentIndexChanged.connect(self._on_window_duration_changed)
        layout.addWidget(self.cb_window)

        # Clear Graph Button
        btn_clear = QPushButton("🗑️ Clear")
        btn_clear.setStyleSheet(f"background: {CARD_BG}; color: {TEXT_PRIMARY}; border: 1px solid {BORDER_COLOR}; padding: 4px 8px; border-radius: 4px;")
        btn_clear.clicked.connect(self._clear_history)
        layout.addWidget(btn_clear)

        return panel

    def _create_timing_banner(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"background: {PANEL_BG}; border: 1px solid {BORDER_COLOR}; border-radius: 6px; padding: 2px;")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(16)

        # 1. Spec vs Measured Acquisition Rate
        box1 = QVBoxLayout()
        box1.setSpacing(1)
        l1 = QLabel("1. HARDWARE ACQUISITION")
        l1.setFont(QFont("Segoe UI", 7, QFont.Bold))
        l1.setStyleSheet(f"color: {TEXT_MUTED};")
        self.lbl_hw_spec_rate = QLabel(f"Spec: {FIRMWARE_SPEC_ADC_HZ:.0f} Hz (Tick: 2ms) | Target: {TARGET_HARDWARE_ADC_HZ:.0f} Hz")
        self.lbl_hw_spec_rate.setFont(QFont("Cascadia Code", 9, QFont.Bold))
        self.lbl_hw_spec_rate.setStyleSheet(f"color: #00E5FF;")
        box1.addWidget(l1)
        box1.addWidget(self.lbl_hw_spec_rate)
        layout.addLayout(box1)

        # 2. Measured Serial Transmission Rate
        box2 = QVBoxLayout()
        box2.setSpacing(1)
        l2 = QLabel("2. TRANSMISSION FREQUENCY")
        l2.setFont(QFont("Segoe UI", 7, QFont.Bold))
        l2.setStyleSheet(f"color: {TEXT_MUTED};")
        self.lbl_trans_rate = QLabel("Measured: 0.0 Hz")
        self.lbl_trans_rate.setFont(QFont("Cascadia Code", 9, QFont.Bold))
        self.lbl_trans_rate.setStyleSheet(f"color: {ACCENT_WARN};")
        box2.addWidget(l2)
        box2.addWidget(self.lbl_trans_rate)
        layout.addLayout(box2)

        # 3. Python UI Consumption Loop
        box3 = QVBoxLayout()
        box3.setSpacing(1)
        l3 = QLabel("3. PYTHON POLLING RATE")
        l3.setFont(QFont("Segoe UI", 7, QFont.Bold))
        l3.setStyleSheet(f"color: {TEXT_MUTED};")
        self.lbl_consumer_rate = QLabel("Consumer: 0.0 Hz")
        self.lbl_consumer_rate.setFont(QFont("Cascadia Code", 9, QFont.Bold))
        self.lbl_consumer_rate.setStyleSheet(f"color: {ACCENT_GREEN};")
        box3.addWidget(l3)
        box3.addWidget(self.lbl_consumer_rate)
        layout.addLayout(box3)

        # 4. Sampling Interval Metrics (Avg/Min/Max/Jitter)
        box4 = QVBoxLayout()
        box4.setSpacing(1)
        l4 = QLabel("SAMPLE INTERVAL & JITTER")
        l4.setFont(QFont("Segoe UI", 7, QFont.Bold))
        l4.setStyleSheet(f"color: {TEXT_MUTED};")
        self.lbl_interval_metrics = QLabel("Avg: -- ms | Min: -- | Max: -- | Jitter: ±-- ms")
        self.lbl_interval_metrics.setFont(QFont("Cascadia Code", 9))
        self.lbl_interval_metrics.setStyleSheet(f"color: {TEXT_PRIMARY};")
        box4.addWidget(l4)
        box4.addWidget(self.lbl_interval_metrics)
        layout.addLayout(box4)

        # 5. Packet Health & Drops
        box5 = QVBoxLayout()
        box5.setSpacing(1)
        l5 = QLabel("PACKET INTEGRITY")
        l5.setFont(QFont("Segoe UI", 7, QFont.Bold))
        l5.setStyleSheet(f"color: {TEXT_MUTED};")
        self.lbl_packet_health = QLabel("Total: 0 | Loss: 0.0% | Dropped: 0")
        self.lbl_packet_health.setFont(QFont("Cascadia Code", 9))
        self.lbl_packet_health.setStyleSheet(f"color: {TEXT_PRIMARY};")
        box5.addWidget(l5)
        box5.addWidget(self.lbl_packet_health)
        layout.addLayout(box5)

        return frame

    def _create_graph_area(self) -> QWidget:
        """Create high-performance PyQtGraph layout with 4 synchronized subplots."""
        pg.setConfigOption("background", BG_DARK)
        pg.setConfigOption("foreground", TEXT_PRIMARY)
        pg.setConfigOption("antialias", True)

        self.glw = pg.GraphicsLayoutWidget()
        self.plots: Dict[int, pg.PlotItem] = {}
        self.curves: Dict[int, pg.PlotDataItem] = {}

        for idx in range(4):
            info = CHANNELS[idx]
            p = self.glw.addPlot(row=idx, col=0)
            p.setLabel("left", info.name.split(" - ")[0], units="Counts", color=info.color_hex)
            p.showGrid(x=True, y=True, alpha=0.25)
            p.setYRange(0, 4095, padding=0.05)
            p.setMouseEnabled(x=False, y=False)
            curve = p.plot(pen=pg.mkPen(color=info.qcolor, width=1.8))
            self.plots[idx] = p
            self.curves[idx] = curve

        # Synchronize X-axes across all subplots
        self.plots[1].setXLink(self.plots[0])
        self.plots[2].setXLink(self.plots[0])
        self.plots[3].setXLink(self.plots[0])
        self.plots[3].setLabel("bottom", "Elapsed Time (Seconds)", color=TEXT_MUTED)

        return self.glw

    def _create_bottom_recording_bar(self) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet(f"background: {PANEL_BG}; border: 1px solid {BORDER_COLOR}; border-radius: 6px; padding: 4px;")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(10)

        # Recording Toggle Button
        self.btn_record = QPushButton("⏺️ Start Recording")
        self.btn_record.setFont(QFont("Segoe UI", 9, QFont.Bold))
        self.btn_record.setStyleSheet(f"background: {ACCENT_RED}; color: white; border: none; padding: 6px 16px; border-radius: 4px;")
        self.btn_record.clicked.connect(self._toggle_recording)
        layout.addWidget(self.btn_record)

        # Recording Status Pill
        self.lbl_record_status = QLabel("RECORDER: IDLE")
        self.lbl_record_status.setFont(QFont("Cascadia Code", 8, QFont.Bold))
        self.lbl_record_status.setStyleSheet(f"background: {CARD_BG}; color: {TEXT_MUTED}; border: 1px solid {BORDER_COLOR}; padding: 4px 10px; border-radius: 4px;")
        layout.addWidget(self.lbl_record_status)

        layout.addSpacing(10)
        lbl_markers = QLabel("Test Markers:")
        lbl_markers.setFont(QFont("Segoe UI", 8, QFont.Bold))
        lbl_markers.setStyleSheet(f"color: {TEXT_MUTED};")
        layout.addWidget(lbl_markers)

        # Phase 2 Engineering Test Buttons
        test_buttons = [
            ("A: Stationary", "TEST_A_STATIONARY"),
            ("B: Slight Mvmt", "TEST_B_SLIGHT_MOVEMENT"),
            ("C: Wearing/Adjust", "TEST_C_WEARING_ADJUSTMENT"),
            ("D: Head Mvmt", "TEST_D_HEAD_MOVEMENT"),
            ("E: Clench", "TEST_E_CLENCH"),
            ("F: Repeat Clench", "TEST_F_REPEATED_CLENCH"),
        ]

        for label, tag in test_buttons:
            btn = QPushButton(label)
            btn.setFont(QFont("Segoe UI", 8))
            btn.setStyleSheet(f"background: {CARD_BG}; color: {TEXT_PRIMARY}; border: 1px solid {BORDER_COLOR}; padding: 3px 6px; border-radius: 3px;")
            btn.clicked.connect(lambda checked=False, t=tag: self._set_test_marker(t))
            layout.addWidget(btn)

        layout.addStretch()

        # Export CSV Button
        btn_export = QPushButton("📁 Choose CSV Save Path...")
        btn_export.setStyleSheet(f"background: {CARD_BG}; color: {TEXT_PRIMARY}; border: 1px solid {BORDER_COLOR}; padding: 4px 10px; border-radius: 4px;")
        btn_export.clicked.connect(self._choose_save_path)
        layout.addWidget(btn_export)

        return frame

    # -------------------------------------------------------------------------
    # Connection Management
    # -------------------------------------------------------------------------
    def _refresh_ports(self) -> None:
        self.cb_ports.clear()
        ports = list_serial_ports()
        for dev, desc, hwid in ports:
            self.cb_ports.addItem(f"{dev} ({desc})", dev)

        # Add Wi-Fi UDP option from Phase 1 audit
        self.cb_ports.addItem(f"Wi-Fi UDP Port {WIFI_UDP_PORT} (Direct Network)", "WIFI_UDP")

    def _toggle_connection(self) -> None:
        if self.serial_manager.is_connected:
            self.serial_manager.disconnect()
            self.btn_connect.setText("⚡ Connect")
            self.btn_connect.setStyleSheet(f"background: {ACCENT_BLUE}; color: white; border: none; padding: 5px 14px; border-radius: 4px;")
        else:
            port = self.cb_ports.currentData()
            if not port:
                return
            baud = self.cb_baud.currentData()
            self.btn_connect.setText("⏳ Connecting...")
            ok = self.serial_manager.connect(port, baud)
            if ok:
                self.btn_connect.setText("🔴 Disconnect")
                self.btn_connect.setStyleSheet(f"background: {ACCENT_RED}; color: white; border: none; padding: 5px 14px; border-radius: 4px;")

    def _on_connection_status_changed(self, connected: bool, message: str) -> None:
        if connected:
            self.lbl_status_pill.setText("🟢 CONNECTED")
            self.lbl_status_pill.setStyleSheet(f"background: {CARD_BG}; color: {ACCENT_GREEN}; border: 1px solid {ACCENT_GREEN}; padding: 4px 8px; border-radius: 4px;")
        else:
            self.lbl_status_pill.setText("🔴 DISCONNECTED")
            self.lbl_status_pill.setStyleSheet(f"background: {CARD_BG}; color: {ACCENT_RED}; border: 1px solid {ACCENT_RED}; padding: 4px 8px; border-radius: 4px;")

    # -------------------------------------------------------------------------
    # High-Performance Batch Consumer Loop (40 FPS)
    # -------------------------------------------------------------------------
    def _on_gui_refresh_tick(self) -> None:
        """Consumes incoming raw lines from the worker queue and updates graphs/stats."""
        self.stats_analyzer.update_consumer_tick()
        now = time.perf_counter()
        rel_now = now - self._start_perf_time
        wall_now = time.time()

        samples_processed = 0

        # Non-blocking burst dequeue from worker thread queue
        while not self.serial_manager.raw_queue.empty() and samples_processed < 200:
            try:
                line, arrival_t = self.serial_manager.raw_queue.get_nowait()
                samples_processed += 1
            except Exception:
                break

            sample = self.sensor_reader.parse_line(line, arrival_t, wall_now)
            if sample:
                # 1. Update Statistical Engines
                self.stats_analyzer.feed_sample(sample)

                # 2. Append to Disk Recorder
                if self.data_logger.is_recording:
                    self.data_logger.log_sample(sample)

                # 3. Append to Rolling Graph Deques
                sample_rel_t = sample.timestamp - self._start_perf_time
                for idx in range(4):
                    self.time_buffers[idx].append(sample_rel_t)
                    self.val_buffers[idx].append(sample.channels[idx])

        # Update Timing Telemetry Metrics
        timing = self.stats_analyzer.compute_rates()
        timing.dropped_packets = self.sensor_reader.dropped_packets
        timing.malformed_packets = self.sensor_reader.malformed_packets

        self.lbl_trans_rate.setText(f"Transmission: {timing.measured_transmission_hz:.1f} Hz")
        self.lbl_consumer_rate.setText(f"Consumer: {timing.python_consumer_hz:.1f} Hz")

        avg_str = f"{timing.avg_interval_ms:.1f}" if timing.avg_interval_ms > 0 else "--"
        min_str = f"{timing.min_interval_ms:.1f}" if timing.min_interval_ms != float("inf") else "--"
        max_str = f"{timing.max_interval_ms:.1f}" if timing.max_interval_ms > 0 else "--"
        jit_str = f"{timing.jitter_ms:.1f}" if timing.jitter_ms > 0 else "--"
        self.lbl_interval_metrics.setText(f"Avg: {avg_str} ms | Min: {min_str} | Max: {max_str} | Jitter: ±{jit_str} ms")

        self.lbl_packet_health.setText(
            f"Total: {timing.total_packets} | Loss: {timing.packet_loss_pct:.1f}% | Dropped: {timing.dropped_packets}"
        )

        # Update Channel Display Cards
        for idx in range(4):
            self.channel_cards[idx].update_stats(self.stats_analyzer.channels[idx])

        # Update Rolling Graphs
        cutoff_t = rel_now - self.current_window_sec
        for idx in range(4):
            if self.curves[idx].isVisible():
                t_arr = self.time_buffers[idx]
                v_arr = self.val_buffers[idx]
                if len(t_arr) > 0:
                    self.curves[idx].setData(list(t_arr), list(v_arr))

        # Adjust X-Axis Range
        if self.plots:
            self.plots[0].setXRange(cutoff_t, rel_now, padding=0.01)

        # Update Recording Status Pill
        if self.data_logger.is_recording:
            dur = self.data_logger.duration_seconds
            mins = int(dur // 60)
            secs = int(dur % 60)
            self.lbl_record_status.setText(
                f"🔴 RECORDING: {mins:02d}:{secs:02d} ({self.data_logger.record_count} samples)"
            )
            self.lbl_record_status.setStyleSheet(
                f"background: {CARD_BG}; color: {ACCENT_RED}; border: 1px solid {ACCENT_RED}; padding: 4px 10px; border-radius: 4px;"
            )
        else:
            self.lbl_record_status.setText("RECORDER: IDLE")
            self.lbl_record_status.setStyleSheet(
                f"background: {CARD_BG}; color: {TEXT_MUTED}; border: 1px solid {BORDER_COLOR}; padding: 4px 10px; border-radius: 4px;"
            )

    # -------------------------------------------------------------------------
    # Visualization & Recording Controls
    # -------------------------------------------------------------------------
    def _on_window_duration_changed(self) -> None:
        self.current_window_sec = self.cb_window.currentData()

    def _on_view_mode_changed(self) -> None:
        idx = self.cb_view_mode.currentIndex()
        if idx == 0:
            # Show all 4 stacked
            for i in range(4):
                self.plots[i].setVisible(True)
        else:
            # Show single selected channel
            sel_ch = idx - 1
            for i in range(4):
                self.plots[i].setVisible(i == sel_ch)

    def _clear_history(self) -> None:
        self._start_perf_time = time.perf_counter()
        for idx in range(4):
            self.time_buffers[idx].clear()
            self.val_buffers[idx].clear()
            self.curves[idx].setData([], [])
        self.stats_analyzer.reset()
        self.sensor_reader.reset_stats()

    def _toggle_recording(self) -> None:
        if self.data_logger.is_recording:
            saved = self.data_logger.stop_recording()
            self.btn_record.setText("⏺️ Start Recording")
            self.btn_record.setStyleSheet(f"background: {ACCENT_RED}; color: white; border: none; padding: 6px 16px; border-radius: 4px;")
        else:
            self.data_logger.start_recording()
            self.btn_record.setText("⏹️ Stop Recording")
            self.btn_record.setStyleSheet(f"background: #757575; color: white; border: none; padding: 6px 16px; border-radius: 4px;")

    def _set_test_marker(self, marker: str) -> None:
        self.data_logger.set_test_marker(marker)
        self.lbl_record_status.setText(f"TAG: {marker}")

    def _choose_save_path(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Select CSV Destination", "", "CSV Files (*.csv)")
        if path:
            self.data_logger.start_recording(custom_path=path)
            self.btn_record.setText("⏹️ Stop Recording")
            self.btn_record.setStyleSheet(f"background: #757575; color: white; border: none; padding: 6px 16px; border-radius: 4px;")

    def closeEvent(self, event) -> None:
        self.serial_manager.disconnect()
        self.data_logger.stop_recording()
        event.accept()
