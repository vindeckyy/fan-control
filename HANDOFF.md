# fan-control v2 handoff

Updated 2026-09-05. Branch `main`. All implementation changes remain uncommitted.
Do not commit, install on the host, or publish a release unless the user asks.

Follow-up: the user requested normal hardware mode and reported the window
could not connect. The installed daemon was v1 (`unknown method 'capabilities'`).
The source installation under `/usr/local` was backed up and upgraded to v2,
then `fan-daemon.service` was restarted and the installed GUI reopened. The
backup is `/var/tmp/fan-control-before-v2.pEFXIQ/installation.tar`; its path is
also saved in `.artifacts/v2/system-backup-path`. Real RPC and the visible GTK
window confirm v2, the tuxedo_io backend, preserved manual targets of 27% on
both fans, live temperatures, and healthy persistent history. The new GUI now
recognizes a legacy daemon and explains that it needs an update/restart.

The user also reported unavailable tachometers. Read-only driver hardware
checks return Uniwill=1, Clevo=0. No hwmon device exposes `fan*_input`. The
Uniwill backend intentionally returns no RPM because its tach reader remains
unverified; this is an existing support limit, not a connection failure.

## Task and direction

The user asked to resume the full v2 redesign, then requested `$antislop`
throughout implementation. The original plan is preserved in
[docs/v2-plan.md](docs/v2-plan.md). The incoming handoff is archived in
[docs/v2-handoff-initial.md](docs/v2-handoff-initial.md); its failure list is obsolete.

The agreed instrumentation direction and ENERGY 1 / RHYTHM 2 / MOTION 1 dials
are recorded in [docs/v2-design.md](docs/v2-design.md). Antislop core, UI,
human, mobile layout, and copywriting skills were applied during the work.

## Implementation

The daemon owns the canonical v2 document. `LegacyStateView` and existing RPC
methods adapt to it so the 110 legacy tests keep their original assertions.
`fan_policy.py` implements migration, first-write v1 backups, validation,
normalization, revision tracking, and atomic saves. Mutations roll back on
validation or persistence failure and return the authoritative configuration.

`fan_engine.py` evaluates policy without I/O. `fan_rules.py` handles transient
rule/schedule overlays, priority arbitration, sustain/cooldown, overnight
windows, and IANA timezones. Critical protection overrides normal bounds,
fan disabling, temporary tests, and rule requests to release control. An
explicit configured release leaves ownership with firmware. Legacy manual
writes now pass through the same safety pipeline and decision tracing.

`fan_controller.py` serializes control ticks and mutations. Traces report
actual write success/failure. RPC errors have stable codes. Capabilities,
per-fan controls, curve CRUD, sensor selection, rules, schedules, history,
and diagnostics are exposed through the existing Unix socket.

`fan_history.py` uses SQLite WAL with bounded batches and memory fallback.
Queries use isolated read connections outside the controller lock. History
failure cannot grow an unbounded pending queue. Downsampling aggregates each
fan and sensor separately so a series cannot disappear through row striding.

The React workspace has eight hash routes: Overview, Fans, Curves, Sensors,
Analytics, Automation, Settings, Diagnostics. Separate stores hold canonical
config, telemetry, connection state, and transient UI state. Mutations await
daemon acknowledgement; revision conflicts refresh without silently retrying.
Stale telemetry retains the last readings. Capability/version states disable
unsupported controls. Dark, light, and system themes and Celsius/Fahrenheit
display are implemented.

Curve Studio supports keyboard/pointer editing, numerical points, preview,
overlays, assignments, and import/export. Rules and weekly schedules have
editors. Analytics displays measured temperature and duties only for present
fans, plus retained history and aggregate fan statistics. Diagnostics exposes
decision traces and native copy/export.

GTK serves the built UI at `fancontrol://app/`; it has no production HTTP
server. Locale normalization fixes a blank WebKit page under `C.UTF-8` caused
by uPlot's Intl formatting. GTK adds native exports/imports, notifications,
and daemon-unavailable startup recovery. The tray has profile/mode controls
and live status. CLI v2 commands use the same RPC client.

Build/install lists include all new Python modules, history tmpfiles and
sysusers entries. Package metadata and app/UI versions are 2.0.0. Package
epoch 1 sorts after the previous date-based version. Node.js 20+ is required
for building; React Router 7.18.3 replaces the initially added vulnerable v6
release. React 18, Vite 6, Vitest 2, uPlot, and Zustand 4 remain in use.

## Verification and artifacts

See [docs/v2-verification.md](docs/v2-verification.md) for evidence and limits.
The final Python suite has 211 passing tests: 110 legacy and 101 v2. The
frontend has 28 passing tests. Lint, version synchronization, production build,
headless simulation, native GTK route/export smoke, and browser acceptance
have been run. Screenshots are in `docs/images/v2/`.

Useful repeatable commands:

```sh
make test
ruff check .
python3 scripts/check_versions.py
npm run build --prefix ui
npm audit --omit=dev --prefix ui
xvfb-run -a python3 scripts/gtk-smoke.py
python3 scripts/ui-demo.py
# In another terminal, against a fresh demo process:
python3 scripts/ui-acceptance.py
```

The browser connector reported no available browser. Visual checks therefore
used local Chromium/Selenium; production route checks used actual WebKitGTK.
`scripts/ui-demo.py` binds loopback only and uses temporary simulated hardware.
Its HTTP bridge is a development harness and is never part of the installed UI.

A Debian binary package was built in an isolated source copy without installing
it on the host. The package and final build, browser, GTK, and headless logs
are preserved locally in `.artifacts/v2/`, privately excluded from Git. The
package's daemon modules and UI were compared byte-for-byte at build time.
That package predates the later legacy-daemon message fix; the source install
has that fix. `/tmp/fan-deb-build-path` and `/tmp/fan-debian-tools-path` identify
that copy and locally unpacked dh-python tooling if still present. Temporary
files may be removed between sessions; validate these paths before use.
The build uses locally unpacked dh-python via PATH, PERL5LIB, and PYTHONPATH,
then `dpkg-buildpackage -d -us -uc -b`. The `-d` bypasses dpkg's installed-package
check because dh-python was unpacked locally; it does not establish a clean
Debian build environment.

## Remaining release limits

- Command execution is deliberately unavailable. `rules.set` rejects
  `run_command` with `UNSUPPORTED`; no allowlist or command runner ships.
- Beyond preserving the host's existing manual duties and checking live
  telemetry, physical cooling behavior, other laptop models, and tray behavior
  across desktop environments have not been validated during this work.
- Python 3.10 syntax was checked locally; runtime tests used Python 3.14.
  The CI matrix still covers 3.10, 3.12, and 3.13 but has not run remotely here.
- RPM and Arch packages have not been built. The pre-existing RPM spec refers
  to a missing `LICENSE`; the owner must supply the intended license file
  before that package can be released. Do not invent license text or authorship.
- The retained Vite/Vitest development toolchain has npm audit advisories.
  The production dependency audit reports zero vulnerabilities.
- Standard native file writes/imports and clipboard writes were exercised;
  desktop portal dialogs and clipboard delivery across desktops need a real
  desktop acceptance pass.

## Workspace details

The original root-owned `ui/node_modules` could not be replaced in place. It
was preserved as `.node_modules-before-v2` at the repository root, excluded
privately in `.git/info/exclude`. The active `ui/node_modules` is user-owned.
Exclude that backup, `.artifacts`, and node_modules from source archives and package copies.

Test loaders reuse `sys.modules` entries to preserve exception-class identity
when both suites run together. Keep module load order consistent. The v2
`__main__` guard is at the end so direct invocation includes regression tests.
Keep history exceptions isolated from control, and keep every persistent edit
behind daemon validation and atomic commit.
