#!/usr/bin/env python3
"""Deterministic rules engine and schedule evaluator.

Both produce *transient runtime policy overlays*: they never rewrite the
persistent configuration. Evaluation is pure — all hardware I/O and clocks
enter through the arguments, so the same inputs always yield the same
decision.
"""

from __future__ import annotations

import datetime
import math

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Rule runtime states, exposed to the Automation page and Diagnostics.
STATE_INACTIVE = "inactive"
STATE_SUSTAINING = "sustaining"
STATE_ACTIVE = "active"
STATE_COOLDOWN = "cooldown"
STATE_APPLIED = "applied"
STATE_SHADOWED = "shadowed"

OVERLAY_PROFILE = "profile"
OVERLAY_MODE = "mode"
OVERLAY_DUTIES = "duties"


def fresh_rule_state():
    """Empty per-rule runtime state (never persisted to configuration)."""
    return {}


def _rule_state(state, rule_id):
    entry = state.get(rule_id)
    if not isinstance(entry, dict):
        entry = {
            "consecutive_match_count": 0,
            "active_since": None,
            "last_fired_at": None,
            "cooldown_until": None,
            "startup_consumed": False,
        }
        state[rule_id] = entry
    # Prune keys so stale fields cannot leak between rule edits.
    return {key: entry.get(key) for key in (
        "consecutive_match_count", "active_since", "last_fired_at",
        "cooldown_until", "startup_consumed",
    )}


def _valid_temp_number(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return 0 < number <= 150


def resolve_sensor_value(sensor_ref, context):
    """Resolve a policy selector (alias or stable sensor id) to a temperature.

    Returns (value, available). Unknown references report unavailable so the
    caller can warn instead of silently reading zero.
    """
    if not sensor_ref:
        return None, False
    sensor_ref = str(sensor_ref)
    temps = context.get("temps") or {}
    if sensor_ref in temps:
        value = temps[sensor_ref]
        if _valid_temp_number(value):
            return float(value), True
        return None, False
    if sensor_ref == "max":
        valid = [float(v) for v in temps.values() if _valid_temp_number(v)]
        return (max(valid), True) if valid else (None, False)
    return None, False


def _in_time_window(now, days, start, end):
    """Weekly window test with explicit midnight-crossing semantics.

    ``23:00 -> 07:00`` on listed day D covers D 23:00 through D+1 07:00.
    """
    try:
        start_parts = str(start).strip().split(":")
        end_parts = str(end).strip().split(":")
        start_min = int(start_parts[0]) * 60 + int(start_parts[1])
        end_min = int(end_parts[0]) * 60 + int(end_parts[1])
    except (TypeError, ValueError, IndexError):
        return False
    minutes = now.hour * 60 + now.minute
    today = WEEKDAYS[now.weekday()]
    yesterday = WEEKDAYS[(now.weekday() - 1) % 7]
    if start_min <= end_min:
        return today in days and start_min <= minutes < end_min
    return (today in days and minutes >= start_min) or (yesterday in days and minutes < end_min)


def local_now(timezone="local"):
    """Current time under zoneinfo semantics; 'local' means system local time."""
    if not timezone or timezone == "local":
        return datetime.datetime.now()
    try:
        from zoneinfo import ZoneInfo

        return datetime.datetime.now(ZoneInfo(timezone))
    except Exception:  # noqa: BLE001 - invalid zone names fall back to local
        return datetime.datetime.now()


def rule_triggered(rule, context, now):
    """Pure trigger test. Returns (matched, unavailable_ref or None)."""
    trigger = rule.get("trigger") or {}
    ttype = trigger.get("type")
    if ttype == "temp_above" or ttype == "temp_below":
        value, available = resolve_sensor_value(trigger.get("sensor"), context)
        if not available or value is None:
            return False, trigger.get("sensor")
        threshold = trigger.get("value")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            return False, trigger.get("sensor")
        return (value > threshold) if ttype == "temp_above" else (value < threshold), None
    if ttype == "rpm_below":
        rpm = (context.get("rpm") or {}).get(str(trigger.get("fan")))
        threshold = trigger.get("value")
        if rpm is None or not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            return False, trigger.get("fan")
        return rpm < threshold, None
    if ttype == "time_range":
        days = trigger.get("days")
        if not isinstance(days, list) or not days:
            return False, None
        if not trigger.get("start") or not trigger.get("end"):
            return False, None
        tz = trigger.get("timezone", "local")
        moment = now
        if tz != "local":
            try:
                from zoneinfo import ZoneInfo

                zone = ZoneInfo(tz)
                if moment is None:
                    moment = local_now(tz)
                elif moment.tzinfo is None:
                    moment = moment.replace(tzinfo=zone)
                else:
                    moment = moment.astimezone(zone)
            except Exception:  # noqa: BLE001 - bad zone falls back to given time
                moment = now if now is not None else local_now()
        if moment is None:
            moment = local_now()
        return _in_time_window(moment, days, trigger["start"], trigger["end"]), None
    if ttype == "on_startup":
        return True, None
    return False, trigger.get("type")


def _canonical_fan(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if text.startswith("fan"):
        text = text[3:]
    try:
        index = int(text)
    except (TypeError, ValueError):
        return None
    if index not in (1, 2, 3):
        return None
    return f"fan{index}"


def _action_key(rule):
    action = rule.get("action") or {}
    atype = action.get("type")
    if atype == "set_duty":
        fan = _canonical_fan(action.get("fan"))
        return f"duty:{fan if fan else 'all'}"
    return atype


def _action_keys(rule, fans_present):
    action = rule.get("action") or {}
    if action.get("type") == "set_duty" and not _canonical_fan(action.get("fan")):
        keys = []
        for fan in fans_present:
            canonical = _canonical_fan(fan)
            keys.append(f"duty:{canonical}" if canonical else f"duty:fan{fan}")
        return keys
    if action.get("type") in ("notify", "run_command"):
        return [f"{action['type']}:{rule['id']}"]
    return [_action_key(rule)]


def _arbitrate(fired, order, fans_present):
    """Deterministic conflict resolution: safety class, priority, config order."""
    ranked = sorted(
        fired,
        key=lambda rule: (
            -(rule.get("safety_class") or 0),
            -(rule.get("priority") or 0),
            order.get(rule["id"], 0),
        ),
    )
    winners = {}
    for rule in ranked:
        for key in _action_keys(rule, fans_present):
            winners.setdefault(key, rule)
    return winners, ranked


def _cooldown_timestamp(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime.datetime):
        try:
            return value.timestamp()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _now_timestamp(moment):
    if moment is None:
        return datetime.datetime.now().timestamp()
    if isinstance(moment, datetime.datetime):
        try:
            return moment.timestamp()
        except (OverflowError, OSError, ValueError):
            return datetime.datetime.now().timestamp()
    return datetime.datetime.now().timestamp()


def _in_cooldown(cooldown_until, now):
    end = _cooldown_timestamp(cooldown_until)
    if end is None:
        return False
    return _now_timestamp(now) < end


def _cooldown_until(now, cooldown_seconds):
    try:
        base = _now_timestamp(now)
    except Exception:  # noqa: BLE001 - fall back to wall clock
        base = datetime.datetime.now().timestamp()
    return base + max(0, cooldown_seconds)


def evaluate_rules(rules, context, now, state=None, *, startup=False):
    """Evaluate rules against a sensor snapshot.

    Overlays are derived from every *active* rule each tick, so a rule keeps
    its effect while it applies rather than only on its transition tick.
    Returns a result dict with per-rule runtime states, the resulting policy
    overlay, and side-effect requests (notifications, command refs). The
    caller owns the state dict; the updated copy is returned separately.
    """
    state = dict(state or {})
    order = {rule.get("id"): index for index, rule in enumerate(rules)}
    applied_ids, active_rules, sustaining_ids, cooldown_ids = set(), [], set(), set()
    notifications, commands = [], []
    warnings = []
    profile_overlay = None
    mode_overlay = None
    duties_overlay = {}

    for rule in rules:
        rule_id = rule.get("id")
        if not rule_id:
            continue
        if not rule.get("enabled", True):
            state.pop(rule_id, None)
            continue
        entry = _rule_state(state, rule_id)
        condition = rule.get("condition") or {}
        sustain_ticks = max(0, int(condition.get("sustain_ticks") or 0))
        cooldown_seconds = max(0, int(condition.get("cooldown_seconds") or 0))

        if rule.get("trigger", {}).get("type") == "on_startup":
            if not startup:
                continue
            if entry.get("startup_consumed"):
                continue
            state[rule_id] = {**entry, "startup_consumed": True, "active_since": now, "last_fired_at": now}
            active_rules.append(rule)
            applied_ids.add(rule_id)
            continue

        matched, missing_ref = rule_triggered(rule, context, now)
        if missing_ref:
            warnings.append(f"rule {rule_id!r} references unavailable sensor {missing_ref!r}")

        now_active = entry.get("active_since") is not None
        cooling = _in_cooldown(entry.get("cooldown_until"), now)

        if matched:
            if now_active:
                entry["consecutive_match_count"] = int(entry.get("consecutive_match_count") or 0) + 1
                active_rules.append(rule)
            elif cooling:
                cooldown_ids.add(rule_id)
            else:
                entry["consecutive_match_count"] = int(entry.get("consecutive_match_count") or 0) + 1
                if entry["consecutive_match_count"] >= max(1, sustain_ticks):
                    entry["active_since"] = now
                    entry["last_fired_at"] = now
                    entry["consecutive_match_count"] = 0
                    entry["cooldown_until"] = (
                        _cooldown_until(now, cooldown_seconds)
                        if cooldown_seconds
                        else None
                    )
                    active_rules.append(rule)
                    applied_ids.add(rule_id)
                else:
                    sustaining_ids.add(rule_id)
        else:
            entry["consecutive_match_count"] = 0
            entry["active_since"] = None
            if cooling:
                cooldown_ids.add(rule_id)
        state[rule_id] = entry

    fans_present = context.get("fans_present") or ()
    winners, ranked = _arbitrate(active_rules, order, fans_present)
    active_ids = {rule["id"] for rule in active_rules}
    shadowed_ids = {rule["id"] for rule in ranked
                    if not any(winners.get(key) is rule for key in _action_keys(rule, fans_present))}

    for rule in active_rules:
        if rule["id"] in shadowed_ids:
            continue
        action = rule.get("action") or {}
        atype = action.get("type")
        if atype == "set_profile" and action.get("profile") in ("silent", "balanced", "performance", "custom"):
            profile_overlay = action["profile"]
        elif atype == "set_mode" and action.get("mode") in ("auto", "manual", "released"):
            mode_overlay = action["mode"]
        elif atype == "set_duty":
            pct = action.get("pct")
            if isinstance(pct, (int, float)) and not isinstance(pct, bool) and 0 <= pct <= 100:
                fan = _canonical_fan(action.get("fan"))
                if fan:
                    duties_overlay[fan] = int(pct)
                else:
                    for fan_id in context.get("fans_present") or ():
                        canonical = _canonical_fan(fan_id) or f"fan{fan_id}"
                        if winners.get(f"duty:{canonical}") is rule:
                            duties_overlay[canonical] = int(pct)
        elif atype == "notify":
            notifications.append({"rule": rule["id"], "message": str(action.get("message") or rule.get("name") or "")})
        elif atype == "run_command":
            commands.append({"rule": rule["id"], "command_ref": action.get("command_ref")})

    def _state_label(rule_id):
        if rule_id in shadowed_ids:
            return STATE_SHADOWED
        if rule_id in applied_ids:
            return STATE_APPLIED
        if rule_id in active_ids:
            return STATE_ACTIVE
        if rule_id in sustaining_ids:
            return STATE_SUSTAINING
        if rule_id in cooldown_ids:
            return STATE_COOLDOWN
        return STATE_INACTIVE

    return {
        "states": {rule["id"]: _state_label(rule["id"]) for rule in rules if rule.get("id")},
        "runtime": state,
        "overlay": {
            OVERLAY_PROFILE: profile_overlay,
            OVERLAY_MODE: mode_overlay,
            OVERLAY_DUTIES: duties_overlay,
        },
        "applied": sorted(applied_ids),
        "active": sorted(active_ids),
        "shadowed": sorted(shadowed_ids),
        "notifications": notifications,
        "commands": commands,
        "warnings": warnings,
    }


def evaluate_schedule(schedule, now=None):
    """Resolve the schedule overlay. Later entries win on overlap."""
    result = {"active": [], "profile": None, "mode": None}
    if not schedule or not schedule.get("enabled"):
        return result
    tz_name = schedule.get("timezone", "local")
    if now is None:
        now = local_now(tz_name)
    elif tz_name and tz_name != "local":
        try:
            from zoneinfo import ZoneInfo

            zone = ZoneInfo(tz_name)
            if now.tzinfo is None:
                now = now.replace(tzinfo=zone)
            else:
                now = now.astimezone(zone)
        except Exception:  # noqa: BLE001 - bad zone falls back to given time
            pass
    for item in schedule.get("items") or []:
        days = item.get("days")
        if not isinstance(days, list) or not _in_time_window(now, days, item.get("start"), item.get("end")):
            continue
        result["active"].append(item.get("id"))
        if item.get("profile"):
            result["profile"] = item["profile"]
        if item.get("mode"):
            result["mode"] = item["mode"]
    return result


def validate_rule_payload(rule):
    """Structural validation for rules.set. Raises ValueError with a message."""
    from fan_policy import ACTION_TYPES, PROFILES, TRIGGER_TYPES, _valid_hhmm

    if not isinstance(rule, dict):
        raise ValueError("rule must be an object")
    rule_id = rule.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        raise ValueError("rule id must be a non-empty string")
    trigger = rule.get("trigger")
    if not isinstance(trigger, dict) or trigger.get("type") not in TRIGGER_TYPES:
        raise ValueError(f"rule {rule_id!r}: trigger.type must be one of {', '.join(TRIGGER_TYPES)}")
    action = rule.get("action")
    if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
        raise ValueError(f"rule {rule_id!r}: action.type must be one of {', '.join(ACTION_TYPES)}")
    if action.get("type") == "run_command":
        if not isinstance(action.get("command_ref"), str) or not action["command_ref"].strip():
            raise ValueError("run_command requires a command_ref from the administrator allowlist")
    if action.get("type") == "set_duty":
        pct = action.get("pct")
        if not isinstance(pct, (int, float)) or isinstance(pct, bool) or not 0 <= pct <= 100:
            raise ValueError("set_duty requires pct 0-100")
    if trigger.get("type") in ("temp_above", "temp_below"):
        value = trigger.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 150:
            raise ValueError("temperature triggers require a numeric value")
        if not isinstance(trigger.get("sensor"), str) or not trigger["sensor"]:
            raise ValueError("temperature triggers require a sensor selector")
    if trigger["type"] == "rpm_below":
        value = trigger.get("value")
        if _canonical_fan(trigger.get("fan")) is None or not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 20000:
            raise ValueError("RPM triggers require a valid fan and threshold 0-20000")
    if trigger["type"] == "time_range":
        if not _valid_hhmm(trigger.get("start")) or not _valid_hhmm(trigger.get("end")):
            raise ValueError("time triggers require start/end in HH:MM")
        days = trigger.get("days")
        if not isinstance(days, list) or not days or not all(d in WEEKDAYS for d in days):
            raise ValueError("time triggers require valid weekdays")
    if action["type"] == "set_profile" and action.get("profile") not in PROFILES:
        raise ValueError("unknown rule profile")
    if action["type"] == "set_mode" and action.get("mode") not in ("auto", "manual", "released"):
        raise ValueError("unknown rule mode")
    if action["type"] == "set_duty" and action.get("fan") is not None and _canonical_fan(action["fan"]) is None:
        raise ValueError("unknown action fan")
    condition = rule.get("condition")
    if condition is not None and not isinstance(condition, dict):
        raise ValueError("condition must be an object")
    return True
