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

The project has no third-party runtime or test dependencies:

```bash
python3 -m py_compile fan-daemon.py fan-gui.py test_fan_control.py
python3 -m unittest -v
```

For dashboard changes, also run:

```bash
FAN_CONTROL_CONFIG=/tmp/fan-control-demo.json python3 fan-gui.py --demo
```

Verify both desktop and narrow-screen layouts, manual control, automatic
profiles, custom curves, EC-auto release, and light/dark themes.

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
