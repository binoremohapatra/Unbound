"""test_piezo_testing.py - Unit tests for Phase 2 Live Piezo Sensor Testing Suite."""
import csv
import tempfile
import time
from pathlib import Path

from piezo_testing.data_logger import DataLogger
from piezo_testing.sensor_reader import PiezoSample, SensorReader
from piezo_testing.serial_manager import SerialManager
from piezo_testing.signal_statistics import SignalStatisticsAnalyzer


def test_sensor_reader_formats():
    reader = SensorReader()
    now = time.perf_counter()
    wall = time.time()

    # 1. Compact CSV
    s1 = reader.parse_line("S,100,200,300,0,10", now, wall)
    assert s1 is not None
    assert s1.ch1 == 100.0
    assert s1.ch2 == 200.0
    assert s1.ch3 == 300.0
    assert s1.ch4 == 0.0
    assert s1.seq == 10

    # 2. Wi-Fi JSON Telemetry Frame
    s2 = reader.parse_line('{"ch":[550, 620, 150, 0],"seq":11,"dx":0.0,"dy":0.0}', now + 0.016, wall + 0.016)
    assert s2 is not None
    assert s2.ch1 == 550.0
    assert s2.ch2 == 620.0
    assert s2.ch3 == 150.0
    assert s2.ch4 == 0.0
    assert s2.seq == 11
    assert s2.interval_ms > 0.0

    # 3. Standard Link Telemetry JSON
    s3 = reader.parse_line('{"k":"tele","raw":[120, 80, 240, 15],"seq":12}', now + 0.032, wall + 0.032)
    assert s3 is not None
    assert s3.ch1 == 120.0
    assert s3.ch2 == 80.0
    assert s3.ch3 == 240.0
    assert s3.ch4 == 15.0
    assert s3.seq == 12

    # 4. Scope frame
    s4 = reader.parse_line('{"k":"scope","x":[[10, 20],[30, 40],[50, 60],[70, 80]],"seq":13}', now + 0.048, wall + 0.048)
    assert s4 is not None
    assert s4.ch1 == 20.0
    assert s4.ch2 == 40.0
    assert s4.ch3 == 60.0
    assert s4.ch4 == 80.0

    # 5. Non-sensor / Malformed lines should return None
    assert reader.parse_line("", now, wall) is None
    assert reader.parse_line("[BOOT] Initializing...", now, wall) is None
    assert reader.parse_line("PONG", now, wall) is None


def test_signal_statistics_independent_channels():
    analyzer = SignalStatisticsAnalyzer()
    now = time.perf_counter()

    # Feed 10 samples with distinct channel values
    for i in range(10):
        sample = PiezoSample(
            timestamp=now + (i * 0.016),
            wall_time=time.time() + (i * 0.016),
            ch1=100.0 + i * 10,     # 100 .. 190
            ch2=200.0 - i * 5,      # 200 .. 155
            ch3=50.0 if i % 2 == 0 else 500.0,  # Alternating
            ch4=0.0,
            seq=i + 1,
            packet_type="test",
            raw_line="",
            interval_ms=16.0,
        )
        analyzer.feed_sample(sample)

    # Verify Channel 1
    ch1 = analyzer.channels[0]
    assert ch1.min_val == 100.0
    assert ch1.max_val == 190.0
    assert ch1.current == 190.0

    # Verify Channel 2 (completely independent of CH1)
    ch2 = analyzer.channels[1]
    assert ch2.max_val == 200.0
    assert ch2.min_val == 155.0
    assert ch2.current == 155.0

    # Verify Channel 3
    ch3 = analyzer.channels[2]
    assert ch3.max_val == 500.0
    assert ch3.min_val == 50.0

    # Verify Rate Computation
    timing = analyzer.compute_rates()
    assert timing.total_packets == 10
    assert timing.avg_interval_ms == 16.0


def test_data_logger_recording_and_markers():
    logger = DataLogger()
    with tempfile.TemporaryDirectory() as tmp_dir:
        csv_path = Path(tmp_dir) / "test_recording.csv"
        logger.start_recording(custom_path=str(csv_path))
        assert logger.is_recording is True

        # Tag with a specific test phase
        logger.set_test_marker("TEST_E_CLENCH")

        sample = PiezoSample(
            timestamp=1000.5,
            wall_time=1700000000.123,
            ch1=350.0,
            ch2=420.0,
            ch3=110.0,
            ch4=0.0,
            seq=42,
            packet_type="tele",
            raw_line="raw_dummy",
            interval_ms=16.2,
        )
        logger.log_sample(sample)
        saved = logger.stop_recording()
        assert logger.is_recording is False
        assert saved == csv_path

        # Validate CSV contents
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            assert "ch1_raw_left_temple" in header
            assert "test_marker" in header

            row = next(reader)
            assert row[2] == "350.0"
            assert row[3] == "420.0"
            assert row[4] == "110.0"
            assert row[5] == "0.0"
            assert row[6] == "42"
            assert row[9] == "TEST_E_CLENCH"


def test_serial_manager_queue_safety():
    mgr = SerialManager(max_queue_size=10)
    # Fill beyond queue capacity
    for i in range(25):
        mgr._enqueue_line(f"packet_{i}", time.perf_counter())

    # Queue should not exceed 10 items and should not raise queue.Full
    assert mgr.raw_queue.qsize() <= 10
    # Must retain newest packets (not stuck on packet_0)
    line, _ = mgr.raw_queue.get_nowait()
    assert line.startswith("packet_")
