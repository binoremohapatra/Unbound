#!/usr/bin/env python3
"""tools/camera_probe.py — Probe camera backends, formats, measured fps, and luma.

Reports:
- opened?
- negotiated width, height, fps, FOURCC
- MEASURED fps from frame arrival intervals
- mean luma (0-255) per second
- exposure/gain/brightness property queries and write tests
No frames saved to disk.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Dict

import cv2
import numpy as np


def decode_fourcc(v: float) -> str:
    try:
        iv = int(v)
        return "".join([chr((iv >> 8 * i) & 0xFF) for i in range(4)])
    except Exception:
        return "UNKNOWN"


def probe_backend(cam_idx: int, backend_id: int, backend_name: str, seconds: float) -> Dict[str, Any]:
    res: Dict[str, Any] = {
        "backend": backend_name,
        "index": cam_idx,
        "opened": False,
    }

    t0 = time.perf_counter()
    cap = cv2.VideoCapture(cam_idx, backend_id)
    open_time = time.perf_counter() - t0
    res["open_duration_s"] = round(open_time, 3)

    if not cap.isOpened():
        return res

    res["opened"] = True

    # Read back properties
    w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    fps_prop = cap.get(cv2.CAP_PROP_FPS)
    fourcc_val = cap.get(cv2.CAP_PROP_FOURCC)
    fourcc_str = decode_fourcc(fourcc_val)

    exp_val = cap.get(cv2.CAP_PROP_EXPOSURE)
    gain_val = cap.get(cv2.CAP_PROP_GAIN)
    bright_val = cap.get(cv2.CAP_PROP_BRIGHTNESS)
    auto_exp = cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)

    # Test setting exposure
    can_set_exp = False
    try:
        if exp_val is not None:
            can_set_exp = cap.set(cv2.CAP_PROP_EXPOSURE, exp_val)
    except Exception:
        can_set_exp = False

    res["negotiated"] = {
        "width": int(w) if w else 0,
        "height": int(h) if h else 0,
        "reported_fps": fps_prop,
        "fourcc": fourcc_str,
        "exposure": exp_val,
        "gain": gain_val,
        "brightness": bright_val,
        "auto_exposure": auto_exp,
        "can_set_exposure": bool(can_set_exp),
    }

    # Measure actual frame arrival
    frame_intervals = []
    luma_per_second: Dict[int, list] = {}
    last_frame_t = time.perf_counter()
    start_t = last_frame_t
    frames_count = 0

    while time.perf_counter() - start_t < seconds:
        ret, frame = cap.read()
        now = time.perf_counter()
        if not ret or frame is None:
            time.sleep(0.005)
            continue

        frames_count += 1
        dt = now - last_frame_t
        last_frame_t = now
        frame_intervals.append(dt)

        sec_bucket = int(now - start_t)
        # Compute mean luma
        if len(frame.shape) == 3:
            # Standard Rec.601 / Rec.709 luma approx
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            luma = float(np.mean(gray))
        else:
            luma = float(np.mean(frame))

        luma_per_second.setdefault(sec_bucket, []).append(luma)

    cap.release()

    total_time = time.perf_counter() - start_t
    measured_fps = frames_count / total_time if total_time > 0 else 0.0

    mean_luma_by_sec = {
        sec: round(float(np.mean(vals)), 2) for sec, vals in sorted(luma_per_second.items())
    }
    overall_mean_luma = round(float(np.mean([v for vals in luma_per_second.values() for v in vals])), 2) if luma_per_second else 0.0

    res["measured"] = {
        "frames_received": frames_count,
        "duration_s": round(total_time, 3),
        "measured_fps": round(measured_fps, 2),
        "overall_mean_luma": overall_mean_luma,
        "mean_luma_per_second": mean_luma_by_sec,
        "p50_frame_interval_ms": round(float(np.median(frame_intervals) * 1000), 2) if frame_intervals else 0.0,
        "p95_frame_interval_ms": round(float(np.percentile(frame_intervals, 95) * 1000), 2) if frame_intervals else 0.0,
    }

    return res


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe camera backends and performance.")
    parser.add_argument("--seconds", type=float, default=5.0, help="Seconds to measure per backend (default: 5.0)")
    parser.add_argument("--index", type=int, default=0, help="Camera index (default: 0)")
    parser.add_argument("--json", action="store_true", help="Output pure JSON")
    args = parser.parse_args()

    backends = []
    if sys.platform == "win32":
        backends.append((cv2.CAP_DSHOW, "CAP_DSHOW"))
        backends.append((cv2.CAP_MSMF, "CAP_MSMF"))
    else:
        backends.append((cv2.CAP_ANY, "CAP_ANY"))

    results = []
    for bid, bname in backends:
        r = probe_backend(args.index, bid, bname, args.seconds)
        results.append(r)

    out = {"camera_index": args.index, "backends": results}
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"=== Camera Probe Report (Index {args.index}, {args.seconds}s duration) ===")
        for b in results:
            print(f"\n--- Backend: {b['backend']} ---")
            print(f"  Opened: {b['opened']} (in {b.get('open_duration_s', 0)}s)")
            if b['opened']:
                neg = b['negotiated']
                meas = b['measured']
                print(f"  Negotiated: {neg['width']}x{neg['height']} @ {neg['reported_fps']} fps ({neg['fourcc']})")
                print(f"  Exposure: {neg['exposure']} (can_set={neg['can_set_exposure']}), AutoExp={neg['auto_exposure']}, Gain={neg['gain']}, Brightness={neg['brightness']}")
                print(f"  Measured FPS: {meas['measured_fps']} ({meas['frames_received']} frames in {meas['duration_s']}s)")
                print(f"  Intervals: p50={meas['p50_frame_interval_ms']}ms, p95={meas['p95_frame_interval_ms']}ms")
                print(f"  Mean Luma: {meas['overall_mean_luma']} / 255 (by sec: {meas['mean_luma_per_second']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())

