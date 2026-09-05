"""GTK4 + WebKitGTK 6 shell. Imported only when a window or tray is needed."""

from __future__ import annotations

import json
import mimetypes
import pathlib
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("WebKit", "6.0")
gi.require_version("JavaScriptCore", "6.0")
gi.require_version("Soup", "3.0")

from gi.repository import Gio, GLib, Gtk, Soup, WebKit
from gi.repository import JavaScriptCore as JSC

from fan_backend import DemoBackend
from fan_controller import FanController
from fan_rpc import RpcClient, control_socket_path
from fan_runtime import runtime_dir

APP_ID = "org.community.FanControl"
SCHEME = "fancontrol"
INJECT = r"""
(function(){
  if (window.__fanControl) return;
  window.__fanControl = {
    async call(method, params){
      if (!(window.webkit && webkit.messageHandlers && webkit.messageHandlers.fan)) {
        throw new Error("native bridge unavailable");
      }
      const payload = {method: method, params: params || {}};
      const raw = await webkit.messageHandlers.fan.postMessage(payload);
      if (raw && typeof raw === "object" && raw.error) throw new Error(raw.error);
      return raw;
    },
    event(name, payload){
      window.dispatchEvent(new CustomEvent("fan-event", {detail: {name, payload}}));
    }
  };
})();
"""


def ui_root():
    here = pathlib.Path(__file__).resolve().parent
    candidates = [
        here / "ui" / "dist",
        pathlib.Path("/usr/local/share/fan-control/ui"),
        pathlib.Path("/usr/share/fan-control/ui"),
    ]
    for path in candidates:
        if (path / "index.html").is_file():
            return path
    return here / "ui" / "dist"


class _InProcessAdapter:
    def __init__(self, controller):
        self.controller = controller

    def handle(self, method, params=None):
        return self.controller.handle(method, params or {})

    def close(self):
        self.controller.close()


class _RpcClientAdapter:
    def __init__(self, client):
        self.client = client

    def handle(self, method, params=None):
        return self.client.call(method, params or {})

    def close(self):
        self.client.close()

class FanApplication(Gtk.Application):
    def __init__(self, args):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.args = args
        self.controller = None
        self._rpc = None
        self.webview = None
        self.window = None
        self.status_label = None
        self.stop = threading.Event()
        self.runtime = runtime_dir(demo=args.demo, override=args.runtime_dir)
        self._notified_critical = False

    def do_activate(self):
        if self.window:
            self.window.present()
            return
        try:
            self._setup_controller()
        except Exception as exc:
            self._fatal_window(str(exc))
            return
        self._build_window()
        self._start_loops()
        GLib.timeout_add(500, self._push_live)
        GLib.timeout_add(2000, self._push_history)
        self.window.present()

    def _fatal_window(self, message):
        window = Gtk.ApplicationWindow(application=self, title="Fan Control")
        window.set_default_size(720, 240)
        label = Gtk.Label(label=message)
        label.set_wrap(True)
        label.set_margin_top(24)
        label.set_margin_bottom(24)
        label.set_margin_start(24)
        label.set_margin_end(24)
        window.set_child(label)
        window.present()
        self.window = window

    def _setup_controller(self):
        config_path = pathlib.Path(self.args.config)
        if self.args.demo:
            self.controller = FanController(
                backend=DemoBackend(),
                config_path=config_path,
                runtime_dir=self.runtime,
                demo=True,
            )
            self._rpc = _InProcessAdapter(self.controller)
        else:
            sock_path = control_socket_path(self.runtime)
            client = RpcClient(sock_path)
            try:
                client.connect()
            except ConnectionError:
                subprocess.run(["systemctl", "start", "fan-daemon"], capture_output=True)
                for _ in range(20):
                    time.sleep(0.5)
                    try:
                        client.connect()
                        break
                    except (ConnectionError, FileNotFoundError):
                        continue
                else:
                    raise RuntimeError(
                        "fan-daemon is not running; start it with: systemctl start fan-daemon"
                    )
            except PermissionError as exc:
                raise RuntimeError(str(exc)) from exc
            self._rpc = _RpcClientAdapter(client)
    def _build_window(self):
        window = Gtk.ApplicationWindow(application=self, title="Fan Control")
        window.set_default_size(1100, 760)
        window.set_size_request(900, 640)
        header = Gtk.HeaderBar()
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label(label="Fan Control")
        title.add_css_class("title")
        sub_text = getattr(self.controller.backend, "name", "fan-daemon") if self.controller else "fan-daemon"
        subtitle = Gtk.Label(label=sub_text)
        titles.append(title)
        titles.append(subtitle)
        header.set_title_widget(titles)
        self.status_label = Gtk.Label(label="Connecting")
        self.status_label.add_css_class("dim-label")
        header.pack_end(self.status_label)
        window.set_titlebar(header)

        context = WebKit.WebContext.new()
        security = context.get_security_manager()
        security.register_uri_scheme_as_secure(SCHEME)
        security.register_uri_scheme_as_local(SCHEME)
        security.register_uri_scheme_as_cors_enabled(SCHEME)
        context.register_uri_scheme(SCHEME, self._serve_ui)

        ucm = WebKit.UserContentManager()
        ucm.register_script_message_handler_with_reply("fan", None)
        ucm.connect("script-message-with-reply-received", self._on_message)
        script = WebKit.UserScript.new(
            INJECT,
            WebKit.UserContentInjectedFrames.ALL_FRAMES,
            WebKit.UserScriptInjectionTime.START,
            None,
            None,
        )
        ucm.add_script(script)

        settings = WebKit.Settings()
        settings.set_enable_javascript(True)
        settings.set_enable_developer_extras(bool(self.args.debug))
        settings.set_allow_file_access_from_file_urls(False)
        settings.set_allow_universal_access_from_file_urls(False)
        settings.set_javascript_can_access_clipboard(False)
        try:
            settings.set_hardware_acceleration_policy(WebKit.HardwareAccelerationPolicy.ALWAYS)
        except (AttributeError, GLib.Error):
            pass

        webview = WebKit.WebView(web_context=context, user_content_manager=ucm, settings=settings)
        webview.connect("load-failed", self._on_load_failed)
        if self.args.debug:
            webview.get_inspector().show()
        webview.load_uri(f"{SCHEME}://app/index.html")
        window.set_child(webview)
        window.connect("close-request", self._on_close)
        self.window = window
        self.webview = webview

    def _serve_bytes(self, request, data, mime, status=200):
        # ES modules always fetch with CORS. finish() sends no ACAO header, so
        # WebKit discards JS/CSS and the page stays blank.
        stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(data))
        response = WebKit.URISchemeResponse.new(stream, len(data))
        response.set_status(status, "OK" if status == 200 else "Error")
        response.set_content_type(mime)
        headers = Soup.MessageHeaders.new(Soup.MessageHeadersType.RESPONSE)
        headers.append("Access-Control-Allow-Origin", "*")
        headers.append("Cache-Control", "no-store")
        response.set_http_headers(headers)
        request.finish_with_response(response)

    def _serve_ui(self, request):
        root = ui_root().resolve()
        parsed = urllib.parse.urlparse(request.get_uri())
        rel = urllib.parse.unquote(parsed.path or "/").lstrip("/") or "index.html"
        if rel.endswith("/"):
            rel += "index.html"
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            request.finish_error(GLib.Error.new_literal(Gio.io_error_quark(), "forbidden", Gio.IOErrorEnum.PERMISSION_DENIED))
            return
        if not candidate.is_file():
            html = (
                b"<!doctype html><meta charset=utf-8><title>Fan Control</title>"
                b"<body style='font:14px sans-serif;padding:2rem'>"
                b"<h1>UI not built</h1><p>Run <code>npm ci && npm run build</code> in <code>ui/</code>.</p>"
            )
            self._serve_bytes(request, html, "text/html")
            return
        data = candidate.read_bytes()
        mime = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if candidate.name.endswith(".js"):
            mime = "text/javascript"
        self._serve_bytes(request, data, mime)

    def _on_message(self, _ucm, js_value, reply):
        try:
            payload = json.loads(js_value.to_json(0))
            if not isinstance(payload, dict):
                raise ValueError("message must be an object")
            method = payload.get("method")
            params = payload.get("params") or {}
            if method in ("history.pick_export", "curves.pick_export", "curves.pick_import"):
                GLib.idle_add(self._file_op, method, params)
                ctx = js_value.get_context()
                reply.return_value(JSC.Value.new_from_json(ctx, json.dumps({"ok": True, "async": True})))
                return
            result = self._rpc.handle(method, params)
            ctx = js_value.get_context()
            reply.return_value(JSC.Value.new_from_json(ctx, json.dumps(result, default=str)))
            if method not in ("snapshot", "history", "history.export", "diagnose", "curves.list", "curves.export"):
                GLib.idle_add(self._push_full)
        except Exception as exc:
            reply.return_error_message(str(exc))
            if self.args.debug:
                traceback.print_exc()

    def _file_op(self, method, params):
        dialog = Gtk.FileDialog()
        if method == "history.pick_export":
            dialog.set_initial_name("fan-history.csv")
            dialog.save(self.window, None, lambda d, res: self._save_csv(d, res))
        elif method == "curves.pick_export":
            dialog.set_initial_name(f"{params.get('name', 'curve')}.json")
            dialog.save(self.window, None, lambda d, res: self._save_curve(d, res, params))
        else:
            dialog.open(self.window, None, lambda d, res: self._open_curve(d, res))
        return False

    def _save_csv(self, dialog, result):
        try:
            file = dialog.save_finish(result)
            payload = self._rpc.handle("history.export", {})["csv"]
            file.replace_contents(payload.encode(), None, False, Gio.FileCreateFlags.NONE, None)
            self._eval_event("toast", {"message": "History exported"})
        except GLib.Error:
            pass
        except Exception as exc:
            self._eval_event("toast", {"message": str(exc)})

    def _save_curve(self, dialog, result, params):
        try:
            file = dialog.save_finish(result)
            exported = self._rpc.handle("curves.export", params)
            file.replace_contents(json.dumps(exported, indent=2).encode(), None, False, Gio.FileCreateFlags.NONE, None)
            self._eval_event("toast", {"message": "Curve exported"})
        except GLib.Error:
            pass
        except Exception as exc:
            self._eval_event("toast", {"message": str(exc)})

    def _open_curve(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            _ok, contents, _etag = file.load_contents()
            data = json.loads(contents)
            name = data.get("name") or pathlib.Path(file.get_basename()).stem
            self._rpc.handle("curves.save", {"name": name, "curve": data.get("curve")})
            self._eval_event("toast", {"message": f"Imported {name}"})
            self._push_full()
        except GLib.Error:
            pass
        except Exception as exc:
            self._eval_event("toast", {"message": str(exc)})

    def _eval_event(self, name, payload):
        if not self.webview:
            return
        blob = json.dumps(payload, default=str, separators=(",", ":"))
        if name == "live" and blob == getattr(self, "_last_live", None):
            return
        if name == "live":
            self._last_live = blob
        script = "window.__fanControl&&window.__fanControl.event(" + json.dumps(name) + "," + blob + ")"
        self.webview.evaluate_javascript(script, -1, None, None, None, None)

    def _push_live(self):
        return self._push_snapshot(False)

    def _push_full(self):
        self._push_snapshot(True)
        return False

    def _push_snapshot(self, full=False):
        if self.stop.is_set() or not self.webview:
            return False
        try:
            snap = self._rpc.handle("snapshot" if full else "live", {})
        except Exception:
            if self.status_label and self.status_label.get_text() != "Daemon unreachable":
                self.status_label.set_text("Daemon unreachable")
            return True
        self._eval_event("snapshot" if full else "live", snap)
        self._update_header(snap)
        self._maybe_notify(snap)
        return True

    def _push_history(self):
        if self.stop.is_set() or not self.webview:
            return False
        try:
            payload = self._rpc.handle("history", {})
        except Exception:
            return True
        hist = payload.get("history") or []
        if not getattr(self, "_history_seeded", False):
            self._history_seeded = True
            self._eval_event("history", payload)
        elif hist:
            self._eval_event("history-append", hist[-1])
        return True

    def _update_header_status(self, text):
        if self.status_label and self.status_label.get_text() != text:
            self.status_label.set_text(text)

    def _update_header(self, snap=None):
        if self.stop.is_set() or not self.status_label:
            return False
        if snap is None:
            try:
                snap = self._rpc.handle("live", {})
            except Exception:
                if self.status_label.get_text() != "Daemon unreachable":
                    self.status_label.set_text("Daemon unreachable")
                return True
        age = time.time() - (snap.get("updated") or 0)
        if age < 3:
            text = "Live"
        elif snap.get("fault_missing", 0) >= 3:
            text = "Fault · firmware auto"
        else:
            text = "Readback delayed"
        if snap.get("critical_active"):
            text = "Critical cooling"
        if snap.get("demo"):
            text = f"Demo · {text}"
        if self.status_label.get_text() != text:
            self.status_label.set_text(text)
        return True

    def _maybe_notify(self, snap):
        temp = snap.get("control_temp")
        critical = snap.get("critical_temp", 95)
        enabled = (snap.get("alerts") or {}).get("desktop")
        if temp is not None and temp >= critical and enabled and not self._notified_critical:
            self._notified_critical = True
            notification = Gio.Notification.new("Fan Control")
            notification.set_body(f"Critical control temperature: {temp:.1f}°C. Maximum cooling engaged.")
            self.send_notification("critical-temp", notification)
        if temp is not None and temp < critical - 5:
            self._notified_critical = False

    def _start_loops(self):
        if not self.controller:
            return
        def loop(fn, interval):
            while not self.stop.wait(interval):
                try:
                    fn()
                except Exception:
                    traceback.print_exc()

        threading.Thread(target=loop, args=(self.controller.tick_sensors, 2), daemon=True).start()
        threading.Thread(target=loop, args=(self.controller.tick_readback, 1), daemon=True).start()
        threading.Thread(target=loop, args=(self.controller.tick_control, 0.25), daemon=True).start()
        threading.Thread(target=loop, args=(self.controller.tick_history, 2), daemon=True).start()
        try:
            self.controller.tick_sensors()
            self.controller.tick_readback()
            self.controller.tick_control()
            self.controller.tick_history()
        except Exception:
            traceback.print_exc()
    def _on_load_failed(self, _view, _event, uri, error):
        print(f"WebKit load failed: {uri}: {error}", flush=True)
        return False

    def _on_close(self, *_):
        self._teardown()
        return False

    def _teardown(self):
        if self.stop.is_set():
            return
        self.stop.set()
        if self.controller:
            self.controller.close()
        if self._rpc:
            self._rpc.close()
    def do_shutdown(self):
        self._teardown()
        Gtk.Application.do_shutdown(self)


class TrayApplication(Gtk.Application):
    """Display-only tray: never locks the EC. Profile changes SIGHUP the daemon."""

    INTROSPECT = """
    <node>
      <interface name="org.kde.StatusNotifierItem">
        <property name="Category" type="s" access="read"/>
        <property name="Id" type="s" access="read"/>
        <property name="Title" type="s" access="read"/>
        <property name="Status" type="s" access="read"/>
        <property name="WindowId" type="i" access="read"/>
        <property name="IconName" type="s" access="read"/>
        <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
        <property name="ItemIsMenu" type="b" access="read"/>
        <method name="ContextMenu"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
        <method name="Activate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
        <method name="SecondaryActivate"><arg type="i" name="x" direction="in"/><arg type="i" name="y" direction="in"/></method>
        <method name="Scroll"><arg type="i" name="delta" direction="in"/><arg type="s" name="orientation" direction="in"/></method>
      </interface>
    </node>
    """

    def __init__(self, args):
        super().__init__(application_id=APP_ID + ".Tray", flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.args = args
        self.runtime = runtime_dir(demo=False, override=args.runtime_dir)
        self.config_path = pathlib.Path(args.config)
        self.popover = None
        self._bus = None
        self._registration = None

    def do_activate(self):
        if self.popover:
            return
        self.hold()
        self._build_menu()
        self._export_sni()

    def _build_menu(self):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)
        for profile in ("silent", "balanced", "performance", "custom"):
            button = Gtk.Button(label=profile.title())
            button.connect("clicked", lambda _b, name=profile: self._set_profile(name))
            box.append(button)
        open_btn = Gtk.Button(label="Open dashboard")
        open_btn.connect("clicked", lambda *_: self._open_gui())
        quit_btn = Gtk.Button(label="Quit tray")
        quit_btn.connect("clicked", lambda *_: self.quit())
        box.append(open_btn)
        box.append(quit_btn)
        popover.set_child(box)
        dummy = Gtk.Window()
        dummy.set_decorated(False)
        dummy.set_default_size(1, 1)
        dummy.set_child(Gtk.Box())
        dummy.set_visible(False)
        popover.set_parent(dummy)
        self.popover = popover
        self._dummy = dummy

    def _set_profile(self, name):
        try:
            with RpcClient(control_socket_path(self.runtime)) as client:
                client.call("profile", {"profile": name})
        except Exception:
            notification = Gio.Notification.new("Fan Control")
            notification.set_body("fan-daemon unreachable")
            self.send_notification("error", notification)
        self.popover.popdown()

    def _open_gui(self):
        extra = []
        if self.args.demo:
            extra.append("--demo")
        extra.extend(["--config", str(self.args.config)])
        subprocess.Popen([sys.executable, sys.argv[0], *extra], start_new_session=True)
        self.popover.popdown()

    def _export_sni(self):
        Gio.bus_get(Gio.BusType.SESSION, None, self._on_bus)

    def _on_bus(self, _src, result):
        try:
            bus = Gio.bus_get_finish(result)
        except GLib.Error:
            return
        self._bus = bus
        node = Gio.DBusNodeInfo.new_for_xml(self.INTROSPECT)
        self._registration = bus.register_object(
            "/StatusNotifierItem",
            node.interfaces[0],
            self._on_method,
            self._on_get_property,
            None,
        )
        bus.call(
            "org.kde.StatusNotifierWatcher",
            "/StatusNotifierWatcher",
            "org.kde.StatusNotifierWatcher",
            "RegisterStatusNotifierItem",
            GLib.Variant("(s)", (bus.get_unique_name(),)),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
            None,
        )

    def _on_method(self, _conn, _sender, _path, _iface, method, params, invocation):
        x = y = 0
        if params.n_children() >= 2:
            x, y = params[0], params[1]
        if method in ("ContextMenu", "Activate", "SecondaryActivate"):
            GLib.idle_add(self._popup, x, y)
        invocation.return_value(None)

    def _on_get_property(self, _conn, _sender, _path, _iface, name):
        values = {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", "fan-control"),
            "Title": GLib.Variant("s", "Fan Control"),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", "fan-control"),
            "ItemIsMenu": GLib.Variant("b", False),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("fan-control", [], "Fan Control", "Display-only tray")),
        }
        return values.get(name)

    def _popup(self, x, y):
        self.popover.popup()
        return False

    def do_shutdown(self):
        if self._bus and self._registration:
            self._bus.unregister_object(self._registration)
        Gtk.Application.do_shutdown(self)


def run_application(args):
    app = TrayApplication(args) if args.tray else FanApplication(args)
    return app.run([])
