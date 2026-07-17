# fan-control Roadmap Spec

Status: design. Not yet implemented.
Owner: Hayden. Repo: vindeckyy/fan-control.

## Why this exists

The current fan-control ships a working fan slider + preset UI but the
user can only set a value and pray. Three real gaps that the user
flagged in this session:

1. **Presets misbehave** — Stop works, the others sometimes don't, or
   the displayed value doesn't match the requested percent.
2. **No automation** — every duty value is a hand-set number. There's
   no curve, no hysteresis tuning, no "match my workload" mode.
3. **No feedback** — the GUI shows a single EC temperature and current
   duty. There's no fan RPM, no history, no signal when something
   goes wrong.

This spec covers v1, v2, and v3. v1 ships this session.

## Non-goals (YAGNI)

- Mobile layout. The GUI runs on a desktop with a real keyboard.
- Authentication. Localhost only; the device is already privileged.
- Multi-host. The GUI is bound to one machine.
- Cloud sync, telemetry. Offline-first; no outbound network.
- Custom curve *editor*. v1 ships a sensible default curve plus
  a small set of named profiles (Silent, Balanced, Performance).

## v1 — Ship this session

### 1.1 Auto-curve

The fan daemon reads CPU temperature from k10temp every 2 seconds and
picks a duty from a curve. The curve is selected by a `profile`
parameter — default is "Balanced".

Three profiles ship:

| Profile     | 50°C  | 60°C  | 70°C  | 80°C  | 90°C  | 95°C  |
|-------------|-------|-------|-------|-------|-------|-------|
| Silent      | 0     | 0     | 60    | 120   | 170   | 198   |
| Balanced    | 0     | 50    | 100   | 150   | 180   | 198   |
| Performance | 0     | 80    | 140   | 180   | 198   | 198   |

The "Max" cap is 198 (EC firmware wraps at 200). Capped by
`fan-control.max_duty` config.

### 1.2 RPM reading

The kernel module exposes fan RPM via existing Uniwill/Clevo WMI
methods. For the barebone (GWTN156-2BK) the Uniwill read at
`R_UW_FANSPEED` (0x1804) returns a duty not RPM. A separate tach
read is needed; the kernel driver does not yet expose it.

v1 ships the **slot** in the UI ("RPM: N/A on this hardware") and
attempts the read. When the kernel exposes it, the field populates
without UI changes. Test in a TTY before committing the placeholder
logic to the daemon — if the read corrupts state, the daemon
crashes the EC. Ponytail: do not enable RPM read on the GWTN156-2BK
until verified on a model that exposes it.

### 1.3 Preset + curve switcher in the GUI

A row of preset buttons: Silent | Low | Med | High | Max. Each maps
to a profile and an override. Clicking a preset:

- Sets the current `profile` to that preset's name.
- Sets the manual slider override to 0% (let the curve drive).
- Sets a 10-second override timer. During that window the user can
  drag the slider to clamp the curve. After 10s the curve resumes.

If the curve is enabled, the preset button label shows a small
"curve" badge so the user knows manual control is temporary.

### 1.4 Display consistency

The percent number, the bar fill, and the EC read-back all show
the **same number** (the slider's percent, 0-100). The raw duty
is exposed in a "raw" tab for debugging.

Already shipped in this session. Listed for completeness.

### 1.5 Light theme

The Dark and Midnight themes ship today. Add a Light theme using
the user-supplied palette. v1 finishes the light theme.

## v2 — Next session

- **History graphs.** Backend stores last 30 minutes of (temp, duty)
  per fan. Frontend shows line charts (no chart library — inline SVG).
- **Alerts.** Optional webhook or desktop notification when CPU
  crosses 90°C for 30s.
- **Multiple profiles per workload.** Save named profiles to JSON
  in `~/.hermes/fan-control/profiles.json`. Switch via CLI or tray.

## v3 — Later

- **System tray icon.** KDE Plasma tray applet showing duty + temp.
- **CLI.** `fan-ctl set 1 60` and `fan-ctl status`.
- **Hotkeys.** Global shortcuts for Silent/Performance toggle.

## Architecture (v1)

```
fan-daemon.py        # reads CPU temp, picks duty from curve, writes EC
                     # owns /dev/tuxedo_io while running
fan-gui.py           # HTTP server on 127.0.0.1:4444, owns /dev/tuxedo_io
                     # while running, conflicts with daemon
                     # (daemon is stopped while GUI is open)
fan-daemon.service   # systemd unit for daemon

HTTP API (v1, extends v0):
  GET  /              # dashboard HTML
  GET  /snapshot      # { fan1, fan2, ec_temp1, ec_temp2, profile,
                       #   targets, temps, max_duty }
  POST /set           # { fan, pct } - manual override
  POST /config        # { profile, max_duty, hysteresis }
  POST /profile       # { name: "silent"|"balanced"|"performance" }
  POST /restore       # release manual control back to EC

State:
  - profile: "silent"|"balanced"|"performance" (default "balanced")
  - targets: { 1, 2 }       # percent, default 0
  - manual_until: timestamp # if > now, write targets; else write curve duty
  - max_duty: int (default 198)
  - hysteresis: int (default 5)

Curve evaluation:
  - Read CPU temp from k10temp /sys/class/hwmon/hwmonN/temp1_input.
  - Find interval [t0,t1] containing temp. Linear interp.
  - Clamp to [0, max_duty].
  - Write to EC at 10Hz.
  - When manual_until > now: use targets instead of curve.

## Open questions for user

1. Is the rpm read attempt a v1 must, or v2? (Tach register unknown
   on GWTN156-2BK; risk of writing wrong EC offset.)
2. Should the curve hysteresis be profile-dependent, or single global?
3. Naming: "Silent / Balanced / Performance" — keep, or rename to
   "Quiet / Auto / Boost"?

## Failure modes

- **k10temp missing.** Some Ryzen models have k10temp disabled.
  Fall back to EC temp sensors, log a warning. Already implemented.
- **EC writes ignored.** If the kernel module for the barebone's
  hardware-ID doesn't match any tuxedo-drivers table, writes return
  0 and the poller no-ops. Surfaced via /snapshot `auto: false` flag.
- **GUI/daemon race.** Solved by sync-stop daemon before opening FD.
  Already implemented.

## Out-of-scope: file layout

The repo currently has 4 files. v1 keeps that count. New
functionality is appended to existing files unless complexity
demands a split. Specifically:

- `fan-daemon.py` grows to handle profiles + curves.
- `fan-gui.py` grows a profile-picker row and a Light theme.

A new file is only justified if it has a single clear purpose that
existing files can't carry without bloat.

## Acceptance (v1)

- Daemon and GUI each ship as standalone binaries.
- Switching profiles changes the curve behavior visibly within 5s.
- Manual slider override returns to curve after 10s.
- All themes render without overflow.
- v0 regression: presets apply instantly, percent display matches EC.
