#!/usr/bin/env python3
"""System diagnostic checks for fan-control hardware and modules."""

from __future__ import annotations

import io
import pathlib
import subprocess


def find_cpu_sensor():
    """Locate the sysfs temp1_input path for supported CPU hwmon chips."""
    for name in ("k10temp", "coretemp", "zenpower"):
        for hwmon in sorted(pathlib.Path("/sys/class/hwmon").glob("hwmon*")):
            try:
                if (hwmon / "name").read_text().strip() == name:
                    return hwmon / "temp1_input"
            except OSError:
                continue
    return None


def diagnose(config_path):
    """Return system state summary for troubleshooting; does not open devices."""
    buf = io.StringIO()

    def p(*args):
        print(*args, file=buf)

    mods = {}
    try:
        out = subprocess.run(["lsmod"], capture_output=True, text=True, timeout=5).stdout
        for mod in ("tuxedo_io", "clevo_wmi", "uniwill_wmi", "tuxedo_keyboard", "clevo_acpi"):
            mods[mod] = mod in out
    except Exception as exc:  # noqa: BLE001
        mods["_error"] = str(exc)

    devs = sorted(pathlib.Path("/dev").glob("*_io"))
    cfg = pathlib.Path(config_path)
    clevo = pathlib.Path("/sys/class/leds/clevo-acpi::kbd_backlight/device")
    clevo_fans = sorted(clevo.glob("fan*_manual_duty")) if clevo.exists() else []

    p(f"config    {config_path}  {'exists' if cfg.exists() else 'no file'}")
    for mod, loaded in mods.items():
        p(f"  module  {mod}:  {'loaded' if loaded else '—'}")
    p(f"  device  /dev/*_io:  {devs or 'none'}")
    p(f"  clevo   fan attrs: {[f.name for f in clevo_fans] or 'none'}")
    p(f"  sensor  cpu:      {find_cpu_sensor() or 'not found'}")
    try:
        nvidia_ok = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5).returncode == 0
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        nvidia_ok = False
    p(f"  nvidia  nvidia-smi: {'available' if nvidia_ok else 'no'}")
    if not devs and not clevo_fans:
        p("  >>> no fan-control hardware found. See README 'Compatibility' for setup.")

    return buf.getvalue()
