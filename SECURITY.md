# Security policy

Fan Control runs with elevated privileges and writes to an embedded controller,
so security and safe failure behavior are treated as core requirements.

## Reporting a vulnerability

Do not disclose a suspected vulnerability in a public issue. Use GitHub's
[private vulnerability reporting](https://github.com/vindeckyy/fan-control/security/advisories/new)
to provide:

- the affected version or commit;
- reproduction steps or a proof of concept;
- expected impact;
- any known mitigation;
- whether hardware access is required.

You should receive an acknowledgement after the report is reviewed. Please
allow time for a fix before publishing technical details.

## Scope

Security-relevant areas include:

- the WebKitGTK JSON-RPC bridge (`script-message-with-reply-received`) on Linux
  and the loopback HTTP/RPC bridge in `fan_windows_gui.py` on Windows;
- the custom `fancontrol://` origin and Content-Security-Policy on Linux;
- privileged process boundaries (the background daemon runs as root on Linux
  and as SYSTEM/Administrator on Windows to access the EC; the desktop
  dashboard runs unprivileged). Linux clients use
  `/run/fan-control/control.sock` restricted to the `fan-control` group;
  Windows clients use a loopback TCP port published in
  `%PROGRAMDATA%\fan-control\run\control.sock` because official CPython
  Windows builds do not expose `AF_UNIX`;
- configuration handling;
- EC read/write validation;
- firmware handoff;
- exclusive lock files under `/run/fan-control` (Linux) or
  `%PROGRAMDATA%\fan-control\run` (Windows);
- Unix domain socket framing and permissions (0660 root:fan-control) on Linux,
  and the Windows port file's inherited ACL plus the unprivileged GUI's
  127.0.0.1-only listener.

On Linux there is no HTTP API and no TCP listener; the previous localhost
dashboard on port 4444 has been removed. On Windows the unprivileged GUI
serves the bundled dashboard over a loopback-only HTTP bridge, and the
daemon's control channel is a loopback TCP socket whose ephemeral port is
published in the run directory.

This is an unofficial community project without a guaranteed response SLA.
Manufacturer support channels cannot provide support for this software.

## Configuration and telemetry in v2

The daemon owns `/etc/fan-control.json` on Linux and
`%PROGRAMDATA%\fan-control\fan-control.json` on Windows. Before its first write
after loading v1 configuration, it preserves `fan-control.v1.backup.json`
beside the original. It writes the replacement through a temporary file,
fsync, and atomic rename. A failed persistent mutation returns an error and
restores the in-memory configuration. GUI and CLI mutations share daemon
validation and optional revision checks.

Telemetry defaults to `/var/lib/fan-control/history.db` on Linux and
`%PROGRAMDATA%\fan-control\data\history.db` on Windows. Linux packaging creates
`/var/lib/fan-control` as `0750 root:fan-control`. The standard Python SQLite
module is required. Persistence failures degrade analytics to memory history;
fan control continues. Retention is configurable in Settings. The daemon's
`--data-dir` or `FAN_CONTROL_DATA_DIR` can change the database directory.

Automation changes effective runtime policy without overwriting saved mode
or profile. Critical protection takes precedence over automatic overlays,
fan tests, and duty limits. Explicit EC Auto returns control to firmware.

Command execution is not shipped. RPC rejects `run_command` rules with
`UNSUPPORTED`, and the daemon never launches their command references. Future
support requires an administrator-owned allowlist, absolute executables, argv
arrays, a sanitized environment, time/output/concurrency limits, and auditing.

Diagnostics copy/export omits configuration and database paths and raw sensor
records. Review remaining warnings before sharing, since backend error text
may contain system-specific details.

The optional `scripts/ui-demo.py` acceptance harness binds only to loopback
and uses an isolated simulated controller. It is a development tool and is
not installed as an application service.
