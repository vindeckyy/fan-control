#!/usr/bin/env python3
"""Shared thermal policy for the daemon, GUI, and CLI.

Duty is always a percentage 0-100. Critical temperature bypasses the noise cap.
"""

from __future__ import annotations

import copy
import json
import math
import pathlib
import sys

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
    if value is None or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return 0 < number <= 150


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
        if not math.isfinite(temp) or not math.isfinite(duty):
            raise ValueError("curve values must be finite numbers")
        points[max(0, min(150, int(round(temp))))] = max(0, min(SAFE_MAX_DUTY, int(round(duty))))
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
    import csv
    import io

    sensors = []
    try:
        rows = csv.reader(io.StringIO(output))
    except csv.Error:
        return sensors
    for parts in rows:
        parts = [part.strip() for part in parts]
        if len(parts) < 3:
            continue
        index, temperature, name = parts[0], parts[1], ",".join(parts[2:])
        try:
            temperature = float(temperature)
        except ValueError:
            continue
        if 0 < temperature <= 150:
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
    if sys.platform == "win32":
        return _scan_sensors_windows(include_nvidia=include_nvidia)
    found = []
    base = pathlib.Path(hwmon_dir)
    if base.exists():
        for hwmon in sorted(base.glob("hwmon*")):
            try:
                name = (hwmon / "name").read_text().strip()
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            for path in sorted(hwmon.glob("temp*_input")):
                try:
                    temp = int(path.read_text().strip()) / 1000
                    if 0 < temp <= 150:
                        label_path = path.with_name(path.name.replace("_input", "_label"))
                        label = label_path.read_text().strip() if label_path.exists() else path.stem.replace("_input", "")
                        found.append({"name": name, "label": label, "temp": temp})
                except (OSError, ValueError, UnicodeDecodeError):
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
    for index in (1, 2, 3):
        raw = targets.get(str(index), targets.get(index))
        if isinstance(raw, bool) or raw is None:
            continue
        try:
            cleaned[str(index)] = max(0, min(100, int(raw)))
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
    """Persist a legacy flat config atomically (compat path; v2 uses save_document)."""
    import os as _os

    path = pathlib.Path(path)
    payload = config_payload(apply_defaults(state))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w") as handle:
        handle.write(json.dumps(payload, indent=2) + "\n")
        handle.flush()
        _os.fsync(handle.fileno())
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


# ---------------------------------------------------------------------------
# Configuration v2: versioned schema, migration, validation, atomic writes.
# The v2 document is the canonical persistent store. Legacy flat (v1) configs
# migrate deterministically; the legacy helpers above remain for compat.
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 2
PROTOCOL_VERSION = 2
CONFIG_REVISION_START = 1

V2_MODES = ("auto", "manual", "released")
LEGACY_MODE_MAP = {"curve": "auto", "manual": "manual", "released": "released"}
FAN_IDS = ("fan1", "fan2", "fan3")
CURVE_TEMP_SOURCES = ("cpu", "gpu", "max")
RETENTION_MIN_DAYS = 1
RETENTION_MAX_DAYS = 365

# Keys consumed by the v1->v2 migration; anything else is preserved verbatim.
_V1_CONSUMED_KEYS = {
    "mode", "profile", "max_duty", "hysteresis", "critical_temp", "linked",
    "curve", "curve_cpu", "curve_gpu", "named_curves", "alerts", "cpu_sensor",
    "gpu_sensor", "theme", "targets", "version", "revision",
}


class ConfigError(ValueError):
    """Structurally invalid configuration; carries a stable machine code."""

    def __init__(self, message, code="CONFIG_ERROR"):
        super().__init__(message)
        self.code = code


def _clamp(value, low, high, default):
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError
        return int(max(low, min(high, value)))
    except (TypeError, ValueError):
        return default


def _valid_pin(value):
    """Accept both legacy {name, label} pins and new stable-ID pin strings."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        name, label = value.get("name"), value.get("label")
        if isinstance(name, str) and isinstance(label, str) and name.strip() and label.strip():
            return {"name": name.strip(), "label": label.strip()}
    return None


def sanitize_curve_id(name):
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(name).strip().lower())
    cleaned = cleaned.strip("_") or "curve"
    return f"named_{cleaned[:48]}"


def _default_fan(index):
    return {
        "name": {1: "CPU Fan", 2: "GPU Fan"}.get(index, f"Fan {index}"),
        "enabled": True,
        "control": {"type": "linked", "curve_ref": None, "target": 0},
        "min_duty": 0,
        "max_duty": 100,
    }


def default_config_v2():
    return {
        "version": SCHEMA_VERSION,
        "revision": CONFIG_REVISION_START,
        "mode": "manual",
        "profile": "balanced",
        "safety": {
            "critical_temp": 95,
            "global_max_duty": 100,
            "hysteresis": 5,
            "firmware_fallback": True,
        },
        "display": {"theme": "dark", "temperature_unit": "c"},
        "notifications": {"enabled": False, "critical_temp": True},
        "sensors": {"cpu_pin": None, "gpu_pin": None, "pinned": []},
        "fans": {fan_id: _default_fan(i) for i, fan_id in enumerate(FAN_IDS, start=1)},
        "curves": {},
        "rules": [],
        "schedule": {"enabled": False, "timezone": "local", "items": []},
        "history": {"persist": True, "retention_days": 7},
    }


def _curve_document(points, name, temp_source):
    return {"name": name, "temp_source": temp_source, "points": [list(p) for p in points]}


def detect_config_version(data):
    if not isinstance(data, dict):
        return 0
    return data.get("version") if isinstance(data.get("version"), int) else 1


def upgrade_config_v1_to_v2(data):
    """Deterministically map a legacy flat config onto the v2 schema.

    Curve IDs are stable across repeated migrations: ``legacy_custom`` for the
    shared curve, ``cpu_default``/``gpu_default`` for per-side curves and
    ``named_<sanitized>`` for named curves (collisions resolved by suffixing).
    """
    data = migrate_config(data)
    doc = default_config_v2()
    doc["revision"] = _clamp(data.get("revision"), 1, 2**31 - 1, CONFIG_REVISION_START)

    doc["mode"] = LEGACY_MODE_MAP.get(data.get("mode"), "manual")
    profile = data.get("profile")
    doc["profile"] = profile if profile in PROFILES else "balanced"

    safety = doc["safety"]
    safety["critical_temp"] = _clamp(data.get("critical_temp"), 70, 110, 95)
    safety["global_max_duty"] = _clamp(data.get("max_duty"), 20, SAFE_MAX_DUTY, SAFE_MAX_DUTY)
    safety["hysteresis"] = _clamp(data.get("hysteresis"), 0, 30, 5)

    theme = data.get("theme")
    doc["display"]["theme"] = theme if theme in ("dark", "light") else "dark"
    alerts = data.get("alerts") if isinstance(data.get("alerts"), dict) else {}
    doc["notifications"]["enabled"] = bool(alerts.get("desktop", False))

    doc["sensors"]["cpu_pin"] = _valid_pin(data.get("cpu_sensor"))
    doc["sensors"]["gpu_pin"] = _valid_pin(data.get("gpu_sensor"))

    curves = {}
    shared = _optional_curve(data.get("curve"))
    curves["legacy_custom"] = _curve_document(shared or PROFILES["custom"], "Custom", "max")
    cpu_curve = _optional_curve(data.get("curve_cpu"))
    if cpu_curve:
        curves["cpu_default"] = _curve_document(cpu_curve, "CPU Curve", "cpu")
    gpu_curve = _optional_curve(data.get("curve_gpu"))
    if gpu_curve:
        curves["gpu_default"] = _curve_document(gpu_curve, "GPU Curve", "gpu")
    raw_named = data.get("named_curves") if isinstance(data.get("named_curves"), dict) else {}
    used_ids = set(curves)
    for name, curve in raw_named.items():
        if not isinstance(name, str) or not name.strip() or _optional_curve(curve) is None:
            continue
        base = sanitize_curve_id(name)
        curve_id, suffix = base, 2
        while curve_id in used_ids:
            curve_id = f"{base}_{suffix}"
            suffix += 1
        used_ids.add(curve_id)
        curves[curve_id] = _curve_document(_optional_curve(curve), name.strip(), "max")
    doc["curves"] = curves

    linked = data.get("linked")
    linked = True if not isinstance(linked, bool) else linked
    targets = data.get("targets") if isinstance(data.get("targets"), dict) else {}

    def _target(index):
        for key in (str(index), index):
            if key in targets:
                return _clamp(targets[key], 0, 100, 0)
        return 0

    for index, fan_id in enumerate(FAN_IDS, start=1):
        fan = doc["fans"][fan_id]
        fan["control"]["target"] = _target(index)
        if linked:
            fan["control"]["type"] = "linked"
            continue
        if index == 1 and cpu_curve:
            fan["control"] = {"type": "curve", "curve_ref": "cpu_default", "target": _target(1)}
        elif index >= 2 and gpu_curve:
            fan["control"] = {"type": "curve", "curve_ref": "gpu_default", "target": _target(index)}
        else:
            fan["control"] = {
                "type": "profile",
                "temp_source": "cpu" if index == 1 else "gpu",
                "curve_ref": None,
                "target": _target(index),
            }

    for key, value in data.items():
        if key not in _V1_CONSUMED_KEYS and key not in doc:
            doc[key] = value
    return doc


def _normalize_curve_document(curve_id, entry, warnings):
    if not isinstance(entry, dict):
        raise ConfigError(f"curve {curve_id!r} must be an object")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        warnings.append(f"curve {curve_id!r}: missing name, using id")
        name = curve_id
    source = entry.get("temp_source", "max")
    if source not in CURVE_TEMP_SOURCES and not (isinstance(source, str) and source.strip()):
        warnings.append(f"curve {curve_id!r}: bad temp_source, using 'max'")
        source = "max"
    try:
        points = normalize_curve(entry.get("points"))
    except ValueError as exc:
        raise ConfigError(f"curve {curve_id!r}: {exc}") from exc
    return {"name": name.strip(), "temp_source": source, "points": [list(p) for p in points]}


VALID_CONTROL_TYPES = ("linked", "profile", "curve", "manual")
TRIGGER_TYPES = ("temp_above", "temp_below", "rpm_below", "time_range", "on_startup")
ACTION_TYPES = ("set_profile", "set_mode", "set_duty", "notify", "run_command")


def _normalize_control(entry, warnings, fan_id):
    if not isinstance(entry, dict):
        warnings.append(f"{fan_id}: invalid control, using linked")
        return dict(_default_fan(1)["control"])
    ctype = entry.get("type")
    if ctype not in VALID_CONTROL_TYPES:
        warnings.append(f"{fan_id}: unknown control type {ctype!r}, using linked")
        ctype = "linked"
    control = {
        "type": ctype,
        "curve_ref": entry.get("curve_ref") if isinstance(entry.get("curve_ref"), str) else None,
        "target": _clamp(entry.get("target"), 0, 100, 0),
    }
    if ctype == "profile":
        source = entry.get("temp_source")
        control["temp_source"] = source if source in ("cpu", "gpu") else "cpu"
    return control


def _normalize_rule(entry, warnings, seen_ids):
    from fan_rules import validate_rule_payload

    try:
        validate_rule_payload(entry)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if not isinstance(entry, dict):
        raise ConfigError("each rule must be an object")
    rule_id = entry.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ConfigError("rule id must be a non-empty string")
    rule_id = rule_id.strip()
    if rule_id in seen_ids:
        raise ConfigError(f"duplicate rule id {rule_id!r}")
    seen_ids.add(rule_id)
    trigger = entry.get("trigger") if isinstance(entry.get("trigger"), dict) else {}
    if trigger.get("type") not in TRIGGER_TYPES:
        raise ConfigError(f"rule {rule_id!r}: invalid trigger type")
    action = entry.get("action") if isinstance(entry.get("action"), dict) else {}
    if action.get("type") not in ACTION_TYPES:
        raise ConfigError(f"rule {rule_id!r}: invalid action type")
    condition = entry.get("condition") if isinstance(entry.get("condition"), dict) else {}
    return {
        "id": rule_id,
        "name": str(entry.get("name") or rule_id),
        "enabled": bool(entry.get("enabled", True)),
        "priority": _clamp(entry.get("priority"), 0, 1000, 100),
        "safety_class": _clamp(entry.get("safety_class"), 0, 1000, 0),
        "trigger": copy.deepcopy(trigger),
        "condition": {
            "sustain_ticks": _clamp(condition.get("sustain_ticks"), 0, 1000, 0),
            "cooldown_seconds": _clamp(condition.get("cooldown_seconds"), 0, 86400, 0),
        },
        "action": copy.deepcopy(action),
    }


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _valid_hhmm(value):
    return _canonical_hhmm(value) is not None


def _canonical_hhmm(value):
    """Validate a clock time and return it as zero-padded ``HH:MM`` or None."""
    if not isinstance(value, str) or ":" not in value:
        return None
    parts = value.strip().split(":")
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        return None
    hours, minutes = int(parts[0].strip()), int(parts[1].strip())
    if not 0 <= hours <= 23 and 0 <= minutes <= 59:
        return None
    return f"{hours:02d}:{minutes:02d}"


def normalize_config(data):
    """Validate and normalize a v2 document. Returns (doc, warnings).

    Range violations are clamped and reported as warnings; structurally
    invalid input raises ConfigError so the original file stays untouched.
    """
    if not isinstance(data, dict):
        raise ConfigError("config must contain a JSON object")
    version = detect_config_version(data)
    if version != SCHEMA_VERSION:
        raise ConfigError(f"unsupported config version {version!r}")
    doc = default_config_v2()
    doc["revision"] = _clamp(data.get("revision"), 1, 2**31 - 1, CONFIG_REVISION_START)
    warnings = []

    doc["mode"] = data.get("mode") if data.get("mode") in V2_MODES else "manual"
    doc["profile"] = data.get("profile") if data.get("profile") in PROFILES else "balanced"

    safety = data.get("safety") if isinstance(data.get("safety"), dict) else {}
    doc["safety"]["critical_temp"] = _clamp(safety.get("critical_temp"), 70, 110, 95)
    doc["safety"]["global_max_duty"] = _clamp(safety.get("global_max_duty"), 20, SAFE_MAX_DUTY, SAFE_MAX_DUTY)
    doc["safety"]["hysteresis"] = _clamp(safety.get("hysteresis"), 0, 30, 5)
    doc["safety"]["firmware_fallback"] = bool(safety.get("firmware_fallback", True))

    display = data.get("display") if isinstance(data.get("display"), dict) else {}
    doc["display"]["theme"] = display.get("theme") if display.get("theme") in ("dark", "light", "system") else "dark"
    unit = display.get("temperature_unit")
    doc["display"]["temperature_unit"] = unit if unit in ("c", "f") else "c"

    notifications = data.get("notifications") if isinstance(data.get("notifications"), dict) else {}
    doc["notifications"]["enabled"] = bool(notifications.get("enabled", False))
    doc["notifications"]["critical_temp"] = bool(notifications.get("critical_temp", True))

    sensors = data.get("sensors") if isinstance(data.get("sensors"), dict) else {}
    doc["sensors"]["cpu_pin"] = _valid_pin(sensors.get("cpu_pin"))
    doc["sensors"]["gpu_pin"] = _valid_pin(sensors.get("gpu_pin"))
    pinned = sensors.get("pinned")
    doc["sensors"]["pinned"] = [p for p in pinned if isinstance(p, str)] if isinstance(pinned, list) else []

    fans = data.get("fans")
    if not isinstance(fans, dict):
        raise ConfigError("fans must be an object")
    seen_fan_ids = set()
    for fan_id, entry in fans.items():
        if fan_id not in FAN_IDS:
            warnings.append(f"ignoring unknown fan {fan_id!r}")
            continue
        seen_fan_ids.add(fan_id)
        if not isinstance(entry, dict):
            raise ConfigError(f"fan {fan_id!r} must be an object")
        fan = doc["fans"][fan_id]
        fan["name"] = str(entry.get("name") or fan["name"])[:64]
        fan["enabled"] = bool(entry.get("enabled", True))
        fan["control"] = _normalize_control(entry.get("control"), warnings, fan_id)
        min_duty = _clamp(entry.get("min_duty"), 0, 100, 0)
        max_duty = _clamp(entry.get("max_duty"), 0, 100, 100)
        if min_duty > max_duty:
            warnings.append(f"{fan_id}: min_duty {min_duty} > max_duty {max_duty}, swapping")
            min_duty, max_duty = max_duty, min_duty
        fan["min_duty"], fan["max_duty"] = min_duty, max_duty

    curves = data.get("curves")
    if curves is None:
        curves = {}
    if not isinstance(curves, dict):
        raise ConfigError("curves must be an object")
    for curve_id, entry in curves.items():
        if not isinstance(curve_id, str) or not curve_id.strip():
            raise ConfigError("curve ids must be non-empty strings")
        doc["curves"][curve_id] = _normalize_curve_document(curve_id, entry, warnings)

    rules = data.get("rules", [])
    if not isinstance(rules, list):
        raise ConfigError("rules must be a list")
    seen_rule_ids = set()
    for entry in rules:
        doc["rules"].append(_normalize_rule(entry, warnings, seen_rule_ids))

    schedule = data.get("schedule") if isinstance(data.get("schedule"), dict) else {}
    doc["schedule"]["enabled"] = bool(schedule.get("enabled", False))
    timezone = schedule.get("timezone")
    doc["schedule"]["timezone"] = timezone if isinstance(timezone, str) and timezone.strip() else "local"
    if doc["schedule"]["timezone"] != "local":
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(doc["schedule"]["timezone"])
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ConfigError("schedule timezone must be a valid IANA zone or local") from exc
    items = schedule.get("items")
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ConfigError("schedule.items must be a list")
    seen_schedule_ids = set()
    for item in items:
        if not isinstance(item, dict):
            raise ConfigError("schedule items must be objects")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ConfigError("schedule item id must be a non-empty string")
        if item_id in seen_schedule_ids:
            raise ConfigError(f"duplicate schedule item id {item_id!r}")
        seen_schedule_ids.add(item_id)
        days = item.get("days")
        if not isinstance(days, list) or not days or not all(d in _WEEKDAYS for d in days):
            raise ConfigError(f"schedule item {item_id!r}: days must be a list of weekday names")
        start = _canonical_hhmm(item.get("start"))
        end = _canonical_hhmm(item.get("end"))
        if start is None or end is None:
            raise ConfigError(f"schedule item {item_id!r}: start/end must be HH:MM")
        profile = item.get("profile")
        mode = item.get("mode")
        if profile is not None and profile not in PROFILES:
            raise ConfigError(f"schedule item {item_id!r}: unknown profile {profile!r}")
        if mode is not None and mode not in V2_MODES:
            raise ConfigError(f"schedule item {item_id!r}: unknown mode {mode!r}")
        doc["schedule"]["items"].append({
            "id": item_id,
            "days": list(days),
            "start": start,
            "end": end,
            "profile": profile,
            "mode": mode,
        })

    history = data.get("history") if isinstance(data.get("history"), dict) else {}
    doc["history"]["persist"] = bool(history.get("persist", True))
    doc["history"]["retention_days"] = _clamp(
        history.get("retention_days"), RETENTION_MIN_DAYS, RETENTION_MAX_DAYS, 7
    )

    for key, value in data.items():
        if key not in doc:
            warnings.append(f"ignoring unknown config key {key!r}")
            doc[key] = value
    return doc, warnings


def validate_config(data):
    """Raise ConfigError when the document cannot be used at all."""
    normalize_config(data)


def migrate_document(data):
    """Dispatch on detected schema version. Returns (doc, warnings, migrated)."""
    version = detect_config_version(data)
    if version == SCHEMA_VERSION:
        return normalize_config(data) + (False,)
    if version == 1:
        return (*normalize_config(upgrade_config_v1_to_v2(data)), True)
    raise ConfigError(f"unsupported config version {version!r}")


def load_document(path):
    """Load a config file of any supported version as a v2 document.

    Returns (doc, warnings, migrated, original_version).
    """
    path = pathlib.Path(path)
    try:
        value = json.loads(path.read_text())
    except FileNotFoundError:
        doc, warnings = normalize_config(default_config_v2())
        return doc, warnings, False, None
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read config: {exc}") from exc
    original_version = detect_config_version(value)
    doc, warnings, migrated = migrate_document(value)
    return doc, warnings, migrated, original_version


def save_document(path, doc, *, original_version=None):
    """Persist a v2 document atomically, backing up v1 before first migration."""
    path = pathlib.Path(path)
    payload = json.dumps(doc, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if original_version == 1 and path.exists():
        backup = path.with_name(path.stem + ".v1.backup.json")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w") as handle:
        handle.write(payload)
        handle.flush()
        import os as _os

        _os.fsync(handle.fileno())
    temporary.replace(path)
    if hasattr(_os, "O_DIRECTORY") and sys.platform != "win32":
        try:
            dir_fd = _os.open(str(path.parent), _os.O_DIRECTORY)
        except (OSError, AttributeError):
            return
        try:
            _os.fsync(dir_fd)
        except (OSError, AttributeError):
            pass
        finally:
            try:
                _os.close(dir_fd)
            except (OSError, UnboundLocalError):
                pass


APP_VERSION = "2.0.0"

GPU_NAME_HINTS_EXTENDED = GPU_NAME_HINTS


def _hwmon_device_id(hwmon_path):
    """Stable per-boot device identity for a hwmon directory.

    Derived from the physical device syspath (e.g. ``platform-nct6775.656``,
    ``pci-0000:05:00.0``) — never from the volatile ``hwmonX`` index.
    """
    try:
        target = (hwmon_path / "device").resolve()
    except OSError:
        return "unknown"
    try:
        rel = target.relative_to(pathlib.Path("/sys/devices"))
    except ValueError:
        return target.name or "unknown"
    parts = rel.parts
    if not parts:
        return "unknown"
    bus, device = parts[0], parts[-1]
    if "pci" in bus or (":" in device and bus != "virtual"):
        return f"pci-{device}"
    return f"{bus}-{device}"


def _record_from_value(name, label, value, *, sensor_id, channel, source, runtime_path, hwmon_index=None):
    aliases = []
    if name in CPU_CHIPS:
        aliases.append("cpu")
    if (
        name in GPU_CHIPS
        or any(hint in str(name).lower() for hint in GPU_NAME_HINTS)
        or "gpu" in str(label).lower()
    ):
        aliases.append("gpu")
    return {
        "id": sensor_id,
        "name": name,
        "label": label,
        "channel": channel,
        "kind": "temperature",
        "value": value,
        "temp": value,
        "unit": "c",
        "source": source,
        "runtime_path": runtime_path,
        "hwmon_index": hwmon_index,
        "available": True,
        "aliases": aliases,
    }


def _normalize_nvidia_bus_id(bus_id):
    """Canonicalize an nvidia-smi PCI bus id for stable sensor identity."""
    if not isinstance(bus_id, str) or not bus_id.strip():
        return None
    return bus_id.strip().lower()


def parse_nvidia_smi_records(output):
    """Parse ``index,temperature.gpu,name[,pci.bus_id]`` lines."""
    import csv
    import io

    records = []
    try:
        rows = list(csv.reader(io.StringIO(output)))
    except csv.Error:
        return records
    for parts in rows:
        parts = [part.strip() for part in parts]
        if len(parts) < 3:
            continue
        index, temperature = parts[0], parts[1]
        if len(parts) == 4:
            name, bus_id = parts[2], _normalize_nvidia_bus_id(parts[3])
        else:
            name, bus_id = ",".join(parts[2:]), None
        try:
            temperature = float(temperature)
        except ValueError:
            continue
        if not 0 < temperature <= 150:
            continue
        identity = bus_id if bus_id else f"idx{index}"
        records.append({
            "index": index,
            "temp": temperature,
            "name": name,
            "bus_id": bus_id,
            "id": f"nvidia:{identity}",
        })
    return records


def _scan_sensor_records_windows(include_nvidia=True):
    """Discover temperature sensors on Windows with stable persistent identities."""
    import shutil
    import subprocess

    records = []
    seen_ids = set()

    # 1. ACPI thermal zones via PowerShell CIM / WMI
    try:
        ps_cmd = (
            "$ErrorActionPreference='SilentlyContinue'; "
            "Get-CimInstance -Namespace root/wmi -ClassName MSAcpi_ThermalZoneTemperature | "
            "ForEach-Object { $_.InstanceName + '|' + $_.CurrentTemperature }"
        )
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=3, check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line or "|" not in line:
                    continue
                parts = line.split("|", 1)
                inst, temp_raw = parts[0].strip(), parts[1].strip()
                try:
                    raw_k = float(temp_raw)
                    temp_c = (raw_k - 2732) / 10.0
                    if 0 < temp_c <= 150:
                        clean_id = inst.replace("\\", "_").replace(":", "_").replace(" ", "_").lower()
                        label = inst.split("\\")[-1] or inst
                        sensor_id = f"wmi:acpi:{clean_id}:temp"
                        if sensor_id in seen_ids:
                            sensor_id = f"{sensor_id}~{len(records)}"
                        seen_ids.add(sensor_id)
                        records.append(_record_from_value(
                            "acpi", f"ThermalZone {label}", round(temp_c, 1),
                            sensor_id=sensor_id,
                            channel="acpi",
                            source="wmi",
                            runtime_path=None,
                        ))
                except (ValueError, TypeError):
                    continue
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        pass

    # 2. NVIDIA GPU via nvidia-smi
    if include_nvidia and not any(rec["name"] == "nvidia" for rec in records):
        smi = shutil.which("nvidia-smi")
        if not smi:
            for cand in (
                pathlib.Path("C:\\Windows\\System32\\nvidia-smi.exe"),
                pathlib.Path("C:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe"),
            ):
                if cand.is_file():
                    smi = str(cand)
                    break
        if smi:
            try:
                res = subprocess.run(
                    [smi, "--query-gpu=index,temperature.gpu,name,pci.bus_id",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=2, check=False,
                )
                if res.returncode == 0:
                    for parsed in parse_nvidia_smi_records(res.stdout):
                        records.append(_record_from_value(
                            "nvidia", f"GPU {parsed['index']} · {parsed['name']}", parsed["temp"],
                            sensor_id=parsed["id"],
                            channel="gpu",
                            source="nvidia-smi",
                            runtime_path=None,
                        ))
            except (FileNotFoundError, OSError, subprocess.SubprocessError):
                pass

    return records


def _scan_sensors_windows(include_nvidia=True):
    records = _scan_sensor_records_windows(include_nvidia=include_nvidia)
    return [{"name": r["name"], "label": r["label"], "temp": r["temp"]} for r in records]


def scan_sensor_records(demo=False, include_nvidia=True, hwmon_dir="/sys/class/hwmon"):
    """Discover temperature sensors with stable persistent identities.

    IDs follow ``hwmon:<chip>:<device-identity>:<channel>`` (and
    ``nvidia:<pci-bus-id>``) so curves and rules survive hwmon renumbering
    across boots. The volatile hwmon index is kept as runtime metadata only.
    """
    if demo:
        import math
        import time as _time

        records = []
        for index, (name, label, offset, period) in enumerate((
            ("k10temp", "Tctl", 62.0, 18),
            ("k10temp", "Tccd1", 58.0, 21),
            ("amdgpu", "edge", 54.0, 23),
        )):
            value = offset + (10 if index == 0 else 8 if index == 1 else 7) * math.sin(_time.monotonic() / period)
            records.append(_record_from_value(
                name, label, value,
                sensor_id=f"hwmon:{name}:demo:temp{index + 1}",
                channel=f"temp{index + 1}",
                source="demo",
                runtime_path=None,
            ))
        return records

    if sys.platform == "win32":
        return _scan_sensor_records_windows(include_nvidia=include_nvidia)

    records = []
    seen_ids = set()
    base = pathlib.Path(hwmon_dir)
    if base.exists():
        for hwmon in sorted(base.glob("hwmon*")):
            try:
                name = (hwmon / "name").read_text().strip()
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            devid = _hwmon_device_id(hwmon)
            try:
                index_number = int(hwmon.name.replace("hwmon", ""))
            except ValueError:
                index_number = None
            for path in sorted(hwmon.glob("temp*_input")):
                try:
                    temp = int(path.read_text().strip()) / 1000
                except (OSError, ValueError, UnicodeDecodeError):
                    continue
                if not 0 < temp <= 150:
                    continue
                label_path = path.with_name(path.name.replace("_input", "_label"))
                try:
                    label = label_path.read_text().strip() if label_path.exists() else path.stem.replace("_input", "")
                except (OSError, UnicodeDecodeError):
                    label = path.stem.replace("_input", "")
                channel = path.stem.replace("_input", "")
                sensor_id = f"hwmon:{name}:{devid}:{channel}"
                if sensor_id in seen_ids:
                    sensor_id = f"{sensor_id}~{hwmon.name}"
                seen_ids.add(sensor_id)
                records.append(_record_from_value(
                    name, label, temp,
                    sensor_id=sensor_id,
                    channel=channel,
                    source="hwmon",
                    runtime_path=str(path),
                    hwmon_index=index_number,
                ))
    if include_nvidia and not any(rec["name"] == "nvidia" for rec in records):
        import shutil
        import subprocess

        if shutil.which("nvidia-smi"):
            try:
                result = subprocess.run(
                    ["nvidia-smi", "--query-gpu=index,temperature.gpu,name,pci.bus_id",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=2, check=False,
                )
                if result.returncode == 0:
                    for parsed in parse_nvidia_smi_records(result.stdout):
                        records.append(_record_from_value(
                            "nvidia", f"GPU {parsed['index']} · {parsed['name']}", parsed["temp"],
                            sensor_id=parsed["id"],
                            channel="gpu",
                            source="nvidia-smi",
                            runtime_path=None,
                        ))
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                pass
    return records
