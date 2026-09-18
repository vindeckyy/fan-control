# fan-control roadmap

## v2 implementation

The v2 workspace replaces the single-page dashboard with Overview, Fans,
Curves, Sensors, Analytics, Automation, Settings, and Diagnostics. The daemon
owns versioned configuration, migration backups, per-fan policy, revision
checks, rule and schedule overlays, temporary fan tests, telemetry, and
control decision traces. Legacy RPC and CLI controls remain supported.

Read [the original plan](docs/v2-plan.md) for scope and
[verification notes](docs/v2-verification.md) for tested behavior and limits.

## Windows port

The Windows port ships native PE launchers (`fan-control.exe`, `fan-ctl.exe`,
`fan-daemon.exe`), a PyInstaller bundle, a portable-package script, a
scheduled-task service installer, and two hardware backends:

- `windows_wmi` talks to the Uniwill/Tongfang EC through the ACPI WMI
  `AcpiTest_MULong` interface. It reads EC temperatures and fan tachometers and
  writes manual duty through the EC user fan mode and direct PWM registers;
  verified on a Gateway GWTN156-2BK (Tongfang GK5NR0O firmware).
- `windows_ec` drives the EC directly through ports 0x62/0x66 when an
  InpOut32/64 or WinRing0 helper DLL is present.

Unprivileged clients use the same RPC protocol over a loopback port published
in `%PROGRAMDATA%\fan-control\run` because official CPython Windows builds do
not expose `AF_UNIX`.

## Deferred

- Privileged command execution. `run_command` is rejected by RPC; this release
  has no executable allowlist or command runner.
- The Windows `windows_wmi` backend reads the documented Uniwill tach registers
  (`0x0464`/`0x046C`). Linux hwmon tach support remains model-dependent, pending
  a documented read-only register for each supported model; duty registers must
  not be interpreted as RPM.
- Broader tray coverage across Linux desktop environments.

Remote control, cloud synchronization, calibration, kernel-driver replacement,
and arbitrary plugin execution remain outside v2 scope.
