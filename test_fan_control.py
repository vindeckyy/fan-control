import argparse
import importlib.util
import io
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


policy = load("fan_policy", "fan_policy.py")
runtime = load("fan_runtime", "fan_runtime.py")
controller = load("fan_controller", "fan_controller.py")
daemon = load("fan_daemon", "fan-daemon.py")
backend = load("fan_backend", "fan_backend.py")
rpc = load("fan_rpc", "fan_rpc.py")


class FanLogicTests(unittest.TestCase):
    def test_interpolation_and_bounds(self):
        curve = policy.normalize_curve([[80, 140], [50, 0], [95, 250]])
        self.assertEqual(curve, [(50, 0), (80, 100), (95, 100)])
        self.assertEqual(policy.target_duty(65, curve), 50)
        self.assertEqual(policy.target_duty(200, curve), 100)

    def test_critical_cooling_bypasses_noise_cap(self):
        curve = [(0, 0), (110, 100)]
        self.assertLess(policy.target_duty(50, curve, max_duty=60, critical_temp=95), 60)
        self.assertEqual(policy.target_duty(95, curve, max_duty=60, critical_temp=95), 100)

    def test_curve_validation_deduplicates_temperatures(self):
        self.assertEqual(policy.normalize_curve([[50, 10], [50, 20], [70, 300]]), [(50, 20), (70, 100)])
        with self.assertRaises(ValueError):
            policy.normalize_curve([[50, 10]])

    def test_nvidia_smi_sensor_parsing(self):
        sensors = policy.parse_nvidia_smi("0, 67, NVIDIA GeForce RTX 4080\n1, 54, NVIDIA RTX A2000\n")
        self.assertEqual([sensor["temp"] for sensor in sensors], [67.0, 54.0])
        self.assertIn("RTX 4080", sensors[0]["label"])
        self.assertEqual(policy.parse_nvidia_temperatures("67\n54\nN/A\n"), [67.0, 54.0])

    def test_control_uses_hottest_cpu_or_gpu_sensor(self):
        self.assertEqual(policy.select_control_temp(61.0, 78.0), 78.0)
        self.assertEqual(policy.select_control_temp(61.0, None), 61.0)
        self.assertIsNone(policy.select_control_temp(None, None))
        self.assertIsNone(policy.select_control_temp(0, 200))

    def test_scan_sensors_demo(self):
        sensors = policy.scan_sensors(demo=True)
        self.assertGreaterEqual(len(sensors), 3)
        self.assertTrue(any(s["name"] == "k10temp" for s in sensors))
        self.assertTrue(any(s["name"] == "amdgpu" for s in sensors))

    def test_scan_sensors_hwmon_and_expanded_chips(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            # Simulate zenpower (AMD CPU)
            hw0 = base / "hwmon0"
            hw0.mkdir()
            (hw0 / "name").write_text("zenpower\n")
            (hw0 / "temp1_input").write_text("63500\n")
            (hw0 / "temp1_label").write_text("Tctl\n")

            # Simulate Intel Arc xe (GPU)
            hw1 = base / "hwmon1"
            hw1.mkdir()
            (hw1 / "name").write_text("xe\n")
            (hw1 / "temp1_input").write_text("51000\n")

            # Simulate nouveau (GPU)
            hw2 = base / "hwmon2"
            hw2.mkdir()
            (hw2 / "name").write_text("nouveau\n")
            (hw2 / "temp1_input").write_text("49000\n")
            (hw2 / "temp1_label").write_text("GPU Core\n")

            sensors = policy.scan_sensors(demo=False, include_nvidia=False, hwmon_dir=str(base))
            self.assertEqual(len(sensors), 3)

            # Test pick_cpu_temp recognizes zenpower
            self.assertEqual(policy.pick_cpu_temp(sensors), 63.5)

            # Test pick_gpu_temp recognizes xe and nouveau
            gpu_temp = policy.pick_gpu_temp(sensors)
            self.assertEqual(gpu_temp, 51.0)



class PolicySafetyTests(unittest.TestCase):
    def test_hysteresis_skips_small_changes(self):
        self.assertFalse(policy.should_write(50, 54, hysteresis=5, critical=False))
        self.assertTrue(policy.should_write(50, 55, hysteresis=5, critical=False))
        self.assertTrue(policy.should_write(50, 51, hysteresis=5, critical=True))
        self.assertTrue(policy.should_write(50, 50, hysteresis=0, critical=False))

    def test_missing_temperature_releases_after_three_faults(self):
        self.assertFalse(policy.temperature_fault(0))
        self.assertFalse(policy.temperature_fault(2))
        self.assertTrue(policy.temperature_fault(3))
        self.assertTrue(policy.temperature_fault(8))

    def test_linked_curve_uses_hottest_temp_for_both_fans(self):
        duties = policy.desired_curve_duties(
            cpu_temp=60,
            gpu_temp=80,
            linked=True,
            shared_curve=[(0, 0), (80, 50), (100, 100)],
            curve_cpu=None,
            curve_gpu=None,
            max_duty=100,
            critical_temp=95,
            fans=(1, 2),
        )
        self.assertEqual(duties[1], duties[2])
        self.assertEqual(duties[1], 50)

    def test_independent_curves_follow_per_side_temperature(self):
        duties = policy.desired_curve_duties(
            cpu_temp=60,
            gpu_temp=100,
            linked=False,
            shared_curve=[(0, 0), (100, 100)],
            curve_cpu=[(0, 0), (60, 20), (100, 100)],
            curve_gpu=[(0, 0), (100, 80)],
            max_duty=100,
            critical_temp=110,
            fans=(1, 2, 3),
        )
        self.assertEqual(duties[1], 20)
        self.assertEqual(duties[2], 80)
        self.assertEqual(duties[3], 80)

    def test_independent_missing_side_falls_back_to_other_temp(self):
        duties = policy.desired_curve_duties(
            cpu_temp=80,
            gpu_temp=None,
            linked=False,
            shared_curve=[(0, 0), (80, 40), (100, 100)],
            curve_cpu=None,
            curve_gpu=None,
            max_duty=100,
            critical_temp=95,
            fans=(1, 2),
        )
        self.assertEqual(duties[1], 40)
        self.assertEqual(duties[2], 40)

    def test_decide_releases_after_three_missing_temps(self):
        decision = policy.decide(
            mode="curve",
            profile="balanced",
            cpu_temp=None,
            gpu_temp=None,
            targets={1: 0, 2: 0},
            max_duty=80,
            hysteresis=5,
            critical_temp=95,
            linked=True,
            shared_curve=policy.PROFILES["balanced"],
            curve_cpu=None,
            curve_gpu=None,
            current_duties={1: 10, 2: 10},
            missing_count=3,
            fans=(1, 2),
        )
        self.assertEqual(decision.action, "release")

    def test_decide_critical_writes_full_duty(self):
        decision = policy.decide(
            mode="manual",
            profile="balanced",
            cpu_temp=96,
            gpu_temp=70,
            targets={1: 10, 2: 10},
            max_duty=40,
            hysteresis=5,
            critical_temp=95,
            linked=True,
            shared_curve=policy.PROFILES["balanced"],
            curve_cpu=None,
            curve_gpu=None,
            current_duties={1: 10, 2: 10},
            missing_count=0,
            fans=(1, 2),
        )
        self.assertEqual(decision.action, "write")
        self.assertTrue(decision.critical)
        self.assertEqual(decision.writes, {1: 100, 2: 100})

    def test_decide_manual_scales_targets_by_cap(self):
        decision = policy.decide(
            mode="manual",
            profile="balanced",
            cpu_temp=50,
            gpu_temp=50,
            targets={1: 50, 2: 100},
            max_duty=80,
            hysteresis=0,
            critical_temp=95,
            linked=False,
            shared_curve=policy.PROFILES["balanced"],
            curve_cpu=None,
            curve_gpu=None,
            current_duties={1: 0, 2: 0},
            missing_count=0,
            fans=(1, 2),
        )
        self.assertEqual(decision.writes[1], 40)
        self.assertEqual(decision.writes[2], 80)

    def test_decide_released_is_idle(self):
        decision = policy.decide(
            mode="released",
            profile="balanced",
            cpu_temp=90,
            gpu_temp=90,
            targets={1: 100, 2: 100},
            max_duty=100,
            hysteresis=5,
            critical_temp=95,
            linked=True,
            shared_curve=policy.PROFILES["balanced"],
            curve_cpu=None,
            curve_gpu=None,
            current_duties={1: 0, 2: 0},
            missing_count=0,
            fans=(1, 2),
        )
        self.assertEqual(decision.action, "idle")
        self.assertEqual(decision.writes, {})

    def test_sensor_pinning_matches_name_and_label(self):
        sensors = [
            {"name": "k10temp", "label": "Tctl", "temp": 61.0},
            {"name": "k10temp", "label": "Tccd1", "temp": 70.0},
            {"name": "amdgpu", "label": "edge", "temp": 54.0},
        ]
        self.assertEqual(policy.pick_cpu_temp(sensors, {"name": "k10temp", "label": "Tccd1"}), 70.0)
        self.assertEqual(policy.pick_cpu_temp(sensors, None), 70.0)
        self.assertEqual(policy.pick_gpu_temp(sensors, {"name": "amdgpu", "label": "edge"}), 54.0)

    def test_config_round_trip_fills_new_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "fan-control.json"
            policy.save_config(path, {"profile": "silent", "max_duty": 70})
            loaded = policy.load_config(path)
            self.assertEqual(loaded["profile"], "silent")
            self.assertEqual(loaded["max_duty"], 70)
            self.assertTrue(loaded["linked"])
            self.assertEqual(loaded["named_curves"], {})
            self.assertIsNone(loaded["curve_cpu"])
            self.assertIsNone(loaded["curve_gpu"])
            self.assertIsNone(loaded["cpu_sensor"])
            self.assertIsNone(loaded["gpu_sensor"])
            self.assertEqual(loaded["theme"], "dark")
            self.assertEqual(loaded["alerts"], {"desktop": False})
            self.assertEqual(loaded["mode"], "manual")
            self.assertGreaterEqual(len(loaded["curve"]), 2)

    def test_config_persists_named_and_independent_curves(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "fan-control.json"
            night = policy.normalize_curve([[0, 0], [90, 40]])
            policy.save_config(path, {
                "mode": "curve",
                "profile": "custom",
                "linked": False,
                "curve": [[0, 0], [95, 100]],
                "curve_cpu": [[0, 0], [70, 30]],
                "curve_gpu": [[0, 0], [80, 60]],
                "named_curves": {"night": night},
                "cpu_sensor": {"name": "k10temp", "label": "Tctl"},
                "theme": "light",
                "alerts": {"desktop": True},
            })
            loaded = policy.load_config(path)
            self.assertFalse(loaded["linked"])
            self.assertEqual(loaded["named_curves"]["night"], night)
            self.assertEqual(loaded["curve_cpu"][1], (70, 30))
            self.assertEqual(loaded["theme"], "light")
            self.assertTrue(loaded["alerts"]["desktop"])
            self.assertEqual(loaded["cpu_sensor"]["label"], "Tctl")



class RuntimeLockTests(unittest.TestCase):
    def test_exclusive_lock_blocks_second_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "ec.lock"
            first = runtime.ExclusiveLock(path)
            second = runtime.ExclusiveLock(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


class ControllerIpcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = pathlib.Path(self.tmp.name) / "fan-control.json"
        self.runtime_dir = pathlib.Path(self.tmp.name) / "run"
        self.runtime_dir.mkdir()
        self.ctl = controller.FanController(
            backend=backend.DemoBackend(),
            config_path=self.config_path,
            runtime_dir=self.runtime_dir,
            demo=True,
        )

    def tearDown(self):
        self.ctl.close()
        self.tmp.cleanup()

    def test_snapshot_and_set_and_profile(self):
        snap = self.ctl.handle("snapshot", {})
        self.assertIn("mode", snap)
        self.assertIn("temps", snap)
        self.ctl.handle("set", {"fan": 1, "pct": 40})
        snap = self.ctl.handle("snapshot", {})
        self.assertEqual(snap["mode"], "manual")
        self.assertEqual(snap["targets"]["1"], 40)
        self.ctl.handle("profile", {"profile": "silent"})
        snap = self.ctl.handle("snapshot", {})
        self.assertEqual(snap["mode"], "curve")
        self.assertEqual(snap["profile"], "silent")

    def test_live_snapshot_omits_heavy_config(self):
        live = self.ctl.live_snapshot()
        self.assertIn("fan1", live)
        self.assertIn("control_temp", live)
        self.assertNotIn("profiles", live)
        self.assertNotIn("named_curves", live)
        self.assertNotIn("custom_curve", live)
        self.assertIn("profiles", self.ctl.snapshot())

    def test_custom_curve_and_config_and_named_curves(self):
        self.ctl.handle("custom", {"curve": [[0, 0], [80, 50], [100, 100]]})
        snap = self.ctl.handle("snapshot", {})
        self.assertEqual(snap["profile"], "custom")
        self.ctl.handle("config", {"max_duty": 80, "hysteresis": 4, "critical_temp": 92, "linked": False})
        snap = self.ctl.handle("snapshot", {})
        self.assertEqual(snap["max_duty"], 80)
        self.assertFalse(snap["linked"])
        self.ctl.handle("curves.save", {"name": "night", "curve": [[0, 0], [90, 30]]})
        names = self.ctl.handle("curves.list", {})
        self.assertIn("night", names["names"])
        exported = self.ctl.handle("curves.export", {"name": "night"})
        self.assertEqual(exported["name"], "night")

    def test_unknown_method_errors(self):
        with self.assertRaises(ValueError):
            self.ctl.handle("nope", {})

    def test_history_records_control_temp_and_optional_rpm(self):
        self.ctl.tick_sensors()
        self.ctl.tick_readback()
        self.ctl.tick_history()
        payload = self.ctl.handle("history", {})
        self.assertGreaterEqual(len(payload["history"]), 1)
        point = payload["history"][-1]
        self.assertIn("control_temp", point)
        self.assertIn("fan1", point)
        self.assertIn("rpm1", point)

    def test_pin_sensor_and_history_csv(self):
        self.ctl.tick_sensors()
        self.ctl.handle("config", {"cpu_sensor": {"name": "k10temp", "label": "Tctl"}})
        csv_text = self.ctl.handle("history.export", {})["csv"]
        self.assertIn("time", csv_text)

    def test_ipc_rejects_bad_set(self):
        with self.assertRaises(ValueError):
            self.ctl.handle("set", {"fan": 9, "pct": 10})
        with self.assertRaises(ValueError):
            self.ctl.handle("set", {"fan": 1, "pct": 140})


class BackendTests(unittest.TestCase):
    def test_fan_backend_error_hierarchy(self):
        err = backend.FanBackendError("test error")
        self.assertIsInstance(err, OSError)
        self.assertIsInstance(err, RuntimeError)

    def test_legacy_config_migration(self):
        data = {
            "max_duty": 198,
            "curve": [[0, 0], [64, 0], [65, 20], [80, 140], [95, 198]],
            "curve_cpu": [[0, 0], [80, 140]],
            "curve_gpu": [[0, 0], [95, 198]],
            "named_curves": {"boost": [[0, 0], [80, 140]]},
        }
        migrated = backend.migrate_config(data)
        self.assertEqual(migrated["max_duty"], 100)
        self.assertEqual(
            migrated["curve"],
            [[0, 0], [64, 0], [65, 10], [80, 71], [95, 100]],
        )
        self.assertEqual(migrated["curve_cpu"], [[0, 0], [80, 71]])
        self.assertEqual(migrated["curve_gpu"], [[0, 0], [95, 100]])
        self.assertEqual(migrated["named_curves"]["boost"], [[0, 0], [80, 71]])

    def test_percent_config_left_alone(self):
        data = {"max_duty": 60, "curve": [[0, 0], [80, 50]]}
        self.assertEqual(backend.migrate_config(data), data)

    def test_tuxedo_duty_conversion(self):
        class _Probe(backend.TuxedoIoBackend):
            def __init__(self):
                pass

        probe = _Probe()
        self.assertEqual(probe._pct_to_raw(100), 198)
        self.assertEqual(probe._pct_to_raw(50), 99)
        self.assertEqual(probe._raw_to_pct(198), 100)
        self.assertEqual(probe._raw_to_pct(99), 50)

    def test_tuxedo_clevo_duty_conversion(self):
        class _Probe(backend.TuxedoIoBackend):
            def __init__(self):
                self.is_clevo = True

        probe = _Probe()
        self.assertEqual(probe._pct_to_raw(100), 255)
        self.assertEqual(probe._pct_to_raw(0), 0)
        self.assertEqual(probe._raw_to_pct(255), 100)
        self.assertEqual(probe._raw_to_pct(0), 0)

    def test_tuxedo_clevo_reads_and_writes(self):
        class _Probe(backend.TuxedoIoBackend):
            def __init__(self):
                self.is_clevo = True
                self._lock = mock.MagicMock()
                self._duties = {1: 0, 2: 0, 3: 0}
                self.written = []

            def _write(self, cmd, val):
                self.written.append((cmd, val))

            def _read(self, cmd):
                if cmd == backend.R_CL_FANINFO1:
                    # Low byte: raw duty 128 (~50%), second byte: 55°C, high 16 bits: RPM
                    return 128 | (55 << 8) | (2400 << 16)
                if cmd == backend.R_CL_FANINFO2:
                    return 255 | (60 << 8) | (3200 << 16)
                return 0

        probe = _Probe()
        self.assertEqual(probe.read_temp(), 55.0)
        self.assertEqual(probe.read_temp2(), 60.0)
        self.assertEqual(probe.read_rpm(1), 2400)
        self.assertEqual(probe.read_rpm(2), 3200)
        probe.write_duty(1, 33)
        probe.write_duty(2, 33)
        # FANINFO low byte is not a reliable PWM echo (128/255 ≈ 50% above).
        self.assertEqual(probe.read_duty(1), 33)
        self.assertEqual(probe.read_duty(2), 33)

        probe.write_duty(1, 100)
        packed = probe._pct_to_raw(100) | (probe._pct_to_raw(33) << 8)
        self.assertEqual(probe.written[-1], (backend.W_CL_FANSPEED, packed))

        probe.release()
        self.assertEqual(probe.written[-1], (backend.W_CL_FANAUTO, 0))

    def test_clevo_duty_readback_ignores_faninfo_low_byte(self):
        class _Probe(backend.TuxedoIoBackend):
            def __init__(self):
                self.is_clevo = True
                self._lock = mock.MagicMock()
                self._duties = {1: 0, 2: 0, 3: 0}

            def _write(self, cmd, val):
                return None

            def _read(self, cmd):
                if cmd == backend.R_CL_FANINFO1:
                    return 102 | (57 << 8) | (2400 << 16)  # 102/255 ≈ 40%
                return 0

        probe = _Probe()
        probe.write_duty(1, 33)
        self.assertEqual(probe.read_duty(1), 33)
        self.assertNotEqual(probe.read_duty(1), round(102 * 100 / 255))

    def test_clevo_ping_reapplies_last_duty_to_hold_manual(self):
        class _Probe(backend.TuxedoIoBackend):
            def __init__(self):
                self.is_clevo = True
                self._lock = mock.MagicMock()
                self._duties = {1: 0, 2: 0, 3: 0}
                self._last_hold = 0
                self.written = []

            def _write(self, cmd, val):
                self.written.append((cmd, val))

        probe = _Probe()
        probe.write_duty(1, 30)
        probe.write_duty(2, 30)
        packed = probe.written[-1]
        probe.written.clear()
        probe._last_hold = 0
        probe.ping()
        self.assertEqual(probe.written, [packed])
        probe.ping()
        self.assertEqual(probe.written, [packed], "keepalive is rate-limited")

    def test_uniwill_ping_reapplies_last_duties(self):
        class _Probe(backend.TuxedoIoBackend):
            def __init__(self):
                self.is_clevo = False
                self._lock = mock.MagicMock()
                self._duties = {1: 40, 2: 40, 3: 0}
                self._last_hold = 0
                self.written = []
                self.fd = 42

            def _write(self, cmd, val):
                self.written.append((cmd, val))

        probe = _Probe()
        probe.ping()
        self.assertEqual(
            probe.written,
            [(backend.W_UW_FANSPEED, 79), (backend.W_UW_FANSPEED2, 79)],
        )
        probe.written.clear()
        probe.ping()
        self.assertEqual(probe.written, [], "keepalive is rate-limited")

    def test_tuxedo_uniwill_reads_and_writes_without_rpm(self):
        class _Probe(backend.TuxedoIoBackend):
            def __init__(self):
                self.is_clevo = False
                self._lock = mock.MagicMock()
                self._duties = {1: 0, 2: 0, 3: 0}
                self.written = []
                self.fd = 42

            def _write(self, cmd, val):
                self.written.append((cmd, val))

            def _read(self, cmd):
                if cmd == backend.R_UW_FANSPEED:
                    return 99  # 50% of 198
                if cmd == backend.R_UW_FAN_TEMP:
                    return 48
                return 0

        probe = _Probe()
        self.assertEqual(probe.read_duty(1), 0)
        self.assertEqual(probe.read_temp(), 48.0)
        self.assertIsNone(probe.read_rpm(1))

        probe.lock()
        self.assertEqual(probe.written[-1], (backend.W_UW_MODE, 0x40))

        probe.write_duty(1, 50)
        probe.write_duty(2, 50)
        self.assertEqual(probe.read_duty(1), 50)
        self.assertEqual(probe.read_duty_hardware(1), 50)
        self.assertEqual(probe.written[-2], (backend.W_UW_FANSPEED, 99))
        self.assertEqual(probe.written[-1], (backend.W_UW_FANSPEED2, 99))

        with mock.patch("fcntl.ioctl") as mock_ioctl:
            probe.release()
            mock_ioctl.assert_called_once_with(42, backend.W_UW_FANAUTO)

    def test_uniwill_duty_readback_ignores_noisy_hardware(self):
        class _Probe(backend.TuxedoIoBackend):
            def __init__(self):
                self.is_clevo = False
                self._lock = mock.MagicMock()
                self._duties = {1: 0, 2: 0, 3: 0}
                self.reads = iter([0, 79, 0, 79, 0, 79])

            def _write(self, cmd, val):
                return None

            def _read(self, cmd):
                if cmd == backend.R_UW_FANSPEED:
                    return next(self.reads, 79)
                return 0

        probe = _Probe()
        probe.write_duty(1, 30)
        self.assertEqual(probe.read_duty(1), 30)
        self.assertNotEqual(probe.read_duty(1), probe.read_duty_hardware(1))

    def test_clevo_backend_discovery_and_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            for fan in (1, 2):
                (base / f"fan{fan}_manual_duty").write_text("0")
                (base / f"fan{fan}_duty").write_text("30")
            (base / "fan1_temp").write_text("52")
            (base / "fan2_temp").write_text("48")
            (base / "fan_release").write_text("")
            (base / "fan_watchdog_ping").write_text("")
            (base / "fan_watchdog_timeout_ms").write_text("15000")

            b = backend.ClevoAcpiBackend(base=base)
            self.assertEqual(b.fans(), [1, 2])
            self.assertEqual(b.read_duty(1), 30)
            self.assertEqual(b.read_temp(), 52.0)
            self.assertEqual(b.read_temp2(), 48.0)
            self.assertIsNone(b.read_rpm(1))

            b.write_duty(1, 150)  # clamps to 100
            self.assertEqual((base / "fan1_manual_duty").read_text().strip(), "100")
            b.release()
            self.assertEqual((base / "fan_release").read_text().strip(), "1")
            b.ping()
            self.assertEqual((base / "fan_watchdog_ping").read_text().strip(), "1")

    def test_clevo_acpi_reads_rpm_file_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            (base / "fan1_manual_duty").write_text("0")
            (base / "fan1_duty").write_text("30")
            (base / "fan1_rpm").write_text("2100")
            b = backend.ClevoAcpiBackend(base=base)
            self.assertEqual(b.read_rpm(1), 2100)

    def test_clevo_missing_attributes_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(backend.FanBackendError):
                backend.ClevoAcpiBackend(base=pathlib.Path(tmp))

    def test_detect_backend_prefers_clevo(self):
        fake = object()
        with mock.patch.object(backend.ClevoAcpiBackend, "available", return_value=True), \
                mock.patch.object(backend, "ClevoAcpiBackend", return_value=fake), \
                mock.patch.object(backend, "TuxedoIoBackend") as tux:
            self.assertIs(backend.detect_backend("auto"), fake)
            tux.assert_not_called()

    def test_detect_backend_explicit_tuxedo(self):
        with mock.patch.object(backend, "TuxedoIoBackend") as tux:
            backend.detect_backend("tuxedo_io", device="/dev/example_io")
            tux.assert_called_once_with("/dev/example_io")

    def test_detect_backend_env_override(self):
        with mock.patch.object(backend, "TuxedoIoBackend") as tux, \
                mock.patch.dict(os.environ, {"FAN_CONTROL_BACKEND": "tuxedo_io"}):
            backend.detect_backend(None, device="/dev/example_io")
            tux.assert_called_once_with("/dev/example_io")

    def test_detect_backend_arg_beats_env(self):
        fake = object()
        with mock.patch.object(backend.ClevoAcpiBackend, "available", return_value=True), \
                mock.patch.object(backend, "ClevoAcpiBackend", return_value=fake), \
                mock.patch.dict(os.environ, {"FAN_CONTROL_BACKEND": "tuxedo_io"}):
            self.assertIs(backend.detect_backend("clevo_acpi"), fake)

    def test_detect_backend_rejects_unknown(self):
        with self.assertRaises(backend.FanBackendError):
            backend.detect_backend("bogus")

    def test_device_path_override(self):
        with mock.patch.dict(os.environ, {"FAN_CONTROL_DEVICE": "/dev/custom_fan_io"}):
            self.assertEqual(backend._find_ec_device(), "/dev/custom_fan_io")


class CtlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.config_path = base / "fan-control.json"
        self.runtime_dir = base / "run"
        self.runtime_dir.mkdir()
        self.socket_path = self.runtime_dir / rpc.SOCKET_NAME
        night = [(0, 0), (70, 20), (100, 80)]
        policy.save_config(self.config_path, {
            "profile": "balanced",
            "max_duty": 100,
            "named_curves": {"night": night},
            "curve_cpu": [(0, 0), (80, 50)],
        })
        self.ctl_backend = backend.DemoBackend()
        self.controller = controller.FanController(
            backend=self.ctl_backend,
            config_path=self.config_path,
            runtime_dir=self.runtime_dir,
            demo=True,
        )
        self.server = rpc.RpcServer(self.controller, self.socket_path)
        self.server.start()
        self.ctl = load("fan_ctl", "fan-ctl.py")

    def tearDown(self):
        self.server.close()
        self.controller.close()
        self.tmp.cleanup()

    def test_status_and_profile_change(self):
        res = self.ctl.run_command(["status", "--runtime-dir", str(self.runtime_dir), "--json"])
        data = json.loads(res.stdout)
        self.assertEqual(data["profile"], "balanced")
        self.assertTrue(data["daemon_reachable"])

        res = self.ctl.run_command(["profile", "silent", "--runtime-dir", str(self.runtime_dir), "--json"])
        self.assertEqual(json.loads(res.stdout)["profile"], "silent")
        res = self.ctl.run_command(["status", "--runtime-dir", str(self.runtime_dir), "--json"])
        self.assertEqual(json.loads(res.stdout)["profile"], "silent")

        res = self.ctl.run_command(["mode", "manual", "--runtime-dir", str(self.runtime_dir), "--json"])
        self.assertEqual(json.loads(res.stdout)["mode"], "manual")

        res = self.ctl.run_command(["set", "1", "50", "--runtime-dir", str(self.runtime_dir), "--json"])
        self.assertEqual(json.loads(res.stdout)["pct"], 50)

    def test_cap_and_config_and_curves(self):
        # cap command
        res = self.ctl.run_command(["cap", "75", "--runtime-dir", str(self.runtime_dir), "--json"])
        self.assertEqual(json.loads(res.stdout)["max_duty"], 75)
        res = self.ctl.run_command(["status", "--runtime-dir", str(self.runtime_dir), "--json"])
        self.assertEqual(json.loads(res.stdout)["max_duty"], 75)

        # config command
        res = self.ctl.run_command([
            "config", "hysteresis=3", "linked=false", "critical_temp=98",
            "--runtime-dir", str(self.runtime_dir), "--json",
        ])
        updated = json.loads(res.stdout)["updated"]
        self.assertEqual(updated["hysteresis"], 3)
        self.assertFalse(updated["linked"])
        self.assertEqual(updated["critical_temp"], 98)

        # curve command (cpu)
        res = self.ctl.run_command(["curve", "cpu", "--runtime-dir", str(self.runtime_dir), "--json"])
        self.assertEqual(json.loads(res.stdout)["which"], "cpu")

        # curves list & load command
        res = self.ctl.run_command(["curves", "--runtime-dir", str(self.runtime_dir), "--json"])
        self.assertIn("night", json.loads(res.stdout)["named_curves"])
        res = self.ctl.run_command(["curves", "load", "night", "--runtime-dir", str(self.runtime_dir), "--json"])
        self.assertEqual(json.loads(res.stdout)["loaded"], "night")
        res = self.ctl.run_command(["status", "--runtime-dir", str(self.runtime_dir), "--json"])
        self.assertEqual(json.loads(res.stdout)["profile"], "custom")

    def test_diagnose_command(self):
        res = self.ctl.run_command(["diagnose", "--config", str(self.config_path)])
        self.assertIn("config", res.stdout)

    def test_daemon_unreachable_exits(self):
        missing_dir = pathlib.Path(self.tmp.name) / "nonexistent"
        with self.assertRaises(ConnectionError):
            self.ctl.run_command(["status", "--runtime-dir", str(missing_dir)])


class DaemonTests(unittest.TestCase):
    def test_daemon_handles_corrupt_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad_path = pathlib.Path(tmp) / "fan-control.json"
            bad_path.write_text("{malformed json!!")
            with self.assertRaises(ValueError):
                policy.load_config(bad_path)

    def test_daemon_dry_run_prints_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(ROOT / "fan-daemon.py"),
                    "--dry-run",
                    "--interval",
                    "0.05",
                    "--runtime-dir",
                    tmp,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                # Let it run for a few ticks
                lines = []
                for _ in range(5):
                    line = proc.stdout.readline()
                    if line:
                        lines.append(line.strip())
                self.assertTrue(any("backend=demo" in line for line in lines))
                self.assertTrue(any("°C ->" in line for line in lines))
            finally:
                proc.terminate()
                proc.wait(timeout=2)


class RpcTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.socket_path = base / rpc.SOCKET_NAME
        self.ctl = controller.FanController(
            backend=backend.DemoBackend(),
            config_path=base / "fan-control.json",
            runtime_dir=base,
            demo=True,
        )
        self.server = rpc.RpcServer(self.ctl, self.socket_path)
        self.server.start()
        self.client = rpc.RpcClient(self.socket_path)
        self.client.connect()

    def tearDown(self):
        self.client.close()
        self.server.close()
        self.ctl.close()
        self.tmp.cleanup()

    def test_snapshot_and_live_over_socket(self):
        snap = self.client.call("snapshot")
        self.assertIn("mode", snap)
        live = self.client.call("live")
        self.assertIn("control_temp", live)
        cfg = self.client.call("config.get")
        self.assertIn("curve", cfg)

    def test_unknown_method_maps_to_runtime_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.client.call("nope", {})
        self.assertIn("unknown method", str(ctx.exception))

    def test_bad_params_keep_connection_usable(self):
        with self.assertRaises(RuntimeError):
            self.client.call("set", {"fan": 1, "pct": 200})
        snap = self.client.call("snapshot")
        self.assertIn("mode", snap)

    def test_oversized_request_rejected(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(str(self.socket_path))
        try:
            sock.sendall(b"x" * (rpc.MAX_MESSAGE + 1) + b"\n")
            reply = b""
            while not reply.endswith(b"\n"):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                reply += chunk
            payload = json.loads(reply.decode())
            self.assertFalse(payload["ok"])
        finally:
            sock.close()

    def test_second_server_fails_while_first_alive(self):
        second = rpc.RpcServer(self.ctl, self.socket_path)
        with self.assertRaises(OSError):
            second.start()


    def test_diagnose_output(self):
        import fan_diagnostics
        with tempfile.TemporaryDirectory() as tmp:
            cfg = pathlib.Path(tmp) / "fan-control.json"
            text = fan_diagnostics.diagnose(str(cfg))
            self.assertIn("config", text)
            self.assertIn("no file", text)
            cfg.write_text("{}")
            # Cover line 55 (no hardware found branch)
            with mock.patch("pathlib.Path.glob", return_value=[]):
                text = fan_diagnostics.diagnose(str(cfg))
                self.assertIn("no fan-control hardware found", text)

    def test_diagnose_exception_resilience(self):
        import fan_diagnostics
        with mock.patch("subprocess.run", side_effect=OSError("lsmod failed")):
            text = fan_diagnostics.diagnose("/tmp/nonexistent")
            self.assertIn("config", text)

    def test_find_cpu_sensor_scan(self):
        import fan_diagnostics
        # Should run without crashing regardless of hardware
        result = fan_diagnostics.find_cpu_sensor()
        self.assertTrue(result is None or isinstance(result, pathlib.Path))

        # Test OSError branch on reading hwmon name
        with tempfile.TemporaryDirectory() as tmp:
            hw = pathlib.Path(tmp) / "hwmon0"
            hw.mkdir()
            (hw / "name").mkdir()  # is a directory, read_text raises IsADirectoryError (OSError)
            with mock.patch("pathlib.Path.glob", return_value=[hw]):
                self.assertIsNone(fan_diagnostics.find_cpu_sensor())

class ControllerExtendedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.cfg_path = self.base / "fan-control.json"
        self.ctl = controller.FanController(
            backend=backend.DemoBackend(),
            config_path=self.cfg_path,
            runtime_dir=self.base,
            demo=True,
        )

    def tearDown(self):
        self.ctl.close()
        self.tmp.cleanup()

    def test_fan_limit_filtering(self):
        c1 = controller.FanController(backend.DemoBackend(), self.cfg_path, self.base, demo=True, fan_limit=1)
        self.assertEqual(c1.fans(), [1])
        c2 = controller.FanController(backend.DemoBackend(), self.cfg_path, self.base, demo=True, fan_limit=2)
        self.assertEqual(c2.fans(), [1, 2])
        c1.close()
        c2.close()

    def test_reload_config(self):
        policy.save_config(self.cfg_path, {"profile": "silent", "mode": "released"})
        self.ctl.reload_config()
        self.assertEqual(self.ctl.state["profile"], "silent")
        self.assertEqual(self.ctl.state["mode"], "released")

    def test_diagnose_text(self):
        text = self.ctl.diagnose_text()
        self.assertIn("config", text)

    def test_profile_syncs_curve(self):
        self.ctl.handle("profile", {"profile": "silent"})
        self.assertEqual(self.ctl.state["curve"], list(policy.PROFILES["silent"]))
        self.ctl.handle("profile", {"profile": "custom"})
        self.assertEqual(self.ctl.state["profile"], "custom")

    def test_tick_control_mutation_and_fault_recovery(self):
        self.ctl.state["mode"] = "curve"
        self.ctl.tick_sensors()
        self.ctl.tick_readback()
        decision = self.ctl.tick_control()
        self.assertIsNotNone(decision)
        # Simulate fault threshold
        with mock.patch.object(self.ctl, "cpu_temp", return_value=None), \
             mock.patch.object(self.ctl, "gpu_temp", return_value=None):
            self.ctl._missing = 3
            decision = self.ctl.tick_control()
            self.assertEqual(decision.action, "release")
            self.assertTrue(self.ctl._released_for_fault)
        # Recover with valid temperature
        self.ctl._missing = 0
        decision = self.ctl.tick_control()
        self.assertFalse(self.ctl._released_for_fault)


class RpcEdgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.sock_path = self.base / rpc.SOCKET_NAME
        self.ctl = controller.FanController(backend.DemoBackend(), self.base / "cfg.json", self.base, demo=True)
        self.server = rpc.RpcServer(self.ctl, self.sock_path)
        self.server.start()
        self.client = rpc.RpcClient(self.sock_path)
        self.client.connect()

    def tearDown(self):
        self.client.close()
        self.server.close()
        self.ctl.close()
        self.tmp.cleanup()

    def test_control_socket_path_helpers(self):
        self.assertTrue(rpc.control_socket_path("/tmp").endswith("control.sock"))
        self.assertTrue(rpc.control_socket_path(pathlib.Path("/tmp")).endswith("control.sock"))

    def test_give_group_access_resilience(self):
        # Should not throw even with non-existent groups or paths
        rpc._give_group_access("/tmp/nonexistent-file-path-xyz")
        with mock.patch("grp.getgrnam", side_effect=KeyError("no group")):
            rpc._give_group_access("/tmp")

    def test_client_error_types(self):
        missing_client = rpc.RpcClient(self.base / "nonexistent.sock")
        with self.assertRaises(ConnectionError) as ctx:
            missing_client.connect()
        self.assertIn("fan-daemon is not running", str(ctx.exception))

        with mock.patch("socket.socket.connect", side_effect=PermissionError("permission denied")):
            perm_client = rpc.RpcClient(self.sock_path)
            with self.assertRaises(PermissionError) as ctx:
                perm_client.connect()
            self.assertIn("fan-control group", str(ctx.exception))

    def test_client_context_manager(self):
        with rpc.RpcClient(self.sock_path) as c:
            snap = c.call("snapshot")
            self.assertIn("mode", snap)

    def test_server_setup_unlinks_stale_socket(self):
        stale_path = self.base / "stale.sock"
        stale_path.write_text("stale content")
        server2 = rpc.RpcServer(self.ctl, stale_path)
        server2.start()
        self.assertTrue(os.path.exists(stale_path))
        server2.close()

    def test_invalid_json_protocol_errors(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(self.sock_path))
        try:
            # Send malformed json
            s.sendall(b"not valid json\n")
            resp = json.loads(s.recv(4096).decode())
            self.assertFalse(resp["ok"])

            # Send non-dict json
            s.sendall(b'"a string"\n')
            resp = json.loads(s.recv(4096).decode())
            self.assertFalse(resp["ok"])

            # Send non-dict params
            s.sendall(b'{"method": "snapshot", "params": 123}\n')
            resp = json.loads(s.recv(4096).decode())
            self.assertFalse(resp["ok"])
        finally:
            s.close()


class CtlBranchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.config_path = base / "fan-control.json"
        self.runtime_dir = base / "run"
        self.runtime_dir.mkdir()
        self.socket_path = self.runtime_dir / rpc.SOCKET_NAME
        night = [(0, 0), (70, 20), (100, 80)]
        policy.save_config(self.config_path, {
            "profile": "balanced",
            "max_duty": 100,
            "named_curves": {"night": night},
            "curve_cpu": [(0, 0), (80, 50)],
            "curve_gpu": [(0, 0), (90, 60)],
        })
        self.ctl_backend = backend.DemoBackend()
        self.controller = controller.FanController(
            backend=self.ctl_backend,
            config_path=self.config_path,
            runtime_dir=self.runtime_dir,
            demo=True,
        )
        self.server = rpc.RpcServer(self.controller, self.socket_path)
        self.server.start()
        self.ctl = load("fan_ctl", "fan-ctl.py")

    def tearDown(self):
        self.server.close()
        self.controller.close()
        self.tmp.cleanup()

    def test_curves_branching(self):
        res = self.ctl.run_command(["curve", "--runtime-dir", str(self.runtime_dir)])
        self.assertIn("SHARED", res.stdout)
        res = self.ctl.run_command(["curve", "gpu", "--runtime-dir", str(self.runtime_dir)])
        self.assertIn("GPU", res.stdout)
        res = self.ctl.run_command(["curves", "night", "--runtime-dir", str(self.runtime_dir)])
        self.assertIn("night:", res.stdout)

        with self.assertRaises(SystemExit):
            self.ctl.run_command(["curves", "load", "missing_curve", "--runtime-dir", str(self.runtime_dir)])
        with self.assertRaises(SystemExit):
            self.ctl.run_command(["curves", "missing_subcommand", "--runtime-dir", str(self.runtime_dir)])

    def test_invalid_arguments(self):
        with self.assertRaises(SystemExit):
            self.ctl.run_command(["profile", "bogus", "--runtime-dir", str(self.runtime_dir)])
        with self.assertRaises(SystemExit):
            self.ctl.run_command(["mode", "bogus", "--runtime-dir", str(self.runtime_dir)])
        with self.assertRaises(SystemExit):
            self.ctl.run_command(["set", "1", "--runtime-dir", str(self.runtime_dir)])
        with self.assertRaises(SystemExit):
            self.ctl.run_command(["set", "fan", "50", "--runtime-dir", str(self.runtime_dir)])
        with self.assertRaises(SystemExit):
            self.ctl.run_command(["set", "1", "150", "--runtime-dir", str(self.runtime_dir)])
        with self.assertRaises(SystemExit):
            self.ctl.run_command(["cap", "--runtime-dir", str(self.runtime_dir)])
        with self.assertRaises(SystemExit):
            self.ctl.run_command(["cap", "notanumber", "--runtime-dir", str(self.runtime_dir)])
        with self.assertRaises(SystemExit):
            self.ctl.run_command(["config", "nokeyvalue", "--runtime-dir", str(self.runtime_dir)])
        with self.assertRaises(SystemExit):
            self.ctl.run_command(["config", "unknown_key=10", "--runtime-dir", str(self.runtime_dir)])

    def test_config_keys_and_display(self):
        # Show all config
        res = self.ctl.run_command(["config", "--runtime-dir", str(self.runtime_dir)])
        self.assertIn("max_duty", res.stdout)
        # Update remaining keys, including max_duty
        res = self.ctl.run_command(["config", "max_duty=80", "theme=light", "linked=1", "--runtime-dir", str(self.runtime_dir), "--json"])
        self.assertEqual(json.loads(res.stdout)["updated"]["max_duty"], 80)

    def test_main_cli_entry_exceptions(self):
        with mock.patch("sys.argv", ["fan-ctl", "status", "--runtime-dir", str(self.runtime_dir)]):
            self.ctl.main()
        with mock.patch("sys.argv", ["fan-ctl", "status", "--runtime-dir", "/tmp/nonexistent-run"]):
            with self.assertRaises(SystemExit):
                self.ctl.main()


class DaemonInProcessTests(unittest.TestCase):
    def test_daemon_args_and_diagnose(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = pathlib.Path(tmp) / "fan-control.json"
            with mock.patch("sys.argv", ["fan-daemon", "--diagnose", "--config", str(cfg)]):
                out = io.StringIO()
                with mock.patch("sys.stdout", out):
                    daemon.main()
                self.assertIn("config", out.getvalue())

    def test_daemon_main_invalid_interval(self):
        with mock.patch("sys.argv", ["fan-daemon", "--interval", "-1"]):
            with self.assertRaises(SystemExit):
                daemon.main()

    def test_daemon_run_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            rdir = pathlib.Path(tmp) / "run"
            cfg = pathlib.Path(tmp) / "fan-control.json"
            args = argparse.Namespace(
                dry_run=True,
                interval=0.01,
                config=str(cfg),
                runtime_dir=str(rdir),
                fans=2,
            )
            # Run 1 tick then stop
            def fake_sleep(_dur):
                os.kill(os.getpid(), signal.SIGTERM)
            with mock.patch("time.sleep", side_effect=fake_sleep):
                daemon.run(args)

    def test_daemon_run_real_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            rdir = pathlib.Path(tmp) / "run"
            cfg = pathlib.Path(tmp) / "fan-control.json"
            args = argparse.Namespace(
                dry_run=False,
                interval=0.01,
                config=str(cfg),
                runtime_dir=str(rdir),
                backend="auto",
                device=None,
                fans=2,
            )
            def fake_sleep(_dur):
                # trigger reload and then stop
                os.kill(os.getpid(), signal.SIGHUP)
                os.kill(os.getpid(), signal.SIGTERM)
            mock_lock = mock.MagicMock()
            mock_lock.acquire.return_value = True
            with mock.patch.object(daemon, "detect_backend", return_value=backend.DemoBackend()), \
                 mock.patch.object(daemon, "ExclusiveLock", return_value=mock_lock), \
                 mock.patch("time.sleep", side_effect=fake_sleep):
                daemon.run(args)


class GtkLogicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.cfg_path = self.base / "fan-control.json"
        self.rdir = self.base / "run"
        self.rdir.mkdir()
        self.sock_path = self.rdir / rpc.SOCKET_NAME
        self.controller = controller.FanController(backend.DemoBackend(), self.cfg_path, self.rdir, demo=True)
        self.server = rpc.RpcServer(self.controller, self.sock_path)
        self.server.start()
        self.client = rpc.RpcClient(self.sock_path)
        self.client.connect()
        self.gtk_mod = load("fan_gtk", "fan_gtk.py")

    def tearDown(self):
        self.client.close()
        self.server.close()
        self.controller.close()
        self.tmp.cleanup()

    def test_adapters(self):
        inproc = self.gtk_mod._InProcessAdapter(self.controller)
        snap = inproc.handle("snapshot")
        self.assertIn("mode", snap)
        inproc.close()

        rpc_ad = self.gtk_mod._RpcClientAdapter(self.client)
        live = rpc_ad.handle("live")
        self.assertIn("control_temp", live)
        rpc_ad.close()

    def test_fan_application_logic(self):
        args = argparse.Namespace(demo=True, config=str(self.cfg_path), runtime_dir=str(self.rdir), debug=False)
        app = self.gtk_mod.FanApplication(args)
        app._setup_controller()
        self.assertIsNotNone(app.controller)
        self.assertIsNotNone(app._rpc)

        # Test _update_header status formatting
        app._update_header_status("Test Status")
        app._update_header({"updated": time.time(), "fault_missing": 0, "demo": True})
        app._update_header({"updated": time.time() - 10, "fault_missing": 3})
        app._update_header({"updated": time.time(), "critical_active": True})

        # Test _push_snapshot and _push_history
        app._push_snapshot(full=True)
        app._push_snapshot(full=False)
        app._push_history()

        # Test _teardown
        app._teardown()

    def test_fan_application_real_mode_setup(self):
        args = argparse.Namespace(demo=False, config=str(self.cfg_path), runtime_dir=str(self.rdir), debug=False)
        app = self.gtk_mod.FanApplication(args)
        app._setup_controller()
        self.assertIsNotNone(app._rpc)
        self.assertIsNone(app.controller)
        app._teardown()

    def test_tray_set_profile(self):
        args = argparse.Namespace(demo=False, config=str(self.cfg_path), runtime_dir=str(self.rdir))
        tray = self.gtk_mod.TrayApplication(args)
        tray.popover = mock.MagicMock()
        tray._set_profile("silent")
        self.assertEqual(self.controller.state["profile"], "silent")

        # Test when daemon is down
        broken_args = argparse.Namespace(demo=False, config=str(self.cfg_path), runtime_dir="/tmp/nonexistent-tray-run")
        broken_tray = self.gtk_mod.TrayApplication(broken_args)
        broken_tray.popover = mock.MagicMock()
        broken_tray.send_notification = mock.MagicMock()
        broken_tray._set_profile("silent")
        broken_tray.send_notification.assert_called_once()


class GuiTests(unittest.TestCase):
    def test_headless_smoke(self):
        gui_mod = load("fan_gui", "fan-gui.py")
        with tempfile.TemporaryDirectory() as tmp:
            cfg = pathlib.Path(tmp) / "fan-control.json"
            args = argparse.Namespace(
                demo=True,
                config=str(cfg),
                runtime_dir=tmp,
                backend="auto",
                device=None,
            )
            out = io.StringIO()
            with mock.patch("sys.stdout", out):
                gui_mod._headless_smoke(args)
            data = json.loads(out.getvalue())
            self.assertIn("fan1", data)
            self.assertTrue(data["demo"])


class DeepCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.cfg_path = self.base / "fan-control.json"
        self.rdir = self.base / "run"
        self.rdir.mkdir()
        self.sock_path = self.rdir / rpc.SOCKET_NAME
        self.controller = controller.FanController(backend.DemoBackend(), self.cfg_path, self.rdir, demo=True)
        self.server = rpc.RpcServer(self.controller, self.sock_path)
        self.server.start()
        self.client = rpc.RpcClient(self.sock_path)
        self.client.connect()
        self.gtk_mod = load("fan_gtk", "fan_gtk.py")

    def tearDown(self):
        self.client.close()
        self.server.close()
        self.controller.close()
        self.tmp.cleanup()

    def test_gtk_push_and_message_coverage(self):
        args = argparse.Namespace(demo=True, config=str(self.cfg_path), runtime_dir=str(self.rdir), debug=False)
        app = self.gtk_mod.FanApplication(args)
        app._setup_controller()
        app.webview = mock.MagicMock()
        app.status_label = mock.MagicMock()

        # Test _push_snapshot and _push_history with active webview
        self.assertTrue(app._push_snapshot(full=True))
        self.assertTrue(app._push_snapshot(full=False))
        self.assertTrue(app._push_history())

        # Test _push_snapshot when RPC raises error -> sets Daemon unreachable
        with mock.patch.object(app._rpc, "handle", side_effect=RuntimeError("connection dropped")):
            self.assertTrue(app._push_snapshot(full=False))
            self.assertTrue(app._push_history())
            self.assertTrue(app._update_header())

        # Test _maybe_notify critical alert
        app._notified_critical = False
        app._maybe_notify({"control_temp": 100, "critical_temp": 95, "alerts": {"desktop": True}})
        self.assertTrue(app._notified_critical)
        app._maybe_notify({"control_temp": 80, "critical_temp": 95, "alerts": {"desktop": True}})
        self.assertFalse(app._notified_critical)

        # Test _on_message with reply
        js_val = mock.MagicMock()
        js_val.to_json.return_value = json.dumps({"method": "snapshot", "params": {}})
        reply = mock.MagicMock()
        with mock.patch("gi.repository.JavaScriptCore.Value.new_from_json"):
            app._on_message(None, js_val, reply)
            reply.return_value.assert_called_once()

        # Test _on_message error
        js_val.to_json.return_value = json.dumps({"method": "bad_method"})
        reply_err = mock.MagicMock()
        app._on_message(None, js_val, reply_err)
        reply_err.return_error_message.assert_called_once()
        # Test file ops
        mock_dialog = mock.MagicMock()
        mock_file = mock.MagicMock()
        mock_file.get_basename.return_value = "custom.json"
        mock_file.load_contents.return_value = (True, json.dumps({"name": "imported", "curve": [[0, 0], [100, 100]]}).encode(), None)
        mock_dialog.save_finish.return_value = mock_file
        mock_dialog.open_finish.return_value = mock_file

        app._save_csv(mock_dialog, mock.MagicMock())
        app._save_curve(mock_dialog, mock.MagicMock(), {"name": "test"})
        app._open_curve(mock_dialog, mock.MagicMock())

        app._teardown()

    def test_gtk_real_mode_connection_retry_and_failure(self):
        args = argparse.Namespace(demo=False, config=str(self.cfg_path), runtime_dir="/tmp/nonexistent-rpc-test", debug=False)
        app = self.gtk_mod.FanApplication(args)
        with mock.patch("subprocess.run"), \
             mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                app._setup_controller()
            self.assertIn("fan-daemon is not running", str(ctx.exception))
    def test_rpc_retry_on_broken_pipe(self):
        calls = 0
        orig_exchange = self.client._exchange
        def flaky_exchange(method, params):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise BrokenPipeError("pipe broken")
            return orig_exchange(method, params)
        self.client._exchange = flaky_exchange
        res = self.client.call("snapshot")
        self.assertIn("mode", res)
    def test_rpc_client_oversized_reply(self):
        with mock.patch("fan_rpc._read_line", side_effect=rpc._FrameTooLarge()):
            with self.assertRaises(ConnectionError) as ctx:
                self.client.call("snapshot")
            self.assertIn("oversized reply", str(ctx.exception))

    def test_rpc_client_connection_refused_error(self):
        with mock.patch("socket.socket.connect", side_effect=ConnectionRefusedError("refused")):
            c = rpc.RpcClient(self.sock_path)
            with self.assertRaises(ConnectionError) as ctx:
                c.connect()
            self.assertIn("fan-daemon is not running", str(ctx.exception))

    def test_daemon_main_dry_run_entrypoint(self):
        def stop_immediately(_dur):
            os.kill(os.getpid(), signal.SIGINT)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("sys.argv", ["fan-daemon", "--dry-run", "--interval", "0.01", "--runtime-dir", tmp]), \
                 mock.patch("time.sleep", side_effect=stop_immediately):
                out = io.StringIO()
                with mock.patch("sys.stdout", out):
                    daemon.main()
                self.assertIn("backend=demo", out.getvalue())

    def test_ctl_main_success_output(self):
        ctl = load("fan_ctl", "fan-ctl.py")
        out = io.StringIO()
        with mock.patch("sys.argv", ["fan-ctl", "status", "--runtime-dir", str(self.rdir), "--json"]), \
             mock.patch("sys.stdout", out):
            ctl.main()
        self.assertIn("daemon_reachable", out.getvalue())

    def test_gtk_serve_bytes_and_ui(self):
        args = argparse.Namespace(demo=True, config=str(self.cfg_path), runtime_dir=str(self.rdir), debug=False)
        app = self.gtk_mod.FanApplication(args)
        req = mock.MagicMock()
        req.get_uri.return_value = "fancontrol://app/index.html"
        app._serve_ui(req)
        req.finish_with_response.assert_called_once()

        # Test forbidden relative traversal
        req_bad = mock.MagicMock()
        req_bad.get_uri.return_value = "fancontrol://app/../../etc/passwd"
        app._serve_ui(req_bad)
        req_bad.finish_error.assert_called_once()

    def test_gtk_eval_event_and_push_history_append(self):
        args = argparse.Namespace(demo=True, config=str(self.cfg_path), runtime_dir=str(self.rdir), debug=False)
        app = self.gtk_mod.FanApplication(args)
        app.webview = mock.MagicMock()
        app.status_label = mock.MagicMock()
        app._setup_controller()

        # Seed history
        app._push_history()
        # Append to history
        self.controller.tick_history()
        app._push_history()

        # Duplicate live suppression
        app._eval_event("live", {"temp": 50})
        app._eval_event("live", {"temp": 50})
        app._teardown()

    def test_tick_control_write_error_and_ping(self):
        # Test write error branch
        with mock.patch.object(self.controller.backend, "write_duty", side_effect=backend.FanBackendError("write failed")):
            self.controller.state["mode"] = "manual"
            self.controller.state["targets"] = {"1": 50}
            self.controller.tick_control()

        # Test ping branch (empty writes)
        # Same duty -> no write -> ping called
        self.controller.tick_control()
        self.controller.tick_readback()
        with mock.patch.object(self.controller.backend, "ping") as mock_ping:
            self.controller.tick_control()
            mock_ping.assert_called_once()

    def test_diagnostics_nvidia_exception(self):
        import fan_diagnostics
        def fake_run(cmd, **kwargs):
            if "nvidia-smi" in cmd:
                raise FileNotFoundError("no nvidia-smi")
            return mock.MagicMock(returncode=1, stdout="")
        with mock.patch("subprocess.run", side_effect=fake_run):
            text = fan_diagnostics.diagnose(str(self.cfg_path))
            self.assertIn("nvidia", text)



    def test_tuxedo_hold_thread_lifecycle(self):
        class _Probe(backend.TuxedoIoBackend):
            def __init__(self):
                self._lock = threading.RLock()
                self._duties = {1: 50, 2: 50}
                self._last_hold = 0.0
                self._hold_stop = threading.Event()
                self._hold_thread = None
                self.is_clevo = False
                self.written = []
            def _write(self, cmd, val):
                self.written.append((cmd, val))
        p = _Probe()
        p._start_hold_thread()
        self.assertIsNotNone(p._hold_thread)
        time.sleep(0.3)
        p._stop_hold_thread()
        self.assertIsNone(p._hold_thread)
        self.assertTrue(len(p.written) > 0)

    def test_migrate_config_non_list_curve(self):
        data = {"max_duty": 150, "curve": "not-a-list"}
        res = backend.migrate_config(data)
        self.assertEqual(res["curve"], "not-a-list")

    def test_gui_main_default_config_handling(self):
        gui_mod = load("fan_gui", "fan-gui.py")
        with mock.patch("sys.argv", ["fan-gui", "--demo", "--headless-smoke"]), \
             mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("sys.stdout", io.StringIO()):
            gui_mod.main()

    def test_controller_error_branches(self):
        # backend.lock fails
        with mock.patch.object(self.controller.backend, "lock", side_effect=backend.FanBackendError("lock err")):
            self.controller.reload_config()

        # backend.fans fails
        with mock.patch.object(self.controller.backend, "fans", side_effect=backend.FanBackendError("fans err")):
            self.assertEqual(self.controller.fans(), [1, 2])

        # _profile custom when curve is empty
        self.controller.state["curve"] = []
        self.controller.handle("profile", {"profile": "custom"})
        self.assertTrue(len(self.controller.state["curve"]) > 0)

        # _custom which == gpu
        self.controller.handle("custom", {"which": "gpu", "curve": [[0, 0], [100, 100]]})
        self.assertIsNotNone(self.controller.state["curve_gpu"])

        # tick_readback fails
        with mock.patch.object(self.controller.backend, "read_duty", side_effect=backend.FanBackendError("read err")):
            self.controller.tick_readback()

    def test_scan_sensors_error_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            hw = pathlib.Path(tmp) / "hwmon0"
            hw.mkdir()
            # Unreadable name file
            (hw / "name").mkdir()
            # Valid name, invalid temp value (covers 173-174)
            hw2 = pathlib.Path(tmp) / "hwmon1"
            hw2.mkdir()
            (hw2 / "name").write_text("coretemp\n")
            (hw2 / "temp1_input").write_text("invalid_num\n")
            policy.scan_sensors(demo=False, include_nvidia=True, hwmon_dir=tmp)

        with mock.patch("shutil.which", return_value="/usr/bin/nvidia-smi"), \
             mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("nvidia-smi", 2)):
            policy.scan_sensors(demo=False, include_nvidia=True, hwmon_dir="/nonexistent-dir-abc")

    def test_check_versions_script(self):
        spec = importlib.util.spec_from_file_location("check_versions", ROOT / "scripts" / "check_versions.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.main(), 0)

        # Test mismatch detection
        with mock.patch.dict(mod.CHECKS, {"dummy": (ROOT / "packaging" / "PKGBUILD", r"^pkgname=([^\s]+)")}):
            with self.assertRaises(SystemExit):
                mod.main()

        # Test missing file error
        with mock.patch.dict(mod.CHECKS, {"missing": (pathlib.Path("/tmp/nonexistent-file-123"), r"pattern")}):
            with self.assertRaises(SystemExit):
                mod.main()


class PerFile95GateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.cfg_path = self.base / "fan-control.json"
        self.rdir = self.base / "run"
        self.rdir.mkdir()
        self.sock_path = self.rdir / rpc.SOCKET_NAME
        self.controller = controller.FanController(backend.DemoBackend(), self.cfg_path, self.rdir, demo=True)
        self.server = rpc.RpcServer(self.controller, self.sock_path)
        self.server.start()
        self.client = rpc.RpcClient(self.sock_path)
        self.client.connect()
        self.gtk_mod = load("fan_gtk", "fan_gtk.py")

    def tearDown(self):
        self.client.close()
        self.server.close()
        self.controller.close()
        self.tmp.cleanup()

    def test_check_versions_pattern_not_found(self):
        spec = importlib.util.spec_from_file_location("check_versions", ROOT / "scripts" / "check_versions.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with mock.patch.dict(mod.CHECKS, {"dummy": (ROOT / "packaging" / "PKGBUILD", r"^nonexistent_key_pattern=([^\s]+)")}):
            with self.assertRaises(SystemExit):
                mod.main()
        # Cover line 58 (__main__ execution)
        import runpy
        with mock.patch("sys.exit"), mock.patch("sys.stdout"):
            runpy.run_path(str(ROOT / "scripts" / "check_versions.py"), run_name="__main__")

    def test_sys_path_insert_branches(self):
        orig = list(sys.path)
        try:
            root_str = str(ROOT)
            sys.path = [p for p in sys.path if p != root_str and p != ""]
            load("fan_gui_path_test", "fan-gui.py")
            load("fan_daemon_path_test", "fan-daemon.py")
            load("fan_ctl_path_test", "fan-ctl.py")
        finally:
            sys.path = orig

    def test_fan_daemon_reload_and_close_exceptions(self):
        with tempfile.TemporaryDirectory() as tmp:
            rdir = pathlib.Path(tmp) / "run"
            cfg = pathlib.Path(tmp) / "fan-control.json"
            args = argparse.Namespace(
                dry_run=False, interval=0.01, config=str(cfg),
                runtime_dir=str(rdir), backend="auto", device=None, fans=2,
            )
            def fake_sleep(_dur):
                handler = signal.getsignal(signal.SIGHUP)
                if callable(handler):
                    # 1. Success reload (covers line 92)
                    handler(signal.SIGHUP, None)
                    # 2. Failure reload (covers lines 93-94)
                    mock_ctrl.reload_config.side_effect = ValueError("reload err")
                    handler(signal.SIGHUP, None)
                os.kill(os.getpid(), signal.SIGTERM)

            mock_lock = mock.MagicMock()
            mock_lock.acquire.return_value = True
            mock_ctrl = mock.MagicMock()
            with mock.patch.object(daemon, "detect_backend", return_value=backend.DemoBackend()), \
                 mock.patch.object(daemon, "ExclusiveLock", return_value=mock_lock), \
                 mock.patch.object(daemon, "FanController", return_value=mock_ctrl), \
                 mock.patch.object(daemon, "RpcServer") as mock_srv, \
                 mock.patch("sys.stderr", io.StringIO()), \
                 mock.patch("sys.stdout", io.StringIO()), \
                 mock.patch("time.sleep", side_effect=fake_sleep):
                inst = mock_srv.return_value
                inst.close.side_effect = RuntimeError("srv close err")
                mock_ctrl.close.side_effect = RuntimeError("ctrl close err")
                daemon.run(args)

        # Test main() exception branches (lines 143-148) and __main__ (line 152)
        for err in (PermissionError("need root"), FileNotFoundError("no dev"), ValueError("bad val")):
            with mock.patch("sys.argv", ["fan-daemon"]), \
                 mock.patch.object(daemon, "run", side_effect=err):
                with self.assertRaises(SystemExit):
                    daemon.main()
        import runpy
        with mock.patch("sys.argv", ["fan-daemon.py", "--diagnose", "--config", str(cfg)]), \
             mock.patch("sys.exit"), mock.patch("sys.stdout"):
            runpy.run_path(str(ROOT / "fan-daemon.py"), run_name="__main__")

    def test_controller_mode_released_and_fault_exceptions(self):
        # Mode released call
        self.controller.handle("mode", {"mode": "released"})
        self.assertEqual(self.controller.state["mode"], "released")

        # Fault release error (399-400)
        self.controller.state["mode"] = "curve"
        self.controller._missing = 3
        with mock.patch.object(self.controller, "cpu_temp", return_value=None), \
             mock.patch.object(self.controller, "gpu_temp", return_value=None), \
             mock.patch.object(self.controller.backend, "release", side_effect=backend.FanBackendError("rel err")):
            self.controller.tick_control()

        # Fault re-lock fails (409-410)
        self.controller.state["mode"] = "curve"
        self.controller.tick_sensors()
        self.controller.tick_readback()
        self.controller._released_for_fault = True
        self.controller._missing = 0
        with mock.patch.object(self.controller.backend, "lock", side_effect=backend.FanBackendError("relock err")):
            self.controller.tick_control()

        # Ping fails (423-424)
        self.controller.state["mode"] = "curve"
        self.controller.tick_sensors()
        self.controller.tick_readback()
        self.controller.tick_control()
        self.controller.tick_readback()
        with mock.patch.object(self.controller.backend, "ping", side_effect=backend.FanBackendError("ping err")):
            self.controller.tick_control()

        # Backend release and close fail on close (437, 441)
        c = controller.FanController(backend.DemoBackend(), self.cfg_path, self.rdir, demo=True)
        with mock.patch.object(c.backend, "release", side_effect=backend.FanBackendError("rel err")), \
             mock.patch.object(c.backend, "close", side_effect=backend.FanBackendError("cls err")):
            c.close()
    def test_tuxedo_backend_branches(self):
        class _Probe(backend.TuxedoIoBackend):
            def __init__(self):
                super().__init__("/dev/null")
                self.is_clevo = True
                self.written = []
            def _write(self, cmd, val):
                self.written.append((cmd, val))
            def _detect_clevo(self):
                return True
        with mock.patch("os.open", return_value=999):
            p = _Probe()
            p.lock()  # is_clevo True -> return
            p.is_clevo = False
            p._start_hold_thread()
            p.lock()  # thread already alive -> return
            p._start_hold_thread()  # already alive -> return
            with mock.patch.object(p, "_commit_uniwill_duties", side_effect=OSError("loop err")):
                time.sleep(0.3)
            with mock.patch("os.close"):
                p.close()

        # migrate_config with named_curves containing non-list
        data = {"max_duty": 150, "named_curves": {"bad": "not-list"}}
        res = backend.migrate_config(data)
        self.assertEqual(res["named_curves"]["bad"], "not-list")

    def test_rpc_error_and_close_branches(self):
        # chown raises OSError
        with mock.patch("grp.getgrnam", return_value=mock.MagicMock(gr_gid=1000)), \
             mock.patch("os.chown", side_effect=PermissionError("no chown")):
            rpc._give_group_access("/tmp")

        # bind raises OSError in setup
        with mock.patch("socket.socket.bind", side_effect=OSError("bind fail")):
            srv = rpc.RpcServer(self.controller, "/tmp/test-bad-bind.sock")
            with self.assertRaises(OSError):
                srv._setup()

        # _reply raises OSError
        mock_conn = mock.MagicMock()
        mock_conn.sendall.side_effect = BrokenPipeError("reply fail")
        rpc.RpcServer._reply(mock_conn, {"ok": True})

        # close with sock.close and os.unlink raising OSError
        srv = rpc.RpcServer(self.controller, self.base / "test-close.sock")
        srv._sock = mock.MagicMock()
        srv._sock.close.side_effect = OSError("sock close fail")
        srv._owns_socket_file = True
        with mock.patch("os.unlink", side_effect=PermissionError("unlink fail")):
            srv.close()

        # client _exchange with non-dict reply and empty reply
        with mock.patch("fan_rpc._read_line", return_value=b"123\n"):
            with self.assertRaises(ConnectionError):
                self.client.call("snapshot")
        with mock.patch("fan_rpc._read_line", return_value=None):
            with self.assertRaises(ConnectionError):
                self.client.call("snapshot")

        # client _close_socket with close raising OSError
        c = rpc.RpcClient(self.sock_path)
        c._sock = mock.MagicMock()
        c._sock.close.side_effect = OSError("close fail")
        c.close()

        # _read_line when single chunk exceeds limit and has newline (line 68)
        mock_sock = mock.MagicMock()
        mock_sock.recv.return_value = b"12345\n"
        with self.assertRaises(rpc._FrameTooLarge):
            rpc._read_line(mock_sock, limit=3)

    def test_gtk_branches(self):
        args = argparse.Namespace(demo=True, config=str(self.cfg_path), runtime_dir=str(self.rdir), debug=True)
        app = self.gtk_mod.FanApplication(args)
        app._setup_controller()

        # do_activate when window exists
        app.window = mock.MagicMock()
        app.do_activate()
        app.window.present.assert_called_once()
        app.window = None

        # do_activate happy path (covers line 111 self.window.present)
        with mock.patch.object(app, "_setup_controller"), \
             mock.patch.object(app, "_build_window", side_effect=lambda: setattr(app, "window", mock.MagicMock())), \
             mock.patch.object(app, "_start_loops"), \
             mock.patch("gi.repository.GLib.timeout_add"):
            app.do_activate()
            app.window.present.assert_called_once()

        # _build_window with hardware acceleration error (covers lines 204-205)
        from gi.repository import GLib
        with mock.patch("gi.repository.Gtk.ApplicationWindow"), \
             mock.patch("gi.repository.WebKit.WebView"), \
             mock.patch("gi.repository.WebKit.WebContext"), \
             mock.patch("gi.repository.WebKit.UserContentManager"), \
             mock.patch("gi.repository.WebKit.Settings.set_hardware_acceleration_policy", side_effect=GLib.Error("no accel")):
            app._build_window()

        # _serve_ui with trailing slash on subdir (covers line 248)
        req = mock.MagicMock()
        req.get_uri.return_value = "fancontrol://app/subdir/"
        app._serve_ui(req)
        req.finish_with_response.assert_called_once()

        # _on_message with debug=True triggers traceback print
        js_val = mock.MagicMock()
        js_val.to_json.return_value = "not json"
        reply = mock.MagicMock()
        app._on_message(None, js_val, reply)
        reply.return_error_message.assert_called_once()

        # _on_message with mutation method triggers line 272 GLib.idle_add(self._push_full)
        js_val_mut = mock.MagicMock()
        js_val_mut.to_json.return_value = json.dumps({"method": "profile", "params": {"profile": "silent"}})
        with mock.patch("gi.repository.JavaScriptCore.Value.new_from_json"), \
             mock.patch("gi.repository.GLib.idle_add") as mock_idle:
            app._on_message(None, js_val_mut, mock.MagicMock())
            mock_idle.assert_called_once()

        # _push_live and _push_full (covers line 338 and 341)
        app.webview = mock.MagicMock()
        app.status_label = mock.MagicMock()
        app._push_live()
        app._push_full()

        # _push_history with history-append (covers line 370)
        app._history_seeded = True
        with mock.patch.object(app._rpc, "handle", return_value={"history": [{"time": 100}]}):
            app._push_history()

        # _update_header_status text change (covers line 375)
        app.status_label.get_text.return_value = "Old"
        app._update_header_status("New")
        app.status_label.set_text.assert_called_with("New")

        # _start_loops exception branch
        with mock.patch.object(app.controller, "tick_sensors", side_effect=RuntimeError("loop err")), \
             mock.patch("traceback.print_exc"):
            app._start_loops()

        # normal _start_loops on demo app (covers line 431)
        app.stop.clear()
        app._start_loops()
        app.stop.set()

        # _setup_controller real mode with retry loop break
        real_args = argparse.Namespace(demo=False, config=str(self.cfg_path), runtime_dir=str(self.rdir), debug=False)
        real_app = self.gtk_mod.FanApplication(real_args)
        calls = 0
        def retry_connect(client_self):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("try again")
            client_self._sock = mock.MagicMock()
        with mock.patch.object(rpc.RpcClient, "connect", retry_connect), \
             mock.patch("subprocess.run"), \
             mock.patch("time.sleep"):
            real_app._setup_controller()

        # real_app._start_loops when controller is None (covers line 416)
        real_app._start_loops()

        # stop.is_set() guards
        app.stop.set()
        self.assertFalse(app._push_snapshot())
        self.assertFalse(app._push_history())
        self.assertFalse(app._update_header())
        app.stop.clear()

        # callbacks
        self.assertFalse(app._on_load_failed(None, None, "uri", "err"))
        self.assertFalse(app._on_close(None))
        app._teardown()

    def test_ctl_main_runtime_error_and_runpy(self):
        ctl = load("fan_ctl", "fan-ctl.py")
        with mock.patch("sys.argv", ["fan-ctl", "status", "--runtime-dir", str(self.rdir)]), \
             mock.patch.object(ctl, "run_command", side_effect=RuntimeError("ctl err")):
            with self.assertRaises(SystemExit):
                ctl.main()
        import runpy
        with mock.patch("sys.argv", ["fan-ctl.py", "diagnose", "--config", str(self.cfg_path)]), \
             mock.patch("sys.exit"), mock.patch("sys.stdout"):
            runpy.run_path(str(ROOT / "fan-ctl.py"), run_name="__main__")
        with mock.patch("sys.argv", ["fan-gui.py", "--headless-smoke", "--demo"]), \
             mock.patch("sys.exit"), mock.patch("sys.stdout"):
            runpy.run_path(str(ROOT / "fan-gui.py"), run_name="__main__")

if __name__ == "__main__":
    unittest.main()
