#!/usr/bin/env python3
"""Hardware backends for fan control.

Two kernel interfaces share one policy layer:

- ``tuxedo_io``: ioctls on a ``/dev/*_io`` character device, raw duty 0-198.
- ``clevo_acpi``: sysfs files under the ``clevo-acpi`` platform device,
  per-fan duty 0-100, with a kernel-side dead-man's-switch.

Backends expose duty as a percentage 0-100. ``tuxedo_io`` translates to its
native 0-198 value at the edge; ``clevo_acpi`` is already in percent.

The clevo-acpi sysfs interface comes from ``arbitrary-string/clevo-acpi-dkms``
(GPL-2.0-or-later). Its semantics were read from that driver source and from
the reference daemon ``arbitrary-string/clevo-control-panel`` (GPL-3.0). No
code is copied from either project; only the documented attribute names and
command contract are consumed.
"""

import ctypes
import fcntl
import math
import os
import pathlib
import threading
import time

MAX_DUTY_PERCENT = 100

# tuxedo_io ioctl encoding. Raw duty domain is 0-198; values near 200 are
# known to behave unpredictably on affected firmware.
MAGIC_RD, MAGIC_WR = 0xEF, 0xF0
IOC_R, IOC_W, SZ = 2, 1, 8

R_FS1 = None
R_FS2 = None
R_TEMP = None
R_TEMP2 = None
W_FS1 = None
W_FS2 = None
W_MODE = None
W_AUTO = None


def ioc(direction, kind, number, size):
    return (direction << 30) | (kind << 8) | number | (size << 16)


R_FS1 = ioc(IOC_R, MAGIC_RD, 0x10, SZ)
R_FS2 = ioc(IOC_R, MAGIC_RD, 0x11, SZ)
R_TEMP = ioc(IOC_R, MAGIC_RD, 0x12, SZ)
R_TEMP2 = ioc(IOC_R, MAGIC_RD, 0x13, SZ)
W_FS1 = ioc(IOC_W, MAGIC_WR, 0x10, SZ)
W_FS2 = ioc(IOC_W, MAGIC_WR, 0x11, SZ)
W_MODE = ioc(IOC_W, MAGIC_WR, 0x12, SZ)
W_AUTO = ioc(0, MAGIC_WR, 0x14, 0)

# The clevo-acpi fan attributes live on the platform device reached through
# the LED classdev symlink. This is how the reference project resolves it.
CLEVO_SYSFS_BASE = pathlib.Path("/sys/class/leds/clevo-acpi::kbd_backlight/device")
CLEVO_WATCHDOG_TIMEOUT_MS = 15000
CLEVO_WATCHDOG_MIN_MS = 5000
CLEVO_WATCHDOG_MAX_MS = 60000

TUXEDO_NATIVE_MAX = 198


class FanBackendError(RuntimeError):
    pass


class FanBackend:
    """Policy-free hardware facade. Duty is always a percentage 0-100."""

    name = ""

    def fans(self):
        """Present fan indices (1-based), e.g. ``[1, 2]``."""
        raise NotImplementedError

    def lock(self):
        """Take manual control, or arm the watchdog that will."""
        raise NotImplementedError

    def release(self):
        """Return control to firmware auto. Idempotent."""
        raise NotImplementedError

    def write_duty(self, fan, percent):
        raise NotImplementedError

    def read_duty(self, fan):
        """Current duty as a percentage 0-100."""
        raise NotImplementedError

    def read_temp(self):
        """Primary EC temperature in degrees C, or ``None``."""
        return None

    def read_temp2(self):
        """Secondary EC temperature in degrees C, or ``None``."""
        return None

    def ping(self):
        """Keep the watchdog alive without an EC write. No-op if absent."""

    def close(self):
        """Release any held resources."""


class TuxedoIoBackend(FanBackend):
    name = "tuxedo_io"
    NATIVE_MAX = TUXEDO_NATIVE_MAX

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR)
        self._lock = threading.RLock()

    def _pct_to_raw(self, percent):
        return round(int(percent) * self.NATIVE_MAX / MAX_DUTY_PERCENT)

    def _raw_to_pct(self, raw):
        return round(int(raw) * MAX_DUTY_PERCENT / self.NATIVE_MAX)

    def _read(self, command):
        buf = ctypes.c_int64()
        fcntl.ioctl(self.fd, command, buf, True)
        return buf.value & 0xFF

    def _write(self, command, value):
        buf = ctypes.c_int64(int(value))
        fcntl.ioctl(self.fd, command, buf, True)

    def fans(self):
        return [1, 2]

    def lock(self):
        with self._lock:
            self._write(W_MODE, 0x40)

    def release(self):
        with self._lock:
            fcntl.ioctl(self.fd, W_AUTO)

    def write_duty(self, fan, percent):
        value = max(0, min(self.NATIVE_MAX, self._pct_to_raw(percent)))
        with self._lock:
            self._write((W_FS1, W_FS2)[fan - 1], value)

    def read_duty(self, fan):
        with self._lock:
            return self._raw_to_pct(self._read((R_FS1, R_FS2)[fan - 1]))

    def read_temp(self):
        try:
            with self._lock:
                return float(self._read(R_TEMP))
        except OSError:
            return None

    def read_temp2(self):
        try:
            with self._lock:
                return float(self._read(R_TEMP2))
        except OSError:
            return None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class ClevoAcpiBackend(FanBackend):
    name = "clevo_acpi"

    def __init__(self, base=None, watchdog_timeout_ms=None):
        self.base = pathlib.Path(base) if base else CLEVO_SYSFS_BASE
        self.watchdog_timeout_ms = watchdog_timeout_ms or CLEVO_WATCHDOG_TIMEOUT_MS
        self._fans = [
            n for n in (1, 2, 3)
            if (self.base / f"fan{n}_manual_duty").exists()
        ]
        if not self._fans:
            raise FanBackendError(
                f"clevo-acpi fan attributes not found under {self.base}"
            )

    @classmethod
    def available(cls, base=None):
        base = pathlib.Path(base) if base else CLEVO_SYSFS_BASE
        return any((base / f"fan{n}_manual_duty").exists() for n in (1, 2, 3))

    def fans(self):
        return list(self._fans)

    def _write(self, name, value):
        (self.base / name).write_text(str(value))

    def set_watchdog_timeout_ms(self, ms):
        ms = max(CLEVO_WATCHDOG_MIN_MS, min(CLEVO_WATCHDOG_MAX_MS, int(ms)))
        self.watchdog_timeout_ms = ms
        self._write("fan_watchdog_timeout_ms", ms)

    def lock(self):
        # Manual mode is armed by the first duty write, not a separate
        # command. Setting the timeout window here is safe either way.
        try:
            self._write("fan_watchdog_timeout_ms", self.watchdog_timeout_ms)
        except OSError:
            pass

    def release(self):
        try:
            self._write("fan_release", "1")
        except OSError:
            pass

    def write_duty(self, fan, percent):
        value = max(0, min(MAX_DUTY_PERCENT, int(percent)))
        self._write(f"fan{fan}_manual_duty", value)

    def read_duty(self, fan):
        return int((self.base / f"fan{fan}_duty").read_text().strip())

    def read_temp(self):
        try:
            return float((self.base / "fan1_temp").read_text().strip())
        except (OSError, ValueError):
            return None

    def read_temp2(self):
        try:
            return float((self.base / "fan2_temp").read_text().strip())
        except (OSError, ValueError):
            return None

    def ping(self):
        try:
            self._write("fan_watchdog_ping", "1")
        except OSError:
            pass


class DemoBackend(FanBackend):
    name = "demo"

    def __init__(self):
        self._duty = {1: 0, 2: 0}

    def fans(self):
        return [1, 2]

    def lock(self):
        pass

    def release(self):
        pass

    def write_duty(self, fan, percent):
        self._duty[fan] = max(0, min(MAX_DUTY_PERCENT, int(percent)))

    def read_duty(self, fan):
        return self._duty[fan]

    def read_temp(self):
        return round(62 + 10 * math.sin(time.monotonic() / 18))

    def read_temp2(self):
        return self.read_temp() + 2


def _find_ec_device():
    configured = os.environ.get("FAN_CONTROL_DEVICE")
    if configured:
        return configured
    candidates = sorted(pathlib.Path("/dev").glob("*_io"))
    if len(candidates) == 1:
        return str(candidates[0])
    if not candidates:
        raise FileNotFoundError(
            "No fan-control hardware found.\n"
            "  tuxedo_io:  install tuxedo-drivers and load the tuxedo-io module\n"
            "    Fedora:  dnf copr enable kallepm/tuxedo-drivers && dnf install tuxedo-drivers\n"
            "    Ubuntu:  apt install tuxedo-drivers-dkms\n"
            "    Arch:    yay -S tuxedo-drivers-dkms\n"
            "  clevo_acpi: install clevo-acpi-dkms (github.com/arbitrary-string/clevo-acpi-dkms)\n"
            "    then run with --backend clevo_acpi once /sys/class/leds/clevo-acpi::kbd_backlight/device/ has fan attrs\n"
            "Run with --diagnose for a full system check."
        )
    names = "; ".join(str(c) for c in candidates)
    raise FileNotFoundError(
        f"multiple fan-control devices found; set FAN_CONTROL_DEVICE or pass --device "
        f"(found: {names})"
    )


def detect_backend(backend=None, device=None):
    """Resolve an explicit or auto-detected backend instance.

    ``backend`` wins over ``FAN_CONTROL_BACKEND``; both accept the same
    ``auto`` / ``tuxedo_io`` / ``clevo_acpi`` values. ``auto`` (or no
    override) prefers clevo-acpi sysfs when present, else tuxedo_io.
    """
    backend = backend or os.environ.get("FAN_CONTROL_BACKEND") or "auto"
    if backend not in ("auto", "tuxedo_io", "clevo_acpi"):
        raise FanBackendError(
            f"unknown backend {backend!r}; expected auto, tuxedo_io, or clevo_acpi"
        )
    if backend == "clevo_acpi":
        return ClevoAcpiBackend()
    if backend == "tuxedo_io":
        return TuxedoIoBackend(device or _find_ec_device())
    # Auto-detect: prefer clevo-acpi sysfs when present, else tuxedo_io.
    if ClevoAcpiBackend.available():
        return ClevoAcpiBackend()
    return TuxedoIoBackend(device or _find_ec_device())


def migrate_config(data):
    """Return a copy of ``data`` with legacy raw (0-198) duties rescaled to percent.

    Configs written before the percent backend existed store ``max_duty`` and
    curve duties in the tuxedo raw 0-198 domain. That domain is detected when
    any duty exceeds 100; once detected, every duty in the config is rescaled
    uniformly, including sub-100 raw values that would otherwise read as
    roughly double their intended percent. Configs already in percent are
    returned unchanged.
    """
    if not isinstance(data, dict):
        return data
    data = dict(data)

    legacy = False
    max_duty = data.get("max_duty")
    if isinstance(max_duty, (int, float)) and not isinstance(max_duty, bool) and max_duty > MAX_DUTY_PERCENT:
        legacy = True

    curve = data.get("curve")
    if isinstance(curve, list):
        for row in curve:
            if (
                isinstance(row, (list, tuple)) and len(row) == 2
                and isinstance(row[1], (int, float)) and not isinstance(row[1], bool)
                and row[1] > MAX_DUTY_PERCENT
            ):
                legacy = True
                break

    if not legacy:
        return data

    def scale(duty):
        return round(duty * MAX_DUTY_PERCENT / TUXEDO_NATIVE_MAX)

    if isinstance(max_duty, (int, float)) and not isinstance(max_duty, bool):
        data["max_duty"] = scale(max_duty)

    if isinstance(curve, list):
        migrated = []
        for row in curve:
            if isinstance(row, (list, tuple)) and len(row) == 2:
                temp, duty = row
                if isinstance(duty, (int, float)) and not isinstance(duty, bool):
                    duty = scale(duty)
                migrated.append([temp, duty])
            else:
                migrated.append(row)
        data["curve"] = migrated

    return data
