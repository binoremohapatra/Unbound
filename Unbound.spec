# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, collect_all

block_cipher = None

mp_datas, mp_binaries, mp_hidden = collect_all('mediapipe')
bleak_datas, bleak_binaries, bleak_hidden = collect_all('bleak')

datas = [
    ('host/profiles/*.json', 'host/profiles'),
    ('protocol/constants.json', 'protocol'),
    ('models/*', 'models'),
    ('face_landmarker.task', '.'),
] + mp_datas + bleak_datas

hiddenimports = [
    'PySide6.QtCore',
    'PySide6.QtGui',
    'PySide6.QtWidgets',
    'host.omniband.app.theme',
    'host.omniband.prune_shield',
    'host.omniband.app.oscilloscope',
    'host.omniband.app.arena_widget',
    'host.omniband.app.icu_view',
    'host.omniband.app.calibration_view',
    'host.omniband.app.hud_overlay',
    'host.omniband.app.tray_agent',
    'host.omniband.app.main_window',
    'host.omniband.app.auto_profile',
    'host.omniband.app.startup_registry',
    'host.omniband.app.somatic_keyboard',
    'host.omniband.app.floating_keyboard',
    'host.omniband.app.cursor_reticle',
    'host.omniband.tracking.paths',
    'host.omniband.tracking.webcam_tracker',
    'host.omniband.tracking.physical_keys',
    'host.omniband.hardware.ble_client',
    'host.omniband.hardware.hardware_calibration',
    'host.omniband.copilot.types',
    'host.omniband.copilot.sim.vehicle',
    'host.omniband.copilot.sim.track',
    'host.omniband.adapters.telemetry.native_arena',
    'bleak',
    'bleak.backends.winrt',
    'bleak.backends.winrt.client',
    'bleak.backends.winrt.scanner',
    'winrt',
    'winrt.windows.devices.bluetooth',
    'winrt.windows.devices.bluetooth.genericattributeprofile',
    'winrt.windows.devices.enumeration',
    'mediapipe',
    'cv2',
    'scipy',
    'scipy.spatial',
] + mp_hidden + bleak_hidden

binaries = mp_binaries + bleak_binaries

a = Analysis(
    ['host/omniband_engine.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'torch', 'torchvision', 'torchaudio', 'librosa', 'IPython'],
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
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    manifest='manifest_admin.xml',
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

