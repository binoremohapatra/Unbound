"""omniband_ping.py - measure OmniBand link latency (ping RTT + tele rate).

  python tools/omniband_ping.py --simulate [--count 20]
  python tools/omniband_ping.py --port COM5 [--baud 115200] [--count 20]

Reports per-ping RTT (device t echo -> host receipt) and tele line rate.
Latency numbers are MEASURED on this machine/link - label them as such.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from host.omniband import protocol  # noqa: E402
from host.omniband.link import Arbiter, SerialTransport, SimTransport  # noqa: E402
from host.omniband.sim_device import SimDevice  # noqa: E402
from host.omniband.constants_gen import CONSTANTS  # noqa: E402

K = CONSTANTS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--port", default="")
    ap.add_argument("--baud", type=int, default=K["link"]["baud_boot"])
    ap.add_argument("--count", type=int, default=20)
    args = ap.parse_args()

    dev = None
    if args.simulate:
        dev = SimDevice()
        tr = SimTransport(dev, "sim")
    elif args.port:
        tr = SerialTransport(args.port, args.baud, "usb")
    else:
        print("need --simulate or --port")
        return 2

    rtts: list[float] = []
    tele_count = 0
    t0 = time.monotonic()

    def on_line(obj):
        nonlocal tele_count
        if obj.get("k") == "tele":
            tele_count += 1
        elif obj.get("k") == "pong":
            th = obj.get("th")
            # RTT measured send->recv on host monotonic clock
            sent = sent_at.get(th)
            if sent is not None:
                rtts.append((time.monotonic() - sent) * 1000.0)

    arb = Arbiter([tr], on_line=on_line)
    tr.open()
    sent_at: dict[int, float] = {}

    # warmup: let hello/tele flow
    end = time.monotonic() + 1.0
    while time.monotonic() < end:
        arb.poll(time.monotonic() * 1000)
        time.sleep(0.005)

    for i in range(args.count):
        th = 1000 + i
        sent_at[th] = time.monotonic()
        arb.send(protocol.cmd_ping(th, cmd_id=f"p{th}"))
        time.sleep(0.05)
        arb.poll(time.monotonic() * 1000)

    time.sleep(0.5)
    arb.poll(time.monotonic() * 1000)
    elapsed = time.monotonic() - t0

    tr.close()
    if dev:
        dev.stop()

    if rtts:
        rtts.sort()
        print(f"pings: {len(rtts)}/{args.count} answered")
        print(f"RTT ms: min={rtts[0]:.1f} median={statistics.median(rtts):.1f} "
              f"p95={rtts[int(len(rtts)*0.95)-1]:.1f} max={rtts[-1]:.1f}")
    else:
        print("no pong replies received")
    print(f"tele lines in {elapsed:.1f}s: {tele_count} "
          f"(~{tele_count/elapsed:.1f}/s, nominal 30)")
    print(f"arbiter stats: {arb.stats}")
    return 0 if rtts else 1


if __name__ == "__main__":
    sys.exit(main())
