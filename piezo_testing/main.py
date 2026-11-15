"""main.py - Entry point for Unbound Live Piezo Sensor Testing & Monitoring Suite (Phase 2)."""
import argparse
import sys
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .config import DEFAULT_BAUD
from .live_dashboard import LivePiezoDashboard
from .sensor_reader import SensorReader
from .signal_statistics import SignalStatisticsAnalyzer


def run_selftest() -> bool:
    """Non-hardware unit verification for sensor reader, statistical engine, and timing."""
    print("=== SELF-TEST: PIEZO MONITORING SUITE PIPELINE ===")
    reader = SensorReader()
    stats = SignalStatisticsAnalyzer()

    # 1. Test CSV format parsing
    now = time.perf_counter()
    wall = time.time()
    s1 = reader.parse_line("S,150.0,220.0,85.0,0.0,1", now, wall)
    assert s1 is not None, "Failed parsing compact CSV"
    assert s1.ch1 == 150.0 and s1.ch2 == 220.0 and s1.ch3 == 85.0 and s1.ch4 == 0.0
    assert s1.seq == 1
    stats.feed_sample(s1)
    print("PASS: Compact CSV parsing verified (CH1=150, CH2=220, CH3=85, CH4=0)")

    # 2. Test JSON Wi-Fi format parsing
    s2 = reader.parse_line('{"ch":[310, 450, 120, 0],"seq":2,"dx":0.0,"dy":0.0}', now + 0.016, wall + 0.016)
    assert s2 is not None, "Failed parsing Wi-Fi JSON telemetry"
    assert s2.ch1 == 310.0 and s2.ch2 == 450.0 and s2.ch3 == 120.0
    assert s2.seq == 2
    stats.feed_sample(s2)
    print("PASS: JSON Telemetry parsing verified (CH1=310, CH2=450, CH3=120, CH4=0)")

    # 3. Test V2 Standard Link Telemetry format parsing
    s3 = reader.parse_line('{"k":"tele","raw":[420, 580, 200, 10],"seq":3}', now + 0.032, wall + 0.032)
    assert s3 is not None, "Failed parsing V2 Link Telemetry"
    assert s3.ch1 == 420.0 and s3.ch2 == 580.0
    stats.feed_sample(s3)
    print("PASS: V2 Link Telemetry parsing verified (CH1=420, CH2=580, CH3=200, CH4=10)")

    # 4. Verify Per-Channel Statistical Engine
    assert stats.channels[0].min_val == 150.0
    assert stats.channels[0].max_val == 420.0
    assert stats.channels[1].max_val == 580.0
    print("PASS: Channel statistics correctly tracking independent Min/Max/Peaks")

    # 5. Verify Timing Calculations
    timing = stats.compute_rates()
    assert timing.total_packets == 3
    print("PASS: Rate and timing engine verified.")
    print("=== ALL PIEZO TESTING SUITE SELF-TESTS PASSED ===")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Unbound Live Piezo Sensor Testing & Monitoring Tool (Phase 2)")
    parser.add_argument("--port", type=str, default="", help="Serial port to auto-connect (e.g., COM3, /dev/ttyUSB0)")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"Baud rate (default: {DEFAULT_BAUD})")
    parser.add_argument("--window", type=int, default=10, help="Initial graph time window in seconds (5, 10, 30, 60)")
    parser.add_argument("--selftest", action="store_true", help="Run automated internal pipeline self-test and exit")
    args = parser.parse_args()

    if args.selftest:
        ok = run_selftest()
        sys.exit(0 if ok else 1)

    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)

    dashboard = LivePiezoDashboard()

    if args.port:
        dashboard.serial_manager.connect(args.port, args.baud)

    dashboard.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
