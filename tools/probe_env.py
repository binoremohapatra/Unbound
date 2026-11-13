#!/usr/bin/env python3
"""tools/probe_env.py — Probe environment APIs and print structured JSON."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

def probe() -> dict:
    info = {
        "python_version": sys.version,
        "pointer_size_bits": 64 if sys.maxsize > 2**32 else 32,
    }

    # MediaPipe
    try:
        import mediapipe as mp
        mp_ver = getattr(mp, "__version__", "unknown")
        has_sol = hasattr(mp, "solutions")
        tasks_ok = False
        fl_ok = False
        flo_ok = False
        rm_ok = False
        bo_ok = False
        try:
            from mediapipe.tasks.python import vision
            fl_ok = hasattr(vision, "FaceLandmarker")
            flo_ok = hasattr(vision, "FaceLandmarkerOptions")
            rm_ok = hasattr(vision, "RunningMode")
            tasks_ok = True
        except Exception as e:
            tasks_err = str(e)

        try:
            from mediapipe.tasks import python as mp_python
            bo_ok = hasattr(mp_python, "BaseOptions")
        except Exception:
            pass

        info["mediapipe"] = {
            "installed": True,
            "version": mp_ver,
            "has_solutions": has_sol,
            "tasks_available": tasks_ok,
            "FaceLandmarker": fl_ok,
            "FaceLandmarkerOptions": flo_ok,
            "RunningMode": rm_ok,
            "BaseOptions": bo_ok,
        }
    except Exception as e:
        info["mediapipe"] = {"installed": False, "error": str(e)}

    # OpenCV
    try:
        import cv2
        has_fdyn = hasattr(cv2, "FaceDetectorYN")
        haardir = getattr(cv2.data, "haarcascades", "")
        haarexists = Path(haardir).is_dir() if haardir else False
        xmls = [p.name for p in Path(haardir).glob("*.xml")] if haarexists else []

        info["opencv"] = {
            "installed": True,
            "version": cv2.__version__,
            "has_FaceDetectorYN": has_fdyn,
            "haarcascades_dir": haardir,
            "haarcascades_exists": haarexists,
            "xml_files_count": len(xmls),
        }
    except Exception as e:
        info["opencv"] = {"installed": False, "error": str(e)}

    # Numpy
    try:
        import numpy as np
        info["numpy"] = {"version": np.__version__}
    except Exception as e:
        info["numpy"] = {"error": str(e)}

    # PySide6
    try:
        import PySide6
        info["PySide6"] = {"version": PySide6.__version__}
    except Exception as e:
        info["PySide6"] = {"error": str(e)}

    # PyInstaller
    try:
        import PyInstaller
        info["PyInstaller"] = {"version": PyInstaller.__version__}
    except Exception as e:
        info["PyInstaller"] = {"error": str(e)}

    return info

if __name__ == "__main__":
    out = probe()
    print(json.dumps(out, indent=2))

