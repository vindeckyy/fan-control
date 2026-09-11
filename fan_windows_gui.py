#!/usr/bin/env python3
"""Windows GUI application launcher for Fan Control.

Hosts the React dashboard locally on loopback, provides the native IPC bridge,
and displays the application using pywebview (Edge WebView2), standalone Edge
app mode, or the system browser.
"""

from __future__ import annotations

import argparse
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


def _make_request_handler(rpc_adapter, dist_dir: pathlib.Path):
    class FanRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Silence HTTP server logs

        def do_GET(self):
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
                # Try starting service if installed
                try:
                    subprocess.run(["net", "start", "fan-daemon"], capture_output=True, timeout=15)
                except (OSError, subprocess.SubprocessError):
                    pass
                for _ in range(10):
                    time.sleep(0.5)
                    try:
                        client.connect()
                        break
                    except (ConnectionError, FileNotFoundError):
                        continue
                else:
                    raise RuntimeError(
                        "fan-daemon is not running; start it with: net start fan-daemon or 'python fan-daemon.py'"
                    )
            except PermissionError as exc:
                raise RuntimeError(str(exc)) from exc
            self.rpc_adapter = _RpcClientAdapter(client)

        dist = ui_root()
        handler = _make_request_handler(self.rpc_adapter, dist)
        self._server = HTTPServer(("127.0.0.1", 0), handler)
        self.port = self._server.server_port
        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()

    def _start_demo_ticker(self):
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

        self._tick_thread = threading.Thread(target=_loop, name="demo-ticker", daemon=True)
        self._tick_thread.start()

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

        # 1. Try pywebview if installed (native embedded WebView2)
        try:
            import webview
            window = webview.create_window(
                APP_TITLE,
                url,
                width=1280,
                height=800,
                min_size=(720, 480),
                text_select=True,
            )
            webview.start(debug=bool(self.args.debug))
            return 0
        except ImportError:
            pass

        # 2. Try Microsoft Edge in application mode (--app)
        edge_candidates = [
            pathlib.Path("C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"),
            pathlib.Path("C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe"),
        ]
        edge_path = None
        for cand in edge_candidates:
            if cand.is_file():
                edge_path = str(cand)
                break
        if not edge_path:
            edge_path = shutil.which("msedge")

        if edge_path:
            profile_dir = pathlib.Path(tempfile.gettempdir()) / "fan-control-edge-profile"
            cmd = [
                edge_path,
                f"--app={url}",
                f"--user-data-dir={profile_dir}",
                "--window-size=1280,800",
                "--no-first-run",
                "--no-default-browser-check",
            ]
            try:
                proc = subprocess.Popen(cmd)
                proc.wait()
                return 0
            except (OSError, subprocess.SubprocessError):
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
            draw.ellipse((4, 4, 60, 60), fill=(40, 120, 240, 255))
            draw.ellipse((20, 20, 44, 44), fill=(255, 255, 255, 255))
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
        print(f"Error starting Fan Control: {exc}", file=sys.stderr)
        return 1
    finally:
        app.close()
