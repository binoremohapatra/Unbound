#!/usr/bin/env python3
"""read_omniband.py - bring-up tool for the OmniBand ESP32 node.

    pip install pyserial
    python read_omniband.py                 # auto-detect the port, run 15 s, print live bars + summary
    python read_omniband.py --port COM5 --seconds 60
    python read_omniband.py --recal        # send 'c' to the board first (recalibrate, keep still 2 s)
    python read_omniband.py --selftest     # no hardware: checks the parser and the statistics

What the summary tells you
    packets/s    ~100 at 921600 baud (50 at 115200)
    dropped      gaps in "seq" counted by the PC. Expect telemetry_div-sized steps (5), so the tool
                 counts a drop only when the gap is NOT a multiple of the telemetry divider or lat spikes.
    clench edges number of 0 -> 1 transitions of "clench"
    lat_ms       DEVICE-SIDE time only (sampling start -> packet write). Not USB / OS / Python latency.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from typing import Optional

REQUIRED = ("piezo_raw", "pulse", "clench", "ch_left", "ch_right", "ch_fwd", "lat_ms")
USB_SERIAL_VIDS = {0x10C4: "CP210x", 0x1A86: "CH340", 0x0403: "FTDI", 0x303A: "Espressif"}


def parse_line(line: str) -> Optional[dict]:
    """Return a packet dict, None for blank/'#' diagnostic lines, or raise ValueError."""
    line = line.strip()
    if not line or line[0] != "{":
        return None
    d = json.loads(line)  # json.JSONDecodeError is a ValueError; a '{...}' line is always an object
    for k in REQUIRED:
        if k not in d:
            raise ValueError(f"missing field {k!r}")
    return d


class Stats:
    def __init__(self, telemetry_div: int = 5) -> None:
        self.telemetry_div = telemetry_div
        self.packets = 0
        self.parse_errors = 0
        self.clench_edges = 0
        self.dropped = 0
        self.lat: list[float] = []
        self._last_seq: Optional[int] = None
        self._last_clench = 0
        self.t_first: Optional[float] = None
        self.t_last: Optional[float] = None

    def add(self, d: dict, t: float) -> bool:
        """Feed one packet; returns True when it is a new clench rising edge."""
        self.packets += 1
        self.t_first = t if self.t_first is None else self.t_first
        self.t_last = t
        self.lat.append(float(d["lat_ms"]))
        seq = d.get("seq")
        if isinstance(seq, int):
            if self._last_seq is not None:
                gap = seq - self._last_seq
                # Telemetry packets are `telemetry_div` loop iterations apart; an extra packet is
                # sent at a clench edge, so gaps of 1..telemetry_div are normal. Larger = lost packets.
                if gap > self.telemetry_div:
                    self.dropped += (gap - 1) // self.telemetry_div
            self._last_seq = seq
        edge = int(d["clench"]) == 1 and self._last_clench == 0
        self._last_clench = int(d["clench"])
        if edge:
            self.clench_edges += 1
        return edge

    def summary(self) -> str:
        dur = (self.t_last - self.t_first) if (self.t_first is not None and self.t_last) else 0.0
        rate = self.packets / dur if dur > 0 else 0.0
        if self.lat:
            lat = sorted(self.lat)
            p50 = statistics.median(lat)
            p95 = lat[min(len(lat) - 1, int(0.95 * (len(lat) - 1)))]
            lat_txt = f"p50 {p50:.3f}  p95 {p95:.3f}  max {lat[-1]:.3f} ms (device-side only)"
        else:
            lat_txt = "n/a"
        return (f"packets: {self.packets} in {dur:.1f}s = {rate:.0f}/s | parse errors: {self.parse_errors} | "
                f"dropped (est.): {self.dropped} | clench edges: {self.clench_edges} | lat_ms {lat_txt}")


def find_port() -> Optional[str]:
    from serial.tools import list_ports  # pyserial
    for p in list_ports.comports():
        if p.vid in USB_SERIAL_VIDS:
            print(f"# using {p.device} ({USB_SERIAL_VIDS[p.vid]}, {p.description})")
            return p.device
    return None


def bar(v: float, width: int = 20) -> str:
    n = int(max(0.0, min(1.0, v)) * width)
    return "#" * n + "." * (width - n)


def run(args: argparse.Namespace) -> int:
    try:
        import serial  # pyserial
    except ImportError:
        print("pyserial missing: pip install pyserial", file=sys.stderr)
        return 2
    port = args.port or find_port()
    if not port:
        print("No ESP32 serial port found. Plug it in, close the Arduino Serial Monitor, or pass --port.",
              file=sys.stderr)
        return 2
    telemetry_div = 5 if args.baud >= 460800 else 10
    st = Stats(telemetry_div)
    try:
        return _read_loop(serial, port, args, st)
    except serial.SerialException as e:
        print(f"\nSerial error on {port}: {e}\n(cable unplugged, port in use by another program, or wrong --port)",
              file=sys.stderr)
        print(st.summary())
        return 1


def _read_loop(serial, port: str, args: argparse.Namespace, st: "Stats") -> int:
    with serial.Serial(port, args.baud, timeout=0.05) as ser:
        if args.recal:
            ser.write(b"c")
        t_end = time.perf_counter() + args.seconds
        t_print = 0.0
        buf = b""
        while time.perf_counter() < t_end:
            buf += ser.read(512)
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = raw.decode("utf-8", errors="replace")
                if text.startswith("#"):
                    print(text.strip())
                    continue
                try:
                    d = parse_line(text)
                except ValueError:
                    st.parse_errors += 1
                    continue
                if d is None:
                    continue
                now = time.perf_counter()
                edge = st.add(d, now)
                if edge:
                    print(f"\n>>> CLENCH  pulse={d['pulse']:.2f} lat_ms={d['lat_ms']:.3f} seq={d.get('seq')}")
                if now - t_print > 0.2:
                    t_print = now
                    print(f"\rL {bar(d['ch_left'])} R {bar(d['ch_right'])} F {bar(d['ch_fwd'])} "
                          f"P {bar(d['pulse'])} raw={d['piezo_raw']:4d}", end="", flush=True)
    print("\n" + st.summary())
    return 0 if st.packets else 1


def selftest() -> int:
    ok = True

    def check(cond: bool, msg: str) -> None:
        nonlocal ok
        print(("  ok  : " if cond else "  FAIL: ") + msg)
        ok = ok and cond

    good = ('{"piezo_raw":123,"pulse":0.010,"clench":0,"ch_left":0.000,"ch_right":0.100,'
            '"ch_fwd":0.200,"lat_ms":0.250,"seq":5}')
    d = parse_line(good)
    check(d is not None and d["piezo_raw"] == 123 and d["seq"] == 5, "valid packet parses")
    check(parse_line("# omniband-fw 1.0 baud=921600") is None, "'#' banner lines are ignored")
    check(parse_line("") is None, "blank line ignored")
    check(parse_line("[1,2]") is None and parse_line("hello") is None, "non-JSON-object text is ignored")
    for bad, why in (('{"piezo_raw":1', "truncated JSON"), ('{"piezo_raw":1}', "missing fields")):
        try:
            parse_line(bad)
            check(False, f"{why} should raise")
        except ValueError:
            check(True, f"{why} raises ValueError")

    st = Stats(telemetry_div=5)
    t = 0.0
    seq = 0
    clench_at = {20, 21, 22, 23, 24, 25}                  # clench level high for 6 packets
    for i in range(60):
        seq += 5
        if i == 30:
            seq += 10                                       # simulate 2 lost telemetry packets
        pkt = dict(d, seq=seq, clench=1 if i in clench_at else 0, lat_ms=0.2 + 0.001 * i)
        st.add(pkt, t)
        t += 0.01
    check(st.clench_edges == 1, "one clench edge counted for a held level")
    check(st.dropped == 2, f"lost packets estimated correctly (got {st.dropped})")
    check(st.packets == 60 and 95 <= st.packets / (st.t_last - st.t_first) <= 105, "packet rate ~100/s")
    # An extra edge-triggered packet (gap of 1..div) must NOT count as a loss.
    st2 = Stats(5)
    for s in (5, 10, 12, 15, 20):
        st2.add(dict(d, seq=s), 0.0)
    check(st2.dropped == 0, "extra edge packet is not counted as a drop")
    print("SELFTEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="OmniBand serial bring-up tool")
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--recal", action="store_true", help="send 'c' (recalibrate) before reading")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    return selftest() if args.selftest else run(args)


if __name__ == "__main__":
    sys.exit(main())
