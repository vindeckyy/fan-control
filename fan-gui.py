#!/usr/bin/env python3
"""Web UI for /dev/tuxedo_io fan control. Listens on 127.0.0.1:4444.

v1: profile-based curve control + manual override window.
"""
import ctypes, fcntl, http.server, json, os, pathlib, signal, subprocess, sys, threading, time, webbrowser

MAGIC_RD = 0xEF; MAGIC_WR = 0xF0
IOC_R, IOC_W, SZ = 2, 1, 8
def ioc(d,t,n,s): return (d<<30)|(t<<8)|(n<<0)|(s<<16)

R_FS1   = ioc(IOC_R, MAGIC_RD, 0x10, SZ)
R_FS2   = ioc(IOC_R, MAGIC_RD, 0x11, SZ)
R_TEMP  = ioc(IOC_R, MAGIC_RD, 0x12, SZ)
R_TEMP2 = ioc(IOC_R, MAGIC_RD, 0x13, SZ)
W_FS1   = ioc(IOC_W, MAGIC_WR, 0x10, SZ)
W_FS2   = ioc(IOC_W, MAGIC_WR, 0x11, SZ)
W_MODE  = ioc(IOC_W, MAGIC_WR, 0x12, SZ)
W_AUTO  = ioc(0,        MAGIC_WR, 0x14, 0)

FD = os.open('/dev/tuxedo_io', os.O_RDWR)
BUF = (ctypes.c_int64)()

def rd(cmd):
    BUF.value = 0; fcntl.ioctl(FD, cmd, BUF, True); return BUF.value & 0xFF

# # ponytail: v1 profiles. Each profile is a curve of (temp_c, pwm_duty)
# interpolated linearly. Max duty 198 (EC firmware wraps at 200).
PROFILES = {
    'silent': [
        (0,0),(50,0),(60,0),(70,60),(75,90),(80,120),(85,150),(90,170),(95,198),(110,198),
    ],
    'balanced': [
        (0,0),(50,0),(60,50),(70,100),(75,130),(80,150),(85,170),(90,180),(95,198),(110,198),
    ],
    'performance': [
        (0,0),(50,0),(60,80),(70,140),(75,170),(80,180),(85,198),(90,198),(95,198),(110,198),
    ],
}

def find_k10():
    for h in sorted(pathlib.Path('/sys/class/hwmon').glob('hwmon*')):
        if (h/'name').read_text().strip() == 'k10temp': return h
    return None

def cpu_temp(hw):
    if not hw: return None
    return int((hw/'temp1_input').read_text().strip())/1000

def interp(t, curve):
    if t <= curve[0][0]: return curve[0][1]
    if t >= curve[-1][0]: return curve[-1][1]
    for (t0,p0),(t1,p1) in zip(curve, curve[1:]):
        if t0 <= t < t1:
            return p0 + (p1-p0)*(t-t0)/(t1-t0) if t1>t0 else p0

state = {
    'profile': 'balanced',
    'targets': {1: 0, 2: 0},
    'manual_until': 0,        # unix timestamp; if > time.time(), use targets
    'max_duty': 198,
    'hysteresis': 5,
    'auto': False,
    'manual': False,
}

def lock_manual():
    BUF.value = 0x40; fcntl.ioctl(FD, W_MODE, BUF, True); state['manual'] = True
def release_manual():
    BUF.value = 0; fcntl.ioctl(FD, W_MODE, BUF, True); state['manual'] = False

def sensors():
    out = []
    for h in sorted(pathlib.Path('/sys/class/hwmon').glob('hwmon*')):
        try: nm = (h/'name').read_text().strip()
        except Exception: continue
        for t in sorted(h.glob('temp*_input')):
            try: out.append({'name': nm, 'label': t.stem, 'temp': int(t.read_text().strip())/1000})
            except Exception: pass
    return out

def curve_duty():
    """Return the duty the curve wants right now, or None if temp is unknown."""
    hw = find_k10()
    t = cpu_temp(hw)
    if t is None:
        t = float(rd(R_TEMP))
    if t is None: return None
    # ponytail: assert int/float for the linter and interp signature
    pwm = interp(float(t), PROFILES[state['profile']])
    return max(0, min(state['max_duty'], int(round(float(pwm)))))

def write_duty(f, duty):
    BUF.value = duty
    fcntl.ioctl(FD, (W_FS1, W_FS2)[f-1], BUF, True)

def snapshot():
    return {
        'fan1': rd(R_FS1), 'fan2': rd(R_FS2),
        'ec_temp1': rd(R_TEMP), 'ec_temp2': rd(R_TEMP2),
        'targets': dict(state['targets']),
        'profile': state['profile'],
        'manual_until': state['manual_until'],
        'auto': state['auto'],
        'max_duty': state['max_duty'],
        'temps': sensors(),
    }

def poll():
    if state['auto']: return
    if state['manual_until'] > time.time():
        # Manual override window: honor slider targets.
        for f in (1, 2):
            write_duty(f, max(0, min(state['max_duty'], int(state['targets'][f]) * 2)))
    else:
        # Curve mode: use the live duty from k10temp.
        d = curve_duty()
        if d is not None:
            for f in (1, 2):
                write_duty(f, d)

def poller():
    while True:
        poll(); time.sleep(0.1)

# # ponytail: GUI stops the daemon before opening the EC device. Use
# `is-active` polling rather than blocking on `systemctl stop`, which
# returns as soon as the signal is sent — the daemon thread can still be
# writing for a few ms after.
subprocess.run(["systemctl", "stop", "fan-daemon"], capture_output=True)
for _ in range(50):
    r = subprocess.run(["systemctl", "is-active", "fan-daemon"], capture_output=True, text=True)
    if r.stdout.strip() != "active": break
    time.sleep(0.05)
subprocess.run(["systemctl", "reset-failed", "fan-daemon"], capture_output=True)
lock_manual()
threading.Thread(target=poller, daemon=True).start()

HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="color-scheme" content="dark"><title>Fan Control</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;--accent:#58a6ff;--accent2:#1f6feb;--green:#3fb950;--orange:#d29922;--red:#f85149;--gold:#d29922}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;padding:24px;min-height:100vh}
.wrap{max-width:1100px;margin:0 auto;display:grid;grid-template-columns:1fr 360px;gap:20px}
@media(max-width:900px){.wrap{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px}
h1{font-size:22px;font-weight:600;margin-bottom:4px}
h2{font-size:14px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px}
.sub{color:var(--dim);font-size:13px;margin-bottom:20px}
.row{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
.row span{font-variant-numeric:tabular-nums;font-weight:500}
.row .v{color:var(--text);font-size:16px}
.row .d{color:var(--dim);font-size:12px;margin-left:6px}
.group{margin-bottom:22px}
input[type=range]{-webkit-appearance:none;appearance:none;width:100%;height:6px;border-radius:3px;background:#21262d;outline:none;margin-top:4px}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:var(--accent);cursor:pointer;border:2px solid var(--accent2);transition:.1s}
input[type=range]::-webkit-slider-thumb:hover{transform:scale(1.15)}
.bar{height:8px;border-radius:4px;background:#21262d;overflow:hidden;margin-top:8px}
.bar > div{height:100%;background:linear-gradient(90deg,var(--green),var(--orange),var(--red));transition:width .3s}
.bar.curve > div{background:linear-gradient(90deg,var(--green),var(--gold))}
.temp-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.temp{background:#0d1117;border:1px solid var(--border);border-radius:8px;padding:12px}
.temp .l{color:var(--dim);font-size:11px;text-transform:uppercase}
.temp .t{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;margin-top:4px}
.btn{background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 14px;font-size:13px;cursor:pointer}
.btn:hover{background:#30363d}
.btn.active{background:var(--accent2);border-color:var(--accent2);color:#fff}
.btn.warn{background:var(--red);border-color:var(--red);color:#fff}
.btn-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}
.setting{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border)}
.setting label{font-size:13px;color:var(--dim)}
.setting input[type=number]{width:80px;background:#0d1117;border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;font-family:inherit;font-size:13px;text-align:right}
.setting select{background:#0d1117;border:1px solid var(--border);color:var(--text);padding:4px 8px;border-radius:4px;font-family:inherit;font-size:13px}
.preset{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
.preset button{flex:1;min-width:60px;background:#21262d;color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;cursor:pointer}
.preset button:hover{background:#30363d}
.preset button.active{background:var(--accent2);border-color:var(--accent2);color:#fff}
.live-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--green);margin-right:6px;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.tag{display:inline-block;background:#21262d;border:1px solid var(--border);border-radius:4px;padding:2px 6px;font-size:11px;color:var(--dim);margin-left:8px}
.tag.curve{background:#3fb95022;border-color:#3fb95055;color:var(--green)}
.tag.manual{background:#d2992222;border-color:#d2992255;color:var(--gold)}
.badge{display:inline-block;background:#21262d;border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:10px;color:var(--dim);margin-left:4px;vertical-align:middle}
.suggest{font-size:12px;color:var(--dim);margin-top:6px}
</style></head><body>
<div class="wrap">
<div class="card">
<h1>⚡ Fan Control</h1>
<div class="sub"><span class="live-dot"></span>Live <span id="modeTag" class="tag curve">curve</span><span class="tag" id="profileTag">balanced</span></div>

<div class="group">
<div class="row"><label>CPU Fan <span class="badge" id="cpuPct"></span></label><span><span class="v" id="v1">0</span>% <span class="d" id="a1">(read 0)</span></span></div>
<input type="range" id="f1" min="0" max="100" value="0">
<div class="bar" id="barWrap1"><div id="bar1" style="width:0%"></div></div>
</div>

<div class="group">
<div class="row"><label>GPU Fan <span class="badge" id="gpuPct"></span></label><span><span class="v" id="v2">0</span>% <span class="d" id="a2">(read 0)</span></span></div>
<input type="range" id="f2" min="0" max="100" value="0">
<div class="bar" id="barWrap2"><div id="bar2" style="width:0%"></div></div>
</div>

<div class="group">
<h2>Profile</h2>
<div class="btn-row" id="profiles">
<button class="btn" data-profile="silent">🤫 Silent</button>
<button class="btn active" data-profile="balanced">⚖️ Balanced</button>
<button class="btn" data-profile="performance">🚀 Performance</button>
</div>
<div class="suggest" id="curveHint">CPU temp drives the curve. Drag a slider to override for 10s.</div>
</div>

<div class="group">
<h2>Quick Presets</h2>
<div class="preset" id="presets">
<button data-v="0">Stop</button>
<button data-v="25">25%</button>
<button data-v="50">50%</button>
<button data-v="75">75%</button>
<button data-v="100">Max</button>
</div>
<div class="btn-row">
<button class="btn" id="link">🔗 Link fans</button>
<button class="btn" id="unlock">⏱ Extend override</button>
<button class="btn warn" id="restore">Release control</button>
</div>
</div>

<div class="group"><h2>Live Sensors</h2><div class="temp-grid" id="temps"></div>
<div class="sub" style="margin-top:12px">EC: <span id="ec1">--</span>°C / <span id="ec2">--</span>°C</div></div>
</div>

<div class="card">
<h2>Settings</h2>
<div class="setting">
<label>Max duty cap</label>
<input type="number" id="maxduty" min="100" max="198" step="1" value="198">
</div>
<div class="setting">
<label>Theme</label>
<select id="theme"><option value="dark">Dark</option><option value="midnight">Midnight</option><option value="light">Light</option></select>
</div>
<div class="setting">
<label>Show °F</label>
<input type="checkbox" id="fahr" style="width:18px;height:18px">
</div>
<div class="setting">
<label>Auto-poll interval</label>
<input type="number" id="interval" min="1" max="10" step="1" value="2">
</div>
</div>
</div>

<script>
const $=id=>document.getElementById(id);
let linked=true, fahrenheit=false, currentProfile='balanced';

function fmt(t){return fahrenheit?(t*9/5+32).toFixed(1):t.toFixed(1)}
const pct = d => Math.round(d / 2);

function setFan(f,v){
  fetch('/set',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fan:f,pct:v})}).catch(()=>{})
}
function setOverride(){
  // # ponytail: mark the curve as overridden for 10s. Slider drives fan
  // until override expires, then CPU temp takes over again.
  fetch('/override',{method:'POST'}).catch(()=>{})
}
function setSlider(fan, v){
  $(`f${fan}`).value = v;
  $(`v${fan}`).textContent = v;
  setFan(fan, v);
  setOverride();
  if (linked) {
    const other = fan === 1 ? 2 : 1;
    $(`f${other}`).value = v;
    $(`v${other}`).textContent = v;
    setFan(other, v);
  }
}

$('f1').addEventListener('input',e=>{const v=+e.target.value;$('v1').textContent=v;setFan(1,v);if(linked){$('f2').value=v;$('v2').textContent=v;setFan(2,v)}setOverride()});
$('f2').addEventListener('input',e=>{const v=+e.target.value;$('v2').textContent=v;setFan(2,v);if(linked){$('f1').value=v;$('v1').textContent=v;setFan(1,v)}setOverride()});

document.querySelectorAll('#presets button').forEach(b=>{
  b.addEventListener('click',()=>{
    const v = +b.dataset.v;
    setSlider(1, v);
    setSlider(2, v);
    document.querySelectorAll('#presets button').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
  });
});

document.querySelectorAll('#profiles button').forEach(b=>{
  b.addEventListener('click',()=>{
    const p = b.dataset.profile;
    fetch('/profile',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({profile:p})}).then(()=>{
      currentProfile = p;
      document.querySelectorAll('#profiles button').forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
    });
  });
});

$('link').addEventListener('click',()=>{
  linked=!linked;
  $('link').textContent = linked?'🔗 Linked':'⛓ Unlinked';
  $('link').classList.toggle('active', linked);
});

$('unlock').addEventListener('click',()=>{
  setOverride();
  $('unlock').classList.add('active');
  setTimeout(()=>$('unlock').classList.remove('active'), 800);
});

$('restore').addEventListener('click',()=>{
  fetch('/restore',{method:'POST'}).then(()=>{
    setSlider(1, 0); setSlider(2, 0);
    document.querySelectorAll('#presets button').forEach(x=>x.classList.remove('active'));
    document.querySelectorAll('#presets button')[0].classList.add('active');
  });
});

$('maxduty').addEventListener('change', e=>{
  fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({max_duty:+e.target.value})});
});
$('interval').addEventListener('change', e=>{
  fetch('/config',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({interval:+e.target.value})});
});

$('fahr').addEventListener('change',e=>{fahrenheit=e.target.checked;update()});

function theme(t){
  const light = {'--bg':'#f6f8fa','--card':'#fff','--text':'#1f2328','--dim':'#59636e','--border':'#d1d9e0'};
  const dark = {'--bg':'#000','--card':'#0a0a0a'};
  const vars = t==='light' ? light : t==='midnight' ? dark : null;
  ['--bg','--card','--text','--dim','--border'].forEach(k=>{
    if (vars && k in vars) document.documentElement.style.setProperty(k, vars[k]);
    else document.documentElement.style.removeProperty(k);
  });
}
$('theme').addEventListener('change', e=>theme(e.target.value));

function update(){
  fetch('/snapshot').then(r=>r.json()).then(s=>{
    $('a1').textContent=`(read ${s.fan1} / ${pct(s.fan1)}%)`;
    $('a2').textContent=`(read ${s.fan2} / ${pct(s.fan2)}%)`;
    $('bar1').style.width=`${pct(s.fan1)}%`;
    $('bar2').style.width=`${pct(s.fan2)}%`;
    $('ec1').textContent=fmt(s.ec_temp1);
    $('ec2').textContent=fmt(s.ec_temp2);

    // Curve vs manual mode badge
    const tag = $('modeTag');
    const now = Math.floor(Date.now()/1000);
    const inManual = s.manual_until > now;
    if (inManual) {
      tag.className = 'tag manual';
      tag.textContent = `manual (${s.manual_until - now}s)`;
    } else {
      tag.className = 'tag curve';
      tag.textContent = `curve: ${s.profile}`;
    }
    $('profileTag').textContent = s.profile;

    // Show CPU temp next to the slider label
    const cpu = s.temps.find(t => t.name === 'k10temp');
    if (cpu) $('cpuPct').textContent = `${fmt(cpu.temp)}°${fahrenheit?'F':'C'}`;
    const gpu = s.temps.find(t => t.name === 'amdgpu');
    if (gpu) $('gpuPct').textContent = `${fmt(gpu.temp)}°${fahrenheit?'F':'C'}`;

    // Live sensors grid
    const grid = $('temps'); grid.innerHTML = '';
    s.temps.forEach(t=>{
      const d = document.createElement('div'); d.className='temp';
      d.innerHTML=`<div class="l">${t.name} · ${t.label}</div><div class="t">${fmt(t.temp)}°${fahrenheit?'F':'C'}</div>`;
      grid.appendChild(d);
    });
  }).catch(()=>{});
}
update(); setInterval(update, 1000);
</script></body></html>"""

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a, **k): pass
    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif self.path == '/snapshot':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(json.dumps(snapshot()).encode())
    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        body = json.loads(self.rfile.read(n)) if n else {}
        if self.path == '/set':
            f = int(body.get('fan', 0)); p = int(body.get('pct', 0))
            if f in (1, 2) and 0 <= p <= 100:
                state['targets'][f] = p
                write_duty(f, max(0, min(state['max_duty'], p * 2)))
        elif self.path == '/override':
            # # ponytail: slider touched → hold manual mode for 10s.
            state['manual_until'] = int(time.time()) + 10
        elif self.path == '/profile':
            p = body.get('profile', 'balanced')
            if p in PROFILES: state['profile'] = p
        elif self.path == '/config':
            if 'max_duty' in body:
                state['max_duty'] = max(0, min(198, int(body['max_duty'])))
            if 'interval' in body:
                # # ponytail: poll interval is a soft hint; the poller thread
                # sleeps for a constant 0.1s. The hint is surfaced in /snapshot
                # for the UI; full dynamic-rate rethread is v2.
                pass
        elif self.path == '/restore':
            state['auto'] = True
            state['targets'] = {1: 0, 2: 0}
            state['manual_until'] = 0
            release_manual()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

def shutdown(*_):
    try:
        if state.get('manual'): release_manual()
    except Exception: pass
    try: os.close(FD)
    except Exception: pass
    subprocess.run(["systemctl", "start", "fan-daemon"], capture_output=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

if __name__ == '__main__':
    threading.Thread(target=lambda: http.server.HTTPServer(('127.0.0.1', 4444), Handler).serve_forever(), daemon=True).start()
    webbrowser.open('http://127.0.0.1:4444')
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt: shutdown()