#!/usr/bin/env python3
"""Exercise the production custom-scheme bundle in WebKitGTK with demo hardware."""
import argparse
import json
import pathlib
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from fan_gtk import FanApplication, Gio, GLib

ROUTES = ("overview", "fans", "curves", "sensors", "analytics", "automation", "settings", "diagnostics")


def main():
    with tempfile.TemporaryDirectory() as directory:
        args = argparse.Namespace(demo=True, config=f"{directory}/config.json", runtime_dir=directory, debug=False)
        app = FanApplication(args)
        state = {"index": 0, "attempts": 0, "failed": False}

        def check_exports():
            root = pathlib.Path(directory)
            app._rpc.handle("curves.set", {"id": "smoke_curve", "name": "Smoke curve", "points": [[0, 0], [95, 100]], "temp_source": "cpu"})
            for method, filename, params in (
                (app._save_csv, "history.csv", None),
                (app._save_diagnostics, "diagnostics.json", None),
                (app._save_curve, "curve.json", {"id": "smoke_curve"}),
            ):
                path = root / filename
                dialog = SimpleNamespace(save_finish=lambda _result, path=path: Gio.File.new_for_path(str(path)))
                if params is None:
                    method(dialog, None)
                else:
                    method(dialog, None, params)
                assert path.stat().st_size > 0, f"Empty export: {filename}"
            diagnostics = json.loads((root / "diagnostics.json").read_text())
            assert "config_path" not in diagnostics["daemon"]
            assert "path" not in diagnostics["history"]
            curve = root / "curve.json"
            data = json.loads(curve.read_text())
            data["name"] = "Smoke imported curve"
            curve.write_text(json.dumps(data))
            app._open_curve(SimpleNamespace(open_finish=lambda _: Gio.File.new_for_path(str(curve))), None)
            curves = app._rpc.handle("curves.list", {})["curves"]
            assert any(c["name"] == "Smoke imported curve" for c in curves)
            app.window.get_clipboard().set(app._diagnostic_export())
            print("PASS native file writes, curve import, diagnostic redaction and clipboard write", flush=True)

        def finish(view, result, *_):
            try:
                data = json.loads(view.evaluate_javascript_finish(result).to_string())
                route = ROUTES[state["index"]]
                if data["heading"].lower() != route or data["scheme"] != "fancontrol:" or not data["bridge"]:
                    state["attempts"] += 1
                    if state["attempts"] > 30:
                        raise RuntimeError(f"route did not initialize: {route}: {data}")
                    return
                print(f"PASS fancontrol://app/index.html#/{route}", flush=True)
                if state["index"] == 0:
                    check_exports()
                state["index"] += 1
                state["attempts"] = 0
                if state["index"] == len(ROUTES):
                    app.quit()
                else:
                    app.webview.evaluate_javascript(f"location.hash = '/{ROUTES[state['index']]}'", -1, None, None, None, None)
            except Exception as exc:
                print(f"FAIL: {exc}", file=sys.stderr)
                state["failed"] = True
                app.quit()

        def check():
            if app.webview and state["index"] < len(ROUTES):
                app.webview.evaluate_javascript(
                    "JSON.stringify({heading: document.querySelector('h1')?.textContent || '', scheme: location.protocol, bridge: !!window.__fanControl, body: document.body.innerText.slice(0, 1000)})",
                    -1, None, None, None, finish,
                )
            return True

        GLib.timeout_add(500, check)
        app.run([])
        return 1 if state["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
