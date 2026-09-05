<div align="center">

# Clevo/Tongfang Fan Control

**A safety-focused Linux fan-control daemon and native WebKitGTK workstation for compatible Clevo/Tongfang systems.**

[![CI](https://github.com/vindeckyy/fan-control/actions/workflows/ci.yml/badge.svg)](https://github.com/vindeckyy/fan-control/actions/workflows/ci.yml)
[![Platform](https://img.shields.io/badge/platform-Linux-1793d1)](https://kernel.org/)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab)](https://www.python.org/)
[![Project status](https://img.shields.io/badge/status-unofficial-f59e0b)](#unofficial-project)

</div>

> [!IMPORTANT]
> **Unofficial community project.** This software is not affiliated with,
> endorsed by, sponsored by, or supported by Clevo, Tongfang, any laptop
> reseller, or any kernel-driver vendor. Use it at your own risk.

Fan Control provides automatic temperature curves, direct manual control,
live CPU/GPU telemetry, dual-axis history graphs, and a native desktop
window. The dashboard is a GTK4 + WebKitGTK 6 application; it does not
open a browser and does not bind a TCP port.

## Features

- Native desktop window (no localhost web server)
- Silent, Balanced, Performance, custom-curve, manual, and firmware-auto modes
- Independent or linked CPU/GPU fan curves and targets
- Visual curve editor with named import/export
- Critical-temperature override that bypasses the user noise cap
- Curve hysteresis to prevent rapid speed hunting
- Automatic handoff to firmware when temperature data becomes unavailable
- Live hwmon sensors with an NVIDIA `nvidia-smi` fallback and optional pinning
- Thirty-minute CPU/GPU temperature and fan-duty history with CSV export
- Read-only Clevo tach (RPM) when the backend exposes it; Uniwill tach is not probed
- Persistent configuration with atomic writes
- Dark/light themes, desktop notifications, display-only tray, `fan-ctl` CLI
- Hardware-free demo mode for safe evaluation and UI development

## Compatibility

Fan Control supports two hardware backends on Linux. It auto-detects which
one is present; use `--backend` or `FAN_CONTROL_BACKEND` to override.

| Requirement | Details |
| --- | --- |
| Hardware | Clevo/Tongfang-based system with a compatible EC interface |
| Backend | `tuxedo_io` (ioctls on a `/dev/*_io` device) or `clevo_acpi` (sysfs) |
| Desktop | GTK 4 and WebKitGTK 6 (`gir1.2-gtk-4.0`, `gir1.2-webkit-6.0`, `python3-gi`) |
| Runtime | Python 3.10 or newer |
| Privileges | Root access for the background daemon; desktop dashboard runs unprivileged |
| Service manager | systemd for the included background service |
| NVIDIA telemetry | Optional; requires a working `nvidia-smi` command |

The `tuxedo_io` backend drives duty through ioctls on a character device
matching `/dev/*_io`. The `clevo_acpi` backend drives per-fan duty through
plain sysfs files under `/sys/class/leds/clevo-acpi::kbd_backlight/device/`,
and relies on that driver's kernel-side watchdog to return control to
firmware auto if the controlling process stops.

Hardware compatibility varies by model and firmware. Start with demo mode,
then verify sensor readings and fan response before enabling the service.

## Safety model

Fan Control treats thermal control as a safety-critical path:

- Duty is authored as a percentage 0-100. The `tuxedo_io` backend maps this
  onto its native raw 0-198 domain at the edge, keeping the known-safe cap of
  198; values near 200 are known to behave unpredictably on affected firmware.
- At `critical_temp`, both fans are commanded to 100% even when a lower noise
  cap is configured.
- After three invalid temperature readings, the daemon returns control to the
  system firmware until valid telemetry returns.
- On the `clevo_acpi` backend, the kernel-side watchdog independently releases
  to firmware auto if the controlling process stops renewing a manual override.
- The background daemon (`fan-daemon`) is the sole root owner of the EC
  interface and runs continuously.
- The dashboard window runs unprivileged as the desktop user. WebKit never
  runs as root; it communicates with `fan-daemon` over a local Unix socket
  restricted to the `fan-control` group (`0660 root:fan-control`).
- Safety-critical policy lives in Python (`fan_policy.py`), not in JavaScript.
> [!CAUTION]
> Confirm the reported temperatures and physical fan response on your exact
> machine. Incorrect low-level fan control can cause overheating or hardware
> damage.

## Installation

### Package installation

Download the binary package from [GitHub Releases](https://github.com/vindeckyy/fan-control/releases/latest) (Debian, Ubuntu, Kali):

```bash
sudo dpkg -i fan-control_2026.9.1-1_amd64.deb
sudo usermod -aG fan-control $USER
sudo systemctl enable --now fan-daemon
```

### From source

Debian/Kali:

```bash
sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-webkit-6.0 nodejs npm
```

Fedora: `python3-gobject gtk4 webkitgtk6.0 nodejs`.
Arch: `python-gobject gtk4 webkitgtk-6.0 nodejs npm`.

Clone, build the UI, and install:

```bash
git clone https://github.com/vindeckyy/fan-control.git
cd fan-control
sudo make install
sudo usermod -aG fan-control $USER
sudo systemctl daemon-reload
sudo systemctl enable --now fan-daemon

Check the service after installation:

```bash
systemctl status fan-daemon
journalctl -u fan-daemon -n 50 --no-pager
```

## Usage

Open the native dashboard (runs unprivileged; no sudo needed):

```bash
fan-gui
```

`fan-daemon` remains running in the background while the window is open and
continues managing the embedded controller.

Preview without root or compatible hardware:

```bash
FAN_CONTROL_CONFIG=/tmp/fan-control-demo.json python3 fan-gui.py --demo
```

Headless snapshot for CI and scripts:

```bash
FAN_CONTROL_CONFIG=/tmp/fan-control-demo.json python3 fan-gui.py --demo --headless-smoke
```

Display-only tray (never locks the EC; profile changes route over the socket):

```bash
fan-gui --tray
```

CLI:

```bash
fan-ctl status --json
fan-ctl profile silent
fan-ctl mode released
fan-ctl set 1 40
fan-ctl cap 80
fan-ctl config hysteresis=3 linked=false
fan-ctl curve cpu
fan-ctl curves
fan-ctl curves load quiet
fan-ctl diagnose
```

Run the daemon directly:

```bash
sudo fan-daemon
fan-daemon --dry-run
fan-daemon --diagnose
```

## Configuration

Both programs read `/etc/fan-control.json`. The dashboard writes this file
atomically when settings change. The daemon reloads it on `SIGHUP` /
`systemctl reload fan-daemon`.

```json
{
  "profile": "balanced",
  "mode": "curve",
  "max_duty": 100,
  "hysteresis": 5,
  "critical_temp": 95,
  "linked": true
}
```

| Key | Default | Purpose |
| --- | ---: | --- |
| `profile` | `balanced` | Active automatic curve |
| `mode` | `manual` | `manual`, `curve`, or `released` |
| `curve` | built-in | Shared `[temperature, duty]` points |
| `curve_cpu` / `curve_gpu` | none | Independent curves when `linked` is false |
| `max_duty` | `100` | Normal-operation noise cap, as a percentage |
| `hysteresis` | `5` | Minimum duty change before curve updates |
| `critical_temp` | `95` | Temperature that forces maximum safe duty |
| `linked` | `true` | Drive both fans from the same curve/target |
| `named_curves` | `{}` | Saved custom curves |
| `cpu_sensor` / `gpu_sensor` | hottest defaults | Pinned hwmon identity `{name, label}` |
| `theme` | `dark` | `dark` or `light` |
| `alerts.desktop` | `false` | Gio notifications at critical temperature |

Backend selection is automatic. Set `FAN_CONTROL_BACKEND` or pass `--backend`
to override. For `tuxedo_io`, set `FAN_CONTROL_DEVICE` or pass `--device`
when multiple candidates exist.

## Architecture

```text
hwmon / NVIDIA telemetry ──► temperature selection ──► curve + safety policy
                                                              │
                                                              ▼
        ┌────── clevo_acpi sysfs ───────┐  ◄── duty target ──┘
        │                                │
backend ┼──── tuxedo_io ioctls ─────────┤  ◄── percent duty
        │                                │
        └──── demo (simulated) ──────────┘
                         │
                         ▼
       firmware auto ◄── ownership handoff ◄── watchdog / release
```

- `fan_backend.py` — hardware backends.
- `fan_policy.py` — shared curves, interpolation, critical override, config.
- `fan_controller.py` — controller logic and JSON-RPC dispatch methods.
- `fan_rpc.py` — Unix-socket JSON-RPC server and client.
- `fan_diagnostics.py` — system and hardware diagnostics.
- `fan_gtk.py` — unprivileged GTK4 + WebKitGTK 6 dashboard and tray.
- `fan-gui.py` — entry point (`--demo`, `--tray`, `--headless-smoke`).
- `fan-daemon.py` — systemd background control service and sole EC owner.
- `fan-ctl.py` — CLI client communicating with the daemon over the socket.
- `ui/` — React + Vite dashboard loaded from `fancontrol://app/`.

## Troubleshooting

### No fan-control hardware found

```bash
sudo fan-daemon --diagnose
sudo fan-ctl diagnose
```

### Dashboard does not open

Install GI bindings (`gir1.2-gtk-4.0`, `gir1.2-webkit-6.0`) and build the UI
(`cd ui && npm ci && npm run build`). Run `python3 fan-gui.py --demo --debug`
to enable the WebKit inspector.

### NVIDIA temperature is missing

```bash
nvidia-smi --query-gpu=index,temperature.gpu,name --format=csv,noheader,nounits
```

## Development

```bash
python3 -m py_compile fan_backend.py fan_policy.py fan_runtime.py fan_controller.py fan-daemon.py fan-gui.py fan-ctl.py test_fan_control.py
python3 -m unittest -v
cd ui && npm ci && npm test && npm run build
FAN_CONTROL_CONFIG=/tmp/fan-control-demo.json python3 fan-gui.py --demo
```

See [CONTRIBUTING.md](CONTRIBUTING.md) before proposing hardware-facing
changes. Security issues should follow [SECURITY.md](SECURITY.md).

## Unofficial project

Clevo and Tongfang names are used only to describe hardware compatibility.
All product names and trademarks belong to their respective owners. This
repository provides no manufacturer warranty, certification, or support.
