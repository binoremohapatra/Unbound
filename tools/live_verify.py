"""live_verify.py — Live camera verification tool for human operator validation (T-09).

Usage:
  python tools/live_verify.py [--seconds 15] [--camera 0]

Outputs:
  - Total frames captured
  - FPS statistics (mean, p50, min, max)
  - Pipeline Tier breakdown (% FACEMESH478, % KEYPOINT5, % NO_FACE)
  - Biometric averages (EAR, MAR, Yaw, Pitch)
  - Pass/Fail verification verdict
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from typing import List

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2
import numpy as np

from host.omniband.tracking.webcam_tracker import (
    RealWebcamTracker,
    TrackerResult,
    enhance_low_light,
)


def run_live_verification(seconds: float = 15.0, camera_id: int = 0) -> int:
    print(f"=== OMNIBAND V2 LIVE CAMERA TRACKER VERIFICATION ({seconds:.1f}s) ===")
    tracker = RealWebcamTracker(camera_id=camera_id)
    det_video, det_image, yunet = tracker._init_models()

    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    cap = cv2.VideoCapture(camera_id, backend)
    if not cap.isOpened():
        cap = cv2.VideoCapture(camera_id)

    if not cap.isOpened():
        print(f"FAIL: Could not open camera {camera_id}")
        return 1

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    print("Warming up camera sensor...")
    for _ in range(15):
        cap.read()
        time.sleep(0.02)

    print(f"Starting live tracking measurement for {seconds:.1f} seconds. Please face camera...")
    results: List[TrackerResult] = []
    frame_times: List[float] = []

    start_t = time.perf_counter()
    prev_t = start_t
    last_timestamp_ms = 0

    while time.perf_counter() - start_t < seconds:
        ret, frame = cap.read()
        if not ret or frame is None:
            time.sleep(0.005)
            continue

        now = time.perf_counter()
        dt = max(1e-4, now - prev_t)
        prev_t = now
        frame_times.append(dt)

        t_ms = int(now * 1000)
        if t_ms <= last_timestamp_ms:
            t_ms = last_timestamp_ms + 1
        last_timestamp_ms = t_ms

        frame = cv2.flip(frame, 1)
        h, w = frame.shape[:2]
        enhanced = enhance_low_light(frame)
        frame_rgb = cv2.cvtColor(enhanced, cv2.COLOR_BGR2RGB)

        res = tracker._execute_pipeline(
            frame_rgb, enhanced, w, h, t_ms, det_video, det_image, yunet
        )
        results.append(res)
        time.sleep(0.005)

    cap.release()
    if det_video:
        det_video.close()
    if det_image:
        det_image.close()

    total_frames = len(results)
    if total_frames == 0:
        print("FAIL: Zero frames processed!")
        return 1

    fps_vals = [1.0 / dt for dt in frame_times if dt > 0]
    mean_fps = float(np.mean(fps_vals))
    p50_fps = float(np.median(fps_vals))
    min_fps = float(np.min(fps_vals))
    max_fps = float(np.max(fps_vals))

    tier_counts = {"FACEMESH478": 0, "KEYPOINT5": 0, "NO_FACE": 0}
    ears = []
    mars = []
    yaws = []
    pitches = []

    for r in results:
        tier_counts[r.tier] = tier_counts.get(r.tier, 0) + 1
        if r.face_found:
            if r.ear is not None:
                ears.append(r.ear)
            if r.mar is not None:
                mars.append(r.mar)
            yaws.append(r.yaw)
            pitches.append(r.pitch)

    print("\n--- MEASUREMENT SUMMARY ---")
    print(f"Total Frames Acquired: {total_frames}")
    print(f"FPS: Mean={mean_fps:.1f}, Median={p50_fps:.1f}, Min={min_fps:.1f}, Max={max_fps:.1f}")
    print("Tier Breakdown:")
    for tier, count in tier_counts.items():
        pct = (count / total_frames) * 100.0
        print(f"  {tier:12s}: {count:4d} frames ({pct:5.1f}%)")

    faces_found = total_frames - tier_counts["NO_FACE"]
    face_pct = (faces_found / total_frames) * 100.0
    print(f"\nOverall Face Presence: {face_pct:.1f}%")

    if ears:
        print(f"Mean EAR: {np.mean(ears):.3f} (min={np.min(ears):.3f}, max={np.max(ears):.3f})")
    if mars:
        print(f"Mean MAR: {np.mean(mars):.3f} (min={np.min(mars):.3f}, max={np.max(mars):.3f})")
    if yaws:
        print(f"Mean Yaw: {np.mean(yaws):+.1f} deg, Mean Pitch: {np.mean(pitches):+.1f} deg")

    if face_pct >= 50.0:
        print("\nVERDICT: [PASS] Face tracking confirmed stable.")
        return 0
    else:
        print("\nVERDICT: [WARN] Face detected in <50% of frames (check lighting / face positioning).")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Live camera tracking verification")
    parser.add_argument("--seconds", type=float, default=5.0, help="Duration to run in seconds")
    parser.add_argument("--camera", type=int, default=0, help="Camera index")
    args = parser.parse_args()
    return run_live_verification(args.seconds, args.camera)


if __name__ == "__main__":
    sys.exit(main())
