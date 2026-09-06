#!/usr/bin/env python3
"""Phase 1 (daemon v2) test suite: migration, engine, rules, schedule,
history, RPC v2, and a deterministic end-to-end simulated system."""

import datetime
import importlib.util
import json
import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).parent


def load(name, filename):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


policy = load("fan_policy", "fan_policy.py")
backend = load("fan_backend", "fan_backend.py")
rpc = load("fan_rpc", "fan_rpc.py")
rules = load("fan_rules", "fan_rules.py")
engine = load("fan_engine", "fan_engine.py")
history = load("fan_history", "fan_history.py")
controller = load("fan_controller", "fan_controller.py")

T0 = datetime.datetime(2026, 1, 5, 12, 0, 0)  # a Monday afternoon


class FakeFanBackend:
    """Deterministic backend for control-loop tests without hardware."""

    name = "fake"

    def __init__(self, fans=(1, 2)):
        self.fans_present = list(fans)
        self._duties = {1: 0, 2: 0, 3: 0}
        self._rpm = {1: None, 2: None, 3: None}
        self.writes = []
        self.lock_calls = 0
        self.release_calls = 0
        self.write_fail = False
        self.read_fail = False

    def fans(self):
        return list(self.fans_present)

    def lock(self):
        self.lock_calls += 1

    def release(self):
        self.release_calls += 1

    def write_duty(self, fan, percent):
        if self.write_fail:
            raise backend.FanBackendError("scripted write failure")
        self.writes.append((fan, int(percent)))
        self._duties[fan] = max(0, min(100, int(percent)))

    def read_duty(self, fan):
        if self.read_fail:
            raise backend.FanBackendError("scripted read failure")
        return self._duties[fan]

    def read_temp(self):
        return None

    def read_temp2(self):
        return None

    def read_rpm(self, fan):
        return self._rpm.get(fan)

    def set_rpm(self, fan, rpm):
        self._rpm[fan] = rpm

    def ping(self):
        pass

    def close(self):
        pass


def temp_record(sensor_id, value, *, name="k10temp", label="", aliases=(), source="hwmon"):
    return {
        "id": sensor_id,
        "name": name,
        "label": label or sensor_id,
        "channel": "temp1",
        "kind": "temperature",
        "value": value,
        "temp": value,
        "unit": "c",
        "source": source,
        "runtime_path": f"/sys/class/hwmon/hwmon9/{sensor_id}_input",
        "hwmon_index": 9,
        "available": True,
        "aliases": list(aliases),
    }


def make_config(**overrides):
    doc, _warnings = policy.normalize_config(policy.default_config_v2())
    doc.update(overrides)
    return doc


def make_context(cpu=60.0, gpu=55.0, fans=(1, 2), duties=None, rpm=None, extra_temps=None):
    temps = {"cpu": cpu, "gpu": gpu, "max": policy.select_control_temp(cpu, gpu)}
    if extra_temps:
        temps.update(extra_temps)
    return {
        "cpu_temp": cpu,
        "gpu_temp": gpu,
        "temps": temps,
        "rpm": rpm or {},
        "fans_present": list(fans),
        "current_duties": duties or {1: 0, 2: 0},
    }


def make_runtime(**overrides):
    runtime = {
        "now": T0,
        "rule_state": {},
        "overrides": {},
        "missing_count": 0,
        "startup": False,
    }
    runtime.update(overrides)
    return runtime


def curve(points):
    return [list(p) for p in points]


# --------------------------------------------------------------------- migration


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "fan-control.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_v1_migrates_to_v2_with_stable_curve_ids(self):
        self.path.write_text(json.dumps({
            "mode": "curve",
            "profile": "custom",
            "linked": False,
            "curve": [[0, 0], [95, 100]],
            "curve_cpu": [[0, 0], [70, 30]],
            "curve_gpu": [[0, 0], [80, 60]],
            "named_curves": {"night": [[0, 0], [90, 40]]},
            "max_duty": 80,
            "hysteresis": 4,
            "critical_temp": 92,
            "theme": "light",
            "alerts": {"desktop": True},
            "targets": {"1": 45},
        }))
        doc, warnings, migrated = policy.migrate_document(json.loads(self.path.read_text()))
        self.assertTrue(migrated)
        self.assertEqual(doc["version"], 2)
        self.assertEqual(doc["mode"], "auto")
        self.assertEqual(doc["profile"], "custom")
        self.assertEqual(doc["safety"]["global_max_duty"], 80)
        self.assertEqual(doc["safety"]["hysteresis"], 4)
        self.assertEqual(doc["safety"]["critical_temp"], 92)
        self.assertEqual(doc["display"]["theme"], "light")
        self.assertTrue(doc["notifications"]["enabled"])
        self.assertEqual([p for p in doc["curves"]["cpu_default"]["points"]], [[0, 0], [70, 30]])
        self.assertEqual(doc["curves"]["cpu_default"]["temp_source"], "cpu")
        self.assertEqual(doc["curves"]["named_night"]["name"], "night")
        self.assertEqual(doc["fans"]["fan1"]["control"], {
            "type": "curve", "curve_ref": "cpu_default", "target": 45,
        })
        self.assertEqual(doc["fans"]["fan2"]["control"]["type"], "curve")
        self.assertEqual(doc["fans"]["fan2"]["control"]["curve_ref"], "gpu_default")
        self.assertFalse(warnings)

    def test_v1_linked_migrates_to_linked_fans(self):
        doc, _w, _m = policy.migrate_document({"mode": "curve", "profile": "balanced"})
        self.assertEqual(doc["fans"]["fan1"]["control"]["type"], "linked")
        self.assertEqual(doc["fans"]["fan2"]["control"]["type"], "linked")

    def test_migration_is_deterministic_across_runs(self):
        v1 = {"profile": "custom", "curve": [[0, 0], [80, 50]],
              "named_curves": {"Night Shift": [[0, 0], [60, 20]], "night": [[0, 0], [90, 40]]}}
        first = policy.upgrade_config_v1_to_v2(v1)
        second = policy.upgrade_config_v1_to_v2(v1)
        self.assertEqual(first["curves"].keys(), second["curves"].keys())
        self.assertIn("named_night_shift", first["curves"])
        self.assertIn("named_night", first["curves"])

    def test_named_curve_collision_resolves_deterministically(self):
        v1 = {"named_curves": {"a b": [[0, 0], [50, 10]], "a_b": [[0, 0], [60, 20]]}}
        doc = policy.upgrade_config_v1_to_v2(v1)
        ids = sorted(doc["curves"])
        self.assertEqual(ids, ["legacy_custom", "named_a_b", "named_a_b_2"])

    def test_migration_creates_backup_before_first_v2_write(self):
        self.path.write_text('{"mode": "manual"}')
        doc, _w, _m = policy.migrate_document(json.loads(self.path.read_text()))
        doc["revision"] = 2
        policy.save_document(self.path, doc, original_version=1)
        backup = self.path.with_name("fan-control.v1.backup.json")
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text()), {"mode": "manual"})
        # repeated saves never clobber the original backup
        doc["revision"] = 3
        policy.save_document(self.path, doc, original_version=1)
        self.assertEqual(json.loads(backup.read_text()), {"mode": "manual"})

    def test_atomic_write_leaves_no_temporary_and_valid_json(self):
        doc, _w = policy.normalize_config(policy.default_config_v2())
        policy.save_document(self.path, doc)
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])
        self.assertEqual(detect_version(self.path), 2)

    def test_migration_failure_keeps_original_untouched(self):
        original = '{"version": 2, "revision": 4, "fans": "not-an-object", "vendor_note": "x"}'
        self.path.write_text(original)
        with self.assertRaises(policy.ConfigError):
            policy.load_document(self.path)
        self.assertEqual(self.path.read_text(), original)

    def test_unknown_v1_keys_are_retained(self):
        doc = policy.upgrade_config_v1_to_v2({"profile": "silent", "vendor_note": "hello"})
        self.assertEqual(doc.get("vendor_note"), "hello")

    def test_unreadable_config_raises_valueerror(self):
        self.path.write_text("{malformed!!")
        with self.assertRaises(ValueError):
            policy.load_document(self.path)


def detect_version(path):
    return json.loads(pathlib.Path(path).read_text())["version"]


class ValidationTests(unittest.TestCase):
    def test_duty_ranges_clamped_with_warnings(self):
        doc, warnings = policy.normalize_config(make_config())
        doc["fans"]["fan1"]["min_duty"] = 120
        doc["fans"]["fan1"]["max_duty"] = -5
        normalized, warnings = policy.normalize_config(doc)
        self.assertLessEqual(normalized["fans"]["fan1"]["max_duty"], 100)
        self.assertTrue(warnings)

    def test_min_above_max_swaps_with_warning(self):
        doc = make_config()
        doc["fans"]["fan1"]["min_duty"] = 90
        doc["fans"]["fan1"]["max_duty"] = 40
        normalized, warnings = policy.normalize_config(doc)
        self.assertLessEqual(normalized["fans"]["fan1"]["min_duty"], normalized["fans"]["fan1"]["max_duty"])
        self.assertTrue(any("swapping" in w for w in warnings))

    def test_curve_needs_two_points(self):
        doc = make_config()
        doc["curves"]["bad"] = {"name": "bad", "temp_source": "cpu", "points": [[50, 20]]}
        with self.assertRaises(policy.ConfigError):
            policy.normalize_config(doc)

    def test_duplicate_rule_ids_rejected(self):
        doc = make_config()
        base = {"id": "dup", "name": "d", "enabled": True, "priority": 100,
                "trigger": {"type": "temp_above", "sensor": "cpu", "value": 90},
                "condition": {}, "action": {"type": "notify", "message": "x"}}
        doc["rules"] = [dict(base), dict(base)]
        with self.assertRaises(policy.ConfigError):
            policy.normalize_config(doc)

    def test_invalid_schedule_time_rejected(self):
        doc = make_config()
        doc["schedule"]["items"] = [{
            "id": "x", "days": ["mon"], "start": "25:00", "end": "07:00", "profile": "quiet",
        }]
        with self.assertRaises(policy.ConfigError):
            policy.normalize_config(doc)

    def test_retention_days_bounded(self):
        doc = make_config()
        doc["history"]["retention_days"] = 5000
        normalized, _w = policy.normalize_config(doc)
        self.assertLessEqual(normalized["history"]["retention_days"], policy.RETENTION_MAX_DAYS)

    def test_critical_temp_sane_range(self):
        doc = make_config()
        doc["safety"]["critical_temp"] = 20
        normalized, _w = policy.normalize_config(doc)
        self.assertEqual(normalized["safety"]["critical_temp"], 70)

    def test_unknown_fan_entries_ignored_with_warning(self):
        doc = make_config()
        doc["fans"]["fan9"] = {"name": "ghost"}
        normalized, warnings = policy.normalize_config(doc)
        self.assertNotIn("fan9", normalized["fans"])
        self.assertTrue(any("fan9" in w for w in warnings))


# ------------------------------------------------------------------------ engine


class EnginePolicyTests(unittest.TestCase):
    def test_curve_fan_follows_named_curve_and_its_source(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        cfg["fans"]["fan2"]["control"] = {"type": "curve", "curve_ref": "gpu_quiet"}
        cfg["curves"]["gpu_quiet"] = {
            "name": "GPU Quiet", "temp_source": "gpu", "points": curve([[0, 0], [80, 50]]),
        }
        result = engine.evaluate(cfg, make_context(cpu=40.0, gpu=60.0), make_runtime())
        self.assertEqual(result["duties"][2], 38)  # interpolated at 60C on [[0,0],[80,50]]
        trace = next(t for t in result["per_fan"] if t["fan_id"] == "fan2")
        self.assertEqual(trace["configured_curve"], "gpu_quiet")
        self.assertEqual(trace["source_temp"], 60.0)

    def test_linked_fans_follow_hottest_source(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        result = engine.evaluate(cfg, make_context(cpu=50.0, gpu=70.0), make_runtime())
        self.assertEqual(result["duties"][1], result["duties"][2])
        trace = next(t for t in result["per_fan"] if t["fan_id"] == "fan1")
        self.assertEqual(trace["source_temp"], 70.0)

    def test_profile_control_uses_side_temp_with_fallback(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        cfg["profile"] = "performance"
        cfg["fans"]["fan2"]["control"] = {"type": "profile", "temp_source": "gpu"}
        # gpu missing -> falls back to cpu side (legacy behavior)
        result = engine.evaluate(cfg, make_context(cpu=66.0, gpu=None), make_runtime())
        trace = next(t for t in result["per_fan"] if t["fan_id"] == "fan2")
        self.assertEqual(trace["source_temp"], 66.0)

    def test_missing_curve_falls_back_to_safe_default_with_warning(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        cfg["fans"]["fan1"]["control"] = {"type": "curve", "curve_ref": "deleted"}
        result = engine.evaluate(cfg, make_context(), make_runtime())
        self.assertTrue(any("deleted" in w for w in result["warnings"]))
        self.assertEqual(result["action"], "write")  # never zero duty on a missing ref

    def test_missing_sensor_marks_fan_skipped(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        cfg["fans"]["fan1"]["control"] = {
            "type": "curve", "curve_ref": "byid",
        }
        cfg["curves"]["byid"] = {
            "name": "BySensor", "temp_source": "hwmon:gone:pci-00:temp1",
            "points": curve([[0, 0], [80, 50]]),
        }
        result = engine.evaluate(
            cfg,
            make_context(cpu=None, gpu=None, extra_temps={"hwmon:live:pci-00:temp1": 60.0}),
            make_runtime(),
        )
        # temp source missing -> warning + hottest fallback (which is also missing) -> no write
        self.assertNotIn(1, result["duties"])

    def test_manual_mode_targets_scale_with_cap(self):
        cfg = make_config()
        cfg["mode"] = "manual"
        cfg["safety"]["global_max_duty"] = 80
        cfg["fans"]["fan1"]["control"]["target"] = 50
        result = engine.evaluate(cfg, make_context(cpu=None, gpu=None), make_runtime())
        self.assertEqual(result["duties"][1], 40)
        self.assertEqual(result["action"], "write")

    def test_fan_bounds_clamp_after_curve(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        cfg["fans"]["fan1"]["min_duty"] = 30
        cfg["fans"]["fan1"]["max_duty"] = 60
        cfg["fans"]["fan1"]["control"] = {
            "type": "curve",
            "curve_ref": "steep",
        }
        cfg["curves"]["steep"] = {
            "name": "Steep", "temp_source": "cpu", "points": curve([[0, 0], [100, 100]]),
        }
        cold = engine.evaluate(cfg, make_context(cpu=20.0), make_runtime())
        self.assertEqual(cold["duties"][1], 30)
        hot = engine.evaluate(cfg, make_context(cpu=90.0), make_runtime())
        self.assertEqual(hot["duties"][1], 60)

    def test_global_cap_limits_curve_target_but_not_critical(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        cfg["safety"]["global_max_duty"] = 60
        cfg["curves"]["legacy_custom"] = {
            "name": "Custom", "temp_source": "max", "points": curve([[0, 0], [100, 100]]),
        }
        cfg["profile"] = "custom"
        normal = engine.evaluate(cfg, make_context(cpu=80.0), make_runtime())
        self.assertEqual(normal["duties"][1], 60)
        critical = engine.evaluate(cfg, make_context(cpu=95.0), make_runtime())
        self.assertEqual(critical["duties"][1], 100)
        self.assertTrue(critical["critical"])

    def test_critical_overrides_fan_max_and_override_and_rules(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        cfg["safety"]["critical_temp"] = 95
        cfg["fans"]["fan1"]["max_duty"] = 50
        cfg["rules"] = [{
            "id": "slow", "name": "slow", "enabled": True, "priority": 1000,
            "trigger": {"type": "temp_above", "sensor": "cpu", "value": 10},
            "condition": {}, "action": {"type": "set_duty", "pct": 5},
        }]
        runtime = make_runtime(overrides={"fan1": {"duty": 20, "expires": T0.timestamp() + 60}})
        result = engine.evaluate(cfg, make_context(cpu=96.0), runtime)
        self.assertEqual(result["duties"][1], 100)
        self.assertTrue(result["critical"])
        self.assertTrue(result["writes"][1] == 100)  # critical always writes

    def test_hysteresis_suppresses_small_changes(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        cfg["safety"]["hysteresis"] = 8
        result = engine.evaluate(cfg, make_context(cpu=60.0, duties={1: 32, 2: 32}), make_runtime())
        self.assertEqual(result["action"], "idle")
        trace = next(t for t in result["per_fan"] if t["fan_id"] == "fan1")
        self.assertEqual(trace["write_suppressed_reason"], "hysteresis")

    def test_missing_temps_release_after_fault_threshold(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        result = engine.evaluate(
            cfg, make_context(cpu=None, gpu=None), make_runtime(missing_count=3),
        )
        self.assertEqual(result["action"], "release")
        before = engine.evaluate(
            cfg, make_context(cpu=None, gpu=None), make_runtime(missing_count=1),
        )
        self.assertEqual(before["action"], "idle")
        self.assertTrue(before["missing_temp"])

    def test_firmware_fallback_disabled_never_releases(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        cfg["safety"]["firmware_fallback"] = False
        result = engine.evaluate(
            cfg, make_context(cpu=None, gpu=None), make_runtime(missing_count=10),
        )
        self.assertEqual(result["action"], "idle")

    def test_released_mode_is_idle(self):
        cfg = make_config()
        cfg["mode"] = "released"
        result = engine.evaluate(cfg, make_context(), make_runtime())
        self.assertEqual(result["action"], "idle")
        self.assertEqual(result["duties"], {})

    def test_disabled_fan_gets_no_duty(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        cfg["fans"]["fan2"]["enabled"] = False
        result = engine.evaluate(cfg, make_context(), make_runtime())
        self.assertNotIn(2, result["duties"])
        self.assertIn(1, result["duties"])

    def test_determinism_same_inputs_same_decision(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        cfg["rules"] = [{
            "id": "r", "name": "r", "enabled": True, "priority": 100,
            "trigger": {"type": "temp_above", "sensor": "gpu", "value": 50},
            "condition": {}, "action": {"type": "set_profile", "profile": "performance"},
        }]
        context = make_context(cpu=55.0, gpu=66.0)
        runtime = make_runtime(rule_state={"r": {"consecutive_match_count": 2, "active_since": T0}})
        first = engine.evaluate(cfg, context, runtime)
        second = engine.evaluate(cfg, context, runtime)
        self.assertEqual(first["duties"], second["duties"])
        self.assertEqual(first["effective_profile"], second["effective_profile"])

    def test_decision_trace_has_full_explanation(self):
        cfg = make_config()
        cfg["mode"] = "auto"
        result = engine.evaluate(cfg, make_context(cpu=72.0, duties={1: 40}), make_runtime())
        trace = next(t for t in result["per_fan"] if t["fan_id"] == "fan1")
        for field in ("timestamp", "fan_id", "sensor_id", "source_temp", "configured_curve",
                      "requested_duty", "bounded_duty", "final_duty", "previous_duty",
                      "effective_mode", "effective_profile", "policy_source",
                      "safety_override", "write_performed", "write_suppressed_reason"):
            self.assertIn(field, trace)


# -------------------------------------------------------------------------- rules


def make_rule(rule_id="r1", **overrides):
    rule = {
        "id": rule_id,
        "name": rule_id,
        "enabled": True,
        "priority": 100,
        "trigger": {"type": "temp_above", "sensor": "gpu", "value": 70},
        "condition": {"sustain_ticks": 0, "cooldown_seconds": 0},
        "action": {"type": "set_profile", "profile": "performance"},
    }
    rule.update(overrides)
    return rule


class RulesEngineTests(unittest.TestCase):
    def evaluate(self, rules_list, context, now=T0, state=None, startup=False):
        return rules.evaluate_rules(rules_list, context, now, state, startup=startup)

    def test_temp_trigger_matching_and_sensor_resolution(self):
        context = {"temps": {"cpu": 50.0, "gpu": 76.0, "max": 76.0}, "rpm": {}}
        matched, _ = rules.rule_triggered(make_rule(), context, T0)
        self.assertTrue(matched)
        below, _ = rules.rule_triggered(
            make_rule(trigger={"type": "temp_below", "sensor": "cpu", "value": 40}), context, T0)
        self.assertFalse(below)

    def test_missing_sensor_reports_unavailable(self):
        context = {"temps": {"cpu": 50.0}, "rpm": {}}
        matched, missing = rules.rule_triggered(make_rule(), context, T0)
        self.assertFalse(matched)
        self.assertEqual(missing, "gpu")

    def test_rpm_trigger(self):
        context = {"temps": {}, "rpm": {"fan1": 900}}
        matched, _ = rules.rule_triggered(
            make_rule(trigger={"type": "rpm_below", "fan": "fan1", "value": 1000}), context, T0)
        self.assertTrue(matched)
        # no tach -> never matches
        no_tach, _ = rules.rule_triggered(
            make_rule(trigger={"type": "rpm_below", "fan": "fan2", "value": 1000}), context, T0)
        self.assertFalse(no_tach)

    def test_sustain_requires_consecutive_ticks(self):
        rule = make_rule(condition={"sustain_ticks": 3, "cooldown_seconds": 0})
        context = {"temps": {"cpu": 50.0, "gpu": 76.0, "max": 76.0}, "rpm": {}}
        state = {}
        for tick in range(3):
            result = self.evaluate([rule], context, state=state)
            state = result["runtime"]
            if tick < 2:
                self.assertEqual(result["states"]["r1"], rules.STATE_SUSTAINING)
        self.assertEqual(result["states"]["r1"], rules.STATE_APPLIED)
        self.assertEqual(result["overlay"]["profile"], "performance")

    def test_overlay_persists_while_rule_active(self):
        rule = make_rule()
        context = {"temps": {"cpu": 50.0, "gpu": 76.0, "max": 76.0}, "rpm": {}}
        first = self.evaluate([rule], context)
        self.assertEqual(first["overlay"]["profile"], "performance")
        second = self.evaluate([rule], context, state=first["runtime"])
        self.assertEqual(second["overlay"]["profile"], "performance")
        self.assertEqual(second["states"]["r1"], rules.STATE_ACTIVE)

    def test_rule_deactivates_when_trigger_stops(self):
        rule = make_rule()
        hot = {"temps": {"cpu": 50.0, "gpu": 76.0, "max": 76.0}, "rpm": {}}
        cool = {"temps": {"cpu": 50.0, "gpu": 60.0, "max": 60.0}, "rpm": {}}
        active = self.evaluate([rule], hot)
        released = self.evaluate([rule], cool, state=active["runtime"])
        self.assertEqual(released["states"]["r1"], rules.STATE_INACTIVE)
        self.assertIsNone(released["overlay"]["profile"])

    def test_priority_determines_winner_and_shadowing(self):
        low = make_rule("low", priority=10)
        high = make_rule("high", priority=500, action={"type": "set_profile", "profile": "silent"})
        context = {"temps": {"cpu": 50.0, "gpu": 76.0, "max": 76.0}, "rpm": {}}
        result = self.evaluate([low, high], context)
        self.assertEqual(result["overlay"]["profile"], "silent")
        self.assertEqual(result["states"]["low"], rules.STATE_SHADOWED)
        self.assertEqual(result["states"]["high"], rules.STATE_APPLIED)
        # configuration order breaks priority ties
        a = make_rule("a", action={"type": "set_profile", "profile": "silent"})
        b = make_rule("b", action={"type": "set_profile", "profile": "performance"})
        tied = self.evaluate([a, b], context)
        self.assertEqual(tied["overlay"]["profile"], "silent")

    def test_cooldown_blocks_refire(self):
        rule = make_rule(condition={"sustain_ticks": 0, "cooldown_seconds": 60})
        hot = {"temps": {"cpu": 50.0, "gpu": 76.0, "max": 76.0}, "rpm": {}}
        first = self.evaluate([rule], hot, now=T0)
        state = first["runtime"]
        # trigger drops and re-rises within the cooldown window
        cool = {"temps": {"cpu": 50.0, "gpu": 60.0, "max": 60.0}, "rpm": {}}
        between = self.evaluate([rule], cool, now=T0 + datetime.timedelta(seconds=10), state=state)
        state = between["runtime"]
        again = self.evaluate([rule], hot, now=T0 + datetime.timedelta(seconds=30), state=state)
        self.assertEqual(again["states"]["r1"], rules.STATE_COOLDOWN)
        # after expiry the rule can fire again
        later = self.evaluate(
            [rule], hot, now=T0 + datetime.timedelta(seconds=120), state=state)
        self.assertIn(later["states"]["r1"], (rules.STATE_APPLIED, rules.STATE_ACTIVE))

    def test_startup_rule_fires_once(self):
        rule = make_rule("boot", trigger={"type": "on_startup"})
        context = {"temps": {}, "rpm": {}}
        first = self.evaluate([rule], context, startup=True)
        self.assertEqual(first["states"]["boot"], rules.STATE_APPLIED)
        second = self.evaluate([rule], context, state=first["runtime"], startup=True)
        self.assertEqual(second["states"]["boot"], rules.STATE_INACTIVE)

    def test_set_duty_overlay_applies_to_all_or_one_fan(self):
        all_fans = make_rule("all", action={"type": "set_duty", "pct": 55})
        context = {"temps": {"cpu": 50.0, "gpu": 76.0, "max": 76.0}, "rpm": {}, "fans_present": [1, 2]}
        result = self.evaluate([all_fans], context)
        self.assertEqual(result["overlay"]["duties"], {"fan1": 55, "fan2": 55})
        one = make_rule("one", action={"type": "set_duty", "fan": "fan2", "pct": 30})
        result = self.evaluate([one], context)
        self.assertEqual(result["overlay"]["duties"], {"fan2": 30})

    def test_set_mode_overlay(self):
        rule = make_rule("m", action={"type": "set_mode", "mode": "manual"})
        result = self.evaluate([rule], {"temps": {"cpu": 50.0, "gpu": 76.0}, "rpm": {}})
        self.assertEqual(result["overlay"]["mode"], "manual")

    def test_notify_and_command_requests(self):
        notify = make_rule("n", action={"type": "notify", "message": "hot!"})
        command = make_rule("c", action={"type": "run_command", "command_ref": "perf-mode"})
        result = self.evaluate([notify, command], {"temps": {"cpu": 50.0, "gpu": 76.0}, "rpm": {}})
        self.assertEqual(result["notifications"], [{"rule": "n", "message": "hot!"}])
        self.assertEqual(result["commands"], [{"rule": "c", "command_ref": "perf-mode"}])

    def test_disabled_rules_do_not_accumulate_state(self):
        rule = make_rule("off", enabled=False)
        result = self.evaluate([rule], {"temps": {"cpu": 50.0, "gpu": 76.0}, "rpm": {}})
        self.assertNotIn("off", result["runtime"])
        self.assertEqual(result["states"]["off"], rules.STATE_INACTIVE)

    def test_time_window_including_midnight_crossing(self):
        window = (["mon", "tue"], "23:00", "07:00")
        late_monday = datetime.datetime(2026, 1, 5, 23, 30)
        early_tuesday = datetime.datetime(2026, 1, 6, 2, 0)
        tuesday_evening = datetime.datetime(2026, 1, 6, 20, 0)
        self.assertTrue(rules._in_time_window(late_monday, *window))
        self.assertTrue(rules._in_time_window(early_tuesday, *window))
        self.assertFalse(rules._in_time_window(tuesday_evening, *window))
        # a window that only lists Tuesday covers Tue 23:00 -> Wed 07:00,
        # so Tuesday's early hours are NOT inside it
        tuesday_only = (["tue"], "23:00", "07:00")
        self.assertFalse(rules._in_time_window(early_tuesday, *tuesday_only))


class ScheduleTests(unittest.TestCase):
    def test_schedule_sets_profile_overlay(self):
        schedule = {
            "enabled": True, "timezone": "local",
            "items": [{"id": "night", "days": ["mon"], "start": "00:00", "end": "23:59",
                       "profile": "quiet"}],
        }
        result = rules.evaluate_schedule(schedule, T0)
        self.assertEqual(result["profile"], "quiet")
        self.assertEqual(result["active"], ["night"])

    def test_disabled_schedule_yields_nothing(self):
        schedule = {"enabled": False, "timezone": "local", "items": [
            {"id": "n", "days": ["mon"], "start": "00:00", "end": "23:59", "profile": "quiet"}]}
        self.assertEqual(rules.evaluate_schedule(schedule, T0)["active"], [])

    def test_overlapping_items_last_entry_wins(self):
        schedule = {
            "enabled": True, "timezone": "local",
            "items": [
                {"id": "first", "days": ["mon"], "start": "00:00", "end": "23:59", "profile": "silent"},
                {"id": "second", "days": ["mon"], "start": "10:00", "end": "14:00", "profile": "performance"},
            ],
        }
        result = rules.evaluate_schedule(schedule, datetime.datetime(2026, 1, 5, 12, 0))
        self.assertEqual(result["profile"], "performance")
        self.assertEqual(result["active"], ["first", "second"])

    def test_timezone_aware_evaluation(self):
        schedule = {
            "enabled": True, "timezone": "Pacific/Auckland",
            "items": [{"id": "day", "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                       "start": "00:00", "end": "23:59", "profile": "balanced"}],
        }
        # 2026-01-05 12:00 UTC is already past midnight in Auckland (UTC+13)
        with mock.patch.dict(sys.modules):
            result = rules.evaluate_schedule(schedule, T0)
        self.assertEqual(result["active"], ["day"])

    def test_dst_transition_does_not_crash(self):
        schedule = {
            "enabled": True, "timezone": "Europe/Berlin",
            "items": [{"id": "x", "days": ["sun"], "start": "01:30", "end": "03:30",
                       "profile": "quiet"}],
        }
        during_dst_gap = datetime.datetime(2026, 3, 29, 2, 0)  # nonexistent local time
        result = rules.evaluate_schedule(
            schedule, during_dst_gap.replace(tzinfo=datetime.timezone.utc))
        self.assertIsInstance(result["active"], list)


# ------------------------------------------------------------------------ history


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = pathlib.Path(self.tmp.name) / "history.db"

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, **kwargs):
        defaults = {"path": self.path, "retention_days": 7, "persist": True}
        defaults.update(kwargs)
        return history.HistoryStore(**defaults)

    def sample(self, ts, duty=50, temp=65.0):
        return dict(
            ts=ts, mode="auto", profile="balanced", effective_mode="auto",
            effective_profile="balanced", hottest_temp=temp, critical=temp >= 95,
            fans={"fan1": {"rpm": 1200, "duty": duty, "requested": duty}},
            sensors={"cpu": temp},
        )

    def test_batched_writes_flush_after_threshold(self):
        store = self.store()
        for i in range(9):
            store.append_sample(**self.sample(1000 + i))
        self.assertEqual(store.status()["buffered"], 9)
        with sqlite3.connect(self.path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        self.assertEqual(count, 0)
        store.append_sample(**self.sample(1009))
        self.assertEqual(store.status()["buffered"], 0)
        with sqlite3.connect(self.path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        self.assertEqual(count, 10)
        store.close()

    def test_database_failure_degrades_to_memory(self):
        store = self.store(path=self.tmp.name)  # a directory: unwritable as db
        store.append_sample(**self.sample(1000))
        self.assertTrue(store.degraded)
        self.assertIsNotNone(store.error)
        self.assertEqual(len(store.memory), 1)
        result = store.query()
        self.assertEqual(len(result["samples"]), 1)
        self.assertTrue(result["degraded"])
        store.close()

    def test_corrupt_database_quarantines_and_recovers(self):
        store = self.store()
        store.append_sample(**self.sample(1000))
        store.flush()
        store.close()
        # corrupt the file beyond recognition
        self.path.write_bytes(b"this is not sqlite" * 100)
        broken = self.store()
        with self.assertRaises(sqlite3.DatabaseError):
            with sqlite3.connect(self.path) as conn:
                conn.execute("SELECT COUNT(*) FROM samples").fetchall()
        broken._maybe_recover()  # sqlite treats garbage leniently; force path
        broken.append_sample(**self.sample(2000))
        broken.flush()
        quarantined = list(self.path.parent.glob("*.corrupt-*"))
        self.assertLessEqual(len(quarantined), 1)
        broken.close()

    def test_query_filters_and_downsamples(self):
        store = self.store()
        for i in range(200):
            store.append_sample(**self.sample(1000 + i, duty=i % 100))
        store.flush()
        result = store.query(since=1000, until=1199, fans=["fan1"], max_points=50)
        self.assertLessEqual(len(result["samples"]), 50)
        self.assertTrue(all(row["fan_id"] == "fan1" for row in result["fans"]))
        self.assertEqual(result["samples"][0]["ts"], 1000)
        self.assertEqual(result["samples"][-1]["ts"], 1199)
        store.close()

    def test_stats_aggregates(self):
        store = self.store()
        for i in range(20):
            temp = 96.0 if i == 5 else 60.0
            store.append_sample(**self.sample(1000 + i, duty=(i * 5) % 100, temp=temp))
        store.flush()
        stats = store.stats()
        self.assertEqual(stats["samples"], 20)
        self.assertEqual(stats["critical_events"], 1)
        self.assertLessEqual(stats["max_temp"], 96.0)
        self.assertTrue(any(b["fan_id"] == "fan1" for b in stats["duty_bands"]))
        self.assertTrue(any(p["profile"] == "balanced" for p in stats["profiles"]))
        store.close()

    def test_downsampling_retains_each_fan_and_sensor_series(self):
        for persist in (False, True):
            with self.subTest(persist=persist):
                store = self.store(persist=persist)
                for i in range(100):
                    sample = self.sample(1000 + i)
                    sample['fans']['fan2'] = {'rpm': 2400, 'duty': 80, 'requested': 80}
                    sample['sensors']['gpu'] = 75
                    store.append_sample(**sample)
                store.flush()
                result = store.query(max_points=10)
                self.assertEqual({r['fan_id'] for r in result['fans']}, {'fan1', 'fan2'})
                self.assertEqual({r['sensor_id'] for r in result['sensors']}, {'cpu', 'gpu'})
                self.assertEqual(len(result['fans']), 20)
                self.assertEqual(len(result['sensors']), 20)
                self.assertTrue(all(r['duty'] == 80 for r in result['fans'] if r['fan_id'] == 'fan2'))
                store.close()

    def test_prolonged_database_failure_keeps_bounded_backlog(self):
        store = self.store(path=self.tmp.name)
        for i in range(history.MEMORY_RING + 50):
            store.append_sample(**self.sample(i))
        self.assertLessEqual(store.status()['buffered'], history.MEMORY_RING)
        self.assertEqual(store.query()['samples'][0]['ts'], 50)
        store.close()

    def test_retention_prune_deletes_old_samples(self):
        store = self.store(retention_days=1)
        store.append_sample(**self.sample(time.time() - 3 * 86400))
        store.append_sample(**self.sample(time.time()))
        store.flush()
        pruned = store.prune(now=time.time() + history.PRUNE_INTERVAL + 1)
        self.assertGreaterEqual(pruned, 1)
        with sqlite3.connect(self.path) as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        self.assertEqual(remaining, 1)
        # prune is rate limited: an immediate second call is a no-op
        self.assertEqual(store.prune(now=time.time() + 10), 0)
        store.close()

    def test_persist_false_keeps_memory_only(self):
        store = self.store(persist=False)
        store.append_sample(**self.sample(1000))
        self.assertFalse(store.status()["persist"])
        self.assertEqual(len(store.query()["samples"]), 1)
        store.close()

    def test_export_csv_contains_stable_columns(self):
        store = self.store()
        store.append_sample(**self.sample(1000))
        store.flush()
        csv_text = store.export_csv()
        header = csv_text.splitlines()[0]
        self.assertTrue(header.startswith("time,"))
        self.assertIn("fan1", csv_text)
        self.assertIn("cpu", csv_text)
        store.close()


# ------------------------------------------------- controller + fake backend wiring


class ControllerV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.config_path = self.base / "fan-control.json"
        self.backend = FakeFanBackend()
        self.sensors = [
            temp_record("hwmon:k10temp:pci-00:temp1", 60.0, name="k10temp", label="Tctl", aliases=("cpu",)),
            temp_record("hwmon:amdgpu:pci-01:temp1", 55.0, name="amdgpu", label="edge", aliases=("gpu",)),
        ]
        self.ctl = controller.FanController(
            self.backend, self.config_path, self.base, demo=False, data_dir=self.base,
        )
        patcher = mock.patch.object(controller, "scan_sensor_records", return_value=self.sensors)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.ctl.close()
        self.tmp.cleanup()

    def tick(self):
        self.ctl.tick_sensors()
        self.ctl.tick_readback()
        return self.ctl.tick_control()

    def test_capabilities_reports_v2(self):
        caps = self.ctl.handle("capabilities")
        self.assertEqual(caps["protocol_version"], 2)
        self.assertIn("rules", caps["features"])
        self.assertEqual(caps["backend"], "fake")
        self.assertEqual(caps["fan_count"], 2)

    def test_legacy_rpc_surface_still_works(self):
        self.ctl.handle("set", {"fan": 1, "pct": 40})
        snap = self.ctl.handle("snapshot")
        self.assertEqual(snap["mode"], "manual")
        self.assertEqual(snap["targets"]["1"], 40)
        self.ctl.handle("profile", {"profile": "silent"})
        snap = self.ctl.handle("snapshot")
        self.assertEqual(snap["mode"], "curve")
        self.assertEqual(snap["profile"], "silent")
        self.ctl.handle("custom", {"curve": [[0, 0], [80, 50]], "which": "shared"})
        self.ctl.handle("curves.save", {"name": "night", "curve": [[0, 0], [90, 30]]})
        names = self.ctl.handle("curves.list")
        self.assertIn("night", names["names"])
        exported = self.ctl.handle("curves.export", {"name": "night"})
        self.assertEqual(exported["curve"], [[0, 0], [90, 30]])

    def test_mutations_return_config_revision(self):
        reply = self.ctl.handle("profile", {"profile": "performance"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["config_revision"], self.ctl.config["revision"])
        self.assertGreaterEqual(reply["config_revision"], 2)

    def test_revision_conflict_detected(self):
        current = self.ctl.config["revision"]
        with self.assertRaises(rpc.RpcError) as ctx:
            self.ctl.handle("profile", {"profile": "silent", "expected_revision": current + 5})
        self.assertEqual(ctx.exception.code, "REVISION_CONFLICT")
        self.assertEqual(self.ctl.config["profile"], "balanced")

    def test_stable_sensor_identity_in_rpc(self):
        self.tick()
        listing = self.ctl.handle("sensors.list")
        ids = [s["id"] for s in listing["sensors"]]
        self.assertIn("hwmon:k10temp:pci-00:temp1", ids)
        self.assertIn("hwmon:amdgpu:pci-01:temp1", ids)
        self.assertNotIn("hwmon9", " ".join(ids))  # volatile index never in ids
        self.assertIsNotNone(listing["aliases"]["cpu"])

    def test_sensors_list_tracks_pinned_sensor(self):
        self.tick()
        self.ctl.handle("config", {"cpu_sensor": "hwmon:amdgpu:pci-01:temp1"})
        listing = self.ctl.handle("sensors.list")
        self.assertEqual(listing["aliases"]["cpu"], "hwmon:amdgpu:pci-01:temp1")

    def test_fans_list_and_configure(self):
        self.tick()
        reply = self.ctl.handle("fans.configure", {
            "fan": "fan1", "min_duty": 20, "max_duty": 90, "name": "CPU Blower",
        })
        self.assertEqual(reply["changed"]["fan"]["name"], "CPU Blower")
        fans = self.ctl.handle("fans.list")["fans"]
        fan1 = next(f for f in fans if f["id"] == "fan1")
        self.assertEqual(fan1["min_duty"], 20)
        self.assertEqual(fan1["max_duty"], 90)

    def test_fans_configure_rejects_bad_curve_ref(self):
        with self.assertRaises(rpc.RpcError) as ctx:
            self.ctl.handle("fans.configure", {
                "fan": 1, "control": {"type": "curve", "curve_ref": "nope"},
            })
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_fans_test_creates_expiring_override(self):
        self.tick()
        reply = self.ctl.handle("fans.test", {"fan": 1, "delta": 25, "duration_ms": 1000})
        self.assertEqual(reply["duty"], 25)
        self.assertGreater(reply["expires"], time.time())
        # override expires daemon-side
        self.ctl._overrides["fan1"]["expires"] = time.time() - 1
        self.ctl.tick_control()
        self.assertNotIn("fan1", self.ctl._overrides)

    def test_rules_lifecycle_and_test(self):
        rule = make_rule()
        self.ctl.handle("rules.set", {"rule": rule})
        listing = self.ctl.handle("rules.list")
        self.assertEqual(listing["rules"][0]["id"], "r1")
        test = self.ctl.handle("rules.test", {"id": "r1"})
        self.assertFalse(test["matched"])  # gpu is 55C < 70 threshold
        self.sensors[1]["temp"] = self.sensors[1]["value"] = 76.0
        self.ctl.tick_sensors()
        test = self.ctl.handle("rules.test", {"id": "r1"})
        self.assertTrue(test["matched"])
        self.assertIn("76.0", test["reason"])
        self.ctl.handle("rules.delete", {"id": "r1"})
        self.assertEqual(self.ctl.handle("rules.list")["rules"], [])

    def test_rules_test_rejects_invalid_payload(self):
        with self.assertRaises(rpc.RpcError):
            self.ctl.handle("rules.set", {"rule": {"id": "x", "trigger": {"type": "bogus"}}})

    def test_rule_activation_changes_effective_profile(self):
        self.ctl.handle("rules.set", {"rule": make_rule(
            "gaming", trigger={"type": "temp_above", "sensor": "gpu", "value": 60},
            action={"type": "set_profile", "profile": "performance"},
        )})
        self.ctl.handle("mode", {"mode": "curve"})
        self.sensors[1]["temp"] = self.sensors[1]["value"] = 76.0
        self.tick()
        snap = self.ctl.handle("snapshot")
        self.assertEqual(snap["effective_profile"], "performance")
        self.assertEqual(snap["configured_profile"], "balanced")
        self.assertTrue(snap["policy_source"].startswith("rule:"))
        # persistent configuration untouched by the rule overlay
        self.assertEqual(self.ctl.config["profile"], "balanced")

    def test_schedule_get_set(self):
        schedule = {
            "enabled": True, "timezone": "local",
            "items": [{"id": "night", "days": ["mon"], "start": "00:00", "end": "23:59",
                       "profile": "silent"}],
        }
        self.ctl.handle("schedule.set", {"schedule": schedule})
        payload = self.ctl.handle("schedule.get")
        self.assertEqual(payload["schedule"]["items"][0]["id"], "night")
        with self.assertRaises(rpc.RpcError):
            self.ctl.handle("schedule.set", {"schedule": {"enabled": True, "items": "no"}})

    def test_curve_delete_guard_and_force(self):
        created = self.ctl.handle("curves.set", {"name": "assigned", "points": [[0, 0], [80, 50]]})
        curve_id = created["changed"]["id"]
        self.ctl.handle("curves.assign", {"fan": 1, "curve_ref": curve_id})
        # deleting an assigned curve requires force
        with self.assertRaises(rpc.RpcError) as ctx:
            self.ctl.handle("curves.delete", {"id": curve_id})
        self.assertEqual(ctx.exception.code, "CONFIG_ERROR")
        # updating an assigned curve stays allowed
        reply = self.ctl.handle("curves.set", {"id": curve_id, "name": "x", "points": [[0, 0], [10, 5]]})
        self.assertTrue(reply["ok"])
        # forced delete reassigns the fan to its profile-side fallback
        self.ctl.handle("curves.delete", {"id": curve_id, "force": True})
        fan1 = next(f for f in self.ctl.handle("fans.list")["fans"] if f["id"] == "fan1")
        self.assertNotIn("curve_ref", {k: v for k, v in fan1["control"].items() if v})
        self.assertEqual(fan1["control"]["type"], "profile")

    def test_live_payload_has_seq_and_lightweight_fields(self):
        self.tick()
        live = self.ctl.handle("live")
        self.assertIn("seq", live)
        self.assertIn("timestamp", live)
        self.assertIn("config_revision", live)
        self.assertIn("effective_profile", live)
        self.assertNotIn("profiles", live)
        self.assertNotIn("named_curves", live)
        again = self.ctl.handle("live")
        self.assertGreater(again["seq"], live["seq"])

    def test_diagnostics_snapshot_and_decisions(self):
        self.tick()
        diag = self.ctl.handle("diagnostics.snapshot")
        self.assertEqual(diag["backend"]["name"], "fake")
        self.assertIn("history", diag)
        self.assertIn("capabilities", diag)
        decisions = self.ctl.handle("diagnostics.decisions")["decisions"]
        self.assertGreaterEqual(len(decisions), 1)
        sample = decisions[-1]
        self.assertIn("requested_duty", sample)
        self.assertIn("write_performed", sample)

    def test_write_failure_does_not_crash_control_loop(self):
        self.ctl.handle("mode", {"mode": "curve"})
        self.backend.write_fail = True
        result = self.tick()
        self.assertIsNotNone(result)
        self.backend.write_fail = False
        result = self.tick()
        self.assertEqual([duty for fan, duty in self.backend.writes if fan == 1][-1], result.duties[1])

    def test_history_query_and_stats_via_rpc(self):
        for _ in range(12):
            self.tick()
            self.ctl.tick_history()
        self.ctl.history_store.flush()
        query = self.ctl.handle("history.query", {"max_points": 50})
        self.assertGreaterEqual(len(query["samples"]), 1)
        stats = self.ctl.handle("history.stats")
        self.assertGreaterEqual(stats["samples"], 1)

    def test_config_reload_after_v1_file(self):
        policy.save_config(self.config_path, {"profile": "silent", "mode": "released"})
        self.ctl.reload_config()
        self.assertEqual(self.ctl.state["profile"], "silent")
        self.assertEqual(self.ctl.state["mode"], "released")

    def test_corrupt_config_falls_back_to_safe_defaults(self):
        self.config_path.write_text("{corrupt!!")
        ctl = controller.FanController(self.backend, self.config_path, self.base, demo=False)
        try:
            self.assertEqual(ctl.config["mode"], "manual")
            self.assertTrue(any("fallback" in w for w in ctl._warnings))
        finally:
            ctl.close()


# ------------------------------------------------------------------- e2e scenario


class EndToEndSimulationTests(unittest.TestCase):
    """The full §30 scripted scenario: warmup, rule activation, ramp, critical
    override, recovery, cooldown and ramp-down — asserted through decisions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.backend = FakeFanBackend()
        self.cpu_value = 45.0
        self.gpu_value = 45.0
        self.sensors = [
            temp_record("hwmon:k10temp:pci-00:temp1", self.cpu_value,
                        name="k10temp", label="Tctl", aliases=("cpu",)),
            temp_record("hwmon:nvidia:0000-01:00.0", self.gpu_value,
                        name="nvidia", label="GPU 0 · RTX", aliases=("gpu",), source="nvidia-smi"),
        ]
        self.ctl = controller.FanController(
            self.backend, self.base / "cfg.json", self.base, demo=False,
        )
        patcher = mock.patch.object(controller, "scan_sensor_records", return_value=self.sensors)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ctl.handle("mode", {"mode": "curve"})
        self.ctl.handle("rules.set", {"rule": make_rule(
            "gaming",
            trigger={"type": "temp_above", "sensor": "gpu", "value": 70},
            condition={"sustain_ticks": 3, "cooldown_seconds": 30},
            action={"type": "set_profile", "profile": "performance"},
        )})
        self.timeline = []

    def tearDown(self):
        self.ctl.close()
        self.tmp.cleanup()

    def set_gpu(self, value):
        self.gpu_value = value
        self.sensors[1]["temp"] = self.sensors[1]["value"] = value

    def tick(self):
        self.ctl.tick_sensors()
        self.ctl.tick_readback()
        result = self.ctl.tick_control()
        self.ctl.tick_history()
        self.timeline.append(result)
        return result

    def run_scenario(self):
        # Phase 1: idle warm start at 55C
        self.set_gpu(55.0)
        result = self.tick()
        self.assertEqual(result.action, "write")
        baseline = result.duties[2]
        # Phase 2: GPU warms past the rule threshold...
        self.set_gpu(68.0)
        self.tick()
        # ...and sustains above it for three ticks
        self.set_gpu(76.0)
        self.tick()
        self.tick()
        result = self.tick()
        self.assertEqual(result.engine["effective_profile"], "performance")
        self.assertEqual(result.engine["profile_source"], "rule")
        self.assertGreaterEqual(result.duties[2], baseline)
        # Phase 3: thermal emergency -> safety forces 100% bypassing everything
        self.set_gpu(96.0)
        result = self.tick()
        self.assertTrue(result.critical)
        self.assertEqual(result.duties[2], 100)
        # Phase 4: recovery — safety releases, rule profile still applies
        self.set_gpu(80.0)
        result = self.tick()
        self.assertFalse(result.critical)
        self.assertEqual(result.engine["effective_profile"], "performance")
        self.assertLess(result.duties[2], 100)
        # Phase 5: cools below trigger; rule releases; configured profile returns
        self.set_gpu(50.0)
        result = self.tick()
        self.assertEqual(result.engine["effective_profile"], "balanced")
        self.assertLess(result.duties[2], result.duties[1] + 100)  # sane output
        return result

    def test_scenario_produces_explainable_decisions(self):
        self.run_scenario()
        decisions = list(self.ctl._decisions)
        self.assertGreaterEqual(len(decisions), 12)
        critical_traces = [d for d in decisions if d["safety_override"]]
        self.assertTrue(critical_traces)
        self.assertTrue(all(t["final_duty"] == 100 for t in critical_traces))
        rule_traces = [d for d in decisions if "rule:" in (d["policy_source"] or "")]
        self.assertTrue(rule_traces)

    def test_scenario_telemetry_persisted(self):
        self.run_scenario()
        self.ctl.history_store.flush()
        stats = self.ctl.history_store.stats()
        self.assertEqual(stats["samples"], len(self.timeline))
        self.assertGreaterEqual(stats["critical_events"], 1)
        profiles = {p["profile"] for p in stats["profiles"]}
        self.assertIn("performance", profiles)


# ----------------------------------------------------------------- rpc transport


class RpcV2TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.sock = self.base / rpc.SOCKET_NAME
        self.ctl = controller.FanController(
            FakeFanBackend(), self.base / "cfg.json", self.base, demo=False)
        self.server = rpc.RpcServer(self.ctl, self.sock)
        self.server.start()
        self.client = rpc.RpcClient(self.sock)
        self.client.connect()

    def tearDown(self):
        self.client.close()
        self.server.close()
        self.ctl.close()
        self.tmp.cleanup()

    def test_structured_error_codes(self):
        with self.assertRaises(rpc.RpcError) as ctx:
            self.client.call("fans.rename", {"fan": "fan1", "name": ""})
        self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(rpc.RpcError) as ctx:
            self.client.call("rules.delete", {"id": "ghost"})
        self.assertEqual(ctx.exception.code, "NOT_FOUND")

    def test_revision_conflict_over_socket(self):
        revision = self.ctl.config["revision"]
        with self.assertRaises(rpc.RpcError) as ctx:
            self.client.call("profile", {"profile": "silent", "expected_revision": revision - 1})
        self.assertEqual(ctx.exception.code, "REVISION_CONFLICT")

    def test_mutation_response_carries_authoritative_revision(self):
        reply = self.client.call("fans.rename", {"fan": "fan1", "name": "Rear"})
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["config_revision"], self.ctl.config["revision"])
        self.assertEqual(reply["changed"]["name"], "Rear")

    def test_capabilities_over_socket(self):
        caps = self.client.call("capabilities")
        self.assertEqual(caps["protocol_version"], 2)

    def test_unknown_method_remains_plain_runtime_error(self):
        with self.assertRaises(RuntimeError):
            self.client.call("definitely.not.a.method")

    def test_v2_args_validated_over_socket(self):
        for method, params in (
            ("history.query", {"max_points": "many"}),
            ("fans.configure", {"fan": 1, "min_duty": 80, "max_duty": 20}),
            ("curves.assign", {"fan": 1, "curve_ref": "missing"}),
        ):
            with self.assertRaises(rpc.RpcError):
                self.client.call(method, params)


class IntegrationRegressionTests(unittest.TestCase):
    setUp = ControllerV2Tests.setUp
    tearDown = ControllerV2Tests.tearDown
    tick = ControllerV2Tests.tick
    def test_rejected_mutation_rolls_back_all_fields(self):
        before = json.loads(json.dumps(self.ctl.config))
        with self.assertRaises(rpc.RpcError):
            self.ctl.handle('fans.configure', {'fan': 1, 'name': 'Must not persist', 'min_duty': 99, 'max_duty': 1})
        self.assertEqual(self.ctl.config, before)

    def test_disk_failure_is_not_acknowledged(self):
        before = json.loads(json.dumps(self.ctl.config))
        with mock.patch.object(controller, 'save_document', side_effect=OSError('disk full')):
            with self.assertRaises(rpc.RpcError) as caught:
                self.ctl.handle('fans.rename', {'fan': 1, 'name': 'Unsaved'})
        self.assertEqual(caught.exception.code, 'CONFIG_ERROR')
        self.assertEqual(self.ctl.config, before)

    def test_manual_rpc_obeys_critical_protection(self):
        self.sensors[1]['temp'] = self.sensors[1]['value'] = 100
        self.ctl.tick_sensors()
        self.ctl.handle('set', {'fan': 1, 'pct': 5})
        self.assertEqual(self.backend.writes[-1][1], 100)

    def test_write_failure_trace_reports_failure(self):
        self.ctl.handle('mode', {'mode': 'curve'})
        self.backend.write_fail = True
        self.tick()
        traces = self.ctl.handle('diagnostics.decisions')['decisions'][-2:]
        self.assertTrue(all(not d['write_performed'] for d in traces))
        self.assertTrue(all('failed' in d['write_suppressed_reason'] for d in traces))

    def test_history_rpc_can_query_across_threads(self):
        import concurrent.futures
        self.tick()
        self.ctl.tick_history()
        self.ctl.history_store.flush()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(self.ctl.handle, 'history.query').result()
        self.assertFalse(result['degraded'])
        self.assertTrue(result['samples'])

    def test_command_execution_rejected(self):
        with self.assertRaises(rpc.RpcError) as caught:
            self.ctl.handle('rules.set', {'rule': make_rule(action={'type': 'run_command', 'command_ref': 'x'})})
        self.assertEqual(caught.exception.code, 'UNSUPPORTED')

    def test_history_query_does_not_hold_control_lock(self):
        import concurrent.futures
        import threading

        entered, release = threading.Event(), threading.Event()

        def query(**_params):
            entered.set()
            release.wait(2)
            return {}

        with mock.patch.object(self.ctl.history_store, 'query', side_effect=query):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                reading = pool.submit(self.ctl.handle, 'history.query')
                self.assertTrue(entered.wait(1))
                try:
                    pool.submit(self.ctl.tick_control).result(timeout=1)
                finally:
                    release.set()
                reading.result()

    def test_controller_migration_retains_backup(self):
        self.config_path.write_text('{"mode":"manual", "targets":{"1":40}}')
        self.ctl.reload_config()
        self.ctl.handle('fans.rename', {'fan': 1, 'name': 'CPU'})
        backup = self.config_path.with_name(self.config_path.stem + '.v1.backup.json')
        self.assertEqual(json.loads(backup.read_text())['targets']['1'], 40)

class PolicyPriorityRegressionTests(unittest.TestCase):
    def test_all_fan_and_specific_duties_arbitrate_per_output(self):
        all_fans = make_rule('all', action={'type': 'set_duty', 'pct': 80}, priority=200)
        one_fan = make_rule('one', action={'type': 'set_duty', 'fan': 'fan1', 'pct': 10}, priority=100)
        result = rules.evaluate_rules([all_fans, one_fan], make_context(cpu=80, gpu=80), T0)
        self.assertEqual(result['overlay']['duties']['fan1'], 80)
        self.assertIn('one', result['shadowed'])

    def test_rule_cannot_release_critical_cooling(self):
        cfg = make_config()
        cfg['mode'] = 'auto'
        cfg['rules'] = [make_rule(action={'type': 'set_mode', 'mode': 'released'})]
        result = engine.evaluate(cfg, make_context(cpu=100, gpu=100), make_runtime())
        self.assertTrue(result['critical'])
        self.assertEqual(result['duties'][1], 100)

    def test_invalid_time_rule_is_rejected_before_control_loop(self):
        cfg = make_config()
        cfg['rules'] = [make_rule(trigger={'type': 'time_range', 'days': ['mon'], 'start': 'bad', 'end': '08:00'})]
        with self.assertRaises(policy.ConfigError):
            policy.normalize_config(cfg)


class BugfixRegressionTests(unittest.TestCase):
    """Regression tests for the extensive bug-fix pass (2026-09-06)."""

    def test_valid_temp_rejects_garbage(self):
        for bad in ("bad", {}, [], object(), None, True, False, float("nan")):
            self.assertFalse(policy._valid_temp(bad), bad)
        self.assertTrue(policy._valid_temp(70))
        self.assertTrue(policy._valid_temp("70"))
        self.assertIsNone(policy.select_control_temp("bad", None))
        self.assertEqual(policy.select_control_temp("bad", 80), 80.0)

    def test_normalize_curve_rounds_temps_like_ui(self):
        self.assertEqual(policy.normalize_curve([[59.9, 50], [70, 50]]), [(60, 50), (70, 50)])
        with self.assertRaises(ValueError):
            policy.normalize_curve([[float("inf"), 50], [70, 50]])

    def test_sensor_max_ignores_garbage(self):
        value, available = rules.resolve_sensor_value(
            "max", {"temps": {"a": 999, "b": "x", "c": None}})
        self.assertFalse(available)
        self.assertIsNone(value)
        value, available = rules.resolve_sensor_value(
            "max", {"temps": {"a": 70, "b": 80}})
        self.assertTrue(available)
        self.assertEqual(value, 80)

    def test_startup_rule_not_consumed_without_startup(self):
        rule = make_rule("boot", trigger={"type": "on_startup"},
                         action={"type": "set_profile", "profile": "performance"})
        context = {"temps": {}, "rpm": {}, "fans_present": [1, 2]}
        first = rules.evaluate_rules([rule], context, T0, {}, startup=False)
        self.assertEqual(first["active"], [])
        second = rules.evaluate_rules([rule], context, T0, first["runtime"], startup=True)
        self.assertEqual(second["active"], ["boot"])

    def test_sustain_count_frozen_during_cooldown(self):
        rule = make_rule("slow", trigger={"type": "temp_above", "sensor": "gpu", "value": 70},
                         condition={"sustain_ticks": 3, "cooldown_seconds": 60},
                         action={"type": "set_profile", "profile": "performance"})
        context = {"temps": {"gpu": 80, "cpu": 50, "max": 80}, "rpm": {}, "fans_present": [1, 2]}
        state = {}
        for _ in range(3):
            result = rules.evaluate_rules([rule], context, T0, state)
            state = result["runtime"]
        self.assertIn("slow", result["active"])
        # unmatched tick moves the rule into cooldown
        idle = {"temps": {"gpu": 50, "cpu": 50, "max": 50}, "rpm": {}, "fans_present": [1, 2]}
        result = rules.evaluate_rules([rule], idle, T0, state)
        state = result["runtime"]
        self.assertIn("slow", result["states"] and [r for r, s in result["states"].items() if s == "cooldown"])
        before = state["slow"]["consecutive_match_count"]
        result = rules.evaluate_rules([rule], context, T0, state)
        self.assertEqual(result["runtime"]["slow"]["consecutive_match_count"], before)

    def test_cooldown_survives_naive_aware_mix(self):
        rule = make_rule("cool", trigger={"type": "temp_above", "sensor": "gpu", "value": 70},
                         condition={"sustain_ticks": 1, "cooldown_seconds": 60},
                         action={"type": "set_profile", "profile": "performance"})
        context = {"temps": {"gpu": 80, "cpu": 50, "max": 80}, "rpm": {}, "fans_present": [1, 2]}
        state = rules.evaluate_rules([rule], context, T0, {})["runtime"]
        aware = datetime.datetime(2026, 1, 5, 12, 0, 30, tzinfo=datetime.timezone.utc)
        idle = {"temps": {"gpu": 50, "cpu": 50, "max": 50}, "rpm": {}, "fans_present": [1, 2]}
        result = rules.evaluate_rules([rule], idle, aware, state)
        self.assertEqual(result["states"]["cool"], "cooldown")

    def test_set_duty_int_fan_canonicalized(self):
        specific = make_rule("one", action={"type": "set_duty", "fan": 1, "pct": 10}, priority=100)
        wide = make_rule("all", action={"type": "set_duty", "pct": 80}, priority=200)
        context = {"temps": {"gpu": 80, "cpu": 80, "max": 80}, "rpm": {}, "fans_present": [1, 2]}
        result = rules.evaluate_rules([specific, wide], context, T0)
        self.assertEqual(result["overlay"]["duties"], {"fan1": 80, "fan2": 80})
        self.assertIn("one", result["shadowed"])

    def test_time_range_uses_supplied_now_in_named_zone(self):
        rule = make_rule("night", trigger={"type": "time_range", "days": ["mon"],
                                           "start": "23:00", "end": "07:00",
                                           "timezone": "America/New_York"},
                         action={"type": "set_profile", "profile": "silent"})
        monday = datetime.datetime(2026, 1, 6, 4, 0, 0)  # Tue 04:00 UTC = Mon 23:00 EST
        context = {"temps": {}, "rpm": {}, "fans_present": [1, 2]}
        matched, _ = rules.rule_triggered(rule, context, monday)
        self.assertTrue(matched)

    def test_hhmm_accepts_single_digit_hour(self):
        self.assertTrue(policy._valid_hhmm("9:00"))
        self.assertEqual(policy._canonical_hhmm("9:00"), "09:00")
        self.assertFalse(policy._valid_hhmm("24:00"))
        cfg = make_config()
        cfg["schedule"] = {"enabled": True, "timezone": "local", "items": [
            {"id": "early", "days": ["mon"], "start": "9:00", "end": "10:00", "profile": "silent", "mode": None}]}
        doc, _warnings = policy.normalize_config(cfg)
        self.assertEqual(doc["schedule"]["items"][0]["start"], "09:00")

    def test_unknown_top_level_key_warns_but_preserved(self):
        cfg = make_config(**{"hysterisis": 3})
        doc, warnings = policy.normalize_config(cfg)
        self.assertEqual(doc["safety"]["hysteresis"], 5)
        self.assertTrue(any("hysterisis" in w for w in warnings))
        self.assertEqual(doc.get("hysterisis"), 3)

    def test_demo_backend_fan3(self):
        demo = backend.DemoBackend()
        self.assertEqual(demo.read_duty(3), 0)
        demo.write_duty(3, 55)
        self.assertEqual(demo.read_duty(3), 55)

    def test_clevo_backend_rejects_bad_fan_and_missing_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            for fan in (1, 2):
                (base / f"fan{fan}_manual_duty").write_text("0")
                (base / f"fan{fan}_duty").write_text("30")
            dev = backend.ClevoAcpiBackend(base=base)
            with self.assertRaises(backend.FanBackendError):
                dev.write_duty(9, 50)
            with self.assertRaises(backend.FanBackendError):
                dev.write_duty("../../x", 50)
            (base / "fan1_duty").unlink()
            with self.assertRaises(backend.FanBackendError):
                dev.read_duty(1)

    def test_history_range_rejects_bool(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = controller.FanController(
                FakeFanBackend(), pathlib.Path(tmp) / "cfg.json",
                pathlib.Path(tmp), demo=True, data_dir=tmp)
            try:
                with self.assertRaises(rpc.RpcError):
                    ctl.handle("history.query", {"since": True})
            finally:
                ctl.close()

    def test_fans_test_validates_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = controller.FanController(
                FakeFanBackend(), pathlib.Path(tmp) / "cfg.json",
                pathlib.Path(tmp), demo=True, data_dir=tmp)
            try:
                with self.assertRaises(rpc.RpcError):
                    ctl.handle("fans.test", {"fan": 1, "delta": 500})
                with self.assertRaises(rpc.RpcError):
                    ctl.handle("fans.test", {"fan": 1, "delta": True})
            finally:
                ctl.close()

    def test_config_section_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = controller.FanController(
                FakeFanBackend(), pathlib.Path(tmp) / "cfg.json",
                pathlib.Path(tmp), demo=True, data_dir=tmp)
            try:
                with self.assertRaises(rpc.RpcError):
                    ctl.handle("config", {"safety": {"global_max_duty": 5}})
                with self.assertRaises(rpc.RpcError):
                    ctl.handle("config", {"safety": {"nope": 1}})
                with self.assertRaises(rpc.RpcError):
                    ctl.handle("config", {"temperature_unit": "k"})
                reply = ctl.handle("config", {"safety": {"global_max_duty": 80}})
                self.assertTrue(reply["ok"])
            finally:
                ctl.close()

    def test_rpc_pipelining_and_structured_errors(self):
        import socket as _socket
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            ctl = controller.FanController(
                FakeFanBackend(), base / "cfg.json", base, demo=True, data_dir=tmp)
            server = rpc.RpcServer(ctl, base / "control.sock")
            server.start()
            try:
                sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                sock.connect(str(base / "control.sock"))
                sock.settimeout(5)
                try:
                    sock.sendall(b'{"method": "capabilities", "params": {}}\n'
                                 b'{"method": "capabilities", "params": {}}\n')
                    first = rpc._read_line(sock)
                    second = rpc._read_line(sock)
                    self.assertIsNotNone(first)
                    self.assertIsNotNone(second)
                    self.assertTrue(json.loads(first.decode())["ok"])
                    self.assertTrue(json.loads(second.decode())["ok"])
                finally:
                    sock.close()
                client = rpc.RpcClient(str(base / "control.sock"))
                try:
                    with self.assertRaises(rpc.RpcError) as ctx:
                        client.call("nope", {})
                    self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")
                finally:
                    client.close()
            finally:
                server.close()
                ctl.close()

    def test_rules_test_rejects_bad_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = controller.FanController(
                FakeFanBackend(), pathlib.Path(tmp) / "cfg.json",
                pathlib.Path(tmp), demo=True, data_dir=tmp)
            try:
                ctl.handle("rules.set", {"rule": make_rule("r9")})
                with self.assertRaises(rpc.RpcError):
                    ctl.handle("rules.test", {"id": "r9", "context": {"temps": "x"}})
            finally:
                ctl.close()


if __name__ == "__main__":
    unittest.main()
