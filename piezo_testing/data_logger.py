"""data_logger.py - High-Performance CSV Sensor Data Logger.

Records raw unmodified piezo channel data, timestamps, sequence numbers, and test markers.
"""
from __future__ import annotations

import csv
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from .config import RECORDINGS_DIR
from .sensor_reader import PiezoSample

log = logging.getLogger("piezo_testing.data_logger")


class DataLogger:
    """Thread-safe streaming CSV recorder for raw sensor data."""

    def __init__(self) -> None:
        self.is_recording: bool = False
        self.file_path: Optional[Path] = None
        self.record_count: int = 0
        self.start_time: float = 0.0

        self._file = None
        self._writer = None
        self._lock = threading.RLock()
        self._current_test_marker: str = "NORMAL"

    @property
    def duration_seconds(self) -> float:
        if not self.is_recording or self.start_time == 0.0:
            return 0.0
        return time.time() - self.start_time

    def set_test_marker(self, marker: str) -> None:
        """Tag live data with an engineering test phase (e.g., TEST_E_CLENCH)."""
        with self._lock:
            self._current_test_marker = marker

    def start_recording(self, custom_path: Optional[str] = None) -> Path:
        """Initialize and open the CSV recording file."""
        with self._lock:
            if self.is_recording:
                self.stop_recording()

            RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
            if custom_path:
                p = Path(custom_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                self.file_path = p
            else:
                timestamp_str = time.strftime("%Y%m%d_%H%M%S")
                self.file_path = RECORDINGS_DIR / f"piezo_recording_{timestamp_str}.csv"

            self._file = open(self.file_path, "w", newline="", encoding="utf-8")
            self._writer = csv.writer(self._file)

            # Standardized CSV Header
            self._writer.writerow([
                "timestamp_iso",
                "perf_time_s",
                "ch1_raw_left_temple",
                "ch2_raw_right_temple",
                "ch3_raw_forehead",
                "ch4_raw_jaw_digital",
                "sequence_number",
                "packet_type",
                "interval_ms",
                "test_marker",
            ])
            self._file.flush()

            self.is_recording = True
            self.record_count = 0
            self.start_time = time.time()
            log.info("Started recording piezo sensor data to %s", self.file_path)
            return self.file_path

    def log_sample(self, sample: PiezoSample) -> None:
        """Stream an uncompressed, unmodified raw sample to disk."""
        if not self.is_recording or self._writer is None:
            return

        with self._lock:
            try:
                iso_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(sample.wall_time))
                millis = int((sample.wall_time % 1) * 1000)
                full_iso = f"{iso_time}.{millis:03d}"

                self._writer.writerow([
                    full_iso,
                    f"{sample.timestamp:.4f}",
                    f"{sample.ch1:.1f}",
                    f"{sample.ch2:.1f}",
                    f"{sample.ch3:.1f}",
                    f"{sample.ch4:.1f}",
                    sample.seq,
                    sample.packet_type,
                    f"{sample.interval_ms:.2f}",
                    self._current_test_marker,
                ])
                self.record_count += 1

                # Flush periodically every 50 records to ensure immediate disk persistence
                if self.record_count % 50 == 0 and self._file:
                    self._file.flush()
            except Exception as e:
                log.error("Failed writing record: %s", e)

    def stop_recording(self) -> Optional[Path]:
        """Finalize and close active recording file."""
        with self._lock:
            if not self.is_recording:
                return None

            self.is_recording = False
            saved_path = self.file_path
            if self._file:
                try:
                    self._file.flush()
                    self._file.close()
                except Exception as e:
                    log.error("Error closing recording file: %s", e)
                self._file = None
                self._writer = None

            log.info("Stopped recording. Wrote %d samples to %s", self.record_count, saved_path)
            return saved_path
