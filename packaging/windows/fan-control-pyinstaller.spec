# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec file for Fan Control Windows Executables

import os
import pathlib
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

try:
    ROOT_DIR = str(pathlib.Path(SPECPATH).resolve().parent.parent)
except NameError:
    ROOT_DIR = str(pathlib.Path.cwd())

# Common hidden imports across our modules
hidden_imports = [
    'fan_backend',
    'fan_policy',
    'fan_runtime',
    'fan_controller',
    'fan_rpc',
    'fan_diagnostics',
    'fan_rules',
    'fan_engine',
    'fan_history',
    'fan_windows_gui',
    'sqlite3',
    'ctypes',
    'ctypes.wintypes',
    'msvcrt',
]

# Optional Windows GUI / Tray / WMI dependencies
for mod in ['webview', 'pystray', 'PIL', 'clr', 'pythonnet']:
    try:
        __import__(mod)
        for sub in collect_submodules(mod):
            if 'android' not in sub and 'ios' not in sub:
                hidden_imports.append(sub)
    except ImportError:
        pass

# Static UI data files
datas = []
ui_dist = os.path.join(ROOT_DIR, 'ui', 'dist')
ico_path = os.path.join(ROOT_DIR, 'packaging', 'icons', 'fan-control.ico')

if os.path.isdir(ui_dist):
    datas.append((ui_dist, 'ui/dist'))
if os.path.isfile(ico_path):
    datas.append((ico_path, 'packaging/icons'))

icon_file = ico_path if os.path.isfile(ico_path) else None

# Analysis for GUI application (fan-gui.py)
a_gui = Analysis(
    [os.path.join(ROOT_DIR, 'fan-gui.py')],
    pathex=[ROOT_DIR],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'numpy'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz_gui = PYZ(a_gui.pure, a_gui.zipped_data, cipher=block_cipher)
exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    [],
    exclude_binaries=True,
    name='fan-control',
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
    icon=icon_file,
)

# Analysis for CLI client (fan-ctl.py)
a_ctl = Analysis(
    [os.path.join(ROOT_DIR, 'fan-ctl.py')],
    pathex=[ROOT_DIR],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'numpy', 'webview', 'pystray', 'PIL'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz_ctl = PYZ(a_ctl.pure, a_ctl.zipped_data, cipher=block_cipher)
exe_ctl = EXE(
    pyz_ctl,
    a_ctl.scripts,
    [],
    exclude_binaries=True,
    name='fan-ctl',
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
    icon=icon_file,
)

# Analysis for Daemon (fan-daemon.py)
a_daemon = Analysis(
    [os.path.join(ROOT_DIR, 'fan-daemon.py')],
    pathex=[ROOT_DIR],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'scipy', 'numpy', 'webview', 'pystray', 'PIL'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz_daemon = PYZ(a_daemon.pure, a_daemon.zipped_data, cipher=block_cipher)
exe_daemon = EXE(
    pyz_daemon,
    a_daemon.scripts,
    [],
    exclude_binaries=True,
    name='fan-daemon',
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
    icon=icon_file,
)

# Collect all into a clean distribution folder
coll = COLLECT(
    exe_gui,
    a_gui.binaries,
    a_gui.zipfiles,
    a_gui.datas,
    exe_ctl,
    a_ctl.binaries,
    a_ctl.zipfiles,
    a_ctl.datas,
    exe_daemon,
    a_daemon.binaries,
    a_daemon.zipfiles,
    a_daemon.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='fan-control-windows',
)
