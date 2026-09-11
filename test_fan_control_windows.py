#!/usr/bin/env python3
"""Unit tests for Windows-specific fan control features, backends, and bridges."""

import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fan_backend as backend
import fan_diagnostics as diagnostics
import fan_policy as policy
import fan_rpc as rpc
import fan_runtime as runtime
import fan_windows_gui as win_gui
from fan_controller import FanController


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

        with mock.patch.object(backend.WindowsEcBackend, "find_driver_dll", return_value="dummy_inpoutx64.dll"), \
             mock.patch("ctypes.CDLL", return_value=mock_dll):
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
        wmi_be = backend.WindowsWmiBackend()
        self.assertEqual(wmi_be.fans(), [1, 2])
        wmi_be.lock()
        wmi_be.write_duty(1, 65)
        self.assertEqual(wmi_be.read_duty(1), 65)
        wmi_be.release()

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
             mock.patch("subprocess.run", return_value=mock_proc), \
             mock.patch("shutil.which", return_value=None):
            records = policy.scan_sensor_records(demo=False, include_nvidia=False)
            self.assertEqual(len(records), 2)
            # 3252 raw Kelvin tenths = 325.2 K = 52.0 °C
            self.assertEqual(records[0]["temp"], 52.0)
            self.assertEqual(records[0]["name"], "acpi")

            sensors = policy.scan_sensors(demo=False, include_nvidia=False)
            self.assertEqual(len(sensors), 2)
            self.assertEqual(sensors[0]["temp"], 52.0)


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
            t = mock.threading.Thread(target=server.serve_forever, daemon=True)
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
