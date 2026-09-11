#!/usr/bin/env python3
"""In-process fan controller: staged control pipeline + JSON-RPC surface.

The v2 configuration document (``self.config``) is the canonical store.
``self.state`` is a read/write legacy (v1 flat) view kept for compatibility
with existing clients; all control decisions flow through the staged
pipeline in :mod:`fan_engine`. The daemon is always authoritative: the UI
previews and requests, this class decides and writes.
"""

from __future__ import annotations

import collections
import copy
import datetime
import math
import os
import pathlib
import sys
import threading
import time

import fan_rules
from fan_backend import FanBackendError
from fan_engine import evaluate as evaluate_pipeline
from fan_history import HistoryStore
from fan_policy import (
    APP_VERSION,
    FAN_IDS,
    PROFILES,
    PROTOCOL_VERSION,
    SAFE_MAX_DUTY,
    SCHEMA_VERSION,
    ConfigError,
    _normalize_rule,
    config_payload,
    default_config_v2,
    history_csv,
    load_document,
    normalize_config,
    normalize_curve,
    pick_cpu_temp,
    pick_gpu_temp,
    sanitize_curve_id,
    save_document,
    scan_sensor_records,
    select_control_temp,
)
from fan_rpc import RpcError
from fan_rules import local_now, validate_rule_payload

HISTORY_LEN = 900
DECISION_RING = 600
LEGACY_MODE_MAP = {"curve": "auto", "manual": "manual", "released": "released"}
V1_MODE_MAP = {v: k for k, v in LEGACY_MODE_MAP.items()}
SYSTEM_CURVE_IDS = ("legacy_custom", "cpu_default", "gpu_default")

# Methods that never mutate persistent config, runtime overrides, or the
# backend: served without a config snapshot/rollback and without the outer
# controller lock (handlers take shorter inner locks as needed).
_READONLY_METHODS = frozenset({
    "snapshot",
    "live",
    "config.get",
    "history",
    "curves.list",
    "curves.export",
    "diagnose",
    "capabilities",
    "fans.list",
    "sensors.list",
    "rules.list",
    "schedule.get",
    "diagnostics.snapshot",
    "diagnostics.decisions",
})


def _int_targets(raw):
    return {int(key): int(value) for key, value in raw.items()}


class TickResult:
    """Legacy-shaped decision object plus the full engine result."""

    def __init__(self, result):
        self._result = result
        self.action = result["action"]
        self.writes = dict(result["writes"])
        self.duties = dict(result["duties"])
        self.critical = result["critical"]
        self.missing_temp = result["missing_temp"]

    @property
    def engine(self):
        return self._result


class LegacyStateView:
    """Read/write adapter presenting the v1 flat config over the v2 document."""

    def __init__(self, controller):
        self._ctl = controller

    # -- read ---------------------------------------------------------------
    def _get(self, key):
        cfg = self._ctl.config
        if key == "mode":
            return V1_MODE_MAP.get(cfg["mode"], cfg["mode"])
        if key == "profile":
            return cfg["profile"]
        if key == "curve":
            return self._profile_curve()
        if key == "curve_cpu":
            return self._curve_tuples("cpu_default")
        if key == "curve_gpu":
            return self._curve_tuples("gpu_default")
        if key == "named_curves":
            return {
                entry["name"]: [tuple(p) for p in entry["points"]]
                for curve_id, entry in cfg["curves"].items()
                if curve_id.startswith("named_")
            }
        if key == "linked":
            types = {cfg["fans"][f]["control"]["type"] for f in ("fan1", "fan2", "fan3")}
            return types == {"linked"}
        if key == "targets":
            return {str(i): cfg["fans"][f"fan{i}"]["control"]["target"] for i in (1, 2, 3)}
        if key == "max_duty":
            return cfg["safety"]["global_max_duty"]
        if key == "hysteresis":
            return cfg["safety"]["hysteresis"]
        if key == "critical_temp":
            return cfg["safety"]["critical_temp"]
        if key == "theme":
            return cfg["display"]["theme"]
        if key == "alerts":
            return {"desktop": cfg["notifications"]["enabled"]}
        if key == "cpu_sensor":
            return cfg["sensors"]["cpu_pin"]
        if key == "gpu_sensor":
            return cfg["sensors"]["gpu_pin"]
        return cfg[key]

    def _curve_tuples(self, curve_id):
        entry = self._ctl.config["curves"].get(curve_id)
        if not entry or not entry.get("points"):
            return None
        return [tuple(p) for p in entry["points"]]

    def _profile_curve(self):
        profile = self._ctl.config["profile"]
        if profile in PROFILES and profile != "custom":
            return list(PROFILES[profile])
        entry = self._ctl.config["curves"].get("legacy_custom")
        if entry and entry.get("points"):
            return [tuple(p) for p in entry["points"]]
        return list(PROFILES["custom"])

    # -- write --------------------------------------------------------------
    def _set(self, key, value):
        cfg = self._ctl.config
        if key == "mode":
            if value not in ("manual", "curve", "released"):
                raise ValueError("unknown mode")
            cfg["mode"] = LEGACY_MODE_MAP[value]
        elif key == "profile":
            if value not in PROFILES:
                raise ValueError("unknown profile")
            cfg["profile"] = value
        elif key == "curve":
            points = normalize_curve(value) if value else []
            cfg["curves"].setdefault("legacy_custom", {
                "name": "Custom", "temp_source": "max", "points": [],
            })["points"] = [list(p) for p in points]
        elif key in ("curve_cpu", "curve_gpu"):
            curve_id = "cpu_default" if key == "curve_cpu" else "gpu_default"
            if value is None:
                cfg["curves"].pop(curve_id, None)
            else:
                points = normalize_curve(value)
                cfg["curves"][curve_id] = {
                    "name": "CPU Curve" if key == "curve_cpu" else "GPU Curve",
                    "temp_source": "cpu" if key == "curve_cpu" else "gpu",
                    "points": [list(p) for p in points],
                }
        elif key == "linked":
            linked = bool(value)
            for index, fan_id in enumerate(FAN_IDS, start=1):
                control = cfg["fans"][fan_id]["control"]
                if linked:
                    control["type"] = "linked"
                    control["curve_ref"] = None
                elif index == 1 and cfg["curves"].get("cpu_default"):
                    control.update({"type": "curve", "curve_ref": "cpu_default"})
                elif index >= 2 and cfg["curves"].get("gpu_default"):
                    control.update({"type": "curve", "curve_ref": "gpu_default"})
                else:
                    control.update({
                        "type": "profile",
                        "temp_source": "cpu" if index == 1 else "gpu",
                        "curve_ref": None,
                    })
        elif key == "targets":
            raw = value if isinstance(value, dict) else {}
            for index in (1, 2, 3):
                for k in (str(index), index):
                    if k in raw:
                        cfg["fans"][f"fan{index}"]["control"]["target"] = max(0, min(100, int(raw[k])))
        elif key == "max_duty":
            cfg["safety"]["global_max_duty"] = max(20, min(SAFE_MAX_DUTY, int(value)))
        elif key == "hysteresis":
            cfg["safety"]["hysteresis"] = max(0, min(30, int(value)))
        elif key == "critical_temp":
            cfg["safety"]["critical_temp"] = max(70, min(110, int(value)))
        elif key == "theme":
            if value in ("dark", "light"):
                cfg["display"]["theme"] = value
        elif key == "alerts":
            if isinstance(value, dict):
                cfg["notifications"]["enabled"] = bool(value.get("desktop", False))
        elif key in ("cpu_sensor", "gpu_sensor"):
            pin_key = "cpu_pin" if key == "cpu_sensor" else "gpu_pin"
            cfg["sensors"][pin_key] = value if isinstance(value, dict) else None
        else:
            cfg[key] = value

    # -- mapping protocol ----------------------------------------------------
    def __getitem__(self, key):
        return self._get(key)

    def __setitem__(self, key, value):
        self._set(key, value)

    def get(self, key, default=None):
        try:
            return self._get(key)
        except KeyError:
            return default

    def __contains__(self, key):
        try:
            self._get(key)
            return True
        except KeyError:
            return False

    def keys(self):
        return dict(self).keys()

    def values(self):
        return dict(self).values()

    def items(self):
        return dict(self).items()

    def __iter__(self):
        return iter(dict(self))

    def __len__(self):
        return len(dict(self))

    def to_dict(self):
        return {
            "mode": self._get("mode"), "profile": self._get("profile"),
            "max_duty": self._get("max_duty"), "hysteresis": self._get("hysteresis"),
            "critical_temp": self._get("critical_temp"), "linked": self._get("linked"),
            "curve": self._get("curve"), "curve_cpu": self._get("curve_cpu"),
            "curve_gpu": self._get("curve_gpu"), "named_curves": self._get("named_curves"),
            "alerts": self._get("alerts"), "cpu_sensor": self._get("cpu_sensor"),
            "gpu_sensor": self._get("gpu_sensor"), "theme": self._get("theme"),
            "targets": self._get("targets"),
        }


def _resolve_pin(records, pin):
    if isinstance(pin, str) and pin:
        for record in records:
            if record["id"] == pin:
                if not record.get("available", True):
                    return None
                try:
                    return float(record["value"])
                except (TypeError, ValueError, OverflowError):
                    return None
        return None
    return pin  # legacy {name, label} pins are handled by pick_*


def _resolve_cpu_temp(records, config, readback):
    pin = config["sensors"]["cpu_pin"]
    fallback = readback.get("ec_temp1", 0)
    pinned = _resolve_pin(records, pin)
    if pinned is not None and not isinstance(pinned, dict):
        return pinned
    return pick_cpu_temp(records, pinned, fallback)


def _resolve_gpu_temp(records, config):
    pin = config["sensors"]["gpu_pin"]
    pinned = _resolve_pin(records, pin)
    if pinned is not None and not isinstance(pinned, dict):
        return pinned
    return pick_gpu_temp(records, pinned)


def _fan_id(value):
    """Accept 'fan1', 1, or '1' and return the canonical config key."""
    if isinstance(value, str) and value in FAN_IDS:
        return value
    try:
        index = int(value)
    except (TypeError, ValueError):
        raise RpcError(f"unknown fan {value!r}", "NOT_FOUND") from None
    if index not in (1, 2, 3):
        raise RpcError(f"unknown fan {value!r}", "NOT_FOUND")
    return f"fan{index}"


class FanController:
    def __init__(self, backend, config_path, runtime_dir, demo=False, fan_limit=None, data_dir=None):
        self.backend = backend
        self.config_path = pathlib.Path(config_path)
        self.runtime_dir = pathlib.Path(runtime_dir)
        self.demo = demo
        self.fan_limit = fan_limit
        self.lock = threading.RLock()
        self.history = collections.deque(maxlen=HISTORY_LEN)
        self._sensor_records = []
        self._readback = {
            "fan1": 0, "fan2": 0, "fan3": 0,
            "rpm1": None, "rpm2": None, "rpm3": None,
            "ec_temp1": 0, "ec_temp2": 0, "updated": 0,
        }
        self._missing = 0
        self._released_for_fault = False
        self._closed = False
        self._live_seq = 0
        self._rule_state = {}
        self._overrides = {}
        self._decisions = collections.deque(maxlen=DECISION_RING)
        self._last_engine = None
        self._warnings = []
        self._startup_tick = True
        self.started_at = time.time()
        self._disk_version = None
        self.config, self._warnings, self._disk_version = self._load_config_document()
        self.state = LegacyStateView(self)
        history_cfg = self.config.get("history") or {}
        if data_dir is None:
            default_data = (
                str(pathlib.Path(os.environ.get("PROGRAMDATA", "C:\\ProgramData")) / "fan-control" / "data")
                if sys.platform == "win32"
                else "/var/lib/fan-control"
            )
            data_dir = os.environ.get("FAN_CONTROL_DATA_DIR") or (
                str(self.runtime_dir) if demo else default_data
            )
        self.history_store = HistoryStore(
            pathlib.Path(data_dir) / "history.db",
            retention_days=history_cfg.get("retention_days", 7),
            persist=bool(history_cfg.get("persist", True)),
        )
        self._history_path = pathlib.Path(data_dir) / "history.db"
        self._notification_seq = 0
        self._notifications = collections.deque(maxlen=30)
        self._active_notifications = set()
        self._backend_error = None
        self._apply_mode_to_backend()

    # ------------------------------------------------------------------ config
    def _load_config_document(self):
        """Load v2 (migrating v1); on failure fall back to safe defaults."""
        try:
            doc, warnings, migrated, original_version = load_document(self.config_path)
            return doc, list(warnings), original_version
        except (ValueError, ConfigError) as exc:
            print(f"warning: config unusable ({exc}); using safe defaults", flush=True)
            doc, warnings = _safe_defaults()
            return doc, warnings + [f"config fallback: {exc}"], None

    def _bump_revision(self):
        self.config["revision"] = int(self.config.get("revision", 0)) + 1
        return self.config["revision"]

    def _persist(self):
        try:
            save_document(self.config_path, self.config, original_version=self._disk_version)
            self._disk_version = 2
        except OSError as exc:
            print(f"warning: could not save {self.config_path}: {exc}", flush=True)

    def _require_revision(self, params):
        expected = params.get("expected_revision")
        if expected is None:
            return
        current = int(self.config.get("revision", 0))
        if int(expected) != current:
            raise RpcError(
                f"configuration changed elsewhere (revision {expected} != {current})",
                "REVISION_CONFLICT",
            )

    def _apply_mode_to_backend(self):
        try:
            if self.config["mode"] == "released":
                self.backend.release()
            else:
                self.backend.lock()
        except (OSError, FanBackendError) as exc:
            print(f"EC lock/release failed: {exc}", flush=True)

    def reload_config(self):
        """SIGHUP path: re-read the config file and re-apply the EC mode."""
        with self.lock:
            try:
                doc, warnings, version = self._load_config_document()
                self.config, self._warnings, self._disk_version = doc, warnings, version
            except Exception as exc:  # noqa: BLE001 - keep running on the current config
                print(f"warning: reload failed, keeping current config ({exc})", flush=True)
                self._warnings = (self._warnings + [f"reload failed: {exc}"])[-30:]
            self._apply_mode_to_backend()

    # ------------------------------------------------------------------ temps
    def _records(self):
        with self.lock:
            return list(self._sensor_records)

    def _resolve_pin(self, records, pin):
        if isinstance(pin, str) and pin:
            for record in records:
                if record["id"] == pin:
                    if not record.get("available", True):
                        return None
                    try:
                        return float(record["value"])
                    except (TypeError, ValueError, OverflowError):
                        return None
            return None
        return pin  # legacy {name, label} pins are handled by pick_*

    def cpu_temp(self):
        with self.lock:
            records = list(self._sensor_records)
            pin = self.config["sensors"]["cpu_pin"]
            fallback = self._readback["ec_temp1"]
        pinned = self._resolve_pin(records, pin)
        if pinned is not None and not isinstance(pinned, dict):
            return pinned
        return pick_cpu_temp(records, pinned, fallback)

    def gpu_temp(self):
        with self.lock:
            records = list(self._sensor_records)
            pin = self.config["sensors"]["gpu_pin"]
        pinned = self._resolve_pin(records, pin)
        if pinned is not None and not isinstance(pinned, dict):
            return pinned
        return pick_gpu_temp(records, pinned)

    def control_temp(self):
        return select_control_temp(self.cpu_temp(), self.gpu_temp())

    def fans(self):
        try:
            present = list(self.backend.fans())
        except (OSError, FanBackendError):
            present = [1, 2]
        return [fan for fan in present if self.fan_limit is None or fan <= self.fan_limit]

    # --------------------------------------------------------------- snapshots
    def _effective(self):
        engine = self._last_engine
        if engine:
            return (
                engine["effective_mode"], engine["effective_profile"],
                engine["policy_source"], engine["active_rules"],
            )
        return self.config["mode"], self.config["profile"], "configured", []

    def snapshot(self):
        fans_present = self.fans()
        with self.lock:
            result = dict(self._readback)
            result.update(config_payload(self.state))
            result["targets"] = {str(k): int(v) for k, v in _int_targets(self.state["targets"]).items()}
            result["custom_curve"] = [list(p) for p in self.state["curve"]]
            result["temps"] = list(self._sensor_records)
            records = list(self._sensor_records)
            readback = dict(self._readback)
            config_mode = self.config["mode"]
            critical_temp = self.config["safety"]["critical_temp"]
            result["fans"] = fans_present
            result["backend"] = getattr(self.backend, "name", "unknown")
            result["demo"] = self.demo
            result["profiles"] = {name: [list(p) for p in curve] for name, curve in PROFILES.items()}
            result["fault_missing"] = self._missing
            eff_mode, eff_profile, policy_source, active_rules = self._effective()
            result["config_revision"] = int(self.config.get("revision", 0))
            result["warnings"] = list(self._warnings)[-10:]
            result["version"] = APP_VERSION
            result["config"] = copy.deepcopy(self.config)
            result["configured_mode"] = self.config["mode"]
            result["configured_profile"] = self.config["profile"]
        cpu = _resolve_cpu_temp(records, result["config"], readback)
        gpu = _resolve_gpu_temp(records, result["config"])
        result["primary_temp"] = cpu
        result["gpu_temp"] = gpu
        result["control_temp"] = select_control_temp(cpu, gpu)
        result["critical_active"] = (
            result["control_temp"] is not None
            and result["control_temp"] >= critical_temp
            and config_mode != "released"
        )
        result["effective_mode"] = eff_mode
        result["effective_profile"] = eff_profile
        result["policy_source"] = policy_source
        result["active_rules"] = active_rules
        return result

    def live_snapshot(self):
        with self.lock:
            self._live_seq += 1
            result = dict(self._readback)
            result["targets"] = {str(k): int(v) for k, v in _int_targets(self.state["targets"]).items()}
            result["temps"] = list(self._sensor_records)
            records = list(self._sensor_records)
            readback = dict(self._readback)
            config_snap = copy.deepcopy(self.config)
            result["mode"] = self.state["mode"]
            result["profile"] = self.config["profile"]
            result["linked"] = bool(self.state.get("linked"))
            result["demo"] = self.demo
            result["fault_missing"] = self._missing
            eff_mode, eff_profile, policy_source, active_rules = self._effective()
            result["effective_mode"] = eff_mode
            result["effective_profile"] = eff_profile
            result["policy_source"] = policy_source
            result["active_rules"] = active_rules
            result["configured_mode"] = self.config["mode"]
            result["configured_profile"] = self.config["profile"]
            result["backend_state"] = getattr(self.backend, "name", "unknown")
            result["backend_error"] = self._backend_error
            result["alerts"] = {"desktop": self.config["notifications"]["enabled"]}
            result["critical_temp"] = self.config["safety"]["critical_temp"]
            result["critical_alerts"] = self.config["notifications"]["critical_temp"]
            result["notifications"] = list(self._notifications)
            result["config_revision"] = int(self.config.get("revision", 0))
            result["seq"] = self._live_seq
            result["timestamp"] = time.time()
        cpu = _resolve_cpu_temp(records, config_snap, readback)
        gpu = _resolve_gpu_temp(records, config_snap)
        result["primary_temp"] = cpu
        result["gpu_temp"] = gpu
        result["control_temp"] = select_control_temp(cpu, gpu)
        result["critical_active"] = (
            result["control_temp"] is not None
            and result["control_temp"] >= config_snap["safety"]["critical_temp"]
            and config_snap["mode"] != "released"
        )
        return result

    # -------------------------------------------------------------------- RPC
    def handle(self, method, params=None):
        params = params or {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        handlers = {
            # legacy surface (kept for old UI/CLI compatibility)
            "snapshot": lambda p: self.snapshot(),
            "live": lambda p: self.live_snapshot(),
            "config.get": lambda p: config_payload(self.state),
            "history": lambda p: {"history": self._history()},
            "set": self._set,
            "profile": self._profile,
            "mode": self._mode,
            "custom": self._custom,
            "config": self._config,
            "curves.save": self._curves_save,
            "curves.list": lambda p: self._curves_list(),
            "curves.export": self._curves_export,
            "curves.delete": self._curves_delete,
            "curves.load": self._curves_load,
            "history.export": lambda p: self._history_export(),
            "diagnose": lambda p: {"text": self.diagnose_text()},
            # v2 surface
            "capabilities": lambda p: self._capabilities(),
            "fans.list": lambda p: self._fans_list(),
            "fans.rename": self._fans_rename,
            "fans.configure": self._fans_configure,
            "fans.test": self._fans_test,
            "curves.set": self._curves_set,
            "curves.assign": self._curves_assign,
            "sensors.list": lambda p: self._sensors_list(),
            "rules.list": lambda p: self._rules_list(),
            "rules.set": self._rules_set,
            "rules.delete": self._rules_delete,
            "rules.test": self._rules_test,
            "schedule.get": lambda p: self._schedule_get(),
            "schedule.set": self._schedule_set,
            "history.query": self._history_query,
            "history.stats": self._history_stats,
            "diagnostics.snapshot": lambda p: self._diagnostics_snapshot(),
            "diagnostics.decisions": lambda p: {"decisions": list(self._decisions)},
        }
        handler = handlers.get(method)
        if handler is None:
            raise ValueError(f"unknown method {method!r}")
        if method in ("history.query", "history.stats", "history.export"):
            return handler(params)
        if method in _READONLY_METHODS:
            return handler(params)
        with self.lock:
            before = copy.deepcopy(self.config)
            try:
                return handler(params)
            except Exception as exc:
                self.config = before
                if method in ("mode", "profile", "set", "custom", "curves.load"):
                    self._apply_mode_to_backend()
                if isinstance(exc, RpcError):
                    raise
                if isinstance(exc, ConfigError):
                    raise RpcError(str(exc), "CONFIG_ERROR") from exc
                if isinstance(exc, (TypeError, ValueError, OverflowError)):
                    if "." in method:
                        raise RpcError(str(exc), "INVALID_ARGUMENT") from exc
                    raise
                if isinstance(exc, (OSError, FanBackendError)):
                    raise RpcError(str(exc), "BACKEND_ERROR") from exc
                raise

    # ------------------------------------------------------------- legacy RPC
    def _history(self):
        with self.lock:
            return list(self.history)

    def _history_export(self):
        if self.history_store is not None:
            csv_text = self.history_store.export_csv()
            if csv_text.count("\n") > 1:
                return {"csv": csv_text}
        with self.lock:
            return {"csv": history_csv(list(self.history))}

    def _commit(self, changed=None):
        """Bump the revision, persist, and return the standard mutation reply."""
        normalized, warnings = normalize_config(self.config)
        normalized["revision"] = int(self.config.get("revision", 0)) + 1
        try:
            save_document(self.config_path, normalized, original_version=self._disk_version)
        except OSError as exc:
            raise RpcError(f"configuration was not saved: {exc}", "CONFIG_ERROR") from exc
        self.config = normalized
        self._disk_version = 2
        self._warnings.extend(warnings)
        if self.history_store.persist != normalized["history"]["persist"]:
            self.history_store.close()
            self.history_store = HistoryStore(self._history_path, **normalized["history"])
        self.history_store.retention_days = normalized["history"]["retention_days"]
        reply = {"ok": True, "config_revision": normalized["revision"], "config": copy.deepcopy(normalized)}
        if changed is not None:
            reply["changed"] = changed
        return reply

    def _set(self, params):
        fan, pct = int(params.get("fan", 0)), int(params.get("pct", -1))
        fans = self.fans()
        if fan not in fans or not 0 <= pct <= 100:
            raise ValueError("fan must be a present fan index and percent 0-100")
        with self.lock:
            self._require_revision(params)
            linked = bool(self.state.get("linked"))
            affected = fans if linked else [fan]
            self.state["mode"] = "manual"
            for target in affected:
                control = self.config["fans"][f"fan{target}"]["control"]
                control["target"] = pct
            self.backend.lock()
            reply = self._commit({"targets": {str(t): pct for t in affected}, "mode": "manual"})
            self.tick_control()
        return reply

    def _profile(self, params):
        profile = params.get("profile")
        if profile not in PROFILES:
            raise ValueError("unknown profile")
        with self.lock:
            self._require_revision(params)
            self.config["profile"] = profile
            self.config["mode"] = "auto"
            if profile == "custom":
                entry = self.config["curves"].setdefault("legacy_custom", {
                    "name": "Custom", "temp_source": "max", "points": [],
                })
                if not entry.get("points"):
                    entry["points"] = [list(p) for p in PROFILES["custom"]]
            self.backend.lock()
            return self._commit({"profile": profile, "mode": "auto"})

    def _mode(self, params):
        mode = params.get("mode")
        if mode not in ("manual", "curve", "released"):
            raise ValueError("unknown mode")
        with self.lock:
            self._require_revision(params)
            self.config["mode"] = LEGACY_MODE_MAP[mode]
            if mode == "released":
                self.backend.release()
            else:
                self.backend.lock()
            return self._commit({"mode": mode})

    def _custom(self, params):
        curve = normalize_curve(params.get("curve"))
        which = params.get("which", "shared")
        with self.lock:
            self._require_revision(params)
            points = [list(p) for p in curve]
            if which == "cpu":
                self.config["curves"]["cpu_default"] = {
                    "name": "CPU Curve", "temp_source": "cpu", "points": points,
                }
                fan1 = self.config["fans"]["fan1"]["control"]
                if fan1["type"] in ("curve", "profile"):
                    fan1.update({"type": "curve", "curve_ref": "cpu_default"})
            elif which == "gpu":
                self.config["curves"]["gpu_default"] = {
                    "name": "GPU Curve", "temp_source": "gpu", "points": points,
                }
                for fan_id in ("fan2", "fan3"):
                    control = self.config["fans"][fan_id]["control"]
                    if control["type"] in ("curve", "profile"):
                        control.update({"type": "curve", "curve_ref": "gpu_default"})
            else:
                self.config["curves"]["legacy_custom"] = {
                    "name": "Custom", "temp_source": "max", "points": points,
                }
            self.config["profile"] = "custom"
            self.config["mode"] = "auto"
            self.backend.lock()
            return self._commit({"profile": "custom", "which": which})

    @staticmethod
    def _checked_int(name, value, low, high):
        if isinstance(value, bool):
            raise RpcError(f"{name} must be an integer {low}-{high}", "INVALID_ARGUMENT")
        try:
            number = int(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RpcError(f"{name} must be an integer {low}-{high}", "INVALID_ARGUMENT") from exc
        if not low <= number <= high:
            raise RpcError(f"{name} must be {low}-{high}", "INVALID_ARGUMENT")
        return number

    def _config(self, params):
        with self.lock:
            self._require_revision(params)
            view = self.state
            if "max_duty" in params:
                view["max_duty"] = self._checked_int("max_duty", params["max_duty"], 20, SAFE_MAX_DUTY)
            if "hysteresis" in params:
                view["hysteresis"] = self._checked_int("hysteresis", params["hysteresis"], 0, 30)
            if "critical_temp" in params:
                view["critical_temp"] = self._checked_int("critical_temp", params["critical_temp"], 70, 110)
            if "linked" in params:
                view["linked"] = bool(params["linked"])
            if "theme" in params:
                if params["theme"] not in ("dark", "light"):
                    raise RpcError("theme must be dark or light", "INVALID_ARGUMENT")
                view["theme"] = params["theme"]
            if "alerts" in params and isinstance(params["alerts"], dict):
                view["alerts"] = params["alerts"]
            if "cpu_sensor" in params:
                pin = params["cpu_sensor"]
                self.config["sensors"]["cpu_pin"] = pin if isinstance(pin, (dict, str)) else None
            if "gpu_sensor" in params:
                pin = params["gpu_sensor"]
                self.config["sensors"]["gpu_pin"] = pin if isinstance(pin, (dict, str)) else None
            if "temperature_unit" in params:
                unit = params["temperature_unit"]
                if unit not in ("c", "f"):
                    raise RpcError("temperature_unit must be c or f", "INVALID_ARGUMENT")
                self.config["display"]["temperature_unit"] = unit
            for section in ("safety", "display", "notifications", "sensors", "history"):
                if section in params:
                    if not isinstance(params[section], dict):
                        raise RpcError(f"{section} must be an object", "INVALID_ARGUMENT")
                    self._validate_section(section, params[section])
                    self.config[section].update(params[section])
            return self._commit()

    @staticmethod
    def _validate_section(section, values):
        check = FanController._checked_int
        if section == "safety":
            allowed = {"critical_temp", "global_max_duty", "hysteresis", "firmware_fallback"}
            for key in values:
                if key not in allowed:
                    raise RpcError(f"unknown safety key {key!r}", "INVALID_ARGUMENT")
            if "critical_temp" in values:
                check("safety.critical_temp", values["critical_temp"], 70, 110)
            if "global_max_duty" in values:
                check("safety.global_max_duty", values["global_max_duty"], 20, SAFE_MAX_DUTY)
            if "hysteresis" in values:
                check("safety.hysteresis", values["hysteresis"], 0, 30)
        elif section == "display":
            allowed = {"theme", "temperature_unit"}
            for key in values:
                if key not in allowed:
                    raise RpcError(f"unknown display key {key!r}", "INVALID_ARGUMENT")
            if "theme" in values and values["theme"] not in ("dark", "light", "system"):
                raise RpcError("display.theme must be dark, light, or system", "INVALID_ARGUMENT")
            if "temperature_unit" in values and values["temperature_unit"] not in ("c", "f"):
                raise RpcError("display.temperature_unit must be c or f", "INVALID_ARGUMENT")
        elif section == "notifications":
            allowed = {"enabled", "critical_temp"}
            for key in values:
                if key not in allowed:
                    raise RpcError(f"unknown notifications key {key!r}", "INVALID_ARGUMENT")
        elif section == "sensors":
            allowed = {"cpu_pin", "gpu_pin", "pinned"}
            for key in values:
                if key not in allowed:
                    raise RpcError(f"unknown sensors key {key!r}", "INVALID_ARGUMENT")
            for key in ("cpu_pin", "gpu_pin"):
                if key in values and values[key] is not None and not isinstance(values[key], (dict, str)):
                    raise RpcError(f"sensors.{key} must be an object, an ID string, or null", "INVALID_ARGUMENT")
            if "pinned" in values and (not isinstance(values["pinned"], list)
                                       or not all(isinstance(p, str) for p in values["pinned"])):
                raise RpcError("sensors.pinned must be an array of IDs", "INVALID_ARGUMENT")
        elif section == "history":
            allowed = {"persist", "retention_days"}
            for key in values:
                if key not in allowed:
                    raise RpcError(f"unknown history key {key!r}", "INVALID_ARGUMENT")
            if "retention_days" in values:
                check("history.retention_days", values["retention_days"], 1, 365)

    def _curves_save(self, params):
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("curve name required")
        curve = normalize_curve(params.get("curve"))
        with self.lock:
            self._require_revision(params)
            curve_id = None
            for cid, entry in self.config["curves"].items():
                if cid.startswith("named_") and entry.get("name") == name:
                    curve_id = cid
                    break
            if curve_id is None:
                base = sanitize_curve_id(name)
                curve_id, suffix = base, 2
                while curve_id in self.config["curves"]:
                    curve_id = f"{base}_{suffix}"
                    suffix += 1
            self.config["curves"][curve_id] = {
                "name": name, "temp_source": "max", "points": [list(p) for p in curve],
            }
            return self._commit({"id": curve_id, "name": name})

    def _curves_list(self):
        with self.lock:
            return {"names": sorted(
                entry.get("name", cid)
                for cid, entry in self.config["curves"].items()
                if cid.startswith("named_")
            ), "curves": [self._curve_out(cid) for cid in self.config["curves"]],
                "config_revision": self.config["revision"]}

    def _find_named_curve(self, name):
        for cid, entry in self.config["curves"].items():
            if cid.startswith("named_") and entry.get("name") == name:
                return cid, entry
        raise RpcError(f"unknown curve {name!r}", "NOT_FOUND")

    def _curves_export(self, params):
        if params.get("id"):
            with self.lock:
                if params["id"] not in self.config["curves"]:
                    raise RpcError("unknown curve", "NOT_FOUND")
                curve = self._curve_out(params["id"])
                return {**curve, "curve": curve["points"]}
        name = str(params.get("name", "")).strip()
        with self.lock:
            _cid, entry = self._find_named_curve(name)
            return {"name": name, "curve": [list(p) for p in entry["points"]]}

    def _curves_delete(self, params):
        """Delete a curve by v2 id or legacy name.

        Deleting a curve assigned to fans requires ``force`` (or reassignment);
        legacy callers that only know names keep their old behavior.
        """
        with self.lock:
            self._require_revision(params)
            curve_id = params.get("id")
            if curve_id is not None:
                if curve_id not in self.config["curves"]:
                    raise RpcError(f"unknown curve {curve_id!r}", "NOT_FOUND")
                force = bool(params.get("force", False))
            else:
                name = str(params.get("name", "")).strip()
                curve_id, _entry = self._find_named_curve(name)
                force = bool(params.get("force", True))
            self._delete_curve_by_id(curve_id, force=force)
            return self._commit({"deleted": curve_id})

    def _curves_load(self, params):
        name = str(params.get("name", "")).strip()
        with self.lock:
            self._require_revision(params)
            _cid, entry = self._find_named_curve(name)
            self.config["curves"]["legacy_custom"] = {
                "name": "Custom", "temp_source": "max",
                "points": [list(p) for p in entry["points"]],
            }
            self.config["profile"] = "custom"
            self.config["mode"] = "auto"
            self.backend.lock()
            return self._commit({"loaded": name})

    # --------------------------------------------------------------- v2 RPC
    def _capabilities(self):
        return {
            "protocol_version": PROTOCOL_VERSION,
            "schema_version": SCHEMA_VERSION,
            "version": APP_VERSION,
            "features": [
                "rules", "history", "per_fan_curves", "schedule",
                "decision_trace", "fan_test", "sensors_v2",
            ],
            "backend": getattr(self.backend, "name", "unknown"),
            "fan_count": len(self.fans()),
            "demo": self.demo,
        }

    def _fan_record(self, fan):
        fan_id = f"fan{fan}"
        cfg = self.config["fans"].get(fan_id) or default_config_v2()["fans"][fan_id]
        engine = self._last_engine
        trace = None
        if engine:
            for row in engine["per_fan"]:
                if row["fan_id"] == fan_id:
                    trace = row
                    break
        rpm_key, duty_key = f"rpm{fan}", f"fan{fan}"
        return {
            "id": fan_id,
            "index": fan,
            "name": cfg.get("name", fan_id),
            "enabled": cfg.get("enabled", True),
            "control": dict(cfg.get("control") or {"type": "linked"}),
            "min_duty": cfg.get("min_duty", 0),
            "max_duty": cfg.get("max_duty", 100),
            "rpm": self._readback.get(rpm_key),
            "duty": self._readback.get(duty_key, 0),
            "requested_duty": trace["final_duty"] if trace else None,
            "source_temp": trace["source_temp"] if trace else None,
            "sensor": trace["sensor_id"] if trace else None,
            "curve": trace["configured_curve"] if trace else None,
            "write_suppressed_reason": trace["write_suppressed_reason"] if trace else None,
        }

    def _fans_list(self):
        with self.lock:
            return {
                "fans": [self._fan_record(fan) for fan in self.fans()],
                "config_revision": int(self.config.get("revision", 0)),
            }

    def _fans_rename(self, params):
        fan_id = _fan_id(params.get("fan"))
        name = str(params.get("name", "")).strip()
        if not name:
            raise RpcError("name must be a non-empty string", "INVALID_ARGUMENT")
        with self.lock:
            self._require_revision(params)
            self.config["fans"][fan_id]["name"] = name[:64]
            return self._commit({"fan": fan_id, "name": name[:64]})

    def _fans_configure(self, params):
        fan_id = _fan_id(params.get("fan"))
        with self.lock:
            self._require_revision(params)
            fan = self.config["fans"][fan_id]
            if "name" in params:
                name = str(params["name"]).strip()
                if name:
                    fan["name"] = name[:64]
            if "enabled" in params:
                fan["enabled"] = bool(params["enabled"])
            if "min_duty" in params or "max_duty" in params:
                raw_min = params.get("min_duty", fan["min_duty"])
                raw_max = params.get("max_duty", fan["max_duty"])
                if isinstance(raw_min, bool) or isinstance(raw_max, bool):
                    raise RpcError("bounds must satisfy 0 <= min_duty <= max_duty <= 100", "INVALID_ARGUMENT")
                try:
                    min_duty = int(raw_min)
                    max_duty = int(raw_max)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise RpcError("bounds must satisfy 0 <= min_duty <= max_duty <= 100", "INVALID_ARGUMENT") from exc
                if not 0 <= min_duty <= max_duty <= 100:
                    raise RpcError("bounds must satisfy 0 <= min_duty <= max_duty <= 100", "INVALID_ARGUMENT")
                fan["min_duty"], fan["max_duty"] = min_duty, max_duty
            if "control" in params:
                control = params["control"]
                if not isinstance(control, dict):
                    raise RpcError("control must be an object", "INVALID_ARGUMENT")
                ctype = control.get("type")
                if ctype not in ("linked", "profile", "curve", "manual"):
                    raise RpcError(f"unknown control type {ctype!r}", "INVALID_ARGUMENT")
                new_control = dict(fan["control"])
                new_control["type"] = ctype
                if ctype == "curve":
                    ref = control.get("curve_ref")
                    if ref not in self.config["curves"]:
                        raise RpcError(f"unknown curve {ref!r}", "NOT_FOUND")
                    new_control["curve_ref"] = ref
                if ctype == "profile":
                    source = control.get("temp_source")
                    new_control["temp_source"] = source if source in ("cpu", "gpu") else (
                        "cpu" if fan_id == "fan1" else "gpu"
                    )
                if ctype == "manual":
                    target = control.get("target", fan["control"].get("target", 0))
                    if not isinstance(target, (int, float)) or isinstance(target, bool) or not 0 <= target <= 100:
                        raise RpcError("manual target must be 0-100", "INVALID_ARGUMENT")
                    new_control["target"] = int(target)
                fan["control"] = new_control
            return self._commit({"fan": self._fan_record(int(fan_id[-1]))})

    def _fans_test(self, params):
        fan_id = _fan_id(params.get("fan"))
        raw_delta = params.get("delta", 10)
        if isinstance(raw_delta, bool):
            raise RpcError("delta must be an integer", "INVALID_ARGUMENT")
        try:
            delta = int(raw_delta)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RpcError("delta must be an integer", "INVALID_ARGUMENT") from exc
        if not -100 <= delta <= 100:
            raise RpcError("delta must be between -100 and 100", "INVALID_ARGUMENT")
        try:
            duration_ms = int(params.get("duration_ms", 5000))
        except (TypeError, ValueError, OverflowError) as exc:
            raise RpcError("duration_ms must be an integer", "INVALID_ARGUMENT") from exc
        duration_ms = max(500, min(60000, duration_ms))
        with self.lock:
            engine = self._last_engine
            current = None
            if engine and int(fan_id[-1]) in engine["duties"]:
                current = engine["duties"][int(fan_id[-1])]
            if current is None:
                current = int(self._readback.get(fan_id, 0) or 0)
            duty = max(0, min(SAFE_MAX_DUTY, current + delta))
            self._overrides[fan_id] = {"duty": duty, "expires": time.time() + duration_ms / 1000}
            return {
                "ok": True,
                "fan": fan_id,
                "duty": duty,
                "expires": self._overrides[fan_id]["expires"],
                "duration_ms": duration_ms,
            }

    def _curve_out(self, curve_id):
        entry = self.config["curves"][curve_id]
        assigned = [
            fan_id for fan_id in FAN_IDS
            if self.config["fans"][fan_id]["control"].get("curve_ref") == curve_id
        ]
        return {
            "id": curve_id,
            "name": entry.get("name", curve_id),
            "temp_source": entry.get("temp_source", "max"),
            "points": [list(p) for p in entry.get("points", [])],
            "system": curve_id in SYSTEM_CURVE_IDS,
            "assigned_to": assigned,
        }

    def _curves_set(self, params):
        with self.lock:
            self._require_revision(params)
            try:
                points = normalize_curve(params.get("points"))
            except ValueError as exc:
                raise RpcError(str(exc), "INVALID_ARGUMENT") from exc
            curve_id = params.get("id")
            if curve_id is not None and not isinstance(curve_id, str):
                raise RpcError("curve id must be a string", "INVALID_ARGUMENT")
            name = str(params.get("name") or curve_id or "curve").strip() or "curve"
            if curve_id is None:
                base = sanitize_curve_id(name)
                curve_id, suffix = base, 2
                while curve_id in self.config["curves"]:
                    curve_id = f"{base}_{suffix}"
                    suffix += 1
            source = params.get("temp_source", "max")
            if source not in ("cpu", "gpu", "max") and not isinstance(source, str):
                source = "max"
            self.config["curves"][curve_id] = {
                "name": name, "temp_source": source, "points": [list(p) for p in points],
            }
            return self._commit({"id": curve_id, "curve": self._curve_out(curve_id)})
    def _delete_curve_by_id(self, curve_id, force):
        assigned = [
            fan_id for fan_id in FAN_IDS
            if self.config["fans"][fan_id]["control"].get("curve_ref") == curve_id
        ]
        if assigned and not force:
            raise RpcError(
                f"curve {curve_id!r} is assigned to {', '.join(assigned)}",
                "CONFIG_ERROR",
            )
        self.config["curves"].pop(curve_id, None)
        for fan_id in assigned:
            control = self.config["fans"][fan_id]["control"]
            control.update({
                "type": "profile",
                "temp_source": "cpu" if fan_id == "fan1" else "gpu",
                "curve_ref": None,
            })

    def _curves_assign(self, params):
        fan_id = _fan_id(params.get("fan"))
        curve_ref = params.get("curve_ref")
        with self.lock:
            self._require_revision(params)
            if curve_ref not in self.config["curves"]:
                raise RpcError(f"unknown curve {curve_ref!r}", "NOT_FOUND")
            control = self.config["fans"][fan_id]["control"]
            control.update({"type": "curve", "curve_ref": curve_ref})
            return self._commit({"fan": fan_id, "curve_ref": curve_ref})

    def _sensors_list(self):
        records = self._records()
        engine = self._last_engine
        attribution = {}
        if engine:
            for row in engine["per_fan"]:
                attribution[row["fan_id"]] = row["sensor_id"]
        pin_cpu = self.config["sensors"]["cpu_pin"]
        pin_gpu = self.config["sensors"]["gpu_pin"]
        aliases = {}
        for alias, pin in (("cpu", pin_cpu), ("gpu", pin_gpu)):
            if isinstance(pin, str):
                aliases[alias] = pin if any(r["id"] == pin for r in records) else None
            else:
                matched = [r["id"] for r in records if alias in r.get("aliases", [])]
                aliases[alias] = matched[0] if matched else None
        return {
            "sensors": records,
            "aliases": aliases,
            "attribution": attribution,
            "hottest": (
                max(records, key=lambda r: r["value"])["id"]
                if records else None
            ),
            "config_revision": int(self.config.get("revision", 0)),
        }

    def _rules_list(self):
        with self.lock:
            engine = self._last_engine
            states = engine["rule_states"] if engine else {}
            rules = []
            for rule in self.config["rules"]:
                runtime = self._rule_state.get(rule["id"]) or {}
                rules.append({
                    **rule,
                    "state": states.get(rule["id"], "inactive"),
                    "runtime": {
                        "consecutive_match_count": runtime.get("consecutive_match_count", 0),
                        "active_since": _iso_or_none(runtime.get("active_since")),
                        "last_fired_at": _iso_or_none(runtime.get("last_fired_at")),
                        "cooldown_until": _iso_or_none(runtime.get("cooldown_until")),
                    },
                })
            return {"rules": rules, "config_revision": int(self.config.get("revision", 0))}

    def _rules_set(self, params):
        rule = params.get("rule")
        with self.lock:
            self._require_revision(params)
            try:
                validate_rule_payload(rule)
            except ValueError as exc:
                raise RpcError(str(exc), "INVALID_ARGUMENT") from exc
            if rule.get("action", {}).get("type") == "run_command":
                raise RpcError("Command execution is not enabled in this release", "UNSUPPORTED")
            normalized = _normalize_rule(rule, [], set())
            existing = list(self.config["rules"])
            index = next((i for i, r in enumerate(existing) if r["id"] == normalized["id"]), None)
            if index is None:
                existing.append(normalized)
            else:
                existing[index] = normalized
            self.config["rules"] = existing
            return self._commit({"rule": normalized})

    def _rules_delete(self, params):
        rule_id = str(params.get("id", "")).strip()
        with self.lock:
            self._require_revision(params)
            remaining = [r for r in self.config["rules"] if r["id"] != rule_id]
            if len(remaining) == len(self.config["rules"]):
                raise RpcError(f"unknown rule {rule_id!r}", "NOT_FOUND")
            self.config["rules"] = remaining
            self._rule_state.pop(rule_id, None)
            return self._commit({"deleted": rule_id})

    def _rules_context(self):
        records = self._records()
        cpu, gpu = self.cpu_temp(), self.gpu_temp()
        temps = {"cpu": cpu, "gpu": gpu, "max": select_control_temp(cpu, gpu)}
        for record in records:
            temps[record["id"]] = record["value"]
        rpm = {f"fan{i}": self._readback.get(f"rpm{i}") for i in (1, 2, 3)}
        return {"temps": temps, "rpm": rpm, "fans_present": self.fans()}

    def _rules_test(self, params):
        rule = params.get("rule")
        if rule is None:
            rule_id = str(params.get("id", "")).strip()
            rule = next((r for r in self.config["rules"] if r["id"] == rule_id), None)
            if rule is None:
                raise RpcError(f"unknown rule {rule_id!r}", "NOT_FOUND")
        try:
            validate_rule_payload(rule)
        except ValueError as exc:
            raise RpcError(str(exc), "INVALID_ARGUMENT") from exc
        context = self._rules_context()
        supplied = params.get("context")
        if supplied is not None:
            if not isinstance(supplied, dict):
                raise RpcError("context must be an object", "INVALID_ARGUMENT")
            for key in ("temps", "rpm"):
                if key in supplied and not isinstance(supplied[key], dict):
                    raise RpcError(f"context.{key} must be an object", "INVALID_ARGUMENT")
            if "fans_present" in supplied and not isinstance(supplied["fans_present"], list):
                raise RpcError("context.fans_present must be an array", "INVALID_ARGUMENT")
            context = {**context, **supplied}
        now = local_now()
        try:
            matched, missing = _test_trigger(rule, context, now)
            action = rule.get("action") or {}
            reason = _trigger_reason(rule, context, matched, missing)
        except (TypeError, ValueError, AttributeError) as exc:
            raise RpcError(f"invalid test context: {exc}", "INVALID_ARGUMENT") from exc
        return {
            "matched": matched,
            "would_activate": matched,
            "action": action,
            "reason": reason,
            "priority": rule.get("priority", 100),
            "sustain_ticks": (rule.get("condition") or {}).get("sustain_ticks", 0),
        }

    def _schedule_get(self):
        with self.lock:
            engine = self._last_engine
            return {
                "schedule": self.config["schedule"],
                "active": engine["schedule_active"] if engine else [],
                "config_revision": int(self.config.get("revision", 0)),
            }

    def _schedule_set(self, params):
        schedule = params.get("schedule")
        if not isinstance(schedule, dict):
            raise RpcError("schedule must be an object", "INVALID_ARGUMENT")
        with self.lock:
            self._require_revision(params)
            merged = dict(self.config)
            merged["schedule"] = schedule
            from fan_policy import normalize_config

            try:
                normalized, _warnings = normalize_config(merged)
            except ConfigError as exc:
                raise RpcError(str(exc), "CONFIG_ERROR") from exc
            self.config["schedule"] = normalized["schedule"]
            return self._commit({"schedule": self.config["schedule"]})

    def _history_query(self, params):
        fans = params.get("fans")
        if fans is not None:
            if not isinstance(fans, list):
                raise RpcError("fans must be an array", "INVALID_ARGUMENT")
            fans = [_fan_id(f) for f in fans]
        sensors = params.get("sensors")
        if sensors is not None and (not isinstance(sensors, list) or
                                    not all(isinstance(s, str) for s in sensors)):
            raise RpcError("sensors must be an array of IDs", "INVALID_ARGUMENT")
        max_points = params.get("max_points")
        try:
            if max_points is not None:
                max_points = max(10, min(5000, int(max_points)))
        except (TypeError, ValueError, OverflowError) as exc:
            raise RpcError("max_points must be an integer", "INVALID_ARGUMENT") from exc
        since, until = self._history_range(params)
        return self.history_store.query(
            since=since,
            until=until,
            sensors=sensors,
            fans=fans,
            max_points=max_points,
        )

    def _history_stats(self, params):
        since, until = self._history_range(params)
        return self.history_store.stats(since=since, until=until)

    @staticmethod
    def _history_range(params):
        try:
            for key in ("since", "until"):
                value = params.get(key)
                if value is not None and isinstance(value, bool):
                    raise ValueError("bool timestamp")
            values = [None if params.get(k) is None else float(params[k]) for k in ("since", "until")]
            if any(v is not None and not math.isfinite(v) for v in values):
                raise ValueError("non-finite timestamp")
            since, until = values
            if since is not None and until is not None and since > until:
                raise ValueError("since is after until")
            return since, until
        except (TypeError, ValueError, OverflowError) as exc:
            raise RpcError("history range requires finite epoch timestamps with since <= until", "INVALID_ARGUMENT") from exc

    def _diagnostics_snapshot(self):
        with self.lock:
            engine = self._last_engine
            return {
                "daemon": {
                    "version": APP_VERSION,
                    "protocol_version": PROTOCOL_VERSION,
                    "schema_version": SCHEMA_VERSION,
                    "config_path": str(self.config_path),
                    "config_revision": int(self.config.get("revision", 0)),
                    "uptime_s": round(time.time() - self.started_at, 1),
                    "demo": self.demo,
                },
                "backend": {
                    "name": getattr(self.backend, "name", "unknown"),
                    "fans": self.fans(),
                },
                "capabilities": self._capabilities(),
                "sensors": self._records(),
                "configuration": {
                    "mode": self.config["mode"],
                    "profile": self.config["profile"],
                    "effective_mode": engine["effective_mode"] if engine else self.config["mode"],
                    "effective_profile": engine["effective_profile"] if engine else self.config["profile"],
                    "warnings": list(self._warnings),
                },
                "runtime": {
                    "overrides": {
                        fan_id: {**entry, "expires_in": round(entry["expires"] - time.time(), 1)}
                        for fan_id, entry in self._overrides.items()
                        if entry["expires"] > time.time()
                    },
                    "missing_temp_ticks": self._missing,
                    "released_for_fault": self._released_for_fault,
                },
                "history": self.history_store.status(),
                "warnings": list(engine["warnings"]) if engine else [],
            }

    # ------------------------------------------------------------------ ticks
    def tick_sensors(self):
        records = scan_sensor_records(demo=self.demo, include_nvidia=True)
        with self.lock:
            self._sensor_records = records

    def tick_readback(self):
        try:
            fans = self.fans()
            snap = {
                "fan1": self.backend.read_duty(1) if 1 in fans else 0,
                "fan2": self.backend.read_duty(2) if 2 in fans else 0,
                "fan3": self.backend.read_duty(3) if 3 in fans else 0,
                "rpm1": self.backend.read_rpm(1) if 1 in fans else None,
                "rpm2": self.backend.read_rpm(2) if 2 in fans else None,
                "rpm3": self.backend.read_rpm(3) if 3 in fans else None,
                "ec_temp1": self.backend.read_temp() or 0,
                "ec_temp2": self.backend.read_temp2() or 0,
                "updated": time.time(),
            }
            with self.lock:
                self._readback = snap
        except (OSError, FanBackendError) as exc:
            print(f"EC read failed: {exc}", flush=True)
            self._backend_error = str(exc)

    def tick_history(self):
        with self.lock:
            records = list(self._sensor_records)
            readback = dict(self._readback)
            config = self.config
            engine = self._last_engine
        cpu = _resolve_cpu_temp(records, config, readback)
        gpu = _resolve_gpu_temp(records, config)
        point = {
            "time": time.time(),
            "temp": cpu,
            "gpu_temp": gpu,
            "control_temp": select_control_temp(cpu, gpu),
            "fan1": readback.get("fan1", 0),
            "fan2": readback.get("fan2", 0),
            "fan3": readback.get("fan3", 0),
            "rpm1": readback.get("rpm1"),
            "rpm2": readback.get("rpm2"),
            "rpm3": readback.get("rpm3"),
        }
        with self.lock:
            self.history.append(point)
            allow = set(self.config["sensors"].get("pinned") or [])
            allow.update({"cpu", "gpu", "max"})
            sensor_values = {}
            for record in self._sensor_records:
                if record["id"] in allow or record.get("aliases"):
                    sensor_values[record["id"]] = record["value"]
            fans_payload = {
                f"fan{i}": {
                    "rpm": self._readback.get(f"rpm{i}"),
                    "duty": self._readback.get(f"fan{i}", 0),
                    "requested": (engine or {}).get("duties", {}).get(i),
                }
                for i in (1, 2, 3)
            }
            self.history_store.append_sample(
                ts=point["time"],
                mode=self.config["mode"],
                profile=self.config["profile"],
                effective_mode=(engine or {}).get("effective_mode", self.config["mode"]),
                effective_profile=(engine or {}).get("effective_profile", self.config["profile"]),
                hottest_temp=point["control_temp"],
                critical=bool((engine or {}).get("critical")),
                fans=fans_payload,
                sensors=sensor_values,
            )
        self.history_store.prune()

    def _prune_overrides(self, now_ts):
        expired = [fid for fid, entry in self._overrides.items() if entry["expires"] <= now_ts]
        for fid in expired:
            del self._overrides[fid]

    def tick_control(self):
        return self._tick_control()

    def _tick_control(self):
        with self.lock:
            fans_present = tuple(self.fans())
            current = {
                1: self._readback["fan1"],
                2: self._readback["fan2"],
                3: self._readback["fan3"],
            }
            rpm = {f"fan{i}": self._readback[f"rpm{i}"] for i in (1, 2, 3)}
            missing = self._missing
            self._prune_overrides(time.time())
            overrides = {fid: dict(entry) for fid, entry in self._overrides.items()}
            records = list(self._sensor_records)
            readback = dict(self._readback)
            config = copy.deepcopy(self.config)
            rule_state = {rid: dict(entry) for rid, entry in self._rule_state.items()}
            startup = self._startup_tick
        cpu = _resolve_cpu_temp(records, config, readback)
        gpu = _resolve_gpu_temp(records, config)
        temps = {"cpu": cpu, "gpu": gpu, "max": select_control_temp(cpu, gpu)}
        for record in records:
            temps[record["id"]] = record["value"]
        context = {
            "cpu_temp": cpu,
            "gpu_temp": gpu,
            "temps": temps,
            "rpm": rpm,
            "fans_present": fans_present,
            "current_duties": current,
        }
        now = local_now(config["schedule"].get("timezone", "local"))
        runtime = {
            "now": now,
            "rule_state": rule_state,
            "overrides": overrides,
            "missing_count": missing,
            "startup": startup,
        }
        result = evaluate_pipeline(config, context, runtime)
        result["failed_writes"] = {}
        for warning in result["warnings"]:
            print(f"policy: {warning}", flush=True)

        if result["action"] == "release":
            try:
                self.backend.release()
            except (OSError, FanBackendError) as exc:
                print(f"EC release failed: {exc}", flush=True)
            with self.lock:
                self._rule_state = result["rule_runtime"]
                self._last_engine = result
                self._startup_tick = False
                self._released_for_fault = True
                self._record_decisions_locked(result)
            return TickResult(result)

        if result["missing_temp"]:
            missing = missing + 1
        else:
            missing = 0

        if result["duties"]:
            with self.lock:
                released = self._released_for_fault
            if released:
                try:
                    self.backend.lock()
                except (OSError, FanBackendError) as exc:
                    print(f"EC lock failed: {exc}", flush=True)
                with self.lock:
                    self._released_for_fault = False
            for fan, duty in result["writes"].items():
                try:
                    self.backend.write_duty(fan, duty)
                    backend_error = None
                except (OSError, FanBackendError) as exc:
                    result["failed_writes"][fan] = str(exc)
                    backend_error = str(exc)
                    print(f"EC write failed: {exc}", flush=True)
                with self.lock:
                    self._backend_error = backend_error
            if not result["writes"]:
                try:
                    self.backend.ping()
                except (OSError, FanBackendError):
                    pass
        with self.lock:
            self._rule_state = result["rule_runtime"]
            self._last_engine = result
            self._startup_tick = False
            self._missing = missing
            active_notifications = set()
            for notification in result["notifications"]:
                key = (notification["rule"], notification["message"])
                active_notifications.add(key)
                if key not in self._active_notifications:
                    self._notification_seq += 1
                    self._notifications.append({**notification, "seq": self._notification_seq})
            self._active_notifications = active_notifications
            self._record_decisions_locked(result)
        return TickResult(result)

    def _record_decisions(self, result):
        with self.lock:
            self._record_decisions_locked(result)

    def _record_decisions_locked(self, result):
        for trace in result["per_fan"]:
            fan_index = int(trace["fan_id"][-1])
            trace = dict(trace)
            trace["write_performed"] = fan_index in result["writes"] and fan_index not in result.get("failed_writes", {})
            if fan_index in result.get("failed_writes", {}):
                trace["write_suppressed_reason"] = "backend write failed: " + result["failed_writes"][fan_index]
            self._decisions.append(trace)

    def diagnose_text(self):
        from fan_diagnostics import diagnose
        return diagnose(self.config_path)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.backend.release()
        except (OSError, FanBackendError):
            pass
        try:
            self.backend.close()
        except (OSError, FanBackendError):
            pass
        try:
            self.history_store.close()
        except Exception:  # noqa: BLE001 - close must never raise
            pass


def _iso_or_none(value):
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.datetime.fromtimestamp(float(value)).isoformat()
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


def _safe_defaults():
    from fan_policy import normalize_config

    doc, warnings = normalize_config(default_config_v2())
    return doc, warnings


def _test_trigger(rule, context, now):
    return fan_rules.rule_triggered(rule, context, now)


def _trigger_reason(rule, context, matched, missing):
    trigger = rule.get("trigger") or {}
    ttype = trigger.get("type")
    if missing:
        return f"sensor {missing!r} unavailable"
    if ttype in ("temp_above", "temp_below"):
        value, _available = fan_rules.resolve_sensor_value(trigger.get("sensor"), context)
        threshold = trigger.get("value")
        symbol = ">" if ttype == "temp_above" else "<"
        return f"{trigger.get('sensor')} {value:.1f} C {symbol} threshold {threshold} C"
    if ttype == "rpm_below":
        rpm = (context.get("rpm") or {}).get(str(trigger.get("fan")))
        return f"{trigger.get('fan')} {rpm} rpm < threshold {trigger.get('value')}"
    if ttype == "time_range":
        return f"in window {trigger.get('start')}-{trigger.get('end')} ({','.join(trigger.get('days') or [])})"
    if ttype == "on_startup":
        return "startup trigger"
    return f"trigger {ttype}"
