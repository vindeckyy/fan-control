import importlib.util
import json
import os
import pathlib
import sys
import tempfile
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

    def test_daemon_reexports_policy(self):
        self.assertIs(daemon.normalize_curve, policy.normalize_curve)
        self.assertIs(daemon.target_duty, policy.target_duty)


class RuntimeLockTests(unittest.TestCase):
    def test_gui_lock_detects_live_and_stale_pids(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = pathlib.Path(tmp)
            self.assertFalse(runtime.gui_lock_held(directory))
            runtime.write_gui_pid(directory, os.getpid())
            self.assertTrue(runtime.gui_lock_held(directory))
            runtime.write_gui_pid(directory, 99999999)
            self.assertFalse(runtime.gui_lock_held(directory))

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
    def test_legacy_config_migration(self):
        data = {"max_duty": 198, "curve": [[0, 0], [64, 0], [65, 20], [80, 140], [95, 198]]}
        migrated = backend.migrate_config(data)
        self.assertEqual(migrated["max_duty"], 100)
        self.assertEqual(
            migrated["curve"],
            [[0, 0], [64, 0], [65, 10], [80, 71], [95, 100]],
        )

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
        self.assertEqual(probe.read_duty(1), 50)
        self.assertEqual(probe.read_duty(2), 100)
        self.assertEqual(probe.read_temp(), 55.0)
        self.assertEqual(probe.read_temp2(), 60.0)
        self.assertEqual(probe.read_rpm(1), 2400)
        self.assertEqual(probe.read_rpm(2), 3200)

        probe.write_duty(1, 100)
        raw1 = probe._pct_to_raw(100)
        self.assertEqual(probe.written[-1], (backend.W_CL_FANSPEED, raw1))

        probe.release()
        self.assertEqual(probe.written[-1], (backend.W_CL_FANAUTO, 0))

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
        self.assertEqual(probe.read_duty(1), 50)
        self.assertEqual(probe.read_temp(), 48.0)
        self.assertIsNone(probe.read_rpm(1))

        probe.lock()
        self.assertEqual(probe.written[-1], (backend.W_UW_MODE, 0x40))

        probe.write_duty(1, 50)
        self.assertEqual(probe.written[-1], (backend.W_UW_FANSPEED, 99))

        with mock.patch("fcntl.ioctl") as mock_ioctl:
            probe.release()
            mock_ioctl.assert_called_once_with(42, backend.W_UW_FANAUTO)

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
    def test_status_and_profile_change(self):
        ctl = load("fan_ctl", "fan-ctl.py")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "fan-control.json"
            runtime_dir = pathlib.Path(tmp) / "run"
            runtime_dir.mkdir()
            policy.save_config(path, {"profile": "balanced"})
            result = ctl.run_command(["status", "--config", str(path), "--runtime-dir", str(runtime_dir), "--json"])
            data = json.loads(result.stdout)
            self.assertEqual(data["profile"], "balanced")
            self.assertFalse(data["gui_exclusive"])
            runtime.write_gui_pid(runtime_dir, os.getpid())
            with self.assertRaises(SystemExit):
                ctl.run_command(["profile", "silent", "--config", str(path), "--runtime-dir", str(runtime_dir)])


if __name__ == "__main__":
    unittest.main()
