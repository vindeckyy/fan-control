#!/usr/bin/env python3
"""Staged thermal control pipeline (pure logic, no hardware I/O).

Given the same configuration, sensor snapshot, rule state, schedule state and
runtime overrides, :func:`evaluate` always produces the same decision. The
controller owns all I/O; this module decides.

Effective control precedence (highest first):

    hardware safety / critical-temperature protection
        temporary diagnostic or fan-test override
        automation rule overlay
        schedule overlay
        persistent user-selected mode/profile
        curve / manual target resolution
        fan-specific min/max constraints
        hysteresis / write suppression
"""

from __future__ import annotations

import datetime

import fan_rules
from fan_policy import (
    FAULT_MISS_THRESHOLD,
    PROFILES,
    SAFE_MAX_DUTY,
    _valid_temp,
    select_control_temp,
    target_duty,
)

VALID_PROFILES = tuple(PROFILES.keys())


def _profile_curve_points(profile, curves):
    """Resolve the global policy curve for a profile name.

    Built-in profiles are code-owned; ``custom`` maps to the migrated
    ``legacy_custom`` curve with a safe fallback when it is missing.
    """
    if profile in PROFILES and profile != "custom":
        return list(PROFILES[profile])
    entry = (curves or {}).get("legacy_custom")
    if entry and entry.get("points"):
        return [list(p) for p in entry["points"]]
    return list(PROFILES["custom"])


def _curve_points(curve_ref, curves):
    entry = (curves or {}).get(curve_ref)
    if entry and entry.get("points"):
        return [list(p) for p in entry["points"]], True
    return list(PROFILES["balanced"]), False


def _side_temp(side, context):
    """Side alias temperature with legacy cross-side fallback."""
    primary = context.get("cpu_temp") if side == "cpu" else context.get("gpu_temp")
    if _valid_temp(primary):
        return primary
    other = context.get("gpu_temp") if side == "cpu" else context.get("cpu_temp")
    return other if _valid_temp(other) else None


def _curve_temp(curve, fan_side, context, warnings):
    source = curve.get("temp_source") if isinstance(curve, dict) else None
    temps = context.get("temps") or {}
    if source in ("cpu", "gpu"):
        return _side_temp(source, context)
    if source == "max" or source is None:
        return select_control_temp(context.get("cpu_temp"), context.get("gpu_temp"))
    if isinstance(source, str) and source in temps:
        value = temps[source]
        if _valid_temp(value):
            return float(value)
        warnings.append(f"curve temp_source {source!r} unavailable; using hottest valid sensor")
        return select_control_temp(context.get("cpu_temp"), context.get("gpu_temp"))
    if isinstance(source, str):
        warnings.append(f"curve temp_source {source!r} unavailable; using hottest valid sensor")
    return select_control_temp(context.get("cpu_temp"), context.get("gpu_temp"))


def _manual_scaled_target(target, cap):
    if isinstance(target, bool):
        return 0
    try:
        value = float(target)
    except (TypeError, ValueError, OverflowError):
        return 0
    if value != value or value in (float("inf"), float("-inf")):
        return 0
    return round(max(0.0, min(100.0, value)) * cap / 100)


def _fan_bounds(cfg):
    min_raw, max_raw = cfg.get("min_duty", 0), cfg.get("max_duty", 100)
    try:
        min_duty = int(min_raw)
    except (TypeError, ValueError):
        min_duty = 0
    try:
        max_duty = int(max_raw)
    except (TypeError, ValueError):
        max_duty = 100
    if isinstance(min_raw, bool):
        min_duty = 0
    if isinstance(max_raw, bool):
        max_duty = 100
    return max(0, min(100, min_duty)), max(0, min(100, max_duty))


def _active_override(overrides, fan_id, now_ts):
    entry = (overrides or {}).get(fan_id)
    try:
        expires = float(entry.get("expires", 0)) if isinstance(entry, dict) else 0
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(entry, dict) and expires > now_ts:
        return entry
    return None


def _fmt_clock(now):
    return now.strftime("%H:%M")


def evaluate(config, context, runtime):
    """Run the full staged pipeline. Returns the tick result dict."""
    now = runtime.get("now") or datetime.datetime.now()
    now_ts = now.timestamp()
    curves = config.get("curves") or {}
    fans_cfg = config.get("fans") or {}
    safety = config.get("safety") or {}
    warnings = []

    schedule_result = fan_rules.evaluate_schedule(config.get("schedule"), now)
    rule_result = fan_rules.evaluate_rules(
        config.get("rules") or [],
        context,
        now,
        runtime.get("rule_state"),
        startup=bool(runtime.get("startup")),
    )

    configured_mode = config.get("mode") if config.get("mode") in ("auto", "manual", "released") else "manual"
    configured_profile = config.get("profile") if config.get("profile") in VALID_PROFILES else "balanced"

    overlay = rule_result["overlay"]
    schedule = schedule_result
    effective_mode = overlay["mode"] or schedule["mode"] or configured_mode
    effective_profile = overlay["profile"] or schedule["profile"] or configured_profile
    if overlay["profile"] or overlay["mode"]:
        policy_source = f"rule:{','.join(rule_result['active'])}" if rule_result["active"] else "rule"
    elif schedule["profile"] or schedule["mode"]:
        policy_source = f"schedule:{','.join(schedule['active'])}"
    else:
        policy_source = "configured"

    cpu_temp = context.get("cpu_temp")
    gpu_temp = context.get("gpu_temp")
    control_temp = select_control_temp(cpu_temp, gpu_temp)
    missing_temp = control_temp is None
    firmware_fallback = safety.get("firmware_fallback", True)
    missing_count = int(runtime.get("missing_count") or 0)

    critical = (
        control_temp is not None
        and control_temp >= safety.get("critical_temp", 95)
        and configured_mode != "released"
    )

    cap = int(safety.get("global_max_duty", SAFE_MAX_DUTY))
    hysteresis = int(safety.get("hysteresis", 0))
    overrides = runtime.get("overrides") or {}
    duties_overlay = overlay["duties"]
    current_duties = context.get("current_duties") or {}

    result = {
        "action": "idle",
        "writes": {},
        "duties": {},
        "critical": critical,
        "missing_temp": missing_temp and effective_mode == "auto",
        "effective_mode": effective_mode,
        "effective_profile": effective_profile,
        "profile_source": (
            "rule" if overlay["profile"] else ("schedule" if schedule["profile"] else "configured")
        ),
        "mode_source": (
            "rule" if overlay["mode"] else ("schedule" if schedule["mode"] else "configured")
        ),
        "policy_source": policy_source,
        "active_rules": rule_result["active"],
        "rule_states": rule_result["states"],
        "rule_runtime": rule_result["runtime"],
        "notifications": rule_result["notifications"],
        "commands": rule_result["commands"],
        "warnings": warnings + rule_result["warnings"],
        "schedule_active": schedule["active"],
        "per_fan": [],
        "control_temp": control_temp,
        "cpu_temp": cpu_temp,
        "gpu_temp": gpu_temp,
    }

    if configured_mode == "released" or (effective_mode == "released" and not critical):
        result["effective_mode"] = "released"
        if configured_mode != "released":
            result["action"] = "release"
        return result

    if missing_temp and effective_mode == "auto":
        if firmware_fallback and missing_count >= FAULT_MISS_THRESHOLD:
            result["action"] = "release"
        result["missing_temp"] = True
        return result
    fans_present = [int(f) for f in context.get("fans_present") or ()]
    profile_points = None

    for fan in fans_present:
        fan_id = f"fan{fan}"
        cfg = fans_cfg.get(fan_id) or {}
        if cfg.get("enabled") is False and not critical:
            continue
        control = cfg.get("control") or {}
        ctype = control.get("type", "linked")
        try:
            previous_duty = int(current_duties.get(fan, current_duties.get(str(fan), 0)) or 0)
        except (TypeError, ValueError, OverflowError):
            previous_duty = 0
        trace = {
            "timestamp": now.isoformat(),
            "fan_id": fan_id,
            "sensor_id": None,
            "source_temp": None,
            "configured_curve": None,
            "requested_duty": None,
            "bounded_duty": None,
            "final_duty": None,
            "previous_duty": previous_duty,
            "effective_mode": effective_mode,
            "effective_profile": effective_profile,
            "policy_source": policy_source,
            "safety_override": critical,
            "write_performed": False,
            "write_suppressed_reason": None,
            "control_type": ctype,
        }

        duty = None
        if critical:
            duty = SAFE_MAX_DUTY
            trace["sensor_id"] = "safety"
            trace["source_temp"] = control_temp
        else:
            override = _active_override(overrides, fan_id, now_ts)
            if override is not None:
                try:
                    raw_duty = float(override["duty"])
                except (TypeError, ValueError, OverflowError, KeyError):
                    raw_duty = 0
                duty = max(0, min(SAFE_MAX_DUTY, int(round(raw_duty))))
                trace["sensor_id"] = "override"
            elif fan_id in duties_overlay or str(fan) in duties_overlay:
                overlay_duty = duties_overlay.get(fan_id, duties_overlay.get(str(fan)))
                try:
                    raw_overlay = float(overlay_duty)
                except (TypeError, ValueError, OverflowError):
                    raw_overlay = 0
                duty = max(0, min(SAFE_MAX_DUTY, int(round(raw_overlay))))
                trace["sensor_id"] = f"rule:{policy_source}"
            elif effective_mode == "manual":
                target = control.get("target", 0)
                duty = _manual_scaled_target(target, cap)
            elif effective_mode == "auto":
                if ctype == "manual":
                    target = control.get("target", 0)
                    duty = _manual_scaled_target(target, cap)
                else:
                    if ctype == "curve":
                        curve_ref = control.get("curve_ref")
                        points, found = _curve_points(curve_ref, curves)
                        curve_cfg = curves.get(curve_ref) or {}
                        if not found:
                            result["warnings"].append(
                                f"{fan_id}: curve {curve_ref!r} missing; using safe default curve"
                            )
                            curve_cfg = {"temp_source": "max"}
                        trace["configured_curve"] = curve_ref if found else "safe-default"
                        temp = _curve_temp(curve_cfg, "cpu" if fan == 1 else "gpu", context, result["warnings"])
                        trace["sensor_id"] = curve_cfg.get("temp_source") if found else "max"
                    elif ctype == "profile":
                        if profile_points is None:
                            profile_points = _profile_curve_points(effective_profile, curves)
                        points = profile_points
                        side = control.get("temp_source") or ("cpu" if fan == 1 else "gpu")
                        if side not in ("cpu", "gpu"):
                            result["warnings"].append(
                                f"{fan_id}: unknown temp_source {side!r}; using side default"
                            )
                            side = "cpu" if fan == 1 else "gpu"
                        temp = _side_temp(side, context)
                        trace["configured_curve"] = f"profile:{effective_profile}"
                        trace["sensor_id"] = side
                    else:  # linked: hottest eligible control source
                        if profile_points is None:
                            profile_points = _profile_curve_points(effective_profile, curves)
                        points = profile_points
                        temp = control_temp
                        trace["configured_curve"] = f"profile:{effective_profile}"
                        trace["sensor_id"] = "max"

                    trace["source_temp"] = temp
                    if temp is None or not _valid_temp(temp):
                        result["warnings"].append(
                            f"{fan_id}: no trustworthy temperature source; no write"
                        )
                        continue  # no trustworthy source for this fan: no write
                    requested = target_duty(temp, points, max_duty=cap, critical_temp=safety.get("critical_temp", 95))
                    trace["requested_duty"] = int(requested)
                    duty = int(requested)

        if duty is None:
            continue
        if trace["requested_duty"] is None:
            trace["requested_duty"] = int(duty)
        min_duty, max_duty = _fan_bounds(cfg)
        bounded = max(min_duty, min(max_duty, int(duty)))
        if not critical:
            bounded = min(bounded, cap, SAFE_MAX_DUTY)
        trace["bounded_duty"] = bounded
        final = SAFE_MAX_DUTY if critical else bounded
        trace["final_duty"] = final
        result["duties"][fan] = final

        previous = trace["previous_duty"]
        if critical or abs(previous - final) >= hysteresis:
            result["writes"][fan] = final
        else:
            trace["write_suppressed_reason"] = "hysteresis"
        result["per_fan"].append(trace)

    result["action"] = "write" if result["writes"] else "idle"
    return result
