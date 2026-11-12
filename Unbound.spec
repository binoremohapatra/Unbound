# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

datas = [
    ('host/profiles/*.json', 'host/profiles'),
    ('protocol/constants.json', 'protocol'),
]

hiddenimports = [
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'host.omniband.app.theme',
    'host.omniband.app.oscilloscope',
    'host.omniband.app.arena_widget',
    'host.omniband.app.icu_view',
    'host.omniband.app.calibration_view',
    'host.omniband.app.hud_overlay',
    'host.omniband.app.tray_agent',
    'host.omniband.app.main_window',
    'host.omniband.copilot.types',
    'host.omniband.copilot.sim.vehicle',
    'host.omniband.copilot.sim.track',
    'host.omniband.adapters.telemetry.native_arena',
]

a = Analysis(
    ['host/omniband_engine.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib'],
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
    name='Unbound',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Unbound',
)
