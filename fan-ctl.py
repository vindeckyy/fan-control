#!/usr/bin/env python3
"""Command-line client for fan-control configuration and daemon reload."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent
for _candidate in (_ROOT, pathlib.Path("/usr/local/lib/fan-control"), pathlib.Path("/usr/lib/fan-control")):
    if (_candidate / "fan_backend.py").is_file() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from fan_policy import PROFILES, load_config, save_config
from fan_runtime import daemon_pid, gui_lock_held, runtime_dir, signal_daemon


class Result:
    def __init__(self, stdout="", code=0):
        self.stdout = stdout
        self.code = code


def _refuse_if_gui(rdir):
    if gui_lock_held(rdir):
        raise SystemExit("dashboard has exclusive control")


def run_command(argv):
    parser = argparse.ArgumentParser(prog="fan-ctl", description=__doc__)
    parser.add_argument("--config", default=os.environ.get("FAN_CONTROL_CONFIG", "/etc/fan-control.json"))
    parser.add_argument("--runtime-dir")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "cmd",
        choices=("status", "profile", "mode", "set", "curve", "diagnose"),
    )
    parser.add_argument("rest", nargs="*")
    args = parser.parse_intermixed_args(argv)
    rdir = runtime_dir(demo=False, override=args.runtime_dir)
    config = load_config(args.config)

    def emit(data, text):
        payload = json.dumps(data, indent=2) + "\n" if args.json else text + "\n"
        return Result(payload)

    if args.cmd == "status":
        data = {
            "profile": config["profile"],
            "mode": config["mode"],
            "max_duty": config["max_duty"],
            "linked": config["linked"],
            "gui_exclusive": gui_lock_held(rdir),
            "daemon_pid": daemon_pid(rdir),
        }
        text = (
            f"profile {data['profile']}  mode {data['mode']}  "
            f"cap {data['max_duty']}%  linked {data['linked']}\n"
            f"gui_exclusive {data['gui_exclusive']}  daemon_pid {data['daemon_pid']}"
        )
        return emit(data, text)

    if args.cmd == "diagnose":
        import importlib.util
        import pathlib
        path = pathlib.Path(__file__).resolve().parent / "fan-daemon.py"
        spec = importlib.util.spec_from_file_location("fan_daemon_cli", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.diagnose(args.config)
        return Result()

    _refuse_if_gui(rdir)

    if args.cmd == "profile":
        if not args.rest or args.rest[0] not in PROFILES:
            raise SystemExit("profile must be silent, balanced, performance, or custom")
        config["profile"] = args.rest[0]
        config["mode"] = "curve"
        save_config(args.config, config)
        signal_daemon(rdir)
        return emit({"ok": True, "profile": args.rest[0]}, f"profile {args.rest[0]}")

    if args.cmd == "mode":
        if not args.rest or args.rest[0] not in ("manual", "curve", "released"):
            raise SystemExit("mode must be manual, curve, or released")
        config["mode"] = args.rest[0]
        save_config(args.config, config)
        signal_daemon(rdir)
        return emit({"ok": True, "mode": args.rest[0]}, f"mode {args.rest[0]}")

    if args.cmd == "set":
        if len(args.rest) < 2:
            raise SystemExit("usage: fan-ctl set FAN PCT")
        fan, pct = int(args.rest[0]), int(args.rest[1])
        if fan not in (1, 2, 3) or not 0 <= pct <= 100:
            raise SystemExit("fan must be 1-3 and percent 0-100")
        config["mode"] = "manual"
        targets = dict(config.get("targets") or {})
        targets[str(fan)] = pct
        config["targets"] = targets
        save_config(args.config, config)
        signal_daemon(rdir)
        return emit({"ok": True, "fan": fan, "pct": pct}, f"fan {fan} -> {pct}%")

    if args.cmd == "curve":
        curve = config["curve"]
        data = {"curve": [list(p) for p in curve]}
        text = "\n".join(f"{t}°C -> {d}%" for t, d in curve)
        return emit(data, text)

    raise SystemExit("unknown command")


def main():
    try:
        result = run_command(sys.argv[1:])
    except SystemExit as exc:
        if exc.code in (0, None):
            return
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            sys.exit(1)
        sys.exit(exc.code)
    if result.stdout:
        sys.stdout.write(result.stdout)


if __name__ == "__main__":
    main()
