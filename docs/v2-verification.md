# v2 verification

Linux verification on 2026-09-05 and Windows port verification on 2026-09-18.
This is an unreleased working tree. After the initial Linux checks, the user
requested normal hardware mode and reported the old installed daemon could not
connect; the source installation was backed up and upgraded to v2. The Windows
port was later completed and exercised against real Uniwill/Tongfang hardware.
Broad cross-model hardware acceptance and release publication remain pending.

## Linux results (2026-09-05)

| Check | Result and evidence |
| --- | --- |
| Python tests | 211 pass: 110 legacy tests and 101 v2 tests, through `make test` |
| Frontend tests | 28 pass across four Vitest files, including confirmation controls inside forms and recovery after a legacy daemon upgrade |
| Static checks | `ruff check .`, `git diff --check`, version synchronization, and Python 3.10 syntax parsing pass |
| UI build | TypeScript check and Vite production bundle pass |
| Production dependencies | `npm audit --omit=dev --prefix ui`: zero vulnerabilities |
| Headless demo | Capabilities, snapshot/live, representative mutation, decisions, and history pass with a temporary configuration |
| WebKitGTK | All eight `fancontrol://app/` routes load through the production bridge under Xvfb |
| Native operations | GTK smoke writes CSV, curve JSON, and redacted diagnostics through Gio; imports a curve; writes diagnostics to the clipboard |
| Browser acceptance | Chromium renders eight routes in dark, light, and 390px layouts; no document overflow; six fault/capability states captured |
| Interaction acceptance | Fan rename/test; curve create/edit/duplicate; rule test/save; preferences and safety; keyboard palette/Escape; modes; sensor selection/pinning; history ranges; schedule edits; diagnostics filter |
| Installation staging | New Python modules, built UI, service prefix, tmpfiles, and sysusers entries are included |
| Debian binary build | `dpkg-buildpackage -d -us -uc -b` succeeds in an isolated source copy with locally unpacked dh-python |
| Version ordering | App and package versions synchronize at 2.0.1; package epoch 1 preserves upgrades from 2026.9.1 |

## Windows results (2026-09-18)

Verified on a Gateway GWTN156-2BK (Uniwill/Tongfang GK5NR0O firmware,
`PROJECT_ID` 0x10, `BIOS_OEM_2` 0x9D) running Windows 11 (10.0.26200) and
Python 3.12.10:

| Check | Result and evidence |
| --- | --- |
| Python tests | 241 pass on Windows: 110 legacy, 118 v2, 13 Windows-specific (15 Linux-only tests skipped), through `python -m unittest` |
| Static checks | `ruff check .` and `python scripts/check_versions.py` pass |
| GUI headless smoke | `fan-gui.py --demo --headless-smoke` emits capabilities, live snapshot, mutation, decisions, and history |
| GUI bridge | Live demo controller served over the loopback bridge; `/rpc` returned live telemetry and `/index.html` + `/bridge.js` served correctly |
| Windows CI spec | PyInstaller spec builds `fan-control.exe`, `fan-ctl.exe`, and `fan-daemon.exe`; frozen GUI headless smoke returns exit 0 with full JSON output |
| Backend detection | `windows_wmi` selected automatically; `available()` requires the exact `AcpiTest_MULong` class instead of generic `AcpiTest_*` matches |
| EC probe | Read-only `GetSetULong` register dump via `AcpiTest_MULong` (project id, fan support bits, PWM, tach, temperatures) |
| Manual fan writes | `windows_wmi` manual mode verified: 20% -> ~1620 RPM, 45% -> ~3290 RPM, 80% -> ~4945 RPM, 100% -> ~5337 RPM on both fans; `release()` returns EC automatic control |
| Daemon end-to-end | Elevated daemon over the loopback RPC; unprivileged `fan-ctl` and Python clients changed duty and read live tachometers |
| Service install | `install-service-windows.ps1` registered `FanControlDaemon` (SYSTEM, at startup) against the project `.venv`; uninstall unregisters and releases manual EC control |
| Runtime IPC | Official CPython Windows builds do not expose `socket.AF_UNIX` on this system, so the loopback TCP port-file channel is the production path |

The probe and write tests are reproducible with elevated, temporary scripts;
no permanent register changes were left behind, and EC automatic control was
restored after each test.

The browser connector had no available browser. Browser acceptance used local
Chromium/Selenium; the separate GTK smoke uses actual GTK4 and WebKitGTK 6.
Screenshots are stored in [images/v2](images/v2/). They identify the backend as
simulated hardware. Test inputs are not measurements from the user's laptop.

The local package and final build/browser/GTK/headless logs are preserved in
`.artifacts/v2/` (privately excluded from Git). The extracted package matches
the daemon modules and UI byte-for-byte at build time. This package predates the
subsequent legacy-daemon compatibility message fix. Its version is `1:2.0.0-1`;
SHA-256 is `d64a9e831ac58951896e0b3e955cc112af9250170453168533a48d29a77dbfa8`.

The Linux source install runs v2 on the host's `tuxedo_io` backend. RPC and the
GTK window confirm live temperatures, preserved 27% manual fan targets, and
healthy SQLite persistence. The backup path was recorded locally on the Linux
host during that work. Driver
hardware checks identify Uniwill; no Linux hwmon tachometer inputs are exposed,
so RPM remains unavailable on that Linux host. The Windows `windows_wmi`
backend added in the port reads the documented Uniwill EC tach registers and
returns RPM on the verified model.

The daemon regressions cover configuration rollback and disk failures, v1
backup preservation, safety on legacy manual writes, rule precedence, actual
write failure traces, cross-thread SQLite access, history reads outside the
control lock, bounded failure backlogs, and per-series downsampling.

Frontend tests cover all routes, missing capabilities, protocol mismatch,
revision conflict refresh, acknowledged config normalization, structured
errors, reconnect, stale values, display conversion, and editor math. The
browser harness complements these with rendered controls and screenshots.

## Reproduce

Linux:

```sh
make test
ruff check .
python3 scripts/check_versions.py
npm run build --prefix ui
npm audit --omit=dev --prefix ui
xvfb-run -a python3 scripts/gtk-smoke.py
```

Windows (PowerShell):

```powershell
python -m unittest -v
ruff check .
python scripts\check_versions.py
python fan-gui.py --demo --headless-smoke
pyinstaller --clean -y packaging\windows\fan-control-pyinstaller.spec
```

Start `python3 scripts/ui-demo.py` in one terminal, then run
`python3 scripts/ui-acceptance.py` in another. Start a fresh harness for a
repeatable acceptance run; it keeps mutations in its temporary controller.
The harness requires system Chromium, chromedriver, and Python Selenium.
Xvfb can emit Mesa/DRI3 acceleration warnings while the GTK checks still pass.

## Antislop review

The design read is recorded in [v2-design.md](v2-design.md). The review applies
to authored UI and documentation; the archived incoming handoff and original
plan are preserved source material.

- Hard gate PASS: authored UI has no em dashes, invented testimonials, customer counts, or marketing claims. Navigation resolves to the eight implemented routes; all data comes from daemon responses or the labeled demo.
- Responsive layout PASS: all eight pages were rendered at 390px and checked for document overflow, with labels retained and sections stacked.
- Contrast PASS: ten primary, secondary, accent, warning, and critical text pairs in dark/light tokens were measured. The lowest ratio was 5.01:1, above 4.5:1. Disabled controls are visibly distinct.
- States PASS: loading and empty components are implemented; warning, critical, stale, disconnected, unsupported-version, and missing-capability screenshots are saved. Stale acceptance asserts the visible state after telemetry stops advancing.
- Keyboard PASS: native inputs expose labels and values; dialogs use `showModal`, restore focus, and close with Escape. Browser acceptance exercises the keyboard palette and Escape; curve points expose arrow/Delete handlers and matching numerical inputs.
- Functional controls PASS: buttons call implemented mutations, navigation, local editing, retry, native export/import, or clipboard handlers. Automated browser and GTK checks exercise the main mutation and export flows; Python tests cover the corresponding RPC behavior.
- Build/run PASS: frontend tests/build, eight native WebKitGTK routes, and browser acceptance were run. The development HTTP harness is separate from the installed application.
- Purpose gate PASS: color, typography, borders, curve grids, and dashed previews have written reasons in the design document. There are no decorative gradients, glow layers, glass surfaces, stock illustrations, or invented brand assets.
- Liveliness PASS: ENERGY 1 / RHYTHM 2 / MOTION 1 follows the supplied direction. CPU/GPU readings, the editable curve, sensor tables, and weekly schedule provide page-specific focal points. Cyan marks selection and telemetry; tabular readings repeat across pages.
- Craftsmanship PASS: page composition follows control tasks, action labels name their effects, panel/control radii differ, semantic colors have consistent roles, and both themes use the same layout and typography. Screenshots were inspected for readability and clipping.

This review records the scope exercised locally. It does not establish every
combination of hardware, desktop portal, assistive technology, or window size.

## Release limits

- `run_command` is intentionally unavailable. RPC returns `UNSUPPORTED`; no
  privileged command runner or allowlist ships.
- Tests use Python 3.14 locally on the Linux host and Python 3.12.10 on the
  Windows host. Python 3.10 syntax passes; the existing CI matrix covers 3.10,
  3.12, and 3.13, including a `windows-latest` job.
- Linux host manual targets and live telemetry were checked after source
  installation. The Windows `windows_wmi` manual writes, tachometer reads, EC
  release, and unprivileged client control were verified on one
  Uniwill/Tongfang model (Gateway GWTN156-2BK). Other Windows models and
  physical cooling response remain untested. Uniwill tach support on Linux
  hwmon is still deferred.
- Tray integration, notification delivery, portal dialogs, and clipboard
  delivery need acceptance on supported desktop environments. Native smoke
  uses real Gio file writes with supplied destinations, bypassing the picker.
- Debian was built on the available host with an unpacked dh-python toolchain,
  not a clean Debian chroot. It was not installed. Arch and RPM builds remain
  unverified. The existing RPM spec references a missing `LICENSE`; the owner
  must provide the intended license file before shipping an RPM.
- Vite 6 and Vitest 2 remain as required by the plan. The full npm audit reports
  five development-tool advisories (three moderate, one high, one critical).
  The newly added router was upgraded to 7.18.3; the production audit is clean.
  See the upstream [navigation advisory](https://github.com/advisories/GHSA-wrjc-x8rr-h8h6)
  and [hydration advisory](https://github.com/advisories/GHSA-337j-9hxr-rhxg).

The local checks support implementation review. Cross-distribution and
physical-device release acceptance remain outstanding.
