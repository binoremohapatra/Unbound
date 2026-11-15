"""sensor_reader.py - Real-Time Piezo Packet Decoder & Stream Parser.

Parses all existing Unbound telemetry, scope, CSV, and Wi-Fi packet formats into raw channel samples.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

log = logging.getLogger("piezo_testing.sensor_reader")


@dataclass
class PiezoSample:
    """Normalized real-time piezo sample containing all 4 channels independently."""
    timestamp: float          # High-resolution perf_counter time
    wall_time: float          # Real wall-clock time for CSV logging
    ch1: float                # Raw ADC count 0..4095 (Left Temple)
    ch2: float                # Raw ADC count 0..4095 (Right Temple)
    ch3: float                # Raw ADC count 0..4095 (Forehead)
    ch4: float                # Raw ADC count 0..4095 (Jaw/Digital)
    seq: int                  # Packet sequence ID
    packet_type: str          # "tele", "wifi_frame", "scope", "csv", "compat_v1"
    raw_line: str             # Original unmodified raw string
    interval_ms: float = 0.0  # Delta time from prior packet in ms

    @property
    def channels(self) -> List[float]:
        return [self.ch1, self.ch2, self.ch3, self.ch4]


class SensorReader:
    """Parses raw text stream into structured PiezoSample data."""

    def __init__(self) -> None:
        self.last_timestamp: float = 0.0
        self.last_seq: Optional[int] = None
        self.total_samples: int = 0
        self.dropped_packets: int = 0
        self.malformed_packets: int = 0

    def parse_line(self, line: str, arrival_time: float, wall_time: float) -> Optional[PiezoSample]:
        """Decode a line into a PiezoSample. Returns None if invalid or non-sensor line."""
        s = line.strip()
        if not s:
            return None

        # Format 1: Compact CSV: "S,ch1,ch2,ch3,ch4,seq"
        if s.startswith("S,"):
            sample = self._parse_compact_csv(s, arrival_time, wall_time)
            if sample:
                return self._finalize_sample(sample)
            self.malformed_packets += 1
            return None

        # Format 2: JSON Payloads (Telemetry, Wi-Fi stream, Scope, Compat V1)
        if s.startswith("{") and s.endswith("}"):
            sample = self._parse_json_payload(s, arrival_time, wall_time)
            if sample:
                return self._finalize_sample(sample)
            self.malformed_packets += 1
            return None

        # Format 3: Raw comma-separated numbers "c1,c2,c3,c4"
        if "," in s:
            parts = s.split(",")
            if len(parts) >= 4:
                try:
                    c1 = float(parts[0])
                    c2 = float(parts[1])
                    c3 = float(parts[2])
                    c4 = float(parts[3])
                    seq = int(parts[4]) if len(parts) > 4 else self.total_samples
                    sample = PiezoSample(
                        timestamp=arrival_time,
                        wall_time=wall_time,
                        ch1=c1, ch2=c2, ch3=c3, ch4=c4,
                        seq=seq,
                        packet_type="csv",
                        raw_line=s,
                    )
                    return self._finalize_sample(sample)
                except ValueError:
                    pass

        self.malformed_packets += 1
        return None

    def _parse_compact_csv(self, s: str, arrival_time: float, wall_time: float) -> Optional[PiezoSample]:
        parts = s.split(",")
        if len(parts) >= 5:
            try:
                c1 = float(parts[1])
                c2 = float(parts[2])
                c3 = float(parts[3])
                c4 = float(parts[4]) if len(parts) > 4 else 0.0
                seq = int(parts[5]) if len(parts) > 5 else self.total_samples
                return PiezoSample(
                    timestamp=arrival_time,
                    wall_time=wall_time,
                    ch1=c1, ch2=c2, ch3=c3, ch4=c4,
                    seq=seq,
                    packet_type="csv",
                    raw_line=s,
                )
            except (ValueError, IndexError):
                return None
        return None

    def _parse_json_payload(self, s: str, arrival_time: float, wall_time: float) -> Optional[PiezoSample]:
        try:
            doc = json.loads(s)
            if not isinstance(doc, dict):
                return None

            # Case A: Wi-Fi UDP Telemetry {"ch": [c1, c2, c3, c4], "seq": ...}
            if "ch" in doc and isinstance(doc["ch"], list):
                arr = doc["ch"]
                c1 = float(arr[0]) if len(arr) > 0 else 0.0
                c2 = float(arr[1]) if len(arr) > 1 else 0.0
                c3 = float(arr[2]) if len(arr) > 2 else 0.0
                c4 = float(arr[3]) if len(arr) > 3 else 0.0
                seq = int(doc.get("seq", self.total_samples))
                return PiezoSample(
                    timestamp=arrival_time,
                    wall_time=wall_time,
                    ch1=c1, ch2=c2, ch3=c3, ch4=c4,
                    seq=seq,
                    packet_type="wifi_frame",
                    raw_line=s,
                )

            # Case B: Standard Link Telemetry {"k": "tele", "raw": [r1, r2, r3, r4], ...}
            if doc.get("k") == "tele" or "raw" in doc:
                raw_arr = doc.get("raw") or doc.get("env") or [0.0, 0.0, 0.0, 0.0]
                if isinstance(raw_arr, list):
                    c1 = float(raw_arr[0]) if len(raw_arr) > 0 else 0.0
                    c2 = float(raw_arr[1]) if len(raw_arr) > 1 else 0.0
                    c3 = float(raw_arr[2]) if len(raw_arr) > 2 else 0.0
                    c4 = float(raw_arr[3]) if len(raw_arr) > 3 else 0.0
                    seq = int(doc.get("seq", self.total_samples))
                    return PiezoSample(
                        timestamp=arrival_time,
                        wall_time=wall_time,
                        ch1=c1, ch2=c2, ch3=c3, ch4=c4,
                        seq=seq,
                        packet_type="tele",
                        raw_line=s,
                    )

            # Case C: Scope frame {"k": "scope", "x": [[...],[...],[...],[...]]}
            if doc.get("k") == "scope" and "x" in doc and isinstance(doc["x"], list):
                x = doc["x"]
                # Take latest sample of each channel history
                c1 = float(x[0][-1]) if len(x) > 0 and len(x[0]) > 0 else 0.0
                c2 = float(x[1][-1]) if len(x) > 1 and len(x[1]) > 0 else 0.0
                c3 = float(x[2][-1]) if len(x) > 2 and len(x[2]) > 0 else 0.0
                c4 = float(x[3][-1]) if len(x) > 3 and len(x[3]) > 0 else 0.0
                seq = int(doc.get("seq", self.total_samples))
                return PiezoSample(
                    timestamp=arrival_time,
                    wall_time=wall_time,
                    ch1=c1, ch2=c2, ch3=c3, ch4=c4,
                    seq=seq,
                    packet_type="scope",
                    raw_line=s,
                )

            # Case D: Compat V1 line {"piezo_raw": val, ...}
            if "piezo_raw" in doc:
                c4 = float(doc["piezo_raw"])
                seq = int(doc.get("seq", self.total_samples))
                return PiezoSample(
                    timestamp=arrival_time,
                    wall_time=wall_time,
                    ch1=0.0, ch2=0.0, ch3=0.0, ch4=c4,
                    seq=seq,
                    packet_type="compat_v1",
                    raw_line=s,
                )

        except Exception as e:
            log.debug("JSON parse error: %s on line %r", e, s)
            return None

        return None

    def _finalize_sample(self, sample: PiezoSample) -> PiezoSample:
        """Compute delta timing and track sequence gaps."""
        if self.last_timestamp > 0.0:
            sample.interval_ms = (sample.timestamp - self.last_timestamp) * 1000.0
        else:
            sample.interval_ms = 0.0
        self.last_timestamp = sample.timestamp

        # Check sequence continuity
        if self.last_seq is not None and sample.seq > self.last_seq:
            gap = sample.seq - self.last_seq - 1
            if 0 < gap < 1000:
                self.dropped_packets += gap
        self.last_seq = sample.seq
        self.total_samples += 1
        return sample

    def reset_stats(self) -> None:
        self.last_timestamp = 0.0
        self.last_seq = None
        self.total_samples = 0
        self.dropped_packets = 0
        self.malformed_packets = 0
