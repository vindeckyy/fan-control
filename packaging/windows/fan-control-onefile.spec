# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for single-file Fan Control Windows executables.
#
# Produces dist/fan-control.exe, dist/fan-ctl.exe, and dist/fan-daemon.exe as
# self-contained one-file builds (no _internal directory, no Python required).

import os
import pathlib
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

try:
    ROOT_DIR = str(pathlib.Path(SPECPATH).resolve().parent.parent)
except NameError:
    ROOT_DIR = str(pathlib.Path.cwd())

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

for mod in ['webview', 'pystray', 'PIL', 'clr', 'pythonnet']:
    try:
        __import__(mod)
        for sub in collect_submodules(mod):
            if 'android' not in sub and 'ios' not in sub:
                hidden_imports.append(sub)
    except ImportError:
        pass

ui_dist = os.path.join(ROOT_DIR, 'ui', 'dist')
ico_path = os.path.join(ROOT_DIR, 'packaging', 'icons', 'fan-control.ico')

gui_datas = []
if os.path.isdir(ui_dist):
    gui_datas.append((ui_dist, 'ui/dist'))
if os.path.isfile(ico_path):
    gui_datas.append((ico_path, 'packaging/icons'))

icon_file = ico_path if os.path.isfile(ico_path) else None

EXCLUDES = ['tkinter', 'matplotlib', 'scipy', 'numpy']


def analysis(script, datas, excludes):
    return Analysis(
        [os.path.join(ROOT_DIR, script)],
        pathex=[ROOT_DIR],
        binaries=[],
        datas=datas,
        hiddenimports=hidden_imports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=excludes,
        win_no_prefer_redirects=False,
        win_private_assemblies=False,
        cipher=block_cipher,
        noarchive=False,
    )


a_gui = analysis('fan-gui.py', gui_datas, EXCLUDES)
pyz_gui = PYZ(a_gui.pure, a_gui.zipped_data, cipher=block_cipher)
exe_gui = EXE(
    pyz_gui,
    a_gui.scripts,
    a_gui.binaries,
    a_gui.zipfiles,
    a_gui.datas,
    [],
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

a_ctl = analysis('fan-ctl.py', [], EXCLUDES + ['webview', 'pystray', 'PIL'])
pyz_ctl = PYZ(a_ctl.pure, a_ctl.zipped_data, cipher=block_cipher)
exe_ctl = EXE(
    pyz_ctl,
    a_ctl.scripts,
    a_ctl.binaries,
    a_ctl.zipfiles,
    a_ctl.datas,
    [],
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

a_daemon = analysis('fan-daemon.py', [], EXCLUDES + ['webview', 'pystray', 'PIL'])
pyz_daemon = PYZ(a_daemon.pure, a_daemon.zipped_data, cipher=block_cipher)
exe_daemon = EXE(
    pyz_daemon,
    a_daemon.scripts,
    a_daemon.binaries,
    a_daemon.zipfiles,
    a_daemon.datas,
    [],
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
