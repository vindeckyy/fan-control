#!/usr/bin/env python3
"""Windows GUI application launcher for Fan Control.

Hosts the React dashboard locally on loopback, provides the native IPC bridge,
and displays the application using pywebview (Edge WebView2), standalone Edge
app mode, or the system browser.
"""

from __future__ import annotations

import json
import mimetypes
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from fan_backend import DemoBackend
from fan_controller import FanController
from fan_rpc import RpcClient, RpcError, control_socket_path
from fan_runtime import runtime_dir

APP_TITLE = "Fan Control"

BRIDGE_JS = """\
(function(){
  if (window.__fanControl) return;
  window.__fanControl = {
    async call(method, params = {}) {
      const response = await fetch('/rpc', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({method, params})
      });
      const data = await response.json();
      if (data && typeof data === 'object' && data.__fan_error) {
        const err = new Error(data.__fan_error.message || 'daemon error');
        err.code = data.__fan_error.code || 'UNKNOWN';
        throw err;
      }
      if (data && typeof data === 'object' && data.error) {
        const msg = typeof data.error === 'object' ? data.error.message : data.error;
        throw new Error(msg || 'daemon error');
      }
      return (data && data.result !== undefined) ? data.result : data;
    },
    event(name, payload) {
      window.dispatchEvent(new CustomEvent('fan-event', {detail: {name, payload}}));
    }
  };

  let active = true;
  window.addEventListener('beforeunload', () => { active = false; });

  async function pollLive() {
    if (!active) return;
    try {
      const res = await window.__fanControl.call('live');
      window.__fanControl.event('live', res);
    } catch (_) {}
    setTimeout(pollLive, 500);
  }

  async function pollHistory() {
    if (!active) return;
    try {
      const res = await window.__fanControl.call('history.query', {});
      window.__fanControl.event('history', res);
    } catch (_) {}
    setTimeout(pollHistory, 2000);
  }

  setTimeout(pollLive, 250);
  setTimeout(pollHistory, 600);
})();
"""


def ui_root() -> pathlib.Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        meipass = pathlib.Path(sys._MEIPASS)
        candidates = [
            meipass / "ui" / "dist",
            meipass / "ui",
            meipass / "dist",
        ]
        for path in candidates:
            if (path / "index.html").is_file():
                return path
    here = pathlib.Path(__file__).resolve().parent
    exe_dir = pathlib.Path(sys.executable).resolve().parent
    candidates = [
        here / "ui" / "dist",
        exe_dir / "ui" / "dist",
        exe_dir / "ui",
        pathlib.Path("C:\\Program Files\\fan-control\\ui"),
        pathlib.Path("C:\\Program Files (x86)\\fan-control\\ui"),
    ]
    for path in candidates:
        if (path / "index.html").is_file():
            return path
    return here / "ui" / "dist"


class _InProcessAdapter:
    def __init__(self, controller: FanController):
        self.controller = controller

    def handle(self, method: str, params: dict | None = None):
        return self.controller.handle(method, params or {})

    def close(self):
        self.controller.close()


class _RpcClientAdapter:
    def __init__(self, client: RpcClient):
        self.client = client

    def handle(self, method: str, params: dict | None = None):
        return self.client.call(method, params or {})

    def close(self):
        self.client.close()


def _make_request_handler(rpc_adapter, dist_dir: pathlib.Path, tracker: dict | None = None):
    class FanRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Silence HTTP server logs

        def do_GET(self):
            if tracker is not None:
                tracker["last_seen"] = time.time()
                tracker["connected"] = True
            parsed = urllib.parse.urlparse(self.path)
            req_path = urllib.parse.unquote(parsed.path or "/").lstrip("/") or "index.html"
            if req_path.endswith("/"):
                req_path += "index.html"

            if req_path == "bridge.js":
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(BRIDGE_JS.encode("utf-8"))
                return

            file_path = (dist_dir / req_path).resolve()
            if not file_path.is_file() or not str(file_path).startswith(str(dist_dir.resolve())):
                # Fall back to index.html for SPA hash/history routes
                file_path = dist_dir / "index.html"
                if not file_path.is_file():
                    self.send_error(404, "File not found")
                    return

            try:
                content = file_path.read_bytes()
            except OSError:
                self.send_error(500, "Read error")
                return

            mime_type, _ = mimetypes.guess_type(str(file_path))
            if not mime_type:
                mime_type = "application/octet-stream"

            if file_path.name == "index.html":
                # Inject bridge script and loosen CSP to allow localhost fetch
                content = content.replace(b"<head>", b'<head><script src="/bridge.js"></script>')
                content = content.replace(b"connect-src 'none'", b"connect-src 'self'")
                mime_type = "text/html; charset=utf-8"

            self.send_response(200)
            self.send_header("Content-Type", mime_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            if tracker is not None:
                tracker["last_seen"] = time.time()
                tracker["connected"] = True
            if self.path != "/rpc":
                self.send_error(404, "Not found")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8"))
                method = payload.get("method", "")
                params = payload.get("params") or {}
                result = rpc_adapter.handle(method, params)
                response = {"ok": True, "result": result}
            except RpcError as exc:
                response = {"ok": False, "__fan_error": {"code": exc.code, "message": str(exc)}}
            except Exception as exc:
                response = {"ok": False, "__fan_error": {"code": "INTERNAL", "message": str(exc)}}

            data = json.dumps(response, default=str).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

    return FanRequestHandler


class WindowsFanApp:
    def __init__(self, args):
        self.args = args
        self.runtime = runtime_dir(demo=args.demo, override=args.runtime_dir)
        self.controller = None
        self.rpc_adapter = None
        self._server = None
        self._server_thread = None
        self._tick_thread = None
        self._stop_event = threading.Event()
        self.port = 0
        self.tracker = {"last_seen": time.time(), "connected": False}

    def setup(self):
        config_path = pathlib.Path(self.args.config)
        if self.args.demo:
            self.controller = FanController(
                backend=DemoBackend(),
                config_path=config_path,
                runtime_dir=self.runtime,
                demo=True,
            )
            self.rpc_adapter = _InProcessAdapter(self.controller)
            self._start_demo_ticker()
        else:
            sock_path = control_socket_path(self.runtime)
            client = RpcClient(sock_path)
            try:
                client.connect()
            except (ConnectionError, FileNotFoundError):
                # 1. Try starting scheduled task or Windows service if registered
                try:
                    subprocess.run(["schtasks", "/run", "/tn", "FanControlDaemon"], capture_output=True, timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass
                try:
                    subprocess.run(["net", "start", "fan-daemon"], capture_output=True, timeout=5)
                except (OSError, subprocess.SubprocessError):
                    pass

                # 2. Try launching background daemon process if available
                app_dir = pathlib.Path(__file__).resolve().parent
                daemon_candidates = [
                    app_dir / "fan-daemon.exe",
                    pathlib.Path(sys.executable).resolve().parent / "fan-daemon.exe",
                    app_dir / "fan-daemon.py",
                ]
                for cand in daemon_candidates:
                    if cand.is_file():
                        try:
                            cflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
                            if cand.suffix == ".py":
                                subprocess.Popen([sys.executable, str(cand)], creationflags=cflags)
                            else:
                                subprocess.Popen([str(cand)], creationflags=cflags)
                            break
                        except Exception:
                            pass

                for _ in range(12):
                    time.sleep(0.5)
                    try:
                        client.connect()
                        break
                    except (ConnectionError, FileNotFoundError):
                        continue
                else:
                    # 3. Fall back to in-process controller with detected hardware backend
                    try:
                        from fan_backend import detect_backend
                        backend = detect_backend(getattr(self.args, "backend", "auto"))
                        self.controller = FanController(
                            backend=backend,
                            config_path=config_path,
                            runtime_dir=self.runtime,
                            demo=False,
                        )
                        self.rpc_adapter = _InProcessAdapter(self.controller)
                        self._start_ticker("inprocess-ticker")
                    except Exception as exc:
                        raise RuntimeError(
                            f"fan-daemon is not running and in-process controller could not start: {exc}\n"
                            "Fan control requires Administrator access. Start the background service "
                            "(PowerShell as Administrator):\n"
                            r"  powershell -ExecutionPolicy Bypass -File scripts\install-service-windows.ps1"
                            "\n"
                            "or launch fan-daemon.exe from an Administrator terminal."
                        ) from exc
            except PermissionError as exc:
                raise RuntimeError(str(exc)) from exc
            if self.rpc_adapter is None:
                self.rpc_adapter = _RpcClientAdapter(client)

        dist = ui_root()
        handler = _make_request_handler(self.rpc_adapter, dist, self.tracker)
        self._server = HTTPServer(("127.0.0.1", 0), handler)
        self.port = self._server.server_port
        print(f"Serving UI at http://127.0.0.1:{self.port}/index.html", flush=True)
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()

    def _start_ticker(self, name="ticker"):
        def _loop():
            while not self._stop_event.is_set():
                try:
                    self.controller.tick_sensors()
                    self.controller.tick_readback()
                    self.controller.tick_control()
                    self.controller.tick_history()
                except Exception:
                    pass
                self._stop_event.wait(0.5)

        self._tick_thread = threading.Thread(target=_loop, name=name, daemon=True)
        self._tick_thread.start()

    def _start_demo_ticker(self):
        self._start_ticker("demo-ticker")

    def close(self):
        self._stop_event.set()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self.rpc_adapter is not None:
            try:
                self.rpc_adapter.close()
            except Exception:
                pass
            self.rpc_adapter = None

    def run_window(self):
        url = f"http://127.0.0.1:{self.port}/index.html"

        # 1. Try Microsoft Edge or Chrome in standalone application mode (--app)
        # Dedicated window without browser tabs/toolbars, isolated profile, exactly like native desktop app
        browser_candidates = [
            pathlib.Path("C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"),
            pathlib.Path("C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"),
            pathlib.Path("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"),
            pathlib.Path("C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"),
        ]
        app_browser = None
        for cand in browser_candidates:
            if cand.is_file():
                app_browser = str(cand)
                break
        if not app_browser:
            app_browser = shutil.which("msedge") or shutil.which("chrome")

        if app_browser:
            profile_dir = pathlib.Path(tempfile.gettempdir()) / f"fan-control-app-profile-{os.getpid()}"
            cmd = [
                app_browser,
                f"--app={url}",
                f"--user-data-dir={profile_dir}",
                "--window-size=1280,800",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            try:
                proc = subprocess.Popen(cmd)
                # If Edge runs as a dedicated process, proc.poll() is None until closed.
                # If Edge delegates to an existing instance, proc exits immediately but tracker detects active UI.
                grace_period_end = time.time() + 15.0
                while not self._stop_event.is_set():
                    time.sleep(0.5)
                    if proc.poll() is None:
                        continue
                    # Process exited. If a client connected and was active, wait until window closed (no requests for 3s).
                    if self.tracker.get("connected"):
                        idle = time.time() - self.tracker.get("last_seen", 0)
                        if idle > 3.0:
                            break
                    elif time.time() > grace_period_end:
                        # Process exited and no UI connected within 15 seconds
                        break
                return 0
            except (OSError, subprocess.SubprocessError):
                pass
            finally:
                try:
                    shutil.rmtree(profile_dir, ignore_errors=True)
                except Exception:
                    pass

        # 2. Try pywebview if installed (native embedded WebView2)
        try:
            import webview
            webview.create_window(
                APP_TITLE,
                url,
                width=1280,
                height=800,
                min_size=(720, 480),
                text_select=True,
            )
            webview.start(debug=bool(self.args.debug))
            return 0
        except (ImportError, Exception):
            pass

        # 3. Fallback: open default web browser
        import webbrowser
        print(f"Opening Fan Control dashboard at {url}")
        webbrowser.open(url)
        try:
            while not self._stop_event.is_set():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    def run_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            print("pystray or Pillow not installed. Running background monitor in console.")
            try:
                while not self._stop_event.is_set():
                    time.sleep(2)
            except KeyboardInterrupt:
                pass
            return 0

        def create_icon_image():
            img = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
            draw = ImageDraw.Draw(img)
            draw.ellipse((8, 8, 56, 56), fill=(40, 160, 240, 255), outline=(255, 255, 255, 255), width=2)
            draw.line((32, 12, 32, 52), fill=(255, 255, 255, 255), width=3)
            draw.line((12, 32, 52, 32), fill=(255, 255, 255, 255), width=3)
            return img

        def set_profile(profile_name):
            def _action():
                try:
                    self.rpc_adapter.handle("profile", {"profile": profile_name})
                except Exception as exc:
                    print(f"Tray set profile failed: {exc}", file=sys.stderr)
            return _action

        def set_mode(mode_name):
            def _action():
                try:
                    self.rpc_adapter.handle("mode", {"mode": mode_name})
                except Exception as exc:
                    print(f"Tray set mode failed: {exc}", file=sys.stderr)
            return _action

        def open_dashboard():
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{self.port}/index.html")

        def exit_tray(icon):
            icon.stop()
            self._stop_event.set()

        menu = pystray.Menu(
            pystray.MenuItem("Open Dashboard", lambda: open_dashboard()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Profiles", pystray.Menu(
                pystray.MenuItem("Silent", set_profile("silent")),
                pystray.MenuItem("Balanced", set_profile("balanced")),
                pystray.MenuItem("Performance", set_profile("performance")),
            )),
            pystray.MenuItem("Modes", pystray.Menu(
                pystray.MenuItem("Curve (Auto)", set_mode("curve")),
                pystray.MenuItem("Manual", set_mode("manual")),
                pystray.MenuItem("Released (Firmware)", set_mode("released")),
            )),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", exit_tray),
        )

        icon = pystray.Icon("fan-control", create_icon_image(), APP_TITLE, menu)
        icon.run()
        return 0


def run_application(args) -> int:
    app = WindowsFanApp(args)
    try:
        app.setup()
        if args.tray:
            return app.run_tray()
        return app.run_window()
    except Exception as exc:
        import traceback
        traceback.print_exc()
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, f"Error starting Fan Control:\n\n{exc}", "Fan Control Error", 0x10)
        except Exception:
            pass
        return 1
    finally:
        app.close()
