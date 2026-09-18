#!/usr/bin/env python3
"""Unit tests for Windows-specific fan control features, backends, and bridges."""

import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fan_backend as backend
import fan_diagnostics as diagnostics
import fan_policy as policy
import fan_runtime as runtime
import fan_windows_gui as win_gui


class WindowsRuntimeTests(unittest.TestCase):
    def test_windows_runtime_paths(self):
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.dict(os.environ, {"PROGRAMDATA": "C:\\ProgramData"}):
            p = runtime.runtime_dir(demo=False)
            self.assertIn("fan-control", str(p))

    def test_exclusive_lock_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = pathlib.Path(tmp) / "test.lock"
            mock_msvcrt = mock.MagicMock()

            # First acquisition succeeds
            with mock.patch.object(sys, "platform", "win32"), \
                 mock.patch.object(runtime, "msvcrt", mock_msvcrt):
                first = runtime.ExclusiveLock(lock_path)
                self.assertTrue(first.acquire())
                mock_msvcrt.locking.assert_called_with(first._fd, mock_msvcrt.LK_NBLCK, 1)

                # Second acquisition fails when msvcrt.locking raises OSError
                mock_msvcrt.locking.side_effect = OSError("Lock violation")
                second = runtime.ExclusiveLock(lock_path)
                self.assertFalse(second.acquire())

                # Release first
                mock_msvcrt.locking.side_effect = None
                first.release()
                mock_msvcrt.locking.assert_called_with(mock.ANY, mock_msvcrt.LK_UNLCK, 1)

                # Second can now acquire
                self.assertTrue(second.acquire())
                second.release()


class WindowsBackendTests(unittest.TestCase):
    def test_windows_ec_backend_missing_driver(self):
        with mock.patch.object(backend.WindowsEcBackend, "find_driver_dll", return_value=None):
            self.assertFalse(backend.WindowsEcBackend.available())
            with self.assertRaises(backend.FanBackendError) as ctx:
                backend.WindowsEcBackend()
            self.assertIn("No Windows fan-control EC driver found", str(ctx.exception))

    def test_windows_ec_backend_mock_driver(self):
        mock_dll = mock.MagicMock()
        mock_dll.DlPortReadPortUchar.return_value = 0  # IBF = 0, OBF = 0

        dll_mock_target = "ctypes.WinDLL" if sys.platform == "win32" else "ctypes.CDLL"
        with mock.patch.object(backend.WindowsEcBackend, "find_driver_dll", return_value="dummy_inpoutx64.dll"), \
             mock.patch(dll_mock_target, return_value=mock_dll):
            ec = backend.WindowsEcBackend("dummy_inpoutx64.dll")
            self.assertEqual(ec.fans(), [1, 2, 3])

            # Write duty
            ec.write_duty(1, 50)
            self.assertEqual(ec.read_duty(1), 50)

            # Release
            ec.release()

            # Invalid duty
            with self.assertRaises(backend.FanBackendError):
                ec.write_duty(1, "invalid")

            ec.close()

    def test_windows_wmi_backend(self):
        class FakeTransport:
            def __init__(self):
                self.regs = {
                    0x043E: 63,
                    0x044F: 50,
                    0x0751: 0x00,
                    0x1804: 100,
                    0x1809: 80,
                    0x0464: 0x0C,
                    0x0465: 0xD1,
                    0x046C: 0x0C,
                    0x046D: 0x30,
                }
                self.writes = []
                self.closed = False

            def read(self, addr):
                return self.regs.get(addr, 0)

            def write(self, addr, value):
                self.regs[addr] = value & 0xFF
                self.writes.append((addr, value & 0xFF))

            def close(self):
                self.closed = True

        fake = FakeTransport()
        wmi_be = backend.WindowsWmiBackend(transport=fake)
        self.assertEqual(wmi_be.fans(), [1, 2])
        self.assertEqual(wmi_be.read_temp(), 63.0)
        self.assertEqual(wmi_be.read_temp2(), 50.0)
        self.assertEqual(wmi_be.read_rpm(1), 0x0CD1)
        self.assertEqual(wmi_be.read_rpm(2), 0x0C30)
        self.assertIsNone(wmi_be.read_rpm(3))
        self.assertEqual(wmi_be.read_duty(1), 50)

        wmi_be.write_duty(1, 65)
        self.assertEqual(fake.regs[0x0751], backend.WindowsWmiBackend.EC_MANUAL_BIT)
        self.assertEqual(fake.regs[0x1804], 130)
        self.assertEqual(wmi_be.read_duty(1), 65)

        wmi_be.write_duty(2, 25)
        self.assertEqual(fake.regs[0x1809], 50)
        self.assertEqual(wmi_be.read_duty(2), 25)

        wmi_be.release()
        self.assertEqual(fake.regs[0x0751] & 0x40, 0)

        with self.assertRaises(backend.FanBackendError):
            wmi_be.write_duty(1, "invalid")
        with self.assertRaises(backend.FanBackendError):
            wmi_be.write_duty(3, 50)

        wmi_be.close()
        self.assertTrue(fake.closed)

    def test_windows_wmi_available_requires_exact_class(self):
        found = mock.MagicMock(returncode=0, stdout="AcpiTest_MULong\n")
        missing = mock.MagicMock(returncode=0, stdout="")
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch("subprocess.run", return_value=found) as run:
            self.assertTrue(backend.WindowsWmiBackend.available())
            self.assertIn("-ClassName AcpiTest_MULong", run.call_args[0][0][-1])
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch("subprocess.run", return_value=missing):
            self.assertFalse(backend.WindowsWmiBackend.available())
        with mock.patch.object(sys, "platform", "linux"):
            self.assertFalse(backend.WindowsWmiBackend.available())

    def test_detect_backend_windows(self):
        with mock.patch.object(sys, "platform", "win32"):
            # When auto and drivers unavailable, raises helpful error
            with mock.patch.object(backend.WindowsEcBackend, "available", return_value=False), \
                 mock.patch.object(backend.WindowsWmiBackend, "available", return_value=False):
                with self.assertRaises(backend.FanBackendError) as ctx:
                    backend.detect_backend("auto")
                self.assertIn("No supported fan-control hardware driver found on Windows", str(ctx.exception))

            # When auto and WindowsEcBackend available
            mock_ec_inst = mock.MagicMock()
            with mock.patch.object(backend.WindowsEcBackend, "available", return_value=True), \
                 mock.patch.object(backend, "WindowsEcBackend", return_value=mock_ec_inst):
                res = backend.detect_backend("auto")
                self.assertEqual(res, mock_ec_inst)

            # Linux backend rejected on Windows
            with self.assertRaises(backend.FanBackendError) as ctx:
                backend.detect_backend("tuxedo_io")
            self.assertIn("Linux-only", str(ctx.exception))


class WindowsSensorTests(unittest.TestCase):
    def test_windows_sensor_scanning_wmi_and_nvidia(self):
        sample_ps = "ACPI\\ThermalZone\\TZ00_0|3252\nACPI\\ThermalZone\\TZ01_0|3182\n"
        mock_proc = mock.MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = sample_ps

        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(policy, "_scan_wmi_thermal_zones_clr", return_value=None), \
             mock.patch("subprocess.run", return_value=mock_proc), \
             mock.patch("shutil.which", return_value=None):
            records = policy.scan_sensor_records(demo=False, include_nvidia=False)
            self.assertEqual(len(records), 2)
            # 3252 raw Kelvin tenths = 325.2 K = 52.0 °C
            self.assertEqual(records[0]["temp"], 52.0)
            self.assertEqual(records[0]["name"], "acpi")
            self.assertIn("cpu", records[0]["aliases"])
            self.assertEqual(policy.pick_cpu_temp(records), 52.0)

            sensors = policy.scan_sensors(demo=False, include_nvidia=False)
            self.assertEqual(len(sensors), 2)
            self.assertEqual(sensors[0]["temp"], 52.0)

    def test_windows_sensor_scanning_clr(self):
        fake_zones = [("\\_SB.ECTZ", 65.5)]
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(policy, "_scan_wmi_thermal_zones_clr", return_value=fake_zones), \
             mock.patch("shutil.which", return_value=None):
            records = policy.scan_sensor_records(demo=False, include_nvidia=False)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["temp"], 65.5)
            self.assertEqual(records[0]["label"], "CPU (ECTZ)")
            self.assertIn("cpu", records[0]["aliases"])
            self.assertEqual(policy.pick_cpu_temp(records), 65.5)

    def test_windows_sensor_scanning_3_field_ps(self):
        sample_ps = "\\_SB.ECTZ|3462|346\n"
        mock_proc = mock.MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = sample_ps

        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(policy, "_scan_wmi_thermal_zones_clr", return_value=None), \
             mock.patch("subprocess.run", return_value=mock_proc), \
             mock.patch("shutil.which", return_value=None):
            records = policy.scan_sensor_records(demo=False, include_nvidia=False)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["temp"], 73.0)
            self.assertEqual(records[0]["label"], "CPU (ECTZ)")
            self.assertIn("cpu", records[0]["aliases"])
            self.assertEqual(policy.pick_cpu_temp(records), 73.0)


class WindowsDiagnosticsTests(unittest.TestCase):
    def test_windows_diagnostics(self):
        with mock.patch.object(sys, "platform", "win32"), \
             mock.patch.object(policy, "_scan_sensor_records_windows", return_value=[]):
            out = diagnostics.diagnose("C:\\ProgramData\\fan-control\\fan-control.json")
            self.assertIn("Fan Control Windows Diagnostics", out)
            self.assertIn("driver", out)
            self.assertIn("nvidia", out)


class WindowsGuiBridgeTests(unittest.TestCase):
    def test_bridge_http_server_and_rpc(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            dist_dir = tmp_path / "ui" / "dist"
            dist_dir.mkdir(parents=True)
            (dist_dir / "index.html").write_text("<html><head><meta http-equiv=\"Content-Security-Policy\" content=\"connect-src 'none'\"></head><body>App</body></html>")
            (dist_dir / "test.js").write_text("console.log('test');")

            mock_adapter = mock.MagicMock()
            mock_adapter.handle.return_value = {"profile": "balanced"}

            handler_cls = win_gui._make_request_handler(mock_adapter, dist_dir)
            server = win_gui.HTTPServer(("127.0.0.1", 0), handler_cls)
            port = server.server_port
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()

            import urllib.request
            try:
                # 1. GET index.html (injected script and modified CSP)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/index.html") as resp:
                    body = resp.read().decode("utf-8")
                    self.assertIn('<script src="/bridge.js"></script>', body)
                    self.assertIn("connect-src 'self'", body)

                # 2. GET bridge.js
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/bridge.js") as resp:
                    js = resp.read().decode("utf-8")
                    self.assertIn("window.__fanControl", js)

                # 3. POST /rpc
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/rpc",
                    data=json.dumps({"method": "config.get", "params": {}}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertTrue(data["ok"])
                    self.assertEqual(data["result"], {"profile": "balanced"})
                    mock_adapter.handle.assert_called_with("config.get", {})
            finally:
                server.shutdown()
                server.server_close()


class WindowsSaveDocumentTests(unittest.TestCase):
    def test_save_document_windows_no_o_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            doc_path = pathlib.Path(tmp) / "cfg.json"
            cfg, _ = policy.normalize_config(policy.default_config_v2())
            with mock.patch.object(sys, "platform", "win32"):
                policy.save_document(doc_path, cfg)
                self.assertTrue(doc_path.is_file())
                loaded = json.loads(doc_path.read_text())
                self.assertEqual(loaded["version"], 2)


if __name__ == "__main__":
    unittest.main()
