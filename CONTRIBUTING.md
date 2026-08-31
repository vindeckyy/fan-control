# Contributing

Thank you for helping improve this unofficial Clevo/Tongfang community project.

## Before opening a change

1. Search existing issues and pull requests.
2. Reproduce the behavior with the latest `main` branch.
3. Use `--demo` whenever real EC access is unnecessary.
4. Keep hardware-specific assumptions documented and narrowly scoped.

For bugs, include the laptop model, Linux distribution, kernel version,
Python version, relevant service logs, selected profile, and exact steps to
reproduce. Remove serial numbers and other personal information from logs.

## Development checks

```bash
python3 -m py_compile fan_backend.py fan_policy.py fan_runtime.py fan_controller.py fan-daemon.py fan-gui.py fan-ctl.py test_fan_control.py
python3 -m unittest -v
cd ui && npm ci && npm test && npm run build
```

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

By contributing, you confirm that you have the right to submit the work and
agree that it may be distributed with the project under its applicable terms.
