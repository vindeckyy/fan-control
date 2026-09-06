# fan-control roadmap

## v2 implementation

The v2 workspace replaces the single-page dashboard with Overview, Fans,
Curves, Sensors, Analytics, Automation, Settings, and Diagnostics. The daemon
owns versioned configuration, migration backups, per-fan policy, revision
checks, rule and schedule overlays, temporary fan tests, telemetry, and
control decision traces. Legacy RPC and CLI controls remain supported.

Read [the original plan](docs/v2-plan.md) for scope and
[verification notes](docs/v2-verification.md) for tested behavior and limits.

## Deferred

- Privileged command execution. `run_command` is rejected by RPC; this release
  has no executable allowlist or command runner.
- Verified Uniwill tach support, pending a documented read-only register for
  each supported model. Existing duty registers must not be interpreted as RPM.
- Broader tray coverage across desktop environments.

Remote control, cloud synchronization, calibration, kernel-driver replacement,
and arbitrary plugin execution remain outside v2 scope.
