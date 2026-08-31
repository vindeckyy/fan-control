# fan-control roadmap

## Shipped

- Native GTK4 + WebKitGTK 6 dashboard (no browser, no TCP port)
- Shared `fan_policy.py` used by daemon, GUI, and CLI
- Manual, Silent, Balanced, Performance, custom-curve, and EC-auto modes
- Linked or independent fan targets and per-side custom curves
- Visual curve editor, named curves, GTK import/export
- Dual-axis history graphs, sparklines, CSV export
- Sensor pinning and optional fan 3 when the backend lists it
- Read-only Clevo RPM from tuxedo_io FANINFO; Uniwill tach is not probed
- Curve hysteresis and a cap-independent critical-temperature override
- Automatic firmware handoff when temperature data becomes unavailable
- Exclusive EC lock + GUI pid so a crashed window does not block the daemon forever
- Daemon SIGHUP reload and `fan-ctl`
- Display-only tray and Gio desktop notifications
- Persistent configuration shared through `/etc/fan-control.json`
- Live hwmon sensors plus NVIDIA temperature fallback through `nvidia-smi`
- A second `clevo_acpi` sysfs backend for boards where `tuxedo_io` refuses to bind

## Next

- Packaging for common Linux distributions (AUR, Debian, Fedora copr)
- Polkit helper so the WebKit window does not need to run as root
- Verified Uniwill tach only after a read-only register is confirmed per model

## Later

- Optional per-GPU fan curves beyond CPU/GPU/Aux where a third EC channel is safe
- Broader desktop-environment tray coverage (GNOME extension, Ayatana)

RPM remains unimplemented on Uniwill: the known value on the target
GWTN156-2BK is duty, not tach speed, and probing unknown EC registers risks
hardware state.
