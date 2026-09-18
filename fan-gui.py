#!/usr/bin/env python3
"""Dashboard entry point for Clevo/Tongfang fan control.

Dispatches to the GTK4 + WebKitGTK workspace on Linux and to the native
Windows runner (Edge app window / pywebview / system browser) on Windows.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parent
for _candidate in (_ROOT, pathlib.Path("/usr/local/lib/fan-control"), pathlib.Path("/usr/lib/fan-control")):
    if (_candidate / "fan_backend.py").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from fan_backend import DemoBackend, detect_backend
from fan_controller import FanController
from fan_runtime import runtime_dir


def _write_console(text):
    """Write CLI output, attaching or creating a console in a windowed build."""
    stream = sys.stdout
    if stream is not None:
        try:
            stream.write(text)
            stream.flush()
            return True
        except (OSError, ValueError):
            pass
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if not (kernel32.AttachConsole(-1) or kernel32.AllocConsole()):
            return False
        console = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
        console.write(text)
        sys.stdout = console
        sys.stderr = console
        return True
    except (OSError, AttributeError, ImportError):
        return False


def _headless_smoke(args):
    config_path = pathlib.Path(args.config)
    backend = DemoBackend() if args.demo else detect_backend(args.backend, args.device)
    rdir = runtime_dir(demo=args.demo, override=args.runtime_dir)
    controller = FanController(backend=backend, config_path=config_path, runtime_dir=rdir, demo=args.demo)
    try:
        controller.tick_sensors()
        controller.tick_readback()
        controller.tick_control()
        controller.tick_history()
        result = controller.snapshot()
        result["smoke"] = {
            method: controller.handle(method)
            for method in ("capabilities", "live", "diagnostics.decisions", "history.query")
        }
        if args.demo:
            result["smoke"]["mutation"] = controller.handle("config", {
                "expected_revision": controller.config["revision"], "theme": "dark",
            })
        if not _write_console(json.dumps(result, default=str) + "\n"):
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    0,
                    "Headless smoke output requires a console.\n"
                    "Run fan-control.exe from Command Prompt or PowerShell.",
                    "Fan Control",
                    0x30,
                )
            raise SystemExit(1)
    finally:
        controller.close()


def main():
    default_config = os.environ.get(
        "FAN_CONTROL_CONFIG",
        str(pathlib.Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "fan-control" / "fan-control.json")
        if sys.platform == "win32"
        else "/etc/fan-control.json",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="run with simulated hardware")
    parser.add_argument("--debug", action="store_true", help="enable WebKit inspector")
    parser.add_argument("--headless-smoke", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--tray", action="store_true", help="display-only tray; never locks the EC")
    parser.add_argument("--config", default=default_config)
    parser.add_argument("--device", help="tuxedo_io device path (or set FAN_CONTROL_DEVICE)")
    parser.add_argument(
        "--backend",
        choices=("auto", "tuxedo_io", "clevo_acpi", "windows_ec", "windows_wmi"),
        default="auto",
    )
    parser.add_argument("--runtime-dir")
    args = parser.parse_args()
    if args.demo and args.config == default_config and "FAN_CONTROL_CONFIG" not in os.environ:
        args.config = (
            str(pathlib.Path(tempfile.gettempdir()) / "fan-control-demo.json")
            if sys.platform == "win32"
            else "/tmp/fan-control-demo.json"
        )
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    if args.headless_smoke:
        try:
            _headless_smoke(args)
        except PermissionError:
            sys.exit("need administrator privileges" if sys.platform == "win32" else "need root")
        except FileNotFoundError as exc:
            sys.exit(str(exc))
        return
    if sys.platform == "win32":
        from fan_windows_gui import run_application
    else:
        from fan_gtk import run_application
    raise SystemExit(run_application(args))


if __name__ == "__main__":
    main()
