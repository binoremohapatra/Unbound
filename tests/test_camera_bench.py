"""Tests for tools/camera_bench.py pure logic (Binore). No cv2/mediapipe needed:
FakeCap feeds synthetic frames through measure_run; landmarker results are dicts."""
import csv

import pytest

from tools.camera_bench import (BlinkDetector, evaluate_blink, evaluate_jitter,
                                fps_from_timestamps, jitter_stats, measure_run,
                                percentile, recommend, row_for, summarize_measure,
                                write_csv)


class FakeCap:
    """Capture-like object: read() returns synthetic frames as fast as asked."""

    def __init__(self, fps_hint=0.0):
        self._i = 0
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        self._i += 1
        return True, {"fake": self._i}

    def release(self):
        self.released = True


def fake_clock():
    t = [1000.0]

    def now():
        t[0] += 0.001                      # 1 ms per call -> deterministic-ish
        return t[0]
    return now


# ------------------------------------------------------------------ primitives
def test_fps_from_timestamps():
    assert fps_from_timestamps([]) == 0.0
    assert fps_from_timestamps([1.0]) == 0.0
    ts = [0.0 + i / 60 for i in range(61)]
    assert fps_from_timestamps(ts) == pytest.approx(60.0, abs=0.5)


def test_percentile_edges():
    assert percentile([], 50) == 0.0
    assert percentile([1.0, 2.0, 3.0, 100.0], 50) == pytest.approx(3.0)  # idx round(.5*3)=2
    assert percentile([1.0, 2.0, 3.0, 100.0], 99) == 100.0


def test_jitter_stats_zero_for_still_point():
    pts = [(10.0, 20.0)] * 10
    assert jitter_stats(pts, [50.0] * 10) == (0.0, 0.0)


def test_jitter_stats_scales_with_spread():
    still = [(10.0, 20.0)] * 10
    spread = [(10.0 + (i % 2) * 4.0, 20.0) for i in range(10)]
    s_still, _ = jitter_stats(still, [50.0] * 10)
    s_spread, f_spread = jitter_stats(spread, [50.0] * 10)
    assert s_still == 0.0 and s_spread > 0.0 and f_spread == pytest.approx(s_spread / 50.0)


# ------------------------------------------------------------------ blink classifier
def test_blink_detector_hysteresis_and_min_duration():
    d = BlinkDetector()
    d.update(0.0, 0.1)                     # below on threshold: nothing
    d.update(1.00, 0.70)                   # opens
    d.update(1.10, 0.50)                   # between thresholds: still open
    d.update(1.40, 0.30)                   # closes after 0.40 s >= 0.23 -> HIT
    d.update(2.00, 0.80)                   # opens
    d.update(2.10, 0.30)                   # closes after 0.10 s < 0.23 -> no hit
    d.finish()
    assert d.hits == 1


def test_blink_detector_unclosed_blink_not_counted():
    d = BlinkDetector()
    d.update(0.0, 0.9)
    d.update(5.0, 0.9)                     # never closes
    d.finish()
    assert d.hits == 0


# ------------------------------------------------------------------ measure_run with FakeCap
def test_measure_run_counts_frames_and_times_proc():
    calls = []

    def proc(frame):
        calls.append(frame)
        return {"face": True, "nose": (1.0, 2.0), "iod": 50.0, "blink": 0.1}

    run = measure_run(FakeCap(), 0.05, proc=proc, now_fn=fake_clock())
    assert run["ts"] and len(run["ts"]) == len(run["proc_ms"]) == len(run["out"])
    assert len(calls) == len(run["ts"])
    summ = summarize_measure(run)
    assert summ["frames"] == len(run["ts"]) and summ["face_lost"] == 0
    assert summ["proc_p50"] >= 0.0


def test_measure_run_without_proc_still_timestamps():
    run = measure_run(FakeCap(), 0.03, now_fn=fake_clock())
    assert run["ts"] and run["proc_ms"] == [] and run["out"] == [{}] * len(run["ts"])


def test_measure_run_skips_warmup_frames():
    seen = []

    def proc(frame):
        seen.append(frame)
        return {"face": True}

    cap = FakeCap()
    # warmup 0.02 s + measure 0.02 s on a 1 ms clock: warmup reads happen but are not recorded
    run = measure_run(cap, 0.02, proc=proc, now_fn=fake_clock(), warmup_s=0.02)
    assert cap._i > len(run["ts"])               # more frames read than recorded
    assert len(seen) == len(run["ts"])           # proc only ran on measured frames
    assert all("face" in o for o in run["out"])


def test_summarize_counts_face_lost():
    run = {"ts": [0.0, 0.1, 0.2], "proc_ms": [1.0] * 3,
           "out": [{"face": True}, {"face": False}, {"face": True}]}
    assert summarize_measure(run)["face_lost"] == 1


def test_evaluate_jitter_and_blink_from_run():
    pts = [(float(i), 10.0) for i in range(10)]
    scores = [0.1] * 5 + [0.8] * 3 + [0.2] * 2   # 0.3 s above on-threshold -> 1 blink
    run = {"ts": [float(i) / 10 for i in range(10)], "proc_ms": [],
           "out": [{"face": True, "nose": p, "iod": 50.0, "blink": s}
                   for p, s in zip(pts, scores)]}
    jx, jf = evaluate_jitter(run)
    assert jx >= 0.0 and jf >= 0.0
    hits, det = evaluate_blink(run)
    assert hits == 1 and det.hits == 1


# ------------------------------------------------------------------ recommendation + csv
def _row(w, h, fps, jit, hits, lost):
    return {"width": w, "height": h, "fps_actual": fps, "jitter_frac": jit,
            "blink_hits": hits, "face_lost": lost}


def test_recommend_picks_smallest_passing_resolution():
    rows = [_row(640, 480, 30.0, 0.02, 20, 0),
            _row(640, 360, 30.0, 0.02, 20, 0),
            _row(480, 360, 25.0, 0.02, 20, 0)]        # fps too low -> excluded
    rec = recommend(rows)
    assert "640x360" in rec and "recommended" in rec


def test_recommend_rejects_jitter_over_1_5x_baseline():
    rows = [_row(640, 480, 30.0, 0.02, 20, 0),
            _row(640, 360, 30.0, 0.10, 20, 0),       # jitter 5x baseline -> fails
            _row(480, 360, 30.0, 0.02, 20, 0)]
    assert "480x360" in recommend(rows)


def test_recommend_fails_when_blinks_insufficient():
    rows = [_row(640, 480, 30.0, 0.02, 20, 0),
            _row(640, 360, 30.0, 0.02, 5, 0),
            _row(480, 360, 30.0, 0.02, 5, 0)]
    assert "640x480" in recommend(rows)             # only baseline passes


def test_recommend_no_pass_message():
    rows = [_row(640, 480, 15.0, 0.02, 0, 3),
            _row(640, 360, 15.0, 0.02, 0, 3),
            _row(480, 360, 15.0, 0.02, 0, 3)]
    assert "NO setting passed" in recommend(rows)


def test_config_camera_fps_thresholds_parse_and_clamp():
    from config import parse_config
    cfg, warns = parse_config({"camera_fps_pass": 30, "camera_fps_fail_below": 22})
    assert cfg.camera_fps_pass == 30.0 and cfg.camera_fps_fail_below == 22.0
    cfg2, warns2 = parse_config({"camera_fps_fail_below": 35})   # above pass -> pass raised
    assert cfg2.camera_fps_pass == 36.0 and any("camera_fps" in w for w in warns2)
    cfg3, _ = parse_config({"camera_fps_pass": "fast"})          # bad type -> default kept
    assert cfg3.camera_fps_pass == 28.0


def test_row_for_and_csv_roundtrip(tmp_path):
    run = {"ts": [0.0, 0.1], "proc_ms": [1.5, 2.5],
           "out": [{"face": True}, {"face": False}]}
    row = row_for((640, 360), 30, summarize_measure(run), jitter=(1.2, 0.03),
                  blink_hits=18)
    row["exposure"] = "on"
    p = tmp_path / "bench.csv"
    write_csv(str(p), [row])
    with open(p, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["width"] == "640" and rows[0]["blink_hits"] == "18"
    assert rows[0]["fps_req"] == "30"
