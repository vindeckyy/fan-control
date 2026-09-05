#!/usr/bin/env python3
"""Command-line client for fan-control configuration and live state."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent
for _candidate in (_ROOT, pathlib.Path("/usr/local/lib/fan-control"), pathlib.Path("/usr/lib/fan-control")):
    if (_candidate / "fan_backend.py").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from fan_diagnostics import diagnose
from fan_policy import PROFILES
from fan_rpc import RpcClient, control_socket_path
from fan_runtime import runtime_dir


class Result:
    def __init__(self, stdout="", code=0):
        self.stdout = stdout
        self.code = code


def run_command(argv):
    parser = argparse.ArgumentParser(prog="fan-ctl", description=__doc__)
    parser.add_argument("--config", default=os.environ.get("FAN_CONTROL_CONFIG", "/etc/fan-control.json"))
    parser.add_argument("--runtime-dir")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "cmd",
        choices=("status", "profile", "mode", "set", "cap", "config", "curve", "curves", "diagnose"),
    )
    parser.add_argument("rest", nargs="*")
    args = parser.parse_intermixed_args(argv)

    def emit(data, text):
        payload = json.dumps(data, indent=2) + "\n" if args.json else text + "\n"
        return Result(payload)

    if args.cmd == "diagnose":
        text = diagnose(args.config)
        return emit({"text": text}, text.rstrip())

    rdir = runtime_dir(demo=False, override=args.runtime_dir)
    client = RpcClient(control_socket_path(rdir))
    client.connect()

    if args.cmd == "status":
        cfg = client.call("config.get")
        live = client.call("live")
        data = {
            "profile": cfg["profile"],
            "mode": cfg["mode"],
            "max_duty": cfg["max_duty"],
            "hysteresis": cfg["hysteresis"],
            "critical_temp": cfg["critical_temp"],
            "linked": cfg["linked"],
            "control_temp": live.get("control_temp"),
            "fan1": live.get("fan1"),
            "fan2": live.get("fan2"),
            "fan3": live.get("fan3"),
            "daemon_reachable": True,
        }
        ctrl = f"{data['control_temp']}°C" if data["control_temp"] is not None else "--°C"
        text = (
            f"profile {data['profile']}  mode {data['mode']}  "
            f"cap {data['max_duty']}%  hysteresis {data['hysteresis']}  critical {data['critical_temp']}°C  linked {data['linked']}\n"
            f"control_temp {ctrl}  fan1 {data['fan1']}%  fan2 {data['fan2']}%  daemon_reachable {data['daemon_reachable']}"
        )
        return emit(data, text)

    if args.cmd == "curve":
        cfg = client.call("config.get")
        which = args.rest[0].lower() if args.rest else "shared"
        if which == "cpu":
            curve = cfg.get("curve_cpu") or cfg["curve"]
        elif which == "gpu":
            curve = cfg.get("curve_gpu") or cfg["curve"]
        else:
            curve = cfg["curve"]
        data = {"which": which, "curve": [list(p) for p in curve]}
        text = f"{which.upper()} curve:\n" + "\n".join(f"  {t}°C -> {d}%" for t, d in curve)
        return emit(data, text)

    if args.cmd == "curves":
        cfg = client.call("config.get")
        named = cfg.get("named_curves") or {}
        if args.rest:
            sub = args.rest[0]
            if sub == "load" and len(args.rest) > 1:
                target = args.rest[1]
                if target not in named:
                    raise SystemExit(f"unknown named curve {target!r}")
                client.call("curves.load", {"name": target})
                return emit({"ok": True, "loaded": target}, f"loaded curve {target}")
            if sub in named:
                c = named[sub]
                return emit({"name": sub, "curve": [list(p) for p in c]}, f"{sub}:\n" + "\n".join(f"  {t}°C -> {d}%" for t, d in c))
            raise SystemExit(f"unknown named curve {sub!r}")
        data = {
            "profiles": list(PROFILES.keys()),
            "named_curves": sorted(named.keys()),
        }
        text = f"Built-in profiles: {', '.join(data['profiles'])}\nSaved named curves: {', '.join(data['named_curves']) or 'none'}"
        return emit(data, text)

    if args.cmd == "profile":
        if not args.rest or args.rest[0] not in PROFILES:
            raise SystemExit(f"profile must be one of: {', '.join(PROFILES.keys())}")
        prof = args.rest[0]
        client.call("profile", {"profile": prof})
        return emit({"ok": True, "profile": prof}, f"profile {prof}")

    if args.cmd == "mode":
        if not args.rest or args.rest[0] not in ("manual", "curve", "released"):
            raise SystemExit("mode must be manual, curve, or released")
        m = args.rest[0]
        client.call("mode", {"mode": m})
        return emit({"ok": True, "mode": m}, f"mode {m}")

    if args.cmd == "set":
        if len(args.rest) < 2:
            raise SystemExit("usage: fan-ctl set <fan> <pct>")
        try:
            fan = int(args.rest[0])
            pct = int(args.rest[1])
        except ValueError:
            raise SystemExit("fan and pct must be integers") from None
        if not 0 <= pct <= 100:
            raise SystemExit("pct must be 0-100")
        client.call("set", {"fan": fan, "pct": pct})
        return emit({"ok": True, "fan": fan, "pct": pct}, f"fan {fan} -> {pct}%")

    if args.cmd == "cap":
        if not args.rest:
            raise SystemExit("usage: fan-ctl cap <pct>")
        try:
            val = int(args.rest[0])
        except ValueError:
            raise SystemExit("cap must be an integer percentage") from None
        val = max(20, min(100, val))
        client.call("config", {"max_duty": val})
        return emit({"ok": True, "max_duty": val}, f"cap set to {val}%")

    if args.cmd == "config":
        cfg = client.call("config.get")
        if not args.rest:
            data = {
                "max_duty": cfg["max_duty"],
                "hysteresis": cfg["hysteresis"],
                "critical_temp": cfg["critical_temp"],
                "linked": cfg["linked"],
                "theme": cfg.get("theme", "dark"),
            }
            text = "\n".join(f"{k} = {v}" for k, v in data.items())
            return emit(data, text)
        updated = {}
        for item in args.rest:
            if "=" not in item:
                raise SystemExit(f"config arguments must be key=value (got {item!r})")
            key, val = item.split("=", 1)
            key = key.strip()
            if key == "max_duty":
                updated[key] = max(20, min(100, int(val)))
            elif key == "hysteresis":
                updated[key] = max(0, min(30, int(val)))
            elif key == "critical_temp":
                updated[key] = max(70, min(110, int(val)))
            elif key == "linked":
                updated[key] = val.lower() in ("true", "1", "yes")
            elif key == "theme":
                if val in ("dark", "light"):
                    updated[key] = val
            else:
                raise SystemExit(f"unknown config key: {key}")
        client.call("config", updated)
        return emit({"ok": True, "updated": updated}, f"updated config: {', '.join(f'{k}={v}' for k, v in updated.items())}")

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
    except (ConnectionError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
    if result.stdout:
        sys.stdout.write(result.stdout)


if __name__ == "__main__":
    main()
