#!/usr/bin/env python3
"""Local UI acceptance harness backed by an isolated simulated controller."""
import argparse
import json
import pathlib
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import fan_controller
from fan_backend import DemoBackend
from fan_controller import FanController
from fan_policy import _record_from_value

ROOT = pathlib.Path(__file__).resolve().parents[1]
INJECT = '''<script>
const scenario = new URLSearchParams(location.search).get('state');
let frozenLive;
window.__fanControl = {
 async call(method, params = {}) {
  if (scenario === 'disconnected') throw new Error('Demo: daemon unavailable');
  if (method === 'live' && scenario === 'stale' && frozenLive) return frozenLive;
  const response = await fetch('/rpc', {method: 'POST', body: JSON.stringify({method, params})});
  const data = await response.json();
  if (data.__fan_error) throw Object.assign(new Error(data.__fan_error.message), {code: data.__fan_error.code});
  if (method === 'capabilities' && scenario === 'unsupported') data.protocol_version = 1;
  if (method === 'capabilities' && scenario === 'missing') data.features = [];
  if (method === 'live' && scenario === 'critical') {data.control_temp = 99; data.critical_active = true;}
  if (method === 'live' && scenario === 'warning') data.fault_missing = 2;
  if (method === 'snapshot' && scenario === 'light') data.config.display.theme = 'light';
  if (method === 'live' && scenario === 'stale') frozenLive = data;
  return data;
 },
 event(name, payload) { window.dispatchEvent(new CustomEvent('fan-event', {detail: {name, payload}})); }
};
if (scenario !== 'stale') setInterval(async () => {
 try { window.__fanControl.event('live', await window.__fanControl.call('live')); } catch {}
}, 500);
</script>'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=5173)
    parser.add_argument('--fixture', help='write a fixture JSON and exit')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        ctl = FanController(DemoBackend(), pathlib.Path(directory) / 'config.json', pathlib.Path(directory), demo=True)
        fan_controller.scan_sensor_records = lambda **_: [
            _record_from_value('coretemp', 'CPU package', 57.0, sensor_id='hwmon:coretemp:platform-coretemp.0:temp1', channel='temp1', source='hwmon', runtime_path='/sys/class/hwmon/hwmon1/temp1_input'),
            _record_from_value('amdgpu', 'GPU core', 72.0, sensor_id='hwmon:amdgpu:pci-0000:05:00.0:temp1', channel='temp1', source='hwmon', runtime_path='/sys/class/hwmon/hwmon2/temp1_input'),
        ]
        ctl.handle('mode', {'mode': 'curve'})
        ctl.handle('curves.set', {'id': 'cpu_default', 'name': 'CPU default', 'points': [[35, 20], [55, 40], [75, 75], [95, 100]], 'temp_source': 'cpu'})
        ctl.handle('rules.set', {'rule': {'id': 'gpu_warm', 'name': 'GPU cooling boost', 'enabled': True, 'priority': 100, 'trigger': {'type': 'temp_above', 'sensor': 'gpu', 'value': 70}, 'condition': {'sustain_ticks': 3, 'cooldown_seconds': 30}, 'action': {'type': 'set_profile', 'profile': 'performance'}}})
        for _ in range(12):
            ctl.tick_sensors()
            ctl.tick_readback()
            ctl.tick_control()
            ctl.tick_history()
        ctl.history_store.flush()
        if args.fixture:
            fixture = {method: ctl.handle(method) for method in ('capabilities', 'snapshot', 'live', 'sensors.list', 'rules.list', 'history.query', 'history.stats', 'diagnostics.snapshot', 'diagnostics.decisions')}
            pathlib.Path(args.fixture).write_text(json.dumps(fixture, indent=2))
            ctl.close()
            return

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = unquote(urlparse(self.path).path).lstrip('/') or 'index.html'
                if path == 'demo-bridge.js':
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/javascript')
                    self.end_headers()
                    self.wfile.write(INJECT.removeprefix('<script>').removesuffix('</script>').encode())
                    return
                file = (ROOT / 'ui/dist' / path).resolve()
                if not file.is_relative_to(ROOT / 'ui/dist') or not file.is_file():
                    self.send_error(404)
                    return
                data = file.read_bytes()
                if path == 'index.html':
                    data = data.replace(b'<head>', b'<head><script src="/demo-bridge.js"></script>')
                    data = data.replace(b"connect-src 'none'", b"connect-src 'self'")
                self.send_response(200)
                self.send_header('Content-Type', {'html': 'text/html', 'js': 'text/javascript', 'css': 'text/css'}.get(file.suffix[1:], 'application/octet-stream'))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                try:
                    payload = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                    if payload['method'] == 'live':
                        ctl.tick_sensors()
                        ctl.tick_readback()
                        ctl.tick_control()
                        ctl.tick_history()
                    result = ctl.handle(payload['method'], payload.get('params'))
                except Exception as exc:
                    result = {'__fan_error': {'code': getattr(exc, 'code', 'BACKEND_ERROR'), 'message': str(exc)}}
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(result, default=str).encode())

            def log_message(self, *_):
                pass

        try:
            print(f'Simulated UI: http://127.0.0.1:{args.port}', flush=True)
            HTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
        finally:
            ctl.close()


if __name__ == '__main__':
    main()
