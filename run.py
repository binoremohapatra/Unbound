"""Unbound Handsfree: All-in-One Unified Launcher.

Runs self-check diagnostics, starts all 5 AI modules (Smart Keyboard, Voice Agent,
Screen Understanding, Guardian Auto-Recovery, Adaptive Settings), and automatically
opens the browser HUD.

Usage:
  python run.py          # Start full application + auto-open browser HUD
  python run.py --test   # Run all 700+ tests in one single command
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import threading
import time
import webbrowser

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def print_banner() -> None:
    print(r"""
 ========================================================================
   _    _       _                              _   _                 _     __                 
  | |  | |     | |                            | | | |               | |   / _|                
  | |  | |_ __ | |__   ___  _   _ _ __   __| | | |__   __ _ _ __   __| | | |_ _ __ ___  ___ 
  | |  | | '_ \| '_ \ / _ \| | | | '_ \ / _` | | '_ \ / _` | '_ \ / _` | |  _| '__/ _ \/ _ \
  | |__| | | | | |_) | (_) | |_| | | | | (_| | | | | | (_| | | | | (_| | | | | | |  __/  __/
   \____/|_| |_|_.__/ \___/ \__,_|_| |_|\__,_| |_| |_|\__,_|_| |_|\__,_| |_| |_|  \___|\___|
                                                                                              
             AI-Powered Hands-Free Assistive Technology Platform
 ========================================================================
    """)


def run_all_tests() -> int:
    """Runs all handsfree unit tests and root pytest tests in one command."""
    print(">>> Running Handsfree AI Unit Tests (48 tests)...")
    res1 = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "handsfree/tests", "-v"])

    print("\n>>> Running Root Pytest Suite (654+ tests)...")
    res2 = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q"])

    print("\n" + "=" * 60)
    if res1.returncode == 0 and res2.returncode == 0:
        print("  ALL TESTS PASSED SUCCESSFULLY! Zero Errors.")
    else:
        print("  SOME TESTS REPORTED ISSUES.")
    print("=" * 60)
    return max(res1.returncode, res2.returncode)


def auto_open_browser(url: str, delay: float = 1.5) -> None:
    """Opens default browser to the HUD URL once server is ready."""
    def _open():
        time.sleep(delay)
        print(f"\n[INFO] Opening Unbound Handsfree HUD in your browser: {url}\n")
        try:
            webbrowser.open(url)
        except Exception:
            pass

    t = threading.Thread(target=_open, daemon=True)
    t.start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Unbound Handsfree AI Unified Launcher")
    parser.add_argument("--test", action="store_true", help="Run all unit and integration tests and exit")
    parser.add_argument("--camera", type=int, default=0, help="Webcam device index (default: 0)")
    parser.add_argument("--angle-range-x", type=float, default=20.0, help="Head yaw angle range in degrees (default: 20.0)")
    parser.add_argument("--angle-range-y", type=float, default=15.0, help="Head pitch angle range in degrees (default: 15.0)")
    parser.add_argument("--deadzone", type=float, default=2.5, help="Head deadzone dampening in degrees (default: 2.5)")
    parser.add_argument("--dry-run", action="store_true", help="Run in dry-run mode without real mouse clicks")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser on startup")
    parser.add_argument("--port", type=int, default=8080, help="HUD port (default: 8080)")
    args = parser.parse_args()

    print_banner()

    if args.test:
        sys.exit(run_all_tests())

    # Step 1: Self-Check Diagnostics
    print("[1/3] Checking environment & AI modules...")
    model_file = PROJECT_ROOT / "models" / "face_landmarker.task"
    if not model_file.is_file():
        model_file = PROJECT_ROOT / "face_landmarker.task"
    print(f"      - MediaPipe Model: {'OK' if model_file.is_file() else 'Missing'}")

    pdict = PROJECT_ROOT / "handsfree" / "data" / "personal_dict.json"
    print(f"      - Smart Keyboard Dictionary: {'OK' if pdict.is_file() else 'Initialized'}")

    uprofile = PROJECT_ROOT / "handsfree" / "data" / "user_profile.json"
    print(f"      - Adaptive Personalization: {'OK' if uprofile.is_file() else 'Initialized'}")

    # Step 2: Prepare HUD auto-open
    hud_url = f"http://127.0.0.1:{args.port}/"
    if not args.no_browser:
        auto_open_browser(hud_url, delay=1.6)

    # Step 3: Launch Main Handsfree Engine
    print(f"[2/3] Starting AI Voice, Screen Understanding, Guardian & HUD at {hud_url}...")
    from handsfree.main import HandsfreeEngine

    engine_args = argparse.Namespace(
        profile=str(PROJECT_ROOT / "handsfree" / "profiles" / "default.json"),
        camera=args.camera,
        replay=None,
        dry_run=args.dry_run,
        no_dashboard=False,
        dashboard_host="127.0.0.1",
        dashboard_port=args.port,
        watchdog_timeout=0.75,
        angle_range_x=args.angle_range_x,
        angle_range_y=args.angle_range_y,
        deadzone=args.deadzone,
        invert_x=True,
        enable_blink_click=True,
        enable_dwell_click=False,
    )

    print("[3/3] Controller engine active. Press Ctrl+C in terminal to stop.")
    engine = HandsfreeEngine(engine_args)

    try:
        engine.start()
    except KeyboardInterrupt:
        print("\nStopping Unbound Handsfree cleanly...")
    finally:
        engine.stop()
        print("Shutdown complete. All hardware inputs safely released.")


if __name__ == "__main__":
    main()
