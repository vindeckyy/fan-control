import importlib.util
import os
import pathlib
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


daemon = load("fan_daemon", "fan-daemon.py")
gui = load("fan_gui", "fan-gui.py")
backend = load("fan_backend", "fan_backend.py")


class FanLogicTests(unittest.TestCase):
    def test_interpolation_and_bounds(self):
        curve = daemon.normalize_curve([[80, 140], [50, 0], [95, 250]])
        self.assertEqual(curve, [(50.0, 0), (80.0, 100), (95.0, 100)])
        self.assertEqual(daemon.target_duty(65, curve), 50)
        self.assertEqual(daemon.target_duty(200, curve), 100)

    def test_critical_cooling_bypasses_noise_cap(self):
        curve = [(0, 0), (110, 100)]
        self.assertLess(daemon.target_duty(50, curve, max_duty=60, critical_temp=95), 60)
        self.assertEqual(daemon.target_duty(95, curve, max_duty=60, critical_temp=95), 100)

    def test_curve_validation_deduplicates_temperatures(self):
        self.assertEqual(gui.normalize_curve([[50, 10], [50, 20], [70, 300]]), [(50, 20), (70, 100)])
        with self.assertRaises(ValueError):
            gui.normalize_curve([[50, 10]])

    def test_dashboard_contract(self):
        self.assertIn('id="chart"', gui.HTML)
        self.assertIn('id="curveRows"', gui.HTML)
        self.assertIn("$('chart').toggleAttribute('hidden',!visible)", gui.HTML)
        self.assertNotIn("$('chart').hidden=false", gui.HTML)
        self.assertEqual(gui.HTML.count('id="fan1"'), 1)
        self.assertEqual(gui.HTML.count('id="fan2"'), 1)

    def test_nvidia_smi_sensor_parsing(self):
        sensors = gui.parse_nvidia_smi("0, 67, NVIDIA GeForce RTX 4080\n1, 54, NVIDIA RTX A2000\n")
        self.assertEqual([sensor["temp"] for sensor in sensors], [67.0, 54.0])
        self.assertIn("RTX 4080", sensors[0]["label"])
        self.assertEqual(daemon.parse_nvidia_temperatures("67\n54\nN/A\n"), [67.0, 54.0])

    def test_control_uses_hottest_cpu_or_gpu_sensor(self):
        with mock.patch.object(gui, "primary_temp", return_value=61.0), mock.patch.object(gui, "gpu_temp", return_value=78.0):
            self.assertEqual(gui.control_temp(), 78.0)


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

            b.write_duty(1, 150)  # clamps to 100
            self.assertEqual((base / "fan1_manual_duty").read_text().strip(), "100")
            b.release()
            self.assertEqual((base / "fan_release").read_text().strip(), "1")
            b.ping()
            self.assertEqual((base / "fan_watchdog_ping").read_text().strip(), "1")

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
        with mock.patch.object(backend, "ClevoAcpiBackend", return_value=fake), \
                mock.patch.dict(os.environ, {"FAN_CONTROL_BACKEND": "tuxedo_io"}):
            self.assertIs(backend.detect_backend("clevo_acpi"), fake)

    def test_detect_backend_rejects_unknown(self):
        with self.assertRaises(backend.FanBackendError):
            backend.detect_backend("bogus")

    def test_device_path_override(self):
        with mock.patch.dict(os.environ, {"FAN_CONTROL_DEVICE": "/dev/custom_fan_io"}):
            self.assertEqual(backend._find_ec_device(), "/dev/custom_fan_io")


if __name__ == "__main__":
    unittest.main()
