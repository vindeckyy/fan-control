#!/usr/bin/env python3
"""Hardware backends for fan control.

Two kernel interfaces share one policy layer:

- ``tuxedo_io``: ioctls on a ``/dev/*_io`` character device supporting both
  Uniwill (raw duty 0-198) and Clevo (raw duty 0-255) hardware interfaces.
- ``clevo_acpi``: sysfs files under the ``clevo-acpi`` platform device,
  per-fan duty 0-100, with a kernel-side dead-man's-switch.

Backends expose duty as a percentage 0-100. ``tuxedo_io`` translates to its
native raw domain (0-255 for Clevo, 0-198 for Uniwill) at the edge; ``clevo_acpi``
is already in percent.

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

# tuxedo_io ioctl encoding.
# The tuxedo_io module supports both Uniwill (raw duty 0-198) and Clevo (raw duty 0-255)
# hardware interfaces with distinct ioctl magic numbers and structures.
IOCTL_MAGIC = 0xEC

MAGIC_READ_CL = IOCTL_MAGIC + 1   # 0xED
MAGIC_WRITE_CL = IOCTL_MAGIC + 2  # 0xEE

MAGIC_READ_UW = IOCTL_MAGIC + 3   # 0xEF
MAGIC_WRITE_UW = IOCTL_MAGIC + 4  # 0xF0

IOC_NONE, IOC_W, IOC_R = 0, 1, 2
SZ = ctypes.sizeof(ctypes.c_void_p)  # 8 on 64-bit platforms


def ioc(direction, kind, number, size=SZ):
    return (direction << 30) | (kind << 8) | number | (size << 16)


# Hardware detection
R_HWCHECK_CL = ioc(IOC_R, IOCTL_MAGIC, 0x05, SZ)
R_HWCHECK_UW = ioc(IOC_R, IOCTL_MAGIC, 0x06, SZ)

# Clevo interface
R_CL_FANINFO1 = ioc(IOC_R, MAGIC_READ_CL, 0x10, SZ)
R_CL_FANINFO2 = ioc(IOC_R, MAGIC_READ_CL, 0x11, SZ)
R_CL_FANINFO3 = ioc(IOC_R, MAGIC_READ_CL, 0x12, SZ)
W_CL_FANSPEED = ioc(IOC_W, MAGIC_WRITE_CL, 0x10, SZ)
W_CL_FANAUTO = ioc(IOC_W, MAGIC_WRITE_CL, 0x11, SZ)

# Uniwill interface
R_UW_FANSPEED = ioc(IOC_R, MAGIC_READ_UW, 0x10, SZ)
R_UW_FANSPEED2 = ioc(IOC_R, MAGIC_READ_UW, 0x11, SZ)
R_UW_FAN_TEMP = ioc(IOC_R, MAGIC_READ_UW, 0x12, SZ)
R_UW_FAN_TEMP2 = ioc(IOC_R, MAGIC_READ_UW, 0x13, SZ)
R_UW_MODE = ioc(IOC_R, MAGIC_READ_UW, 0x14, SZ)
W_UW_FANSPEED = ioc(IOC_W, MAGIC_WRITE_UW, 0x10, SZ)
W_UW_FANSPEED2 = ioc(IOC_W, MAGIC_WRITE_UW, 0x11, SZ)
W_UW_MODE = ioc(IOC_W, MAGIC_WRITE_UW, 0x12, SZ)
W_UW_FANAUTO = ioc(IOC_NONE, MAGIC_WRITE_UW, 0x14, 0)

# The clevo-acpi fan attributes live on the platform device reached through
# the LED classdev symlink. This is how the reference project resolves it.
CLEVO_SYSFS_BASE = pathlib.Path("/sys/class/leds/clevo-acpi::kbd_backlight/device")
CLEVO_WATCHDOG_TIMEOUT_MS = 15000
CLEVO_WATCHDOG_MIN_MS = 5000
CLEVO_WATCHDOG_MAX_MS = 60000

TUXEDO_NATIVE_MAX = 198
CLEVO_HOLD_INTERVAL = 1.0
UNIWILL_HOLD_INTERVAL = 0.25


class FanBackendError(OSError, RuntimeError):
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

    def read_rpm(self, fan):
        """Read-only tach in RPM, or ``None`` if this backend/model has none."""
        return None

    def ping(self):
        """Keep the watchdog alive without an EC write. No-op if absent."""

    def close(self):
        """Release any held resources."""


class TuxedoIoBackend(FanBackend):
    name = "tuxedo_io"
    NATIVE_MAX = TUXEDO_NATIVE_MAX
    CLEVO_NATIVE_MAX = 255

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR)
        self._lock = threading.RLock()
        self._duties = {1: 0, 2: 0, 3: 0}
        self._last_hold = 0.0
        self._hold_stop = threading.Event()
        self._hold_thread = None
        self.is_clevo = self._detect_clevo()

    def _detect_clevo(self):
        buf = ctypes.c_int32()
        try:
            fcntl.ioctl(self.fd, R_HWCHECK_UW, buf, True)
            if buf.value == 1:
                return False
        except OSError:
            pass

        try:
            fcntl.ioctl(self.fd, R_HWCHECK_CL, buf, True)
            if buf.value == 1:
                return True
        except OSError:
            pass

        # Fallback heuristic: check if Clevo WMI GUID or module exists
        if pathlib.Path("/sys/bus/wmi/devices/ABBC0F6D-8EA1-11D1-00A0-C90629100000-3").exists():
            return True
        return False

    @property
    def native_max(self):
        return self.CLEVO_NATIVE_MAX if getattr(self, "is_clevo", False) else self.NATIVE_MAX

    def _pct_to_raw(self, percent):
        return round(int(percent) * self.native_max / MAX_DUTY_PERCENT)

    def _raw_to_pct(self, raw):
        return round(int(raw) * MAX_DUTY_PERCENT / self.native_max)

    def _read(self, command):
        buf = ctypes.c_int32()
        fcntl.ioctl(self.fd, command, buf, True)
        return buf.value

    def _write(self, command, value):
        buf = ctypes.c_int32(int(value))
        fcntl.ioctl(self.fd, command, buf, True)

    def fans(self):
        return [1, 2]

    def lock(self):
        with self._lock:
            if getattr(self, "is_clevo", False):
                return
            thread = getattr(self, "_hold_thread", None)
            if thread is not None and thread.is_alive():
                return
            self._write(W_UW_MODE, 0x40)
            self._start_hold_thread()

    def release(self):
        self._stop_hold_thread()
        with self._lock:
            if getattr(self, "is_clevo", False):
                self._write(W_CL_FANAUTO, 0)
            else:
                fcntl.ioctl(self.fd, W_UW_FANAUTO)

    def write_duty(self, fan, percent):
        percent = max(0, min(MAX_DUTY_PERCENT, int(percent)))
        with self._lock:
            self._duties[fan] = percent
            if getattr(self, "is_clevo", False):
                self._commit_clevo_duties()
            else:
                self._commit_uniwill_duties()
                self._start_hold_thread()

    def _commit_clevo_duties(self):
        raw1 = max(0, min(self.CLEVO_NATIVE_MAX, round(self._duties.get(1, 0) * self.CLEVO_NATIVE_MAX / MAX_DUTY_PERCENT)))
        raw2 = max(0, min(self.CLEVO_NATIVE_MAX, round(self._duties.get(2, 0) * self.CLEVO_NATIVE_MAX / MAX_DUTY_PERCENT)))
        raw3 = max(0, min(self.CLEVO_NATIVE_MAX, round(self._duties.get(3, 0) * self.CLEVO_NATIVE_MAX / MAX_DUTY_PERCENT)))
        arg = (raw1 & 0xFF) | ((raw2 & 0xFF) << 8) | ((raw3 & 0xFF) << 16)
        self._write(W_CL_FANSPEED, arg)
        self._last_hold = time.monotonic()

    def _commit_uniwill_duties(self):
        for fan, cmd in ((1, W_UW_FANSPEED), (2, W_UW_FANSPEED2)):
            percent = int(self._duties.get(fan, 0))
            value = max(0, min(self.NATIVE_MAX, self._pct_to_raw(percent)))
            self._write(cmd, value)
        self._last_hold = time.monotonic()

    def _start_hold_thread(self):
        stop = getattr(self, "_hold_stop", None)
        if stop is None or getattr(self, "is_clevo", False):
            return
        if self._hold_thread is not None and self._hold_thread.is_alive():
            return
        stop.clear()
        self._hold_thread = threading.Thread(target=self._hold_loop, name="uniwill-fan-hold", daemon=True)
        self._hold_thread.start()

    def _stop_hold_thread(self):
        stop = getattr(self, "_hold_stop", None)
        thread = getattr(self, "_hold_thread", None)
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self._hold_thread = None

    def _hold_loop(self):
        stop = self._hold_stop
        while not stop.wait(UNIWILL_HOLD_INTERVAL):
            try:
                with self._lock:
                    self._commit_uniwill_duties()
            except (OSError, FanBackendError):
                break

    def ping(self):
        with self._lock:
            now = time.monotonic()
            if getattr(self, "is_clevo", False):
                if now - self._last_hold < CLEVO_HOLD_INTERVAL:
                    return
                self._commit_clevo_duties()
                return
            if now - getattr(self, "_last_hold", 0) < UNIWILL_HOLD_INTERVAL:
                return
            self._commit_uniwill_duties()
            self._start_hold_thread()

    def read_duty(self, fan):
        with self._lock:
            # tuxedo_io readback registers are not a stable PWM echo on either
            # Clevo (FANINFO low byte) or Uniwill (R_UW_FANSPEED*). Use the
            # last duty we commanded so the control loop does not hunt.
            return int(self._duties.get(fan, 0))

    def read_duty_hardware(self, fan):
        """Best-effort hardware readback; may be noisy or laggy on tuxedo_io."""
        with self._lock:
            if getattr(self, "is_clevo", False):
                cmd = (R_CL_FANINFO1, R_CL_FANINFO2, R_CL_FANINFO3)[fan - 1] if fan in (1, 2, 3) else R_CL_FANINFO1
                raw = self._read(cmd) & 0xFF
                return round(raw * MAX_DUTY_PERCENT / self.CLEVO_NATIVE_MAX)
            cmd = (R_UW_FANSPEED, R_UW_FANSPEED2)[fan - 1] if fan in (1, 2) else R_UW_FANSPEED
            return self._raw_to_pct(self._read(cmd) & 0xFF)

    def read_temp(self):
        try:
            with self._lock:
                if getattr(self, "is_clevo", False):
                    val = self._read(R_CL_FANINFO1)
                    temp = (val >> 8) & 0xFF
                    return float(temp) if 0 < temp <= 150 else None
                else:
                    raw = self._read(R_UW_FAN_TEMP) & 0xFF
                    return float(raw) if 0 < raw <= 150 else None
        except OSError:
            return None

    def read_temp2(self):
        try:
            with self._lock:
                if getattr(self, "is_clevo", False):
                    val = self._read(R_CL_FANINFO2)
                    temp = (val >> 8) & 0xFF
                    return float(temp) if 0 < temp <= 150 else None
                else:
                    raw = self._read(R_UW_FAN_TEMP2) & 0xFF
                    return float(raw) if 0 < raw <= 150 else None
        except OSError:
            return None

    def read_rpm(self, fan):
        # Uniwill tach is unverified; never probe those registers.
        if not getattr(self, "is_clevo", False):
            return None
        try:
            with self._lock:
                cmd = (R_CL_FANINFO1, R_CL_FANINFO2, R_CL_FANINFO3)[fan - 1] if fan in (1, 2, 3) else R_CL_FANINFO1
                rpm = (self._read(cmd) >> 16) & 0xFFFF
                return int(rpm) if 0 < rpm < 30000 else None
        except OSError:
            return None

    def close(self):
        self._stop_hold_thread()
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

    def read_rpm(self, fan):
        path = self.base / f"fan{fan}_rpm"
        if not path.exists():
            return None
        try:
            rpm = int(path.read_text().strip())
            return rpm if 0 < rpm < 30000 else None
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

    def read_rpm(self, fan):
        duty = self._duty.get(fan, 0)
        if duty <= 0:
            return 0
        return 800 + int(duty * 18)


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

    def _has_legacy_duty(curve):
        if not isinstance(curve, list):
            return False
        for row in curve:
            if (
                isinstance(row, (list, tuple)) and len(row) == 2
                and isinstance(row[1], (int, float)) and not isinstance(row[1], bool)
                and row[1] > MAX_DUTY_PERCENT
            ):
                return True
        return False

    legacy = False
    max_duty = data.get("max_duty")
    if isinstance(max_duty, (int, float)) and not isinstance(max_duty, bool) and max_duty > MAX_DUTY_PERCENT:
        legacy = True

    for key in ("curve", "curve_cpu", "curve_gpu"):
        if _has_legacy_duty(data.get(key)):
            legacy = True
            break

    named = data.get("named_curves")
    if isinstance(named, dict):
        for curve in named.values():
            if _has_legacy_duty(curve):
                legacy = True
                break

    if not legacy:
        return data

    def scale(duty):
        return round(duty * MAX_DUTY_PERCENT / TUXEDO_NATIVE_MAX)

    def _migrate_curve(curve):
        if not isinstance(curve, list):
            return curve
        migrated = []
        for row in curve:
            if isinstance(row, (list, tuple)) and len(row) == 2:
                temp, duty = row
                if isinstance(duty, (int, float)) and not isinstance(duty, bool):
                    duty = scale(duty)
                migrated.append([temp, duty])
            else:
                migrated.append(row)
        return migrated

    if isinstance(max_duty, (int, float)) and not isinstance(max_duty, bool):
        data["max_duty"] = scale(max_duty)

    for key in ("curve", "curve_cpu", "curve_gpu"):
        if key in data and isinstance(data[key], list):
            data[key] = _migrate_curve(data[key])

    if isinstance(named, dict):
        data["named_curves"] = {k: _migrate_curve(v) for k, v in named.items()}

    return data

