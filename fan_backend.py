#!/usr/bin/env python3
"""Hardware backends for fan control.

Linux kernel interfaces:

- ``tuxedo_io``: ioctls on a ``/dev/*_io`` character device supporting both
  Uniwill (raw duty 0-198) and Clevo (raw duty 0-255) hardware interfaces.
- ``clevo_acpi``: sysfs files under the ``clevo-acpi`` platform device,
  per-fan duty 0-100, with a kernel-side dead-man's-switch.

Windows interfaces:

- ``windows_ec``: direct EC port I/O (ports 0x62/0x66) through an
  InpOut32/64 or WinRing0 helper DLL.
- ``windows_wmi``: Uniwill/Tongfang EC RAM access over the ACPI WMI
  ``AcpiTest_MULong`` method, which also exposes EC temperatures and fan
  tachometers. Manual duty uses the EC user fan mode with the direct PWM
  registers on the EC's 0-200 scale.

Backends expose duty as a percentage 0-100. ``tuxedo_io`` translates to its
native raw domain (0-255 for Clevo, 0-198 for Uniwill) at the edge; ``clevo_acpi``
and the Windows backends work in percent at the API boundary.

The clevo-acpi sysfs interface comes from ``arbitrary-string/clevo-acpi-dkms``
(GPL-2.0-or-later). Its semantics were read from that driver source and from
the reference daemon ``arbitrary-string/clevo-control-panel`` (GPL-3.0). The
Uniwill EC register map and WMI ``GetSetULong`` protocol were verified against
the upstream Linux ``uniwill`` driver and ``tuxedo-drivers``. No code is copied
from those projects; only documented attribute names, register addresses, and
command contracts are consumed.
"""

import ctypes
import math
import os
import pathlib
import sys
import threading
import time

try:
    import fcntl
except ImportError:
    fcntl = None

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
        self._ensure_uniwill_wmi_bound()
        self.is_clevo = self._detect_clevo()

    @staticmethod
    def _ensure_uniwill_wmi_bound():
        dev = "ABBC0F72-8EA1-11D1-00A0-C90629100000-8"
        unbind_path = pathlib.Path(f"/sys/bus/wmi/drivers/uniwill-wmi/{dev}")
        bind_dir = pathlib.Path("/sys/bus/wmi/drivers/uniwill_wmi")
        bound_path = bind_dir / dev
        if unbind_path.exists():
            try:
                pathlib.Path("/sys/bus/wmi/drivers/uniwill-wmi/unbind").write_text(dev)
            except OSError:
                pass
        if bind_dir.exists() and not bound_path.exists():
            try:
                (bind_dir / "bind").write_text(dev)
            except OSError:
                pass

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

        # Fallback heuristic: check if Clevo kernel module/driver or backlight exists
        if (
            pathlib.Path("/sys/module/clevo_wmi").exists()
            or pathlib.Path("/sys/module/clevo_acpi").exists()
            or pathlib.Path("/sys/bus/wmi/drivers/clevo_wmi").exists()
            or pathlib.Path("/sys/class/leds/clevo-acpi::kbd_backlight").exists()
        ):
            return True
        return False

    @property
    def native_max(self):
        return self.CLEVO_NATIVE_MAX if getattr(self, "is_clevo", False) else self.NATIVE_MAX

    def _pct_to_raw(self, percent):
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            pct = 0
        pct = max(0.0, min(float(MAX_DUTY_PERCENT), pct))
        return round(pct * self.native_max / MAX_DUTY_PERCENT)

    def _raw_to_pct(self, raw):
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0
        return round(value * MAX_DUTY_PERCENT / self.native_max)

    def _read(self, command):
        buf = ctypes.c_int32()
        fcntl.ioctl(self.fd, command, buf, True)
        return buf.value

    def _write(self, command, value):
        buf = ctypes.c_int32(int(value))
        fcntl.ioctl(self.fd, command, buf, True)

    def fans(self):
        if getattr(self, "is_clevo", False):
            return [1, 2, 3]
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
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            raise FanBackendError(f"invalid duty {percent!r}") from None
        percent = max(0, min(MAX_DUTY_PERCENT, int(round(pct))))
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
        failures = 0
        while not stop.wait(UNIWILL_HOLD_INTERVAL):
            try:
                with self._lock:
                    self._commit_uniwill_duties()
            except (OSError, FanBackendError):
                failures += 1
                if failures >= 5:
                    break
                continue
            failures = 0

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
                if fan not in (1, 2, 3):
                    raise FanBackendError(f"unknown fan {fan!r}") from None
                cmd = (R_CL_FANINFO1, R_CL_FANINFO2, R_CL_FANINFO3)[fan - 1]
                raw = self._read(cmd) & 0xFF
                return round(raw * MAX_DUTY_PERCENT / self.CLEVO_NATIVE_MAX)
            if fan not in (1, 2):
                raise FanBackendError(f"unknown fan {fan!r}") from None
            cmd = (R_UW_FANSPEED, R_UW_FANSPEED2)[fan - 1]
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
        if fan not in (1, 2, 3):
            raise FanBackendError(f"unknown fan {fan!r}") from None
        try:
            with self._lock:
                cmd = (R_CL_FANINFO1, R_CL_FANINFO2, R_CL_FANINFO3)[fan - 1]
                rpm = (self._read(cmd) >> 16) & 0xFFFF
                return int(rpm) if 0 < rpm < 30000 else None
        except OSError:
            return None

    def close(self):
        self._stop_hold_thread()
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass


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

    @staticmethod
    def _fan_index(fan):
        try:
            text = str(fan).strip().removeprefix("fan")
            index = int(text)
        except (TypeError, ValueError):
            raise FanBackendError(f"unknown fan {fan!r}") from None
        if index not in (1, 2, 3):
            raise FanBackendError(f"unknown fan {fan!r}") from None
        return index

    def write_duty(self, fan, percent):
        index = self._fan_index(fan)
        if index not in self._fans:
            raise FanBackendError(f"fan {index} not present under {self.base}")
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            raise FanBackendError(f"invalid duty {percent!r}") from None
        value = max(0, min(MAX_DUTY_PERCENT, int(round(pct))))
        self._write(f"fan{index}_manual_duty", value)

    def read_duty(self, fan):
        index = self._fan_index(fan)
        try:
            return int((self.base / f"fan{index}_duty").read_text().strip())
        except (OSError, ValueError) as exc:
            raise FanBackendError(f"cannot read fan{index} duty: {exc}") from exc

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
        index = self._fan_index(fan)
        path = self.base / f"fan{index}_rpm"
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
        self._duty = {1: 0, 2: 0, 3: 0}

    def fans(self):
        return [1, 2]

    def lock(self):
        pass

    def release(self):
        pass

    def write_duty(self, fan, percent):
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            return
        try:
            key = int(fan)
        except (TypeError, ValueError):
            return
        self._duty[key] = max(0, min(MAX_DUTY_PERCENT, int(round(pct))))

    def read_duty(self, fan):
        try:
            return int(self._duty.get(int(fan), 0))
        except (TypeError, ValueError):
            return 0

    def read_temp(self):
        return round(62 + 10 * math.sin(time.monotonic() / 18))

    def read_temp2(self):
        return self.read_temp() + 2

    def read_rpm(self, fan):
        duty = self._duty.get(fan, 0)
        if duty <= 0:
            return 0
        return 800 + int(duty * 18)


class WindowsEcBackend(FanBackend):
    """Direct Embedded Controller access on Windows via InpOut32/64 or WinRing0."""

    name = "windows_ec"

    EC_DATA_PORT = 0x62
    EC_CMD_PORT = 0x66
    EC_CMD_SET_FAN = 0x99
    EC_CMD_READ_RAM = 0x80
    IBF_MASK = 0x02
    OBF_MASK = 0x01

    def __init__(self, dll_path=None):
        self._dll = None
        self._lock = threading.RLock()
        self._duties = {1: 0, 2: 0, 3: 0}
        self._is_winring0 = False
        self._dll_path = dll_path or self.find_driver_dll()
        if not self._dll_path:
            raise FanBackendError(
                "No Windows fan-control EC driver found (inpoutx64.dll or WinRing0x64.dll).\n"
                "Place inpoutx64.dll or WinRing0x64.dll in the fan-control directory or System32,\n"
                "or run with --demo for simulated hardware."
            )
        self._init_driver()

    @classmethod
    def find_driver_dll(cls):
        candidates = ("inpoutx64.dll", "inpout32.dll", "WinRing0x64.dll", "WinRing0.dll")
        search_dirs = [
            pathlib.Path(__file__).resolve().parent,
            pathlib.Path.cwd(),
        ]
        if sys.platform == "win32":
            windir = pathlib.Path(os.environ.get("WINDIR", "C:\\Windows"))
            search_dirs.extend([windir / "System32", windir / "SysWOW64"])
        for d in search_dirs:
            for cand in candidates:
                p = d / cand
                if p.is_file():
                    return str(p)
        return None

    @classmethod
    def available(cls):
        return cls.find_driver_dll() is not None

    def _init_driver(self):
        try:
            if hasattr(ctypes, "WinDLL"):
                self._dll = ctypes.WinDLL(self._dll_path)
            else:
                self._dll = ctypes.CDLL(self._dll_path)
            if hasattr(self._dll, "InitializeOls"):
                self._is_winring0 = True
                if not self._dll.InitializeOls():
                    raise FanBackendError("WinRing0 InitializeOls() failed; need Administrator privileges?")
        except (OSError, Exception) as exc:
            raise FanBackendError(f"failed to load EC driver {self._dll_path}: {exc}") from exc

    def _read_port(self, port):
        if self._is_winring0:
            return self._dll.ReadIoPortByte(ctypes.c_ushort(port)) & 0xFF
        return self._dll.DlPortReadPortUchar(ctypes.c_ushort(port)) & 0xFF

    def _write_port(self, port, value):
        if self._is_winring0:
            self._dll.WriteIoPortByte(ctypes.c_ushort(port), ctypes.c_ubyte(value))
        else:
            self._dll.DlPortWritePortUchar(ctypes.c_ushort(port), ctypes.c_ubyte(value))

    def _wait_ibf_clear(self, timeout=0.1):
        start = time.monotonic()
        while (self._read_port(self.EC_CMD_PORT) & self.IBF_MASK) != 0:
            if time.monotonic() - start > timeout:
                return False
            time.sleep(0.001)
        return True

    def _wait_obf_set(self, timeout=0.1):
        start = time.monotonic()
        while (self._read_port(self.EC_CMD_PORT) & self.OBF_MASK) == 0:
            if time.monotonic() - start > timeout:
                return False
            time.sleep(0.001)
        return True

    def fans(self):
        return [1, 2, 3]

    def lock(self):
        pass

    def release(self):
        """Return control to EC firmware auto."""
        with self._lock:
            try:
                if self._wait_ibf_clear():
                    self._write_port(self.EC_CMD_PORT, self.EC_CMD_SET_FAN)
                    if self._wait_ibf_clear():
                        self._write_port(self.EC_DATA_PORT, 0xFF)
            except Exception:
                pass

    def write_duty(self, fan, percent):
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            raise FanBackendError(f"invalid duty {percent!r}") from None
        percent = max(0, min(MAX_DUTY_PERCENT, int(round(pct))))
        raw = round(percent * 255 / MAX_DUTY_PERCENT)
        fan_idx = int(fan)
        with self._lock:
            self._duties[fan_idx] = percent
            try:
                if self._wait_ibf_clear():
                    self._write_port(self.EC_CMD_PORT, self.EC_CMD_SET_FAN)
                    if self._wait_ibf_clear():
                        self._write_port(self.EC_DATA_PORT, fan_idx)
                        if self._wait_ibf_clear():
                            self._write_port(self.EC_DATA_PORT, raw)
            except Exception as exc:
                raise FanBackendError(f"failed to write duty to EC port: {exc}") from exc

    def read_duty(self, fan):
        with self._lock:
            return int(self._duties.get(int(fan), 0))

    def read_temp(self):
        with self._lock:
            try:
                if self._wait_ibf_clear():
                    self._write_port(self.EC_CMD_PORT, self.EC_CMD_READ_RAM)
                    if self._wait_ibf_clear():
                        self._write_port(self.EC_DATA_PORT, 0xCE)
                        if self._wait_obf_set():
                            val = self._read_port(self.EC_DATA_PORT)
                            return float(val) if 0 < val <= 150 else None
            except Exception:
                pass
        return None

    def read_temp2(self):
        with self._lock:
            try:
                if self._wait_ibf_clear():
                    self._write_port(self.EC_CMD_PORT, self.EC_CMD_READ_RAM)
                    if self._wait_ibf_clear():
                        self._write_port(self.EC_DATA_PORT, 0xCF)
                        if self._wait_obf_set():
                            val = self._read_port(self.EC_DATA_PORT)
                            return float(val) if 0 < val <= 150 else None
            except Exception:
                pass
        return None

    def read_rpm(self, fan):
        fan_idx = int(fan)
        if fan_idx not in (1, 2, 3):
            return None
        with self._lock:
            try:
                reg = 0xD3 if fan_idx == 1 else (0xD5 if fan_idx == 2 else 0xD7)
                if self._wait_ibf_clear():
                    self._write_port(self.EC_CMD_PORT, self.EC_CMD_READ_RAM)
                    if self._wait_ibf_clear():
                        self._write_port(self.EC_DATA_PORT, reg)
                        if self._wait_obf_set():
                            hi = self._read_port(self.EC_DATA_PORT)
                            if self._wait_ibf_clear():
                                self._write_port(self.EC_CMD_PORT, self.EC_CMD_READ_RAM)
                                if self._wait_ibf_clear():
                                    self._write_port(self.EC_DATA_PORT, reg + 1)
                                    if self._wait_obf_set():
                                        lo = self._read_port(self.EC_DATA_PORT)
                                        rpm = (hi << 8) | lo
                                        return rpm if 0 < rpm < 30000 else None
            except Exception:
                pass
        return None

    def close(self):
        if self._is_winring0 and self._dll is not None:
            try:
                self._dll.DeinitializeOls()
            except Exception:
                pass
        self._dll = None


class _WmiEcTransport:
    """Uniwill EC RAM access over the ACPI WMI ``AcpiTest_MULong`` method.

    The OEM control center talks to the EC with ``GetSetULong``: the 64-bit
    argument packs a 16-bit address, 16-bit data, and a 16-bit operation
    (``0x0100`` read, ``0x0000`` write). The low byte of the reply is the
    value read; ``0xFEFEFEFE`` signals a failed transaction.
    """

    WMI_NAMESPACE = "root\\wmi"
    WMI_CLASS = "AcpiTest_MULong"
    WMI_METHOD = "GetSetULong"
    READ_OP = 0x0100
    COMM_FAILURE = 0xFEFEFEFE

    def __init__(self):
        try:
            import clr
        except ImportError as exc:
            raise FanBackendError(
                "pythonnet is required for WMI fan control; install it with "
                "'pip install -r requirements-windows.txt'"
            ) from exc
        try:
            clr.AddReference("System.Management")
            import System
            from System.Management import ManagementObjectSearcher
        except Exception as exc:  # noqa: BLE001 - surfaced as a backend error
            raise FanBackendError(f"could not load System.Management for WMI access: {exc}") from exc
        self._system = System
        self._object = None
        try:
            searcher = ManagementObjectSearcher(
                self.WMI_NAMESPACE, f"SELECT * FROM {self.WMI_CLASS}"
            )
            for candidate in searcher.Get():
                self._object = candidate
                break
        except Exception as exc:  # noqa: BLE001 - surfaced as a backend error
            raise FanBackendError(f"WMI EC interface unavailable: {exc}") from exc
        if self._object is None:
            raise FanBackendError(
                f"WMI class {self.WMI_CLASS} not found; this is not a supported Uniwill/Tongfang EC"
            )
        try:
            self.read(0x043E)
        except FanBackendError as exc:
            raise FanBackendError(
                f"cannot access the Uniwill EC over WMI ({exc}); run as Administrator"
            ) from exc

    def _call(self, data):
        try:
            params = self._object.GetMethodParameters(self.WMI_METHOD)
            params["Data"] = self._system.UInt64(data)
            out = self._object.InvokeMethod(self.WMI_METHOD, params, None)
            return int(out["Return"])
        except Exception as exc:  # noqa: BLE001 - surfaced as a backend error
            raise FanBackendError(f"WMI EC call failed: {exc}") from exc

    def read(self, address):
        ret = self._call((self.READ_OP << 32) | (address & 0xFFFF))
        if ret == self.COMM_FAILURE:
            raise FanBackendError(f"EC read failed at 0x{address:04X}")
        return ret & 0xFF

    def write(self, address, value):
        ret = self._call(((value & 0xFF) << 16) | (address & 0xFFFF))
        if ret == self.COMM_FAILURE:
            raise FanBackendError(f"EC write failed at 0x{address:04X}")

    def close(self):
        self._object = None


class WindowsWmiBackend(FanBackend):
    """Uniwill/Tongfang EC fan control over the ACPI WMI interface.

    Manual control uses the EC user fan mode (``0x0751`` bit 6) together with
    the direct PWM registers ``0x1804``/``0x1809`` on the EC's 0-200 scale.
    Reading ``0x0751`` without the bit lets firmware automatic control resume.
    Requires Administrator access.
    """

    name = "windows_wmi"

    EC_CPU_TEMP = 0x043E
    EC_GPU_TEMP = 0x044F
    EC_FAN_RPM = {1: 0x0464, 2: 0x046C}
    EC_MANUAL_MODE = 0x0751
    EC_MANUAL_BIT = 0x40
    EC_PWM_WRITE = {1: 0x1804, 2: 0x1809}
    PWM_MAX = 200

    def __init__(self, transport=None):
        self._transport = transport if transport is not None else _WmiEcTransport()
        self._lock = threading.RLock()
        self._manual = False
        self._duties = {1: 0, 2: 0}

    @classmethod
    def available(cls):
        if sys.platform != "win32":
            return False
        import subprocess
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-CimClass -Namespace root/wmi -ClassName AcpiTest_MULong "
                 "-ErrorAction SilentlyContinue | Select-Object -First 1 "
                 "| ForEach-Object { $_.CimClassName }"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return res.returncode == 0 and bool(res.stdout.strip())
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return False

    def fans(self):
        return [1, 2]

    def lock(self):
        with self._lock:
            if not self._manual:
                value = self._transport.read(self.EC_MANUAL_MODE)
                if not value & self.EC_MANUAL_BIT:
                    self._transport.write(self.EC_MANUAL_MODE, value | self.EC_MANUAL_BIT)
                self._manual = True

    def release(self):
        with self._lock:
            value = self._transport.read(self.EC_MANUAL_MODE)
            if value & self.EC_MANUAL_BIT:
                self._transport.write(self.EC_MANUAL_MODE, value & ~self.EC_MANUAL_BIT)
            self._manual = False

    def write_duty(self, fan, percent):
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            raise FanBackendError(f"invalid duty {percent!r}") from None
        fan_index = int(fan)
        address = self.EC_PWM_WRITE.get(fan_index)
        if address is None:
            raise FanBackendError(f"unsupported fan {fan!r}")
        percent = max(0, min(MAX_DUTY_PERCENT, int(round(pct))))
        raw = round(percent * self.PWM_MAX / MAX_DUTY_PERCENT)
        with self._lock:
            self.lock()
            self._transport.write(address, raw)
            self._duties[fan_index] = percent

    def read_duty(self, fan):
        address = self.EC_PWM_WRITE.get(int(fan))
        if address is None:
            return 0
        with self._lock:
            raw = self._transport.read(address)
        return int(round(raw * MAX_DUTY_PERCENT / self.PWM_MAX))

    def read_temp(self):
        with self._lock:
            value = self._transport.read(self.EC_CPU_TEMP)
        return float(value) if 0 < value <= 150 else None

    def read_temp2(self):
        with self._lock:
            value = self._transport.read(self.EC_GPU_TEMP)
        return float(value) if 0 < value <= 150 else None

    def read_rpm(self, fan):
        address = self.EC_FAN_RPM.get(int(fan))
        if address is None:
            return None
        with self._lock:
            hi = self._transport.read(address)
            lo = self._transport.read(address + 1)
        rpm = (hi << 8) | lo
        return rpm if 0 < rpm < 30000 else None

    def close(self):
        with self._lock:
            if self._transport is not None:
                self._transport.close()


def _find_ec_device():
    configured = os.environ.get("FAN_CONTROL_DEVICE")
    if configured:
        return configured
    if sys.platform == "win32":
        raise FileNotFoundError(
            "No fan-control hardware found on Windows.\n"
            "  Place inpoutx64.dll or WinRing0x64.dll in the fan-control directory or System32,\n"
            "  or run with --demo for simulated evaluation.\n"
            "Run with --diagnose for a full system check."
        )
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

    ``backend`` wins over ``FAN_CONTROL_BACKEND``; both accept
    ``auto``, ``tuxedo_io``, ``clevo_acpi``, ``windows_ec``, ``windows_wmi``.
    ``auto`` (or no override) prefers clevo-acpi sysfs when present, else tuxedo_io on Linux,
    or windows_ec / windows_wmi on Windows.
    """
    backend = backend or os.environ.get("FAN_CONTROL_BACKEND") or "auto"
    if isinstance(backend, str):
        backend = backend.strip().lower()
    valid_backends = ("auto", "tuxedo_io", "clevo_acpi", "windows_ec", "windows_wmi")
    if backend not in valid_backends:
        raise FanBackendError(
            f"unknown backend {backend!r}; expected {', '.join(valid_backends)}"
        )

    if sys.platform == "win32":
        if backend == "windows_ec":
            return WindowsEcBackend()
        if backend == "windows_wmi":
            return WindowsWmiBackend()
        if backend == "auto":
            if WindowsEcBackend.available():
                return WindowsEcBackend()
            if WindowsWmiBackend.available():
                return WindowsWmiBackend()
            raise FanBackendError(
                "No supported fan-control hardware driver found on Windows.\n"
                "  To control physical laptop fans on Windows:\n"
                "    1. Place inpoutx64.dll or WinRing0x64.dll in the fan-control directory or System32\n"
                "    2. Or run as Administrator for ACPI WMI access\n"
                "  For evaluation without hardware, run with --demo (or python fan-daemon.py --dry-run).\n"
                "Run with --diagnose for a full system check."
            )
        raise FanBackendError(f"backend {backend!r} is Linux-only; use windows_ec or --demo on Windows")

    # Linux resolution
    if backend == "clevo_acpi":
        return ClevoAcpiBackend()
    if backend == "tuxedo_io":
        return TuxedoIoBackend(device or _find_ec_device())
    if backend in ("windows_ec", "windows_wmi"):
        raise FanBackendError(f"backend {backend!r} is Windows-only; use tuxedo_io or clevo_acpi on Linux")
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

