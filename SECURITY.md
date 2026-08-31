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
- privileged process boundaries (the dashboard may run as root while it owns the EC);
- configuration handling;
- EC read/write validation;
- firmware handoff;
- exclusive lock files under `/run/fan-control`.

There is no HTTP API and no TCP listener. The previous localhost dashboard
on port 4444 has been removed.

This is an unofficial community project without a guaranteed response SLA.
Manufacturer support channels cannot provide support for this software.
