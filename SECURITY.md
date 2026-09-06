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

- the WebKitGTK JSON-RPC bridge (`script-message-with-reply-received`);
- the custom `fancontrol://` origin and Content-Security-Policy;
- privileged process boundaries (the background daemon runs as root to access the EC; the desktop dashboard runs unprivileged and communicates via /run/fan-control/control.sock restricted to the fan-control group);
- configuration handling;
- EC read/write validation;
- firmware handoff;
- exclusive lock files under `/run/fan-control`.
- Unix domain socket framing, permissions (0660 root:fan-control), and access control;

There is no HTTP API and no TCP listener. The previous localhost dashboard
on port 4444 has been removed.

This is an unofficial community project without a guaranteed response SLA.
Manufacturer support channels cannot provide support for this software.

## Configuration and telemetry in v2

The daemon owns `/etc/fan-control.json`. Before its first write after loading
v1 configuration, it preserves `fan-control.v1.backup.json` beside the original.
It writes the replacement through a temporary file, fsync, and atomic rename.
A failed persistent mutation returns an error and restores the in-memory
configuration. GUI and CLI mutations share daemon validation and optional
revision checks.

Telemetry defaults to `/var/lib/fan-control/history.db`. Packaging creates
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
