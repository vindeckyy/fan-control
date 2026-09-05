#!/usr/bin/env python3
"""Shared thermal policy for the daemon, GUI, and CLI.

Duty is always a percentage 0-100. Critical temperature bypasses the noise cap.
"""

from __future__ import annotations

import json
import pathlib

from fan_backend import MAX_DUTY_PERCENT, migrate_config

SAFE_MAX_DUTY = MAX_DUTY_PERCENT
FAULT_MISS_THRESHOLD = 3
CPU_CHIPS = ("k10temp", "coretemp", "zenpower")
GPU_CHIPS = ("amdgpu", "radeon", "nouveau", "i915", "xe")
GPU_NAME_HINTS = ("nvidia", "amdgpu", "radeon", "nouveau", "intel_gpu", "gpu")


PROFILES = {
    "silent": [(0, 0), (50, 0), (60, 0), (70, 30), (80, 61), (90, 86), (95, 100), (110, 100)],
    "balanced": [(0, 0), (50, 0), (60, 25), (70, 50), (80, 76), (90, 91), (95, 100), (110, 100)],
    "performance": [(0, 0), (50, 0), (60, 40), (70, 71), (80, 91), (85, 100), (110, 100)],
    "custom": [(0, 0), (55, 0), (65, 28), (75, 56), (85, 83), (95, 100), (110, 100)],
}

MODES = ("manual", "curve", "released")


def _valid_temp(value):
    return value is not None and not isinstance(value, bool) and 0 < float(value) <= 150


def normalize_curve(curve):
    if not isinstance(curve, list):
        raise ValueError("curve must be a list")
    points = {}
    for row in curve:
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise ValueError("each point must be [temperature, duty]")
        temp, duty = row
        if isinstance(temp, bool) or isinstance(duty, bool) or not isinstance(temp, (int, float)) or not isinstance(duty, (int, float)):
            raise ValueError("curve values must be numbers")
        points[max(0, min(150, int(temp)))] = max(0, min(SAFE_MAX_DUTY, int(round(duty))))
    result = sorted(points.items())
    if len(result) < 2:
        raise ValueError("curve needs at least two unique temperatures")
    return result


def interpolate(temp, curve):
    if temp <= curve[0][0]:
        return curve[0][1]
    if temp >= curve[-1][0]:
        return curve[-1][1]
    for (t0, d0), (t1, d1) in zip(curve, curve[1:], strict=False):
        if t0 <= temp < t1:
            return d0 + (d1 - d0) * (temp - t0) / (t1 - t0)
    return curve[-1][1]


def target_duty(temp, curve, max_duty=SAFE_MAX_DUTY, critical_temp=95):
    """Critical cooling deliberately bypasses the user noise cap."""
    if temp >= critical_temp:
        return SAFE_MAX_DUTY
    return max(0, min(max_duty, int(round(interpolate(temp, curve)))))


def select_control_temp(*temps):
    valid = [float(value) for value in temps if _valid_temp(value)]
    return max(valid) if valid else None


def should_write(current, desired, hysteresis, critical=False):
    if critical:
        return True
    return abs(int(current) - int(desired)) >= int(hysteresis)


def temperature_fault(missing_count):
    return int(missing_count) >= FAULT_MISS_THRESHOLD


def parse_nvidia_smi(output):
    sensors = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) != 3:
            continue
        index, temperature, name = parts
        try:
            temperature = float(temperature)
        except ValueError:
            continue
        if 0 <= temperature <= 150:
            sensors.append({"name": "nvidia", "label": f"GPU {index} · {name}", "temp": temperature})
    return sensors


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


def _sensor_matches(sensor, pin):
    if not pin:
        return False
    return sensor.get("name") == pin.get("name") and sensor.get("label") == pin.get("label")


def pick_cpu_temp(sensors, pin=None, ec_fallback=None):
    if pin:
        for sensor in sensors:
            if _sensor_matches(sensor, pin) and _valid_temp(sensor.get("temp")):
                return float(sensor["temp"])
    cpu = [sensor["temp"] for sensor in sensors if sensor.get("name") in CPU_CHIPS and _valid_temp(sensor.get("temp"))]
    if cpu:
        return max(cpu)
    return float(ec_fallback) if _valid_temp(ec_fallback) else None


def pick_gpu_temp(sensors, pin=None):
    if pin:
        for sensor in sensors:
            if _sensor_matches(sensor, pin) and _valid_temp(sensor.get("temp")):
                return float(sensor["temp"])
    values = [
        sensor["temp"]
        for sensor in sensors
        if _valid_temp(sensor.get("temp"))
        and (
            sensor.get("name") in GPU_CHIPS
            or any(hint in str(sensor.get("name", "")).lower() for hint in GPU_NAME_HINTS)
            or "gpu" in str(sensor.get("label", "")).lower()
        )
    ]
    return max(values) if values else None


def scan_sensors(demo=False, include_nvidia=True, hwmon_dir="/sys/class/hwmon"):
    """Discover live thermal sensors from sysfs hwmon and optional nvidia-smi."""
    if demo:
        import math
        import time
        return [
            {"name": "k10temp", "label": "Tctl", "temp": 62 + 10 * math.sin(time.monotonic() / 18)},
            {"name": "k10temp", "label": "Tccd1", "temp": 58 + 8 * math.sin(time.monotonic() / 21)},
            {"name": "amdgpu", "label": "edge", "temp": 54 + 7 * math.sin(time.monotonic() / 23)},
        ]
    found = []
    base = pathlib.Path(hwmon_dir)
    if base.exists():
        for hwmon in sorted(base.glob("hwmon*")):
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
    if include_nvidia and not any("nvidia" in str(s.get("name", "")).lower() for s in found):
        import shutil
        import subprocess
        if shutil.which("nvidia-smi"):
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=index,temperature.gpu,name", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=2, check=False,
                )
                if result.returncode == 0:
                    found.extend(parse_nvidia_smi(result.stdout))
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                pass
    return found


def _temp_for_fan(fan, cpu_temp, gpu_temp, linked):
    if linked:
        return select_control_temp(cpu_temp, gpu_temp)
    if fan == 1:
        return cpu_temp if _valid_temp(cpu_temp) else gpu_temp
    return gpu_temp if _valid_temp(gpu_temp) else cpu_temp


def _curve_for_fan(fan, linked, shared_curve, curve_cpu, curve_gpu):
    if linked:
        return shared_curve
    if fan == 1:
        return curve_cpu or shared_curve
    return curve_gpu or shared_curve


def desired_curve_duties(
    cpu_temp,
    gpu_temp,
    linked,
    shared_curve,
    curve_cpu,
    curve_gpu,
    max_duty,
    critical_temp,
    fans=(1, 2),
):
    duties = {}
    for fan in fans:
        temp = _temp_for_fan(fan, cpu_temp, gpu_temp, linked)
        if not _valid_temp(temp):
            continue
        curve = _curve_for_fan(fan, linked, shared_curve, curve_cpu, curve_gpu)
        duties[int(fan)] = target_duty(temp, curve, max_duty, critical_temp)
    return duties


class ControlDecision:
    def __init__(self, action, writes=None, duties=None, critical=False, missing_temp=False):
        self.action = action
        self.writes = writes if writes is not None else {}
        self.duties = duties if duties is not None else {}
        self.critical = critical
        self.missing_temp = missing_temp


def decide(
    *,
    mode,
    profile,
    cpu_temp,
    gpu_temp,
    targets,
    max_duty,
    hysteresis,
    critical_temp,
    linked,
    shared_curve,
    curve_cpu,
    curve_gpu,
    current_duties,
    missing_count,
    fans=(1, 2),
):
    fans = tuple(int(fan) for fan in fans)
    if mode == "released":
        return ControlDecision(action="idle")

    control = select_control_temp(cpu_temp, gpu_temp)
    if mode == "curve" and control is None:
        if temperature_fault(missing_count):
            return ControlDecision(action="release", missing_temp=True)
        return ControlDecision(action="idle", missing_temp=True)

    critical = control is not None and control >= critical_temp
    duties = {}
    if critical:
        duties = {fan: SAFE_MAX_DUTY for fan in fans}
    elif mode == "curve":
        curve = PROFILES[profile] if profile in PROFILES and profile != "custom" else shared_curve
        duties = desired_curve_duties(
            cpu_temp,
            gpu_temp,
            linked,
            curve,
            curve_cpu,
            curve_gpu,
            max_duty,
            critical_temp,
            fans,
        )
    elif mode == "manual":
        for fan in fans:
            pct = int(targets.get(fan, targets.get(str(fan), 0)) or 0)
            duties[fan] = round(max(0, min(100, pct)) * max_duty / 100)
    else:
        return ControlDecision(action="idle")

    writes = {}
    for fan, desired in duties.items():
        current = int(current_duties.get(fan, current_duties.get(str(fan), 0)) or 0)
        if should_write(current, desired, hysteresis, critical):
            writes[fan] = desired
    return ControlDecision(action="write", writes=writes, duties=duties, critical=critical)


def _optional_curve(value):
    if value in (None, [], {}):
        return None
    try:
        return normalize_curve(value)
    except ValueError:
        return None


def _optional_pin(value):
    if not isinstance(value, dict):
        return None
    name, label = value.get("name"), value.get("label")
    if not isinstance(name, str) or not isinstance(label, str):
        return None
    return {"name": name, "label": label}


def default_config():
    return {
        "mode": "manual",
        "profile": "balanced",
        "max_duty": SAFE_MAX_DUTY,
        "hysteresis": 5,
        "critical_temp": 95,
        "linked": True,
        "curve": list(PROFILES["custom"]),
        "curve_cpu": None,
        "curve_gpu": None,
        "named_curves": {},
        "alerts": {"desktop": False},
        "cpu_sensor": None,
        "gpu_sensor": None,
        "theme": "dark",
        "targets": {"1": 0, "2": 0, "3": 0},
    }


def apply_defaults(data):
    base = default_config()
    if not isinstance(data, dict):
        return base
    data = migrate_config(data)
    mode = data.get("mode", base["mode"])
    base["mode"] = mode if mode in MODES else "manual"
    profile = data.get("profile", base["profile"])
    base["profile"] = profile if profile in PROFILES else "balanced"
    try:
        base["max_duty"] = max(20, min(SAFE_MAX_DUTY, int(data.get("max_duty", base["max_duty"]))))
    except (TypeError, ValueError):
        pass
    try:
        base["hysteresis"] = max(0, min(30, int(data.get("hysteresis", base["hysteresis"]))))
    except (TypeError, ValueError):
        pass
    try:
        base["critical_temp"] = max(70, min(110, int(data.get("critical_temp", base["critical_temp"]))))
    except (TypeError, ValueError):
        pass
    if isinstance(data.get("linked"), bool):
        base["linked"] = data["linked"]
    try:
        base["curve"] = normalize_curve(data.get("curve", base["curve"]))
    except ValueError:
        base["curve"] = list(PROFILES["custom"])
    base["curve_cpu"] = _optional_curve(data.get("curve_cpu"))
    base["curve_gpu"] = _optional_curve(data.get("curve_gpu"))
    named = {}
    raw_named = data.get("named_curves") if isinstance(data.get("named_curves"), dict) else {}
    for name, curve in raw_named.items():
        if not isinstance(name, str) or not name.strip():
            continue
        try:
            named[name.strip()] = normalize_curve(curve)
        except ValueError:
            continue
    base["named_curves"] = named
    alerts = data.get("alerts") if isinstance(data.get("alerts"), dict) else {}
    base["alerts"] = {"desktop": bool(alerts.get("desktop", False))}
    base["cpu_sensor"] = _optional_pin(data.get("cpu_sensor"))
    base["gpu_sensor"] = _optional_pin(data.get("gpu_sensor"))
    theme = data.get("theme", "dark")
    base["theme"] = theme if theme in ("dark", "light") else "dark"
    targets = data.get("targets") if isinstance(data.get("targets"), dict) else {}
    cleaned = {"1": 0, "2": 0, "3": 0}
    for key in ("1", "2", "3", 1, 2, 3):
        if key in targets:
            try:
                cleaned[str(int(key))] = max(0, min(100, int(targets[key])))
            except (TypeError, ValueError):
                pass
    base["targets"] = cleaned
    return base


def load_config(path):
    path = pathlib.Path(path)
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        return default_config()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read config: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("config must contain a JSON object")
    return apply_defaults(value)


def config_payload(state):
    def curve_out(curve):
        if curve is None:
            return None
        return [list(point) for point in curve]

    named = {name: curve_out(curve) for name, curve in state.get("named_curves", {}).items()}
    return {
        "mode": state["mode"],
        "profile": state["profile"],
        "max_duty": state["max_duty"],
        "hysteresis": state["hysteresis"],
        "critical_temp": state["critical_temp"],
        "linked": state["linked"],
        "curve": curve_out(state["curve"]),
        "curve_cpu": curve_out(state.get("curve_cpu")),
        "curve_gpu": curve_out(state.get("curve_gpu")),
        "named_curves": named,
        "alerts": dict(state.get("alerts") or {"desktop": False}),
        "cpu_sensor": state.get("cpu_sensor"),
        "gpu_sensor": state.get("gpu_sensor"),
        "theme": state.get("theme", "dark"),
        "targets": {str(k): int(v) for k, v in (state.get("targets") or {}).items()},
    }


def save_config(path, state):
    path = pathlib.Path(path)
    payload = config_payload(apply_defaults(state))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)


def history_csv(rows):
    headers = ["time", "temp", "gpu_temp", "control_temp", "fan1", "fan2", "fan3", "rpm1", "rpm2", "rpm3"]
    lines = [",".join(headers)]
    for row in rows:
        cells = []
        for key in headers:
            value = row.get(key, "")
            cells.append("" if value is None else str(value))
        lines.append(",".join(cells))
    return "\n".join(lines) + "\n"
