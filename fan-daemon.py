#!/usr/bin/env python3
"""Temperature-driven fan control for Clevo/Tongfang barebones."""

from __future__ import annotations

import argparse
import os
import pathlib
import signal
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parent
for _candidate in (_ROOT, pathlib.Path("/usr/local/lib/fan-control"), pathlib.Path("/usr/lib/fan-control")):
    if (_candidate / "fan_backend.py").is_file():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from fan_backend import DemoBackend, detect_backend
from fan_controller import FanController
from fan_diagnostics import diagnose
from fan_rpc import RpcServer
from fan_runtime import ExclusiveLock, runtime_dir

CONFIG_DEFAULT = os.environ.get("FAN_CONTROL_CONFIG", "/etc/fan-control.json")


def _tick_loop(controller, stopped, interval, label="fan daemon"):
    """Run one control iteration; never let a single tick kill the daemon."""
    failures = 0
    while not stopped():
        try:
            controller.tick_sensors()
        except Exception as exc:  # noqa: BLE001 - telemetry must not stop control
            failures += 1
            print(f"{label}: sensor tick failed ({exc})", flush=True)
        try:
            controller.tick_readback()
        except Exception as exc:  # noqa: BLE001 - keep last readback, keep controlling
            failures += 1
            print(f"{label}: readback tick failed ({exc})", flush=True)
        decision = None
        try:
            decision = controller.tick_control()
            failures = 0
        except Exception as exc:  # noqa: BLE001 - control faults stay visible
            failures += 1
            print(f"{label}: control tick failed ({exc})", flush=True)
        try:
            controller.tick_history()
        except Exception as exc:  # noqa: BLE001 - history must never halt control
            print(f"{label}: history tick failed ({exc})", flush=True)
        if failures >= 20:
            print(f"{label}: too many consecutive tick failures; exiting", flush=True)
            break
        yield decision
        try:
            time.sleep(interval)
        except (OSError, OverflowError):
            break


def run(args):
    rdir = runtime_dir(demo=args.dry_run, override=args.runtime_dir)
    data_dir = getattr(args, "data_dir", None) or (
        os.environ.get("FAN_CONTROL_DATA_DIR")
        or (str(rdir) if args.dry_run else "/var/lib/fan-control")
    )

    if args.dry_run:
        controller = FanController(
            backend=DemoBackend(),
            config_path=args.config,
            runtime_dir=rdir,
            demo=True,
            fan_limit=args.fans,
            data_dir=data_dir,
        )
        stopped = False

        def stop(*_):
            nonlocal stopped
            stopped = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)

        print(f"fan daemon: backend=demo, interval={args.interval}s (dry-run)", flush=True)
        try:
            for decision in _tick_loop(controller, lambda: stopped, args.interval):
                ctrl = controller.control_temp()
                print(
                    f"{ctrl if ctrl is not None else '--'}°C -> {(decision.duties or decision.writes) if decision else {}}",
                    flush=True,
                )
        finally:
            controller.close()
        return

    lock = ExclusiveLock(rdir / "ec.lock")
    if not lock.acquire():
        sys.exit("could not acquire exclusive EC lock")

    server = None
    controller = None
    try:
        backend = detect_backend(args.backend, args.device)
        controller = FanController(
            backend=backend,
            config_path=args.config,
            runtime_dir=rdir,
            fan_limit=args.fans,
            data_dir=data_dir,
        )
        server = RpcServer(controller, rdir / "control.sock")
        server.start()

        stopped = False

        def stop(*_):
            nonlocal stopped
            stopped = True

        def reload(*_):
            try:
                controller.reload_config()
                print("fan daemon: reloaded configuration", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"reload failed: {exc}", file=sys.stderr, flush=True)

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGHUP, reload)

        print(
            f"fan daemon: backend={backend.name}, socket={server.socket_path}, interval={args.interval}s",
            flush=True,
        )
        present = controller.fans()
        if args.fans is not None and any(f > args.fans for f in present):
            print(
                f"fan daemon: backend offers fans {present} but --fans={args.fans} hides "
                f"{sorted(f for f in present if f > args.fans)}",
                flush=True,
            )
        for _decision in _tick_loop(controller, lambda: stopped, args.interval):
            pass
    finally:
        if server is not None:
            try:
                server.close()
            except Exception:
                pass
        if controller is not None:
            try:
                controller.close()
            except Exception:
                pass
        lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--config", default=CONFIG_DEFAULT)
    parser.add_argument("--device", help="tuxedo_io device path (or set FAN_CONTROL_DEVICE)")
    parser.add_argument("--backend", choices=("auto", "tuxedo_io", "clevo_acpi"), default="auto")
    parser.add_argument("--fans", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--dry-run", action="store_true", help="print decisions without opening the EC device")
    parser.add_argument("--diagnose", action="store_true", help="print system state and exit (no EC access)")
    parser.add_argument("--runtime-dir", help="override /run/fan-control")
    parser.add_argument("--data-dir", help="override /var/lib/fan-control (history database location)")
    args = parser.parse_args()

    if args.diagnose:
        sys.stdout.write(diagnose(args.config))
        return
    if args.interval <= 0:
        parser.error("interval must be positive")
    try:
        run(args)
    except PermissionError:
        sys.exit("need root")
    except FileNotFoundError as exc:
        sys.exit(str(exc))
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
