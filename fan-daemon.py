#!/usr/bin/env python3
"""Temperature-driven fan control for Clevo/Tongfang barebones."""

import argparse
import os
import pathlib
import signal
import subprocess
import sys
import time

_ROOT = pathlib.Path(__file__).resolve().parent
for _candidate in (_ROOT, pathlib.Path("/usr/local/lib/fan-control"), pathlib.Path("/usr/lib/fan-control")):
    if (_candidate / "fan_backend.py").is_file() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from fan_backend import detect_backend
from fan_policy import (
    PROFILES,
    SAFE_MAX_DUTY,
    decide,
    interpolate,
    load_config,
    normalize_curve,
    parse_nvidia_temperatures,
    pick_cpu_temp,
    pick_gpu_temp,
    select_control_temp,
    target_duty,
)
from fan_runtime import ExclusiveLock, gui_lock_held, runtime_dir, write_daemon_pid, clear_pid

CONFIG_DEFAULT = os.environ.get("FAN_CONTROL_CONFIG", "/etc/fan-control.json")


def find_cpu_sensor():
    for name in ("k10temp", "coretemp"):
        for hwmon in sorted(pathlib.Path("/sys/class/hwmon").glob("hwmon*")):
            try:
                if (hwmon / "name").read_text().strip() == name:
                    return hwmon / "temp1_input"
            except OSError:
                continue
    return None


def diagnose(config_path):
    """Print system state for troubleshooting; does not open the device."""
    mods = {}
    try:
        out = subprocess.run(["lsmod"], capture_output=True, text=True, timeout=5).stdout
        for mod in ("tuxedo_io", "clevo_wmi", "uniwill_wmi", "tuxedo_keyboard", "clevo_acpi"):
            mods[mod] = mod in out
    except Exception as exc:
        mods["_error"] = str(exc)
    devs = sorted(pathlib.Path("/dev").glob("*_io"))
    cfg = pathlib.Path(config_path)
    clevo = pathlib.Path("/sys/class/leds/clevo-acpi::kbd_backlight/device")
    clevo_fans = sorted(clevo.glob("fan*_manual_duty")) if clevo.exists() else []
    print(f"config    {config_path}  {'exists' if cfg.exists() else 'no file'}")
    for mod, loaded in mods.items():
        print(f"  module  {mod}:  {'loaded' if loaded else '—'}")
    print(f"  device  /dev/*_io:  {devs or 'none'}")
    print(f"  clevo   fan attrs: {[f.name for f in clevo_fans] or 'none'}")
    print(f"  sensor  cpu:      {find_cpu_sensor() or 'not found'}")
    nvidia_ok = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5).returncode == 0
    print(f"  nvidia  nvidia-smi: {'available' if nvidia_ok else 'no'}")
    if not devs and not clevo_fans:
        print("  >>> no fan-control hardware found. See README 'Compatibility' for setup.")
    return


def cpu_temp(sensor):
    try:
        return int(sensor.read_text().strip()) / 1000 if sensor else None
    except (OSError, ValueError):
        return None


def gpu_temp():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    temperatures = parse_nvidia_temperatures(result.stdout) if result.returncode == 0 else []
    return max(temperatures) if temperatures else None


def _scan_hwmon():
    found = []
    for hwmon in sorted(pathlib.Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            name = (hwmon / "name").read_text().strip()
        except OSError:
            continue
        for path in sorted(hwmon.glob("temp*_input")):
            try:
                temp = int(path.read_text().strip()) / 1000
                if 0 <= temp <= 150:
                    label_path = path.with_name(path.name.replace("_input", "_label"))
                    label = label_path.read_text().strip() if label_path.exists() else path.stem.replace("_input", "")
                    found.append({"name": name, "label": label, "temp": temp})
            except (OSError, ValueError):
                continue
    return found


def run(args):
    rdir = runtime_dir(demo=False, override=args.runtime_dir)
    if gui_lock_held(rdir):
        sys.exit("GUI has exclusive control; not starting the daemon")
    lock = ExclusiveLock(rdir / "ec.lock")
    if not args.dry_run and not lock.acquire():
        sys.exit("could not acquire exclusive EC lock")
    write_daemon_pid(rdir)

    hold = {"reload": False}

    def request_reload(*_):
        hold["reload"] = True

    def load_hold():
        config = load_config(args.config)
        profile = config.get("profile", args.profile)
        if profile not in PROFILES:
            profile = args.profile
        curve = config["curve"] if profile == "custom" else list(PROFILES.get(profile, PROFILES[args.profile]))
        if args.curve:
            curve = normalize_curve(args.curve)
        hold.update({
            "profile": profile,
            "curve": curve,
            "curve_cpu": config.get("curve_cpu"),
            "curve_gpu": config.get("curve_gpu"),
            "linked": config.get("linked", True),
            "mode": config.get("mode", "curve") if config.get("mode") in ("curve", "released") else "curve",
            "max_duty": config.get("max_duty", args.max_duty),
            "hysteresis": config.get("hysteresis", args.hysteresis),
            "critical_temp": config.get("critical_temp", args.critical_temp),
            "cpu_sensor": config.get("cpu_sensor"),
            "gpu_sensor": config.get("gpu_sensor"),
        })

    load_hold()
    sensor = find_cpu_sensor()
    backend = None if args.dry_run else detect_backend(args.backend, args.device)
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, request_reload)
    if backend:
        if hold["mode"] == "released":
            backend.release()
        else:
            backend.lock()
    print(
        f"fan daemon: profile={hold['profile']}, backend={backend.name if backend else 'dry-run'}, "
        f"{len(hold['curve'])} points, interval={args.interval}s, cap={hold['max_duty']}"
    )

    missing = 0
    released_for_fault = False
    try:
        while not stopped:
            if hold["reload"]:
                hold["reload"] = False
                try:
                    load_hold()
                    print(f"fan daemon: reloaded profile={hold['profile']} mode={hold['mode']}")
                    if backend:
                        if hold["mode"] == "released":
                            backend.release()
                        else:
                            backend.lock()
                except (OSError, ValueError) as exc:
                    print(f"reload failed: {exc}", file=sys.stderr)
            sensors = _scan_hwmon()
            cpu = pick_cpu_temp(sensors, hold.get("cpu_sensor"), cpu_temp(sensor))
            if cpu is None and backend:
                try:
                    cpu = backend.read_temp()
                except OSError:
                    cpu = None
            gpu = pick_gpu_temp(sensors, hold.get("gpu_sensor"))
            if gpu is None:
                gpu = gpu_temp()
            if backend:
                fans = tuple(fan for fan in backend.fans() if fan <= args.fans or fan <= 3)
                current = {}
                for fan in fans:
                    try:
                        current[fan] = backend.read_duty(fan)
                    except OSError:
                        current[fan] = 0
            else:
                fans = (1, 2)[: args.fans]
                current = {fan: 0 for fan in fans}

            decision = decide(
                mode=hold["mode"] if hold["mode"] in ("curve", "released") else "curve",
                profile=hold["profile"],
                cpu_temp=cpu,
                gpu_temp=gpu,
                targets={1: 0, 2: 0, 3: 0},
                max_duty=hold["max_duty"],
                hysteresis=hold["hysteresis"],
                critical_temp=hold["critical_temp"],
                linked=hold["linked"],
                shared_curve=hold["curve"] if hold["profile"] == "custom" else list(PROFILES.get(hold["profile"], hold["curve"])),
                curve_cpu=hold["curve_cpu"],
                curve_gpu=hold["curve_gpu"],
                current_duties=current,
                missing_count=missing,
                fans=fans,
            )
            if decision.missing_temp:
                missing += 1
            else:
                missing = 0
            if decision.action == "release":
                if backend and not released_for_fault:
                    print("temperature unavailable; returning control to the EC", file=sys.stderr)
                    backend.release()
                    released_for_fault = True
                time.sleep(args.interval)
                continue
            if released_for_fault and backend and decision.action == "write":
                backend.lock()
                released_for_fault = False
            if args.dry_run:
                control = select_control_temp(cpu, gpu)
                print(f"{control if control is not None else '--'}°C -> {decision.duties or decision.writes}")
            elif backend:
                if hold["mode"] == "released":
                    backend.ping()
                else:
                    wrote = False
                    for fan, duty in decision.writes.items():
                        backend.write_duty(fan, duty)
                        wrote = True
                    if not wrote:
                        backend.ping()
            time.sleep(args.interval)
    finally:
        if backend:
            try:
                backend.release()
            except OSError:
                pass
            backend.close()
        lock.release()
        clear_pid(rdir, "daemon.pid")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--profile", choices=[name for name in PROFILES if name != "custom"], default="balanced")
    parser.add_argument("--curve", help="JSON list of [temperature, duty] points")
    parser.add_argument("--config", default=CONFIG_DEFAULT)
    parser.add_argument("--device", help="tuxedo_io device path (or set FAN_CONTROL_DEVICE)")
    parser.add_argument("--backend", choices=("auto", "tuxedo_io", "clevo_acpi"), default="auto")
    parser.add_argument("--fans", type=int, choices=(1, 2, 3), default=2)
    parser.add_argument("--hysteresis", type=int, default=5)
    parser.add_argument("--max-duty", type=int, default=SAFE_MAX_DUTY)
    parser.add_argument("--critical-temp", type=float, default=95)
    parser.add_argument("--dry-run", action="store_true", help="print decisions without opening the EC device")
    parser.add_argument("--diagnose", action="store_true", help="print system state and exit (no EC access)")
    parser.add_argument("--runtime-dir", help="override /run/fan-control")
    args = parser.parse_args()
    if args.curve:
        import json
        args.curve = json.loads(args.curve)
    if args.diagnose:
        diagnose(args.config)
        return
    if args.interval <= 0 or args.hysteresis < 0 or not 70 <= args.critical_temp <= 110:
        parser.error("interval must be positive, hysteresis non-negative, and critical temperature 70-110")
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
