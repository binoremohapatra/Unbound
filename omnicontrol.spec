# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for OmniControl.

Supports building both:
  - Admin build: requestedExecutionLevel = requireAdministrator
  - Normal build: requestedExecutionLevel = asInvoker

Set environment variable OMNICONTROL_BUILD_ADMIN=1 for admin build, or 0 for normal.
Defaults to admin build.
"""
import os
import sys

block_cipher = None
build_admin = os.environ.get("OMNICONTROL_BUILD_ADMIN", "1") == "1"
manifest_file = "manifest_admin.xml" if build_admin else "manifest_user.xml"
build_name = "OmniControl_Admin" if build_admin else "OmniControl"

a = Analysis(
    ['omnicontrol.py'],
    pathex=['.'],
    binaries=[],
    datas=[
        ('manifest_admin.xml', '.'),
        ('manifest_user.xml', '.'),
    ],
    hiddenimports=[
        'pystray',
        'PIL',
        'sounddevice',
        'cv2',
        'numpy',
        'ctypes',
        'win32api',
        'win32con',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'torch'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='OmniControl',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest=manifest_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=build_name,
)

