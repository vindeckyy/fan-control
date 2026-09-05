#!/usr/bin/env python3
"""Native WebKitGTK dashboard for Clevo/Tongfang fan control."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import sys

_ROOT = pathlib.Path(__file__).resolve().parent
for _candidate in (_ROOT, pathlib.Path("/usr/local/lib/fan-control"), pathlib.Path("/usr/lib/fan-control")):
    if (_candidate / "fan_backend.py").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from fan_backend import DemoBackend, detect_backend
from fan_controller import FanController
from fan_runtime import runtime_dir


def _headless_smoke(args):
    config_path = pathlib.Path(args.config)
    backend = DemoBackend() if args.demo else detect_backend(args.backend, args.device)
    rdir = runtime_dir(demo=args.demo, override=args.runtime_dir)
    controller = FanController(backend=backend, config_path=config_path, runtime_dir=rdir, demo=args.demo)
    try:
        controller.tick_sensors()
        controller.tick_readback()
        controller.tick_history()
        sys.stdout.write(json.dumps(controller.snapshot(), default=str) + "\n")
    finally:
        controller.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="run with simulated hardware")
    parser.add_argument("--debug", action="store_true", help="enable WebKit inspector")
    parser.add_argument("--headless-smoke", action="store_true", help="print one snapshot and exit")
    parser.add_argument("--tray", action="store_true", help="display-only tray; never locks the EC")
    parser.add_argument("--config", default=os.environ.get("FAN_CONTROL_CONFIG", "/etc/fan-control.json"))
    parser.add_argument("--device", help="tuxedo_io device path (or set FAN_CONTROL_DEVICE)")
    parser.add_argument("--backend", choices=("auto", "tuxedo_io", "clevo_acpi"), default="auto")
    parser.add_argument("--runtime-dir")
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--no-browser", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.demo and args.config == "/etc/fan-control.json" and "FAN_CONTROL_CONFIG" not in os.environ:
        args.config = "/tmp/fan-control-demo.json"
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    if args.headless_smoke:
        try:
            _headless_smoke(args)
        except PermissionError:
            sys.exit("need root")
        except FileNotFoundError as exc:
            sys.exit(str(exc))
        return
    from fan_gtk import run_application
    raise SystemExit(run_application(args))


if __name__ == "__main__":
    main()
