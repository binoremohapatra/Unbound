"""Writes assets/omnicontrol.ico from the same drawing code the tray uses, so the exe, the
installer and the tray icon always match. Run by build.ps1; safe to run by hand.

    python tools/make_icon.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tray import draw_icon

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> Path:
    out = ROOT / "assets" / "omnicontrol.ico"
    out.parent.mkdir(exist_ok=True)
    draw_icon("ACTIVE", 256).save(out, format="ICO", sizes=SIZES)
    return out


if __name__ == "__main__":
    print(main())
