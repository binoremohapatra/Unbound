"""tools/camera_bench.py (Binore): measure what the demo camera REALLY delivers.

For each resolution in RESOLUTIONS it requests MJPG at 30 and 60 fps, warms up 1 s,
then measures for MEASURE_SECONDS: real fps from frame timestamps (never the requested
value), per-frame face-pipeline cost p50/p95 (CLAHE on Y + MediaPipe landmarks - a
conservative superset of the app's FaceMesh path, which uses no CLAHE), face-loss
count, 5 s hands-off jitter of the nose landmark, and a prompted blink test classified
by eyeBlinkLeft/Right blendshapes (0.58 on / 0.35 off, 230 ms minimum) when a
face_landmarker.task model is available.

--auto-exposure on|off|both (default both) toggles CAP_PROP_AUTO_EXPOSURE and reports
whether fps moved: auto exposure in low light commonly caps cameras at 15-20 fps.

Exit codes: 0 = bench completed, 2 = missing cv2/mediapipe or no camera.
Results are appended to bench.csv (default) and a recommendation is printed.

NOTE on honesty: the app's webcam adapter currently runs mp.solutions.face_mesh with
NO CLAHE and NO FaceLandmarker. This bench times CLAHE+landmarks (an upper bound on
pipeline cost) and uses FaceLandmarker only for the blink classifier. If no
face_landmarker.task is found the blink section reports UNAVAILABLE, not zero.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import clock

log = logging.getLogger("omnicontrol.camera_bench")

RESOLUTIONS = ((640, 480), (640, 360), (480, 360))
FPS_REQUESTS = (30, 60)
MEASURE_SECONDS = 10.0
WARMUP_SECONDS = 1.0
STILL_SECONDS = 5.0
BLINK_SECONDS = 30.0
BLINK_TARGET = 20
BLINK_ON, BLINK_OFF, BLINK_MIN_S = 0.58, 0.35, 0.230
MIN_GOOD_FPS = 28.0
CSV_FIELDS = ["exposure", "width", "height", "fps_req", "fps_actual",
              "proc_p50_ms", "proc_p95_ms", "face_lost", "jitter_px", "jitter_frac",
              "blink_hits", "blink_target", "note"]


# ------------------------------------------------------------------ pure logic (unit-tested)
class BlinkDetector:
    """Hysteresis blink classifier: a blink starts at score >= on_th, ends at <= off_th,
    and counts only if it lasted at least min_s seconds."""

    def __init__(self, on_th: float = BLINK_ON, off_th: float = BLINK_OFF,
                 min_s: float = BLINK_MIN_S):
        self.on_th, self.off_th, self.min_s = on_th, off_th, min_s
        self._open_t: float | None = None
        self.hits = 0
        self.blinks: list[float] = []

    def update(self, t: float, score: float) -> None:
        if self._open_t is None:
            if score >= self.on_th:
                self._open_t = t
        elif score <= self.off_th:
            if t - self._open_t >= self.min_s:
                self.hits += 1
                self.blinks.append(t - self._open_t)
            self._open_t = None

    def finish(self) -> None:
        """A blink still open at end of stream cannot be confirmed: discard it."""
        self._open_t = None


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100.0 * (len(s) - 1))))]


def fps_from_timestamps(ts: list[float]) -> float:
    if len(ts) < 2 or ts[-1] <= ts[0]:
        return 0.0
    return (len(ts) - 1) / (ts[-1] - ts[0])


def jitter_stats(points: list[tuple[float, float]], iod: list[float]) -> tuple[float, float]:
    """(std-dev of nose position in px, same as a fraction of mean inter-ocular distance).
    'Jitter' = std of each frame's distance from the run's centroid position."""
    if len(points) < 3:
        return 0.0, 0.0
    mx = sum(p[0] for p in points) / len(points)
    my = sum(p[1] for p in points) / len(points)
    radii = [(x - mx) ** 2 + (y - my) ** 2 for x, y in points]
    var = sum(radii) / len(radii)
    std_px = var ** 0.5
    mean_iod = sum(iod) / len(iod) if iod else 0.0
    return std_px, (std_px / mean_iod if mean_iod > 1e-6 else 0.0)


def measure_run(cap, seconds: float, proc=None,
                 now_fn=clock.now, warmup_s: float = 0.0) -> dict:
    """Read frames for `seconds` (after optional warmup). Works on any capture-like object
    with read()/isOpened(); `proc(frame)` is timed per frame and its dict results collected.
    Returns timestamps, per-frame proc ms and proc outputs - pure data for the caller."""
    t_end = now_fn() + warmup_s + seconds
    t_measure = now_fn() + warmup_s
    ts: list[float] = []
    proc_ms: list[float] = []
    out: list[dict] = []
    while now_fn() < t_end:
        ret, frame = cap.read()
        if not ret:
            continue
        t = now_fn()
        if t < t_measure:
            continue                                # warmup frames: read but don't record
        ts.append(t)
        if proc is not None:
            t0 = now_fn()
            try:
                res = proc(frame)
            except Exception:
                res = {"error": True}
            proc_ms.append((now_fn() - t0) * 1000.0)
            out.append(res if isinstance(res, dict) else {})
        else:
            out.append({})
    return {"ts": ts, "proc_ms": proc_ms, "out": out}


def summarize_measure(run: dict) -> dict:
    ts, pm = run["ts"], run["proc_ms"]
    faces = sum(1 for o in run["out"] if o.get("face"))
    return {"frames": len(ts), "fps": fps_from_timestamps(ts),
            "proc_p50": percentile(pm, 50), "proc_p95": percentile(pm, 95),
            "face_lost": len(ts) - faces if run["out"] else 0}


def evaluate_jitter(run: dict) -> tuple[float, float]:
    pts = [(o["nose"][0], o["nose"][1]) for o in run["out"]
           if o.get("face") and o.get("nose")]
    iod = [o["iod"] for o in run["out"] if o.get("face") and o.get("iod")]
    return jitter_stats(pts, iod)


def evaluate_blink(run: dict) -> tuple[int, BlinkDetector]:
    det = BlinkDetector()
    for o, t in zip(run["out"], run["ts"]):
        s = o.get("blink")
        if s is not None:
            det.update(t, s)
    det.finish()
    return det.hits, det


def recommend(rows: list[dict], blink_target: int = BLINK_TARGET) -> str:
    """rows: per-setting dicts for ONE exposure mode. Picks the smallest resolution whose
    30 fps MJPG run meets: real fps >= 28, jitter <= 640x480 jitter x 1.5, blink hits
    >= 90% of target, face_lost == 0."""
    by_res = {(r["width"], r["height"]): r for r in rows}
    ref = by_res.get((640, 480))
    if ref is None:
        return "no 640x480 baseline row; cannot compare jitter"
    need_jitter = (ref.get("jitter_frac") or 0.0) * 1.5
    need_hits = 0.9 * blink_target
    ok = []
    for r in rows:                                   # each row IS the 30 fps MJPG run
        if (r["fps_actual"] >= MIN_GOOD_FPS and r.get("jitter_frac", 9e9) <= need_jitter
                and r.get("blink_hits", -1) >= need_hits and r.get("face_lost", 1) == 0):
            ok.append(r)
    if not ok:
        return "NO setting passed all gates; use 640x480 and improve lighting/USB"
    best = min(ok, key=lambda r: r["width"] * r["height"])
    return (f"recommended: {best['width']}x{best['height']} MJPG@30 "
            f"(fps={best['fps_actual']:.1f}, jitter={best.get('jitter_frac', 0):.3f} iod, "
            f"blink={best.get('blink_hits', 0)}/{blink_target}, face_lost=0)")


def row_for(res, fps_req, run_summary, jitter=(0.0, 0.0), blink_hits=None,
            blink_target=BLINK_TARGET, note="") -> dict:
    w, h = res
    return {"exposure": "", "width": w, "height": h, "fps_req": fps_req,
            "fps_actual": round(run_summary["fps"], 1),
            "proc_p50_ms": round(run_summary["proc_p50"], 2),
            "proc_p95_ms": round(run_summary["proc_p95"], 2),
            "face_lost": run_summary["face_lost"],
            "jitter_px": round(jitter[0], 2), "jitter_frac": round(jitter[1], 4),
            "blink_hits": blink_hits if blink_hits is not None else "",
            "blink_target": blink_target if blink_hits is not None else "",
            "note": note}


def write_csv(path: str, rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in CSV_FIELDS})


# ------------------------------------------------------------------ hardware helpers
def _load_cv2_mp():
    try:
        import cv2  # noqa: F401
    except ImportError:
        print("camera_bench: opencv-python is not installed (pip install opencv-python)",
              file=sys.stderr)
        sys.exit(2)
    try:
        import mediapipe  # noqa: F401
    except ImportError:
        print("camera_bench: mediapipe is not installed (pip install mediapipe)",
              file=sys.stderr)
        sys.exit(2)
    import cv2 as cv
    import mediapipe as mp
    return cv, mp


def open_capture(cv, camera_id: int, w: int, h: int, fps_req: int,
                 exposure: str | None):
    """Open + configure the camera. Returns (cap, actual_info) or (None, reason)."""
    cap = cv.VideoCapture(camera_id, cv.CAP_DSHOW if os.name == "nt" else cv.CAP_ANY)
    if not cap.isOpened():
        cap.release()
        return None, "could not open"
    fourcc = cv.VideoWriter_fourcc(*"MJPG") if hasattr(cv, "VideoWriter_fourcc") else None
    if fourcc is not None:
        cap.set(cv.CAP_PROP_FOURCC, fourcc)
    cap.set(cv.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv.CAP_PROP_FRAME_HEIGHT, h)
    cap.set(cv.CAP_PROP_FPS, fps_req)
    cap.set(cv.CAP_PROP_BUFFERSIZE, 1)
    if exposure is not None and hasattr(cv, "CAP_PROP_AUTO_EXPOSURE"):
        want = {"on": -1.0, "off": 0.0}[exposure]
        try:
            cap.set(cv.CAP_PROP_AUTO_EXPOSURE, want)
        except Exception:
            pass
    aw = int(cap.get(cv.CAP_PROP_FRAME_WIDTH))
    ah = int(cap.get(cv.CAP_PROP_FRAME_HEIGHT))
    afps = cap.get(cv.CAP_PROP_FPS)
    aexp = None
    if exposure is not None and hasattr(cv, "CAP_PROP_AUTO_EXPOSURE"):
        try:
            aexp = cap.get(cv.CAP_PROP_AUTO_EXPOSURE)
        except Exception:
            aexp = None
    return cap, {"size": (aw, ah), "fps_prop": afps, "exposure_prop": aexp}


def make_face_proc(cv, mp):
    """CLAHE on the Y channel + FaceMesh landmarks -> nose px, inter-ocular px, face flag.
    Mirrors handsfree.adapters.webcam's landmark use (nose=4, eyes=33/263) plus CLAHE,
    which the app does NOT currently apply - timings here are a conservative upper bound."""
    mesh = mp.solutions.face_mesh.FaceMesh(
        max_num_faces=1, refine_landmarks=True,
        min_detection_confidence=0.5, min_tracking_confidence=0.5)
    clahe = cv.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

    def proc(frame):
        ycrcb = cv.cvtColor(frame, cv.COLOR_BGR2YCrCb)
        ycrcb[:, :, 0] = clahe.apply(ycrcb[:, :, 0])
        proc_frame = cv.cvtColor(ycrcb, cv.COLOR_YCrCb2BGR)
        rgb = cv.cvtColor(proc_frame, cv.COLOR_BGR2RGB)
        res = mesh.process(rgb)
        if not res.multi_face_landmarks:
            return {"face": False}
        lm = res.multi_face_landmarks[0].landmark
        h, w = frame.shape[:2]
        nose = lm[4]
        iod = abs(lm[263].x - lm[33].x) * w
        return {"face": True, "nose": (nose.x * w, nose.y * h), "iod": iod}

    proc.close = lambda: mesh.close()
    return proc


def find_face_model() -> str | None:
    """Locate face_landmarker.task: env override, assets/, repo root, or anywhere below."""
    env = os.environ.get("OMNICONTROL_FACE_MODEL")
    if env and Path(env).is_file():
        return env
    root = Path(__file__).resolve().parent.parent
    candidates = [root / "assets" / "face_landmarker.task",
                  root / "face_landmarker.task"]
    candidates += sorted(root.rglob("face_landmarker.task"))
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def make_blink_proc(mp, model_path: str):
    """FaceLandmarker with blendshapes -> max(eyeBlinkLeft, eyeBlinkRight) per frame."""
    vis = mp.tasks.vision
    opts = vis.FaceLandmarkerOptions(
        base_options=vis.BaseOptions(model_asset_path=model_path),
        running_mode=vis.RunningMode.IMAGE,
        num_faces=1,
        output_face_blendshapes=True)
    landmarker = vis.FaceLandmarker.create_from_options(opts)

    def proc(frame):
        rgb = cv2_free_rgb(frame)
        res = landmarker.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        if not res.face_blendshapes:
            return {"blink": 0.0}
        scores = {}
        for cat in res.face_blendshapes[0]:
            name = getattr(cat, "category_name", None) or getattr(cat, "display_name", "")
            scores[name] = getattr(cat, "score", 0.0)
        blink = max(scores.get("eyeBlinkLeft", 0.0), scores.get("eyeBlinkRight", 0.0))
        return {"blink": blink}

    proc.close = lambda: landmarker.close()
    return proc


def cv2_free_rgb(frame):
    """BGR -> RGB without importing cv2 at module level (kept inside hardware path)."""
    import cv2
    return cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)


# ------------------------------------------------------------------ bench driver
def _countdown(seconds: float, label: str) -> None:
    print(f"  >> {label}: keep still / follow prompts for {seconds:.0f} s", flush=True)
    end = clock.now() + seconds
    while clock.now() < end:
        left = end - clock.now()
        print(f"\r  {left:4.1f}s remaining   ", end="", flush=True)
        time.sleep(0.25)
    print()


def bench_exposure(cv, mp, exposure: str | None, camera_id: int,
                   blink_model: str | None, csv_rows: list,
                   measure_s: float, still_s: float, blink_s: float) -> list[dict]:
    """Run all resolutions for one exposure mode; returns per-setting summary rows."""
    face_proc = make_face_proc(cv, mp)
    blink_proc = make_blink_proc(mp, blink_model) if blink_model else None
    summaries: list[dict] = []
    try:
        for w, h in RESOLUTIONS:
            res_rows = []
            run30 = None
            for fps_req in FPS_REQUESTS:
                cap, info = open_capture(cv, camera_id, w, h, fps_req, exposure)
                if cap is None:
                    print(f"  {w}x{h}@{fps_req}: {info} - skipped")
                    res_rows.append(row_for((w, h), fps_req,
                                            {"fps": 0, "proc_p50": 0, "proc_p95": 0,
                                             "face_lost": 0}, note=str(info)))
                    continue
                measure = measure_run(cap, measure_s, proc=face_proc,
                                      warmup_s=WARMUP_SECONDS)
                summ = summarize_measure(measure)
                actual = f"{info['size'][0]}x{info['size'][1]}"
                exp_txt = ""
                if info.get("exposure_prop") is not None:
                    exp_txt = f" exp_prop={info['exposure_prop']}"
                print(f"  {w}x{h} req {fps_req}fps -> cam {actual} "
                      f"{info.get('fps_prop', 0):.0f}fps prop{exp_txt}: "
                      f"real {summ['fps']:.1f} fps, proc p50={summ['proc_p50']:.1f}ms "
                      f"p95={summ['proc_p95']:.1f}ms, face_lost={summ['face_lost']}")
                row = row_for((w, h), fps_req, summ)
                row["exposure"] = exposure or "default"
                row["note"] = f"cam={actual}"
                res_rows.append(row)
                if fps_req == 30:
                    run30 = (cap, measure, info)
                else:
                    cap.release()
            if run30 is None:
                csv_rows.extend(res_rows)
                continue
            cap, measure, info = run30
            # still jitter on the same 30 fps configuration
            _countdown(still_s, f"{w}x{h} stillness")
            still = measure_run(cap, still_s, proc=face_proc)
            jitter = evaluate_jitter(still)
            print(f"    jitter: {jitter[0]:.2f}px ({jitter[1]:.3f} of inter-ocular)")
            # blink test needs FaceLandmarker
            if blink_proc is not None:
                print(f"  >> blink test: blink {BLINK_TARGET} times during {blink_s:.0f} s",
                      flush=True)
                blink_run = measure_run(cap, blink_s, proc=blink_proc)
                hits, det = evaluate_blink(blink_run)
                print(f"    blinks detected: {hits}/{BLINK_TARGET}")
            else:
                hits = ""
                print("    blink test: UNAVAILABLE (no face_landmarker.task found)")
            cap.release()
            # merge jitter+blink onto the 30 fps row
            r30 = next((r for r in res_rows if r["fps_req"] == 30), None)
            if r30 is not None:
                r30["jitter_px"], r30["jitter_frac"] = round(jitter[0], 2), round(jitter[1], 4)
                r30["blink_hits"] = hits
            csv_rows.extend(res_rows)
            if r30 is not None and r30["fps_actual"] > 0:
                summaries.append({"width": w, "height": h,
                                  "fps_actual": r30["fps_actual"],
                                  "jitter_frac": r30["jitter_frac"],
                                  "blink_hits": hits if hits != "" else -1,
                                  "face_lost": r30["face_lost"]})
        return summaries
    finally:
        try:
            face_proc.close()
        except Exception:
            pass
        if blink_proc is not None:
            try:
                blink_proc.close()
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="OmniControl camera benchmark")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--auto-exposure", choices=("on", "off", "both"), default="both",
                    help="CAP_PROP_AUTO_EXPOSURE mode; 'both' runs the bench twice and "
                         "reports whether fps changed")
    ap.add_argument("--csv", default="bench.csv")
    ap.add_argument("--still-seconds", type=float, default=STILL_SECONDS)
    ap.add_argument("--blink-seconds", type=float, default=BLINK_SECONDS)
    ap.add_argument("--measure-seconds", type=float, default=MEASURE_SECONDS)
    a = ap.parse_args()

    cv, mp = _load_cv2_mp()
    modes = ("on", "off") if a.auto_exposure == "both" else (a.auto_exposure,)
    blink_model = find_face_model()
    if blink_model is None:
        print("note: no face_landmarker.task found - blink test will be UNAVAILABLE "
              "(set OMNICONTROL_FACE_MODEL or put the .task in assets/)")
    else:
        print(f"blink model: {blink_model}")

    csv_rows: list[dict] = []
    all_summaries: dict[str, list[dict]] = {}
    for mode in modes:
        print(f"\n=== auto-exposure {mode} ===")
        summ = bench_exposure(cv, mp, mode, a.camera, blink_model, csv_rows,
                              a.measure_seconds, a.still_seconds, a.blink_seconds)
        all_summaries[mode] = summ
        print(f"  recommendation ({mode}): {recommend(summ)}")

    if "on" in all_summaries and "off" in all_summaries:
        print("\n=== auto-exposure comparison (real fps @640x480 MJPG30) ===")
        for mode in ("on", "off"):
            row = next((r for r in all_summaries[mode]
                        if (r["width"], r["height"]) == (640, 480)), None)
            if row:
                print(f"  {mode:>3}: {row['fps_actual']:.1f} fps")
        on_fps = next((r["fps_actual"] for r in all_summaries["on"]
                       if (r["width"], r["height"]) == (640, 480)), 0.0)
        off_fps = next((r["fps_actual"] for r in all_summaries["off"]
                        if (r["width"], r["height"]) == (640, 480)), 0.0)
        delta = off_fps - on_fps
        print(f"  manual exposure {'gains' if delta > 1 else 'loses' if delta < -1 else 'matches'} "
              f"{abs(delta):.1f} fps vs auto")

    write_csv(a.csv, csv_rows)
    print(f"\nwrote {a.csv} ({len(csv_rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
