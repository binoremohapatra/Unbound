# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for OmniControl.   Build:  pyinstaller --noconfirm --clean omnicontrol.spec

Output: dist/OmniControl/ (one folder, not one file: starts faster and trips antivirus less) with
    OmniControl.exe         normal user (asInvoker). Can restart itself elevated: --elevate / tray menu.
    OmniControl-Admin.exe   same program, manifest = requireAdministrator (UAC prompt on every start).
Both share one set of libraries, so shipping two exes costs only a few hundred KB.

Why hiddenimports: main.py loads adapters.* and mapping by NAME at run time (importlib), which
PyInstaller's static analysis cannot see. Only modules that exist on disk are listed, so the build
works today and picks up teammates' files automatically when they are added.

OMNICONTROL_EXCLUDE=cv2,mediapipe,...  leaves those packages out of the bundle (smoke builds, or an
engine-only build). Windowed exe (console=False): there is no stdout, so --doctor attaches to the
parent terminal or shows a message box (see doctor.py).
"""
import importlib.util
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH)                                  # noqa: F821  (provided by PyInstaller)
APP_NAME = "OmniControl"
ICON = ROOT / "assets" / "omnicontrol.ico"

CANDIDATE_MODULES = [
    "adapters.webcam", "adapters.audio", "adapters.switch",       # loaded by name in main.py
    "mapping", "assist", "fastpath", "slowpath", "filters",       # Dishti / Antas, when they arrive
]
hiddenimports = [m for m in CANDIDATE_MODULES if (ROOT / (m.replace(".", "/") + ".py")).exists()]
if sys.platform == "win32":
    hiddenimports.append("pystray._win32")                        # backend chosen at run time
if (ROOT / "handsfree").is_dir():                                # teammates' package, loaded by name
    hiddenimports += collect_submodules("handsfree")

datas, binaries = [], []
for folder in ("profiles", "assets"):                             # read-only files shipped with the app
    if (ROOT / folder).is_dir():
        datas.append((str(ROOT / folder), folder))
if (ROOT / "handsfree" / "profiles").is_dir():
    datas.append((str(ROOT / "handsfree" / "profiles"), "handsfree/profiles"))
if (ROOT / "handsfree" / "dashboard" / "static").is_dir():
    datas.append((str(ROOT / "handsfree" / "dashboard" / "static"),
                  "handsfree/dashboard/static"))
for pkg in ("mediapipe",):                                        # needs its model files at run time
    if importlib.util.find_spec(pkg) is not None and pkg not in os.environ.get("OMNICONTROL_EXCLUDE", ""):
        datas += collect_data_files(pkg)
        binaries += collect_dynamic_libs(pkg)

excludes = [m for m in os.environ.get("OMNICONTROL_EXCLUDE", "").split(",") if m]
excludes += ["tkinter", "unittest", "pytest"]                     # never needed at run time

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],                                           # our flat modules win over site-packages
    binaries=binaries, datas=datas, hiddenimports=hiddenimports,
    hookspath=[], runtime_hooks=[], excludes=excludes, noarchive=False,
)
pyz = PYZ(a.pure)

icon = str(ICON) if sys.platform == "win32" and ICON.exists() else None
common = dict(exclude_binaries=True, console=False, debug=False, strip=False, upx=False, icon=icon)

exe_user = EXE(pyz, a.scripts, [], name=APP_NAME, uac_admin=False, **common)
exe_admin = EXE(pyz, a.scripts, [], name=f"{APP_NAME}-Admin", uac_admin=True, **common)

coll = COLLECT(exe_user, exe_admin, a.binaries, a.datas, strip=False, upx=False, name=APP_NAME)
