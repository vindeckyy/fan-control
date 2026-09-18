# Contributing

Thank you for helping improve this unofficial Clevo/Tongfang community project.

## Before opening a change

1. Search existing issues and pull requests.
2. Reproduce the behavior with the latest `main` branch.
3. Use `--demo` whenever real EC access is unnecessary.
4. Keep hardware-specific assumptions documented and narrowly scoped.

For bugs, include the laptop model, operating system (Linux distribution and
kernel version, or Windows edition and build), Python version, selected
backend, relevant service logs, selected profile, and exact steps to
reproduce. Remove serial numbers and other personal information from logs.

## Development checks

Linux:

```bash
python3 -m py_compile fan_backend.py fan_policy.py fan_runtime.py fan_controller.py fan_rpc.py fan_diagnostics.py fan-daemon.py fan-gui.py fan-ctl.py fan_gtk.py test_fan_control.py
python3 -m unittest -v
make lint
python3 scripts/check_versions.py
cd ui && npm ci && npm test && npm run build
```

Windows (PowerShell):

```powershell
python -m unittest -v
ruff check .
python scripts\check_versions.py
python fan-gui.py --demo --headless-smoke
pyinstaller --clean -y packaging\windows\fan-control-pyinstaller.spec
```

The Windows CI job runs the full test suite, the headless smoke test, and the
PyInstaller build on `windows-latest`.

For dashboard changes, also run:

```bash
FAN_CONTROL_CONFIG=/tmp/fan-control-demo.json python3 fan-gui.py --demo
```

Verify both desktop and ~900px tiled layouts, manual control, automatic
profiles, custom curves, EC-auto release, light/dark themes, history windows,
and the visual curve editor. `--headless-smoke` covers the controller without
opening a window.

## Hardware-facing changes

Never guess unknown EC registers or enable unverified writes. A hardware-facing
pull request must explain:

- the exact models tested;
- whether the operation is read-only or writes state;
- expected register ranges and safety bounds;
- firmware fallback behavior;
- how the change was validated on real hardware.

Keep pull requests focused. Update tests and documentation with behavior
changes, and clearly call out anything that could not be tested physically.

## Releasing

When bumping versions, update `packaging/PKGBUILD`, `packaging/fan-control.spec`, `packaging/debian/changelog`, and `packaging/org.community.FanControl.metainfo.xml`, then verify with:

```bash
python3 scripts/check_versions.py
```

By contributing, you confirm that you have the right to submit the work and
agree that it may be distributed with the project under its applicable terms.
