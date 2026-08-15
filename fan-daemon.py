#!/usr/bin/env python3
"""Temperature-driven fan control for Clevo/Tongfang barebones."""

import argparse
import json

import pathlib
import signal
import subprocess
import sys
import time
from fan_backend import (
    MAX_DUTY_PERCENT,
    detect_backend,
    migrate_config,
)

SAFE_MAX_DUTY = MAX_DUTY_PERCENT

PROFILES = {
    "silent": [(0, 0), (50, 0), (60, 0), (70, 30), (80, 61), (90, 86), (95, 100), (110, 100)],
    "balanced": [(0, 0), (50, 0), (60, 25), (70, 50), (80, 76), (90, 91), (95, 100), (110, 100)],
    "performance": [(0, 0), (50, 0), (60, 40), (70, 71), (80, 91), (85, 100), (110, 100)],
}


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


def parse_nvidia_temperatures(output):
    temperatures = []
    for line in output.splitlines():
        try:
            temperature = float(line.strip())
        except ValueError:
            continue
        if 0 < temperature <= 150:
            temperatures.append(temperature)
    return temperatures


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


def normalize_curve(curve):
    """Validate, sort, deduplicate, and clamp a user-provided curve."""
    if not isinstance(curve, list):
        raise ValueError("curve must be a JSON list")
    points = {}
    for row in curve:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError("each curve point must be [temperature, duty]")
        temp, duty = row
        if isinstance(temp, bool) or isinstance(duty, bool) or not isinstance(temp, (int, float)) or not isinstance(duty, (int, float)):
            raise ValueError("curve values must be numbers")
        points[max(0, min(150, float(temp)))] = max(0, min(SAFE_MAX_DUTY, int(round(duty))))
    curve = sorted(points.items())
    if len(curve) < 2:
        raise ValueError("curve needs at least two unique temperatures")
    return curve


def interpolate(temp, curve):
    if temp <= curve[0][0]:
        return curve[0][1]
    if temp >= curve[-1][0]:
        return curve[-1][1]
    for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
        if t0 <= temp < t1:
            return d0 + (d1 - d0) * (temp - t0) / (t1 - t0)
    raise ValueError("invalid fan curve")


def target_duty(temp, curve, max_duty=SAFE_MAX_DUTY, critical_temp=95):
    """Critical cooling deliberately bypasses the user noise cap."""
    if temp >= critical_temp:
        return SAFE_MAX_DUTY
    return max(0, min(max_duty, int(round(interpolate(temp, curve)))))


def load_config(path):
    if not path:
        return {}
    try:
        value = json.loads(pathlib.Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read config: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("config must contain a JSON object")
    return migrate_config(value)


def run(args):
    config = load_config(args.config)
    profile = config.get("profile", args.profile)
    if profile not in (*PROFILES, "custom"):
        profile = args.profile
    curve = normalize_curve(args.curve or (config.get("curve") if profile == "custom" else None) or PROFILES.get(profile, PROFILES[args.profile]))
    max_duty = max(0, min(SAFE_MAX_DUTY, int(config.get("max_duty", args.max_duty))))
    hysteresis = max(0, int(config.get("hysteresis", args.hysteresis)))
    critical_temp = max(70, min(110, float(config.get("critical_temp", args.critical_temp))))
    sensor = find_cpu_sensor()
    backend = None if args.dry_run else detect_backend(args.backend, args.device)
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if backend:
        backend.lock()
    print(f"fan daemon: profile={profile}, backend={backend.name if backend else 'dry-run'}, {len(curve)} points, interval={args.interval}s, cap={max_duty}")

    missing = 0
    released_for_fault = False
    try:
        while not stopped:
            cpu = cpu_temp(sensor)
            if cpu is None and backend:
                try:
                    cpu = backend.read_temp()
                except OSError:
                    cpu = None
            temperatures = [value for value in (cpu, gpu_temp()) if value is not None and 0 < value <= 150]
            if not temperatures:
                missing += 1
                if backend and missing >= 3 and not released_for_fault:
                    print("temperature unavailable; returning control to the EC", file=sys.stderr)
                    backend.release()
                    released_for_fault = True
                time.sleep(args.interval)
                continue

            temp = max(temperatures)
            if released_for_fault and backend:
                backend.lock()
                released_for_fault = False
            missing = 0
            duty = target_duty(temp, curve, max_duty, critical_temp)
            if args.dry_run:
                print(f"{temp:.1f}°C -> duty {duty}")
            else:
                wrote = False
                for fan in backend.fans():
                    if fan > args.fans:
                        break
                    if temp >= critical_temp or abs(backend.read_duty(fan) - duty) >= hysteresis:
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--profile", choices=PROFILES, default="balanced")
    parser.add_argument("--curve", type=json.loads, help="JSON list of [temperature, duty] points")
    parser.add_argument("--config", default="/etc/fan-control.json")
    parser.add_argument("--device", help="tuxedo_io device path (or set FAN_CONTROL_DEVICE)")
    parser.add_argument("--backend", choices=("auto", "tuxedo_io", "clevo_acpi"), default="auto")
    parser.add_argument("--fans", type=int, choices=(1, 2), default=2)
    parser.add_argument("--hysteresis", type=int, default=5)
    parser.add_argument("--max-duty", type=int, default=SAFE_MAX_DUTY)
    parser.add_argument("--critical-temp", type=float, default=95)
    parser.add_argument("--dry-run", action="store_true", help="print decisions without opening the EC device")
    parser.add_argument("--diagnose", action="store_true", help="print system state and exit (no EC access)")
    args = parser.parse_args()
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
