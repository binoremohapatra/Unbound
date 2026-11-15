"""signal_statistics.py - Real-Time Piezo Signal Statistics & Timing Analyzer.

Calculates independent per-channel statistics and high-accuracy transmission rate metrics.
"""
from __future__ import annotations

import collections
import math
import time
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

from .sensor_reader import PiezoSample


@dataclass
class ChannelStats:
    """Statistical summary for a single independent piezo channel."""
    channel_id: int
    current: float = 0.0
    min_val: float = float("inf")
    max_val: float = float("-inf")
    mean: float = 0.0
    peak_val: float = 0.0
    std_dev: float = 0.0
    peak_to_peak: float = 0.0
    peak_count: int = 0

    # Rolling window for local dynamic stats (last 200 samples)
    _rolling: Deque[float] = field(default_factory=lambda: collections.deque(maxlen=300))
    _last_val: float = 0.0
    _rising: bool = False

    def update(self, val: float) -> None:
        self.current = val
        self._rolling.append(val)

        if val < self.min_val:
            self.min_val = val
        if val > self.max_val:
            self.max_val = val
        if val > self.peak_val:
            self.peak_val = val

        # Rolling statistics
        n = len(self._rolling)
        if n > 0:
            self.mean = sum(self._rolling) / n
            var = sum((x - self.mean) ** 2 for x in self._rolling) / max(1, n - 1)
            self.std_dev = math.sqrt(var)
            self.peak_to_peak = max(self._rolling) - min(self._rolling)

        # Peak detection: local peak turning point above mean + 2.0*std_dev
        if val > self._last_val:
            self._rising = True
        elif val < self._last_val and self._rising:
            self._rising = False
            # Check if crest is significant relative to noise
            if self._last_val > (self.mean + max(30.0, 1.8 * self.std_dev)):
                self.peak_count += 1

        self._last_val = val

    def reset(self) -> None:
        self.current = 0.0
        self.min_val = float("inf")
        self.max_val = float("-inf")
        self.mean = 0.0
        self.peak_val = 0.0
        self.std_dev = 0.0
        self.peak_to_peak = 0.0
        self.peak_count = 0
        self._rolling.clear()
        self._last_val = 0.0
        self._rising = False


@dataclass
class TimingMetrics:
    """High-accuracy packet timing and rate measurements."""
    # 1. Hardware acquisition frequency (spec: 500 Hz firmware / 60 Hz target)
    hardware_spec_hz: float = 500.0

    # 2. Serial data transmission frequency (measured incoming packet rate)
    measured_transmission_hz: float = 0.0

    # 3. Python application reading frequency (UI consumption rate)
    python_consumer_hz: float = 0.0

    # Intervals in milliseconds
    avg_interval_ms: float = 0.0
    min_interval_ms: float = float("inf")
    max_interval_ms: float = 0.0
    jitter_ms: float = 0.0

    # Packet health
    total_packets: int = 0
    dropped_packets: int = 0
    malformed_packets: int = 0
    packet_loss_pct: float = 0.0


class SignalStatisticsAnalyzer:
    """Coordinates multi-channel statistics and frequency calculation."""

    def __init__(self) -> None:
        self.channels: Dict[int, ChannelStats] = {
            0: ChannelStats(channel_id=1),
            1: ChannelStats(channel_id=2),
            2: ChannelStats(channel_id=3),
            3: ChannelStats(channel_id=4),
        }
        self.timing = TimingMetrics()

        # Rolling arrival timestamps for rate calculation (last 1.5 seconds)
        self._arrival_times: Deque[float] = collections.deque(maxlen=250)
        self._intervals: Deque[float] = collections.deque(maxlen=250)
        self._consumer_ticks: Deque[float] = collections.deque(maxlen=100)

    def feed_sample(self, sample: PiezoSample) -> None:
        """Feed a new parsed sample into the statistical engines."""
        now = sample.timestamp
        self._arrival_times.append(now)

        if sample.interval_ms > 0.0:
            self._intervals.append(sample.interval_ms)
            if sample.interval_ms < self.timing.min_interval_ms:
                self.timing.min_interval_ms = sample.interval_ms
            if sample.interval_ms > self.timing.max_interval_ms:
                self.timing.max_interval_ms = sample.interval_ms

        # Update per-channel independent stats
        vals = [sample.ch1, sample.ch2, sample.ch3, sample.ch4]
        for idx in range(4):
            self.channels[idx].update(vals[idx])

        self.timing.total_packets += 1

    def update_consumer_tick(self) -> None:
        """Called every time the GUI consumer loop pulls a batch of samples."""
        self._consumer_ticks.append(time.perf_counter())

    def compute_rates(self) -> TimingMetrics:
        """Compute live transmission rate and consumer rate."""
        now = time.perf_counter()

        # Transmission rate (incoming packets from device)
        # Purge arrival times older than 1.5 seconds
        while self._arrival_times and (now - self._arrival_times[0]) > 1.5:
            self._arrival_times.popleft()

        if len(self._arrival_times) >= 2:
            dt = self._arrival_times[-1] - self._arrival_times[0]
            if dt > 0.05:
                self.timing.measured_transmission_hz = (len(self._arrival_times) - 1) / dt
        else:
            self.timing.measured_transmission_hz = 0.0

        # Python UI consumption rate
        while self._consumer_ticks and (now - self._consumer_ticks[0]) > 1.5:
            self._consumer_ticks.popleft()

        if len(self._consumer_ticks) >= 2:
            dt_c = self._consumer_ticks[-1] - self._consumer_ticks[0]
            if dt_c > 0.05:
                self.timing.python_consumer_hz = (len(self._consumer_ticks) - 1) / dt_c
        else:
            self.timing.python_consumer_hz = 0.0

        # Intervals
        if self._intervals:
            n = len(self._intervals)
            mean_int = sum(self._intervals) / n
            self.timing.avg_interval_ms = mean_int
            var = sum((x - mean_int) ** 2 for x in self._intervals) / max(1, n - 1)
            self.timing.jitter_ms = math.sqrt(var)

        # Loss calculation
        total_expected = self.timing.total_packets + self.timing.dropped_packets
        if total_expected > 0:
            self.timing.packet_loss_pct = (self.timing.dropped_packets / total_expected) * 100.0

        return self.timing

    def reset(self) -> None:
        for ch in self.channels.values():
            ch.reset()
        self.timing = TimingMetrics()
        self._arrival_times.clear()
        self._intervals.clear()
        self._consumer_ticks.clear()
