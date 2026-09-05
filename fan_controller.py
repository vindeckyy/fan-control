#!/usr/bin/env python3
"""In-process fan controller and JSON-RPC surface (no HTTP, no GTK)."""

from __future__ import annotations

import collections
import pathlib
import threading
import time

from fan_backend import FanBackendError
from fan_policy import (
    PROFILES,
    SAFE_MAX_DUTY,
    apply_defaults,
    config_payload,
    decide,
    history_csv,
    load_config,
    normalize_curve,
    pick_cpu_temp,
    pick_gpu_temp,
    save_config,
    scan_sensors,
    select_control_temp,
)

HISTORY_LEN = 900


def _int_targets(raw):
    return {int(key): int(value) for key, value in raw.items()}


class FanController:
    def __init__(self, backend, config_path, runtime_dir, demo=False, fan_limit=None):
        self.backend = backend
        self.config_path = pathlib.Path(config_path)
        self.runtime_dir = pathlib.Path(runtime_dir)
        self.demo = demo
        self.fan_limit = fan_limit
        self.lock = threading.RLock()
        self.history = collections.deque(maxlen=HISTORY_LEN)
        self._sensors = []
        self._readback = {
            "fan1": 0, "fan2": 0, "fan3": 0,
            "rpm1": None, "rpm2": None, "rpm3": None,
            "ec_temp1": 0, "ec_temp2": 0, "updated": 0,
        }
        self._missing = 0
        self._released_for_fault = False
        self._closed = False
        self.state = apply_defaults(self._read_config())
        self._apply_mode_to_backend()

    def _read_config(self):
        try:
            return load_config(self.config_path)
        except ValueError:
            return apply_defaults({})

    def _persist(self):
        try:
            save_config(self.config_path, self.state)
        except OSError as exc:
            print(f"warning: could not save {self.config_path}: {exc}", flush=True)

    def _apply_mode_to_backend(self):
        try:
            if self.state["mode"] == "released":
                self.backend.release()
            else:
                self.backend.lock()
        except (OSError, FanBackendError) as exc:
            print(f"EC lock/release failed: {exc}", flush=True)

    def fans(self):
        try:
            present = list(self.backend.fans())
        except (OSError, FanBackendError):
            present = [1, 2]
        return [fan for fan in present if self.fan_limit is None or fan <= self.fan_limit]

    def reload_config(self):
        """SIGHUP path: re-read the config file and re-apply the EC mode."""
        with self.lock:
            self.state = apply_defaults(self._read_config())
            self._apply_mode_to_backend()

    def cpu_temp(self):
        with self.lock:
            sensors = list(self._sensors)
            pin = self.state.get("cpu_sensor")
            fallback = self._readback["ec_temp1"]
        return pick_cpu_temp(sensors, pin, fallback)

    def gpu_temp(self):
        with self.lock:
            sensors = list(self._sensors)
            pin = self.state.get("gpu_sensor")
        return pick_gpu_temp(sensors, pin)

    def control_temp(self):
        return select_control_temp(self.cpu_temp(), self.gpu_temp())

    def snapshot(self):
        with self.lock:
            result = dict(self._readback)
            result.update(config_payload(self.state))
            result["targets"] = {str(k): int(v) for k, v in _int_targets(self.state["targets"]).items()}
            result["custom_curve"] = [list(p) for p in self.state["curve"]]
            result["temps"] = list(self._sensors)
            result["primary_temp"] = self.cpu_temp()
            result["gpu_temp"] = self.gpu_temp()
            result["control_temp"] = self.control_temp()
            result["fans"] = self.fans()
            result["backend"] = getattr(self.backend, "name", "unknown")
            result["demo"] = self.demo
            result["profiles"] = {name: [list(p) for p in curve] for name, curve in PROFILES.items()}
            result["fault_missing"] = self._missing
            result["critical_active"] = (
                result["control_temp"] is not None
                and result["control_temp"] >= self.state["critical_temp"]
                and self.state["mode"] != "released"
            )
        return result

    def live_snapshot(self):
        with self.lock:
            result = dict(self._readback)
            result["targets"] = {str(k): int(v) for k, v in _int_targets(self.state["targets"]).items()}
            result["temps"] = list(self._sensors)
            result["primary_temp"] = self.cpu_temp()
            result["gpu_temp"] = self.gpu_temp()
            result["control_temp"] = self.control_temp()
            result["mode"] = self.state["mode"]
            result["profile"] = self.state["profile"]
            result["linked"] = bool(self.state.get("linked"))
            result["demo"] = self.demo
            result["fault_missing"] = self._missing
            result["critical_active"] = (
                result["control_temp"] is not None
                and result["control_temp"] >= self.state["critical_temp"]
                and self.state["mode"] != "released"
            )
        return result

    def handle(self, method, params=None):
        params = params or {}
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        handler = {
            "snapshot": lambda: self.snapshot(),
            "live": lambda: self.live_snapshot(),
            "config.get": lambda: config_payload(self.state),
            "history": lambda: self._history(),
            "set": lambda: self._set(params),
            "profile": lambda: self._profile(params),
            "mode": lambda: self._mode(params),
            "custom": lambda: self._custom(params),
            "config": lambda: self._config(params),
            "curves.save": lambda: self._curves_save(params),
            "curves.list": lambda: self._curves_list(),
            "curves.export": lambda: self._curves_export(params),
            "curves.delete": lambda: self._curves_delete(params),
            "curves.load": lambda: self._curves_load(params),
            "history.export": lambda: self._history_export(),
            "diagnose": lambda: {"text": self.diagnose_text()},
        }.get(method)
        if handler is None:
            raise ValueError(f"unknown method {method!r}")
        return handler()

    def _history(self):
        with self.lock:
            return {"history": list(self.history)}

    def _history_export(self):
        with self.lock:
            return {"csv": history_csv(list(self.history))}

    def _set(self, params):
        fan, pct = int(params.get("fan", 0)), int(params.get("pct", -1))
        fans = self.fans()
        if fan not in fans or not 0 <= pct <= 100:
            raise ValueError("fan must be a present fan index and percent 0-100")
        with self.lock:
            self.state["targets"][str(fan)] = pct
            if self.state.get("linked"):
                for other in fans:
                    self.state["targets"][str(other)] = pct
            self.state["mode"] = "manual"
            cap = self.state["max_duty"]
            writes = {}
            if self.state["linked"]:
                for other in fans:
                    writes[other] = round(pct * cap / 100)
            else:
                writes[fan] = round(pct * cap / 100)
            self.backend.lock()
            for target, duty in writes.items():
                self.backend.write_duty(target, duty)
        self._persist()
        return {"ok": True}

    def _profile(self, params):
        profile = params.get("profile")
        if profile not in PROFILES:
            raise ValueError("unknown profile")
        with self.lock:
            self.state["profile"] = profile
            self.state["mode"] = "curve"
            if profile == "custom":
                if not self.state["curve"]:
                    self.state["curve"] = list(PROFILES["custom"])
            else:
                self.state["curve"] = list(PROFILES[profile])
            self.backend.lock()
        self._persist()
        return {"ok": True}

    def _mode(self, params):
        mode = params.get("mode")
        if mode not in ("manual", "curve", "released"):
            raise ValueError("unknown mode")
        with self.lock:
            self.state["mode"] = mode
            if mode == "released":
                self.backend.release()
            else:
                self.backend.lock()
        self._persist()
        return {"ok": True}

    def _custom(self, params):
        curve = normalize_curve(params.get("curve"))
        which = params.get("which", "shared")
        with self.lock:
            if which == "cpu":
                self.state["curve_cpu"] = curve
            elif which == "gpu":
                self.state["curve_gpu"] = curve
            else:
                self.state["curve"] = curve
            self.state["profile"] = "custom"
            self.state["mode"] = "curve"
            self.backend.lock()
        self._persist()
        return {"ok": True}

    def _config(self, params):
        with self.lock:
            if "max_duty" in params:
                self.state["max_duty"] = max(20, min(SAFE_MAX_DUTY, int(params["max_duty"])))
            if "hysteresis" in params:
                self.state["hysteresis"] = max(0, min(30, int(params["hysteresis"])))
            if "critical_temp" in params:
                self.state["critical_temp"] = max(70, min(110, int(params["critical_temp"])))
            if "linked" in params:
                self.state["linked"] = bool(params["linked"])
            if "theme" in params and params["theme"] in ("dark", "light"):
                self.state["theme"] = params["theme"]
            if "alerts" in params and isinstance(params["alerts"], dict):
                self.state["alerts"] = {"desktop": bool(params["alerts"].get("desktop", False))}
            if "cpu_sensor" in params:
                pin = params["cpu_sensor"]
                self.state["cpu_sensor"] = pin if isinstance(pin, dict) else None
            if "gpu_sensor" in params:
                pin = params["gpu_sensor"]
                self.state["gpu_sensor"] = pin if isinstance(pin, dict) else None
        self._persist()
        return {"ok": True}

    def _curves_save(self, params):
        name = str(params.get("name", "")).strip()
        if not name:
            raise ValueError("curve name required")
        curve = normalize_curve(params.get("curve"))
        with self.lock:
            named = dict(self.state["named_curves"])
            named[name] = curve
            self.state["named_curves"] = named
        self._persist()
        return {"ok": True, "name": name}

    def _curves_list(self):
        with self.lock:
            return {"names": sorted(self.state["named_curves"])}

    def _curves_export(self, params):
        name = str(params.get("name", "")).strip()
        with self.lock:
            if name not in self.state["named_curves"]:
                raise ValueError("unknown curve")
            curve = [list(p) for p in self.state["named_curves"][name]]
        return {"name": name, "curve": curve}

    def _curves_delete(self, params):
        name = str(params.get("name", "")).strip()
        with self.lock:
            named = dict(self.state["named_curves"])
            named.pop(name, None)
            self.state["named_curves"] = named
        self._persist()
        return {"ok": True}

    def _curves_load(self, params):
        name = str(params.get("name", "")).strip()
        with self.lock:
            if name not in self.state["named_curves"]:
                raise ValueError("unknown curve")
            self.state["curve"] = list(self.state["named_curves"][name])
            self.state["profile"] = "custom"
            self.state["mode"] = "curve"
            self.backend.lock()
        self._persist()
        return {"ok": True}

    def tick_sensors(self):
        found = scan_sensors(demo=self.demo, include_nvidia=True)
        with self.lock:
            self._sensors = found

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

    def tick_history(self):
        point = {
            "time": int(time.time()),
            "temp": self.cpu_temp(),
            "gpu_temp": self.gpu_temp(),
            "control_temp": self.control_temp(),
            "fan1": self._readback["fan1"],
            "fan2": self._readback["fan2"],
            "fan3": self._readback["fan3"],
            "rpm1": self._readback["rpm1"],
            "rpm2": self._readback["rpm2"],
            "rpm3": self._readback["rpm3"],
        }
        with self.lock:
            self.history.append(point)

    def tick_control(self):
        with self.lock:
            state = dict(self.state)
            current = {
                1: self._readback["fan1"],
                2: self._readback["fan2"],
                3: self._readback["fan3"],
            }
            missing = self._missing
            fans = tuple(self.fans())
        cpu = self.cpu_temp()
        gpu = self.gpu_temp()
        decision = decide(
            mode=state["mode"],
            profile=state["profile"],
            cpu_temp=cpu,
            gpu_temp=gpu,
            targets=_int_targets(state["targets"]),
            max_duty=state["max_duty"],
            hysteresis=state["hysteresis"],
            critical_temp=state["critical_temp"],
            linked=state["linked"],
            shared_curve=list(PROFILES[state["profile"]]) if state["profile"] in PROFILES and state["profile"] != "custom" else state["curve"],
            curve_cpu=state.get("curve_cpu"),
            curve_gpu=state.get("curve_gpu"),
            current_duties=current,
            missing_count=missing,
            fans=fans,
        )
        if decision.action == "idle":
            if decision.missing_temp:
                with self.lock:
                    self._missing = missing + 1
            else:
                with self.lock:
                    self._missing = 0
            return decision
        if decision.action == "release":
            try:
                self.backend.release()
            except (OSError, FanBackendError) as exc:
                print(f"EC release failed: {exc}", flush=True)
            with self.lock:
                self._released_for_fault = True
                self._missing = missing
            return decision
        with self.lock:
            if self._released_for_fault:
                try:
                    self.backend.lock()
                except (OSError, FanBackendError) as exc:
                    print(f"EC lock failed: {exc}", flush=True)
                self._released_for_fault = False
                self._missing = 0
            else:
                self._missing = 0
            for fan, duty in decision.writes.items():
                try:
                    self.backend.write_duty(fan, duty)
                except (OSError, FanBackendError) as exc:
                    print(f"EC write failed: {exc}", flush=True)
            if not decision.writes:
                try:
                    self.backend.ping()
                except (OSError, FanBackendError):
                    pass
        return decision

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
